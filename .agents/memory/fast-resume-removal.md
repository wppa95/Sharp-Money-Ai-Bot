---
name: Fast Resume removal + stale test resolution
description: Complete removal of the Fast Resume startup shortcut and resolution of 11 stale pre-existing test failures; V3.5 ruleset frozen.
---

## Fast Resume Removal

**What was removed:**
- `_fast_resume: bool = False` — module-level flag
- `_FAST_RESUME_THRESHOLD_MINUTES: int = 30` — threshold constant
- The entire fast-resume decision block in `_init_state_from_db` (checkpoint age check + `_fast_resume = True` branch + dual log)
- `_fast_resume` from the `global` declaration in `_init_state_from_db`
- All 4 runtime conditionals in `underdog_job`:
  - Cold-start elif: `not _fast_resume` guard removed → plain `elif not is_removed and is_cold_start:`
  - LC `is_qualified`: `(not is_cold_start or _fast_resume)` → `not is_cold_start`
  - LC debug tracking: `if is_cold_start and not _fast_resume:` → `if is_cold_start:`
  - Standing path gate: `if (not is_cold_start or _fast_resume) and chat_ids:` → `if not is_cold_start and chat_ids:`
- Fast-resume conditional in the cold-start completion log block

**What was preserved:**
- `record_scan_checkpoint()` still called after every scan (health monitoring only)
- `get_scan_checkpoint_age_minutes()` still functional on HealthTracker
- All cold-start scoring, ScanCycleLog, re-entry, dedup, CLV pipeline unchanged

**Why:**
Fast Resume caused cold-start/state-dependent behavior and was harder to reason about than one predictable startup path. Every restart now always performs a full cold-start rescore.

## 11 Stale Test Fixes

All 11 were stale (not genuine regressions):

1. **`TestBqPriorityLabel` (6 tests)** — Expected `"HIGH PRIORITY"` for BQ 80+; `_bq_priority_label` now returns `"💪 STRONG BET"`. Changed assertions to `"STRONG BET"`.

2. **`test_analyst_inline` (2 tests)** — `_make_decision()` MagicMock didn't set `l5_hit_rate` or `l5_games`; alert formatter accessed them and got MagicMock objects → `TypeError` on `:.0%` format. Fixed by adding `d.l5_hit_rate = None; d.l5_games = None` to `_make_decision()`.

3. **`test_cred_burning_protection::test_21/test_22`** — `_is_prop_deduped()` gained 2 required positional args (`dedup_window_seconds`, `min_line_change`) in a prior session; tests used old 5-arg signature. Fixed by passing `cfg.config.UD_ALERT_DEDUP_WINDOW` and `cfg.config.MIN_UNDERDOG_LINE_CHANGE`.

4. **`test_cred_burning_protection::test_24`** — Checked for `active_snaps/all_snaps/snap_map` in source; code uses `ud_snaps`. Added `"ud_snaps" in src` to OR condition.

## Final state
4298 passing · 0 failures · V3.5 ruleset frozen · bot in observe mode
