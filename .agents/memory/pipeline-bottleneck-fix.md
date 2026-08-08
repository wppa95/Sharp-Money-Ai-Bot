---
name: Pipeline bottleneck fix (standing path + esports HFS)
description: Two root causes blocked all non-MLB/NFL sports from ever reaching Telegram; fixes Aug 2026.
---

## Root Cause 1 — _HIGH_FLOOR_STATS excluded all esports stats
Standing path (market_engine.py ~1879): `if _st not in _HFS: continue` blocked CS/LOL props because
"Kills on Maps 1+2" and "Assists on Maps 1+2" were not in `engine/ud_scoring.py::_HIGH_FLOOR_STATS`.
Fix: added those two esports multi-map aggregate stats to the frozenset.

## Root Cause 2 — _processed_keys incorrectly blocked standing path for qualified-but-not-alerted props
`_processed_keys.add((player, stat_type))` was called at line ~1370 for ANY line-changed prop,
even when `should_alert=False` (sub-threshold delta). The standing path skips `_processed_keys`
members. Result: a prop with is_qualified=True, should_alert=False never reached Telegram via
either path. Fix: moved `_processed_keys.add()` to inside the `if should_alert and not is_removed:`
block (~1655) so only alert-eligible props are excluded from the standing path.

## Standing path debug logging added
Added `logger.debug()` for two previously-silent rejection points:
- `has_supporting_data=False` → "standing_gate [no_data]"
- `recommendation==PASS` → "standing_gate [decision_pass]"

## Invariants preserved
- _processed_keys still set for: new-prop path (line 1098), re-entry path (line 1515), should_alert=True line-change (line ~1657)
- 24h dedup prevents standing path double-alerts even after fix
- MLB/NFL strict gates (S-tier, BQ≥85) unchanged
- _HFS frozenset remains immutable; volatile stats (Home Runs, Saves, etc.) NOT added

**Why:** "qualified" in /funnel means watchlist_state='Qualified' (decision non-PASS) — NOT Telegram eligibility. Props can be DB-qualified but blocked by: missing _processed_keys fix, HFS filter, confidence gate, game-live gate, delivery filter.
