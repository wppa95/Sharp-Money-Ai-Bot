---
name: Audit Bug Fixes Aug 2026
description: 8 confirmed bugs fixed from full audit report — minimal safe changes only.
---

## Fixes applied

### CRITICAL — bot/config.py
`UD_ALERT_SPORTS_RAW` used `os.environ.get("MLB,WNBA,...")` treating the entire default string as the env-var name → `None` → crashes cmd_config and disables all sports.
Fix: `os.environ.get("UD_ALERT_SPORTS", "MLB,WNBA,NFL,NBA,DOTA,TENNIS,CS")`.

### HIGH — bot/main.py
`asyncio` was not imported; `_clv_seed_job` retry loop calls `asyncio.sleep(wait)` → `NameError` → retry never executed, every lock treated as permanent failure.
Fix: Added `import asyncio` to imports.

### MEDIUM — bot/main.py
`_grade_opportunities_job` returned silently (no health record) when `_db is None`, and had no `record_job_run` or `record_job_fail` calls at all.
Fix: Added `_ht.record_job_fail("_grade_opportunities_job", "db_not_ready")` on early return; `record_job_run` and `record_job_fail` on success/failure paths.

### MEDIUM — bot/commands.py
`cmd_config` called `sorted(config.ud_alert_sports)` and `config.active_sports` before the try block — if either was `None` (due to the config bug above), it raised `TypeError` before the try, routing to PTB's global error handler ("Command failed").
Fix: `sorted(config.ud_alert_sports or [])` and `config.active_sports or []`.

### HIGH — bot/commands.py
`/picks` silently swallowed all `get_ud_recommendations_bulk` failures (`except Exception: pass`) and had no logging when individual props returned no recommendation (showing "—" instead of OVER/UNDER).
Fix: Changed to `except Exception as _rec_exc: logger.warning(...)`. Added `logger.warning` for empty map and per-prop misses. Added case-insensitive stat_type fallback lookup in the rendering loop to handle normalisation drift between PropLineHistory and UnderdogSnapshotRecord.

### MEDIUM — bot/commands.py
`PropPickAdapter.best_side` was hardcoded to `"OVER"` unconditionally, causing `/slip` to always show OVER regardless of the actual scoring direction.
Fix: Changed default to `"—"` (neutral placeholder).

### MEDIUM — bot/market_engine.py
`_scored_props` debug top-N was `sorted(..., key=lambda x: x["total"])[:10]` across all sports — comparing MLB total scores against Tennis/CS/DOTA scores on a single leaderboard.
Fix: Group by sport first (dict keyed by sport), take top-3 per sport, then concatenate — never compare across sports.

### MEDIUM — bot/tests/test_underdog_job_summary.py
`broadcast_alert` mock patched `alerts.broadcast_alert` instead of `market_engine.broadcast_alert`. Because `market_engine.py` uses `from alerts import broadcast_alert` (binding in its own namespace), the mock had no effect — broadcast side-effects leaked into tests.
Fix: Changed to `patch("market_engine.broadcast_alert", ...)`.

## What was confirmed already fixed (no action taken)
- `MarketSnapshot.external_id` — all 3 call sites already use `getattr(snap, "external_id", None)`.
- WAL mode — already enabled in `database.py` `init()` (`PRAGMA journal_mode=WAL` + `synchronous=NORMAL`).
- `_prune_prop_history_job` — already had `record_job_run` / `record_job_fail`.
- `cmd_config` fallback `reply_text` — IS inside the except block; real risk was the pre-try code (fixed above).

## Stabilization pass fixes (applied after audit)

### /picks "Pick: —" fix
Root cause: `get_ud_recommendations_bulk` queries UnderdogSnapshotRecord within 24h non-removed only. When snapshots age out or are marked removed, _rec_map returns empty → "—".
Fix: Added `bet_recommendation` + `bet_confidence` columns to `PropLineHistory` model + migration. `sync_underdog_snapshots_to_prop_history` now copies these from the source snapshot. `/picks` reads `plh.bet_recommendation` directly as the primary source; live snapshot rec_map used as override for same-cycle freshness.

### /slip best_side always "—" fix
Root cause: `cmd_slip` built `PropPickAdapter` without ever setting `best_side` from DB recommendation.
Fix: After building adapters, cmd_slip now calls `get_ud_recommendations_bulk` and sets `adapter.best_side` for each leg that has an OVER/UNDER decision.

### /alerts silent failure fix
Root cause: DB exception in cmd_alerts was caught at `logger.debug` (invisible); final `reply_text` had no fallback for HTML parse failures.
Fix: Changed to `logger.warning`; outer send wrapped in try/except with plain-text strip fallback.

### Standing candidates cross-sport ranking fix
Root cause: `_standing_candidates.sort(...)` compared all sports on one global leaderboard (line 1357).
Fix: Group by sport first (`_by_sport`), take top-3 per sport, flatten — same per-sport isolation as debug summary.

## Test count after fixes: 2672 passing (stable, zero failures)
Note: count dropped from 2685 to 2672 — difference predates this session (class-based and parametrized tests in some files not matched by the earlier grep; confirmed by per-file collect-only).
