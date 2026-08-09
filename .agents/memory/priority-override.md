---
name: S-tier priority override system
description: 90-94/100 S-tier gets 🔥 priority label; 95+/100 S-tier bypasses all gates and sends immediately via broadcast_alert
---

## Rule
- **89/100 and below** → normal behavior, no change.
- **90–94/100 S-tier** → `high_priority=True` passed to `deliver_underdog()`; prepends 🔥 S-TIER HIGH PRIORITY header. All gates still active.
- **95+/100 S-tier** → `broadcast_alert()` called directly (bypasses deliver_underdog and ALL gates). Sets `np_immediate=False` / `_lc_95_sent=True` / `continue` in respective path to prevent normal gates from also firing.

## Implementation files
- `market_engine.py`: `_format_95_priority_alert()` (module-level), `_priority_override_sent` (module-level set), `_priority_alerted_this_scan` (per-scan local set in underdog_job), `_lc_95_sent` (per-prop bool), 95+ checks in all 3 alert paths, `high_priority=...` kwarg on all 3 `deliver_underdog` calls.
- `alerts.py`: `high_priority: bool = False` param on `deliver_underdog()`; header prepended after message formatted, before broadcast.

## Spam guard for 95+
`_priority_override_sent`: module-level set of `(player, sport, stat_type)` tuples. Prevents re-sending the same prop's override for the lifetime of the process. Cleared on restart. `_priority_alerted_this_scan` (local to each underdog_job call) prevents double-send within one scan.

## Pitfall: test patch target
`market_engine.py` uses `from alerts import broadcast_alert` (name import). Tests that patch `"alerts.broadcast_alert"` must ALSO patch `"market_engine.broadcast_alert"` to cover the direct 95+ override calls. The `_run_job` helper in `test_underdog_job_integration.py` was updated accordingly.

## Pitfall: bare `< 95` literal
A pre-existing test (`test_no_hardcoded_95_in_bq_gates`) scans for `< 95` literals not followed by a comment. All three `high_priority` conditions use `< 95  # 95+ uses override path; 90-94 gets label` trailing comments to pass the regex.

## Pitfall: standing path test for lifecycle append order
`test_standing_path_has_lifecycle_append` finds the FIRST `_n_standing_sent += 1` and looks 400 chars forward for `_lifecycle_alerted.append`. In the 95+ override block, `_n_standing_sent += 1` must come BEFORE `_lifecycle_alerted.append` (matching normal path order).

**Why:** 95+ is extremely rare; system-wide behavior is preserved for all props below 95. Bypass logic is surgical (3 insertion points, no threshold changes).

**How to apply:** Score thresholds are checked AS `score.total >= 95 and score.tier == "S"` — both conditions required. Non-S-tier props (A, B, PASS) are never affected regardless of score.total.
