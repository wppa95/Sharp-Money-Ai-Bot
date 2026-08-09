---
name: V3.5 Reporting Cleanup + Restart Resume
description: Display cleanup (24h /alerts, today-only dashboard), fast-resume checkpoint on restart, cold-start tests; 4030 tests passing Aug 2026.
---

## Changes

### Display cleanup
- `/alerts` window: 72h → 24h; all-time count (`count_actionable_pick_records`) removed from display and query
- `/status`: all-time count removed; only daily (`count_today_actionable_alerts`) shown
- Dashboard: `_gather_daily_trend` changed from 7-day loop `range(6,-1,-1)` to today-only `(0,)` single entry; header changed from "Last 7 Days" → "Today"

### Restart-resume checkpoint (health.py + market_engine.py)
- `HealthTracker.record_scan_checkpoint()` — persists `last_scan_checkpoint_ts` (Unix epoch float) + `last_scan_checkpoint_at` (ISO) to health JSON sidecar after each successful scan
- `HealthTracker.get_scan_checkpoint_age_minutes()` — returns age in minutes, or None if no checkpoint
- `market_engine._fast_resume: bool` — set True in `_init_state_from_db` when checkpoint age < `_FAST_RESUME_THRESHOLD_MINUTES` (30)
- Cold-start rescore path gated: `elif not is_removed and is_cold_start and not _fast_resume:` — skips full rescore when checkpoint is fresh
- Checkpoint recorded at end of every successful `underdog_job` scan (non-fatal if health tracker missing)

### Cold-start behavior (documented, NOT changed)
- `is_cold_start = not _cold_start_done` — True only on first scan; `_cold_start_done` latched after
- Cold-start is NOT a permanent blocker — props scored+saved during cold-start become eligible via standing path on scan 2+
- MLB/NFL UNDER: Tier 2 blocked at all 3 paths regardless of cold-start
- Tier 1 sports (WNBA, CS, DOTA, etc.) allow both OVER and UNDER

### /picks empty after restart: EXPECTED BEHAVIOR
- `/picks` queries `PropOpportunityLog.alert_sent=True` rows (DB-backed, survives restart)
- Empty = no picks alerted today yet; requires multi-snapshot accumulation + all gate passes

## Test files updated
- `bot/tests/test_v35_cold_start_restart.py` — 49 new tests (all 6 categories)
- Updated old tests to match V3.5 behavior: `test_v32_post_freeze_cleanup.py`, `test_v32_final_targeted_fix.py`, `test_v32_phase2_core.py`, `test_v32_consolidated_cleanup.py`, `test_bugs_117_120.py`, `test_dashboard.py`

## Why
- `/alerts` 72h was confusing: showing 3-day history in a "recent alerts" command suggests incomplete coverage when the bot is working fine
- All-time counts in status commands create support confusion (user sees "200 all-time" and thinks today's 0 is a bug)
- Fast-resume avoids rescoring ~1000 props on every restart when the scan ran < 30 min ago — reduces boot time and API pressure

## 4030 tests passing Aug 9 2026
