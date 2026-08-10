---
name: Dedup restore fallback + funnel fixes
description: Root causes and fixes for /funnel reliability, duplicate Aranda alert, and /funnel "Scanned" label confusion (Aug 2026).
---

## Root causes

### Issue 1 — /funnel reliability
- `cmd_funnel` had no `if _db is None:` guard (all other commands do). AttributeError → "Could not load funnel data."
- `get_funnel_summary` used 3 separate DB sessions. During cold-start PropCandidateLog writes (~5000 rows per restart), any of those 3 session acquisitions could hit a lock → exception swallowed by broad except.

**Fix:** Added `if _db is None:` guard to `cmd_funnel`. Merged all 4 queries in `get_funnel_summary` into a single `async with self.session()` block (database.py).

### Issue 2 — Duplicate alert after bot restart (Aranda pattern)
- `log_prop_opportunity` is wrapped in `except Exception: pass` — it fails silently during DB stress.
- When it fails, no `PropOpportunityLog` row is created, so `mark_opportunity_alert_sent` is a no-op (UPDATE matches 0 rows).
- `prop_line_history.first_alert_sent_at` IS updated (its `update_prop_lifecycle_state` call has separate retry isolation).
- After restart, `get_recent_alerted_props_for_dedup` only queries `PropOpportunityLog.alert_sent=True` → finds nothing for that player → dedup dict empty → fresh alert fires (the "duplicate").

**Fix:** `get_recent_alerted_props_for_dedup` now runs a SECONDARY query on `PropLineHistory.first_alert_sent_at >= cutoff` after the main POL query. PLH entries are added only for keys NOT already in the dict (POL takes priority). Uses PLH.line_value as the dedup line (current line, reasonable approximation).

**Why PLH works as fallback:** The `update_prop_lifecycle_state(... "ACTIVE_ALERTED", first_alert_sent_at=now)` call lives in a separate try/except block and is MORE reliable than the POL write. So PLH.first_alert_sent_at is set even when the POL row wasn't created.

### Issues 3 & 4 — /funnel "Scanned" count confusion (NOT a bug)
- PropCandidateLog counts SCORED props (new or line-changed) per scan cycle, not all monitored props.
- In the 24-48h window: ~20+ bot restarts × ~5000 cold-start rows = 100k+ rows.
- Current 24h (stable): only 17-384 rows/hour (incremental only).
- The 24h vs 48h data discrepancy is real — not a query timestamp bug.

**Fix:** Renamed "Scanned" → "Evaluated (new/changed)" in /funnel output. Added explanatory note about restarts inflating older windows.

## How to apply
- Any future "duplicate alert after restart" investigation: check if the player has any `PropOpportunityLog.alert_sent=True` rows AND `PropLineHistory.first_alert_sent_at` is set. If PLH has the timestamp but POL has no True rows, the `log_prop_opportunity` silent fail was the cause.
- /funnel "Could not load": check for `_db is None` (startup race) or DB lock errors — now caught and logged via `logger.exception`.
