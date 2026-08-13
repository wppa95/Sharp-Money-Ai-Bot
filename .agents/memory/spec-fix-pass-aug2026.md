---
name: Spec fix pass Aug 2026
description: Scheduler, gate, and alert format changes from the large spec fix pass (21 items).
---

## Key decisions

### Scheduler / concurrency
- `max_instances=1 → 2` for `underdog_monitor` in `main.py`
- Module-level `_ud_full_scan_running: bool = False` in `market_engine.py`
- Second instance hits the flag → runs fast new-prop fetch only → returns
- `_ud_full_scan_running` is reset to False on ALL exit paths including early returns (fetch exception, empty snapshots) via explicit resets before each `return` and before health recording at end

### S-tier priority override system — REMOVED
- All 4× `_format_95_priority_alert` broadcast_alert calls removed from `underdog_job` (new-prop, lc, standing, stable-refresh)
- `_lc_95_sent`, `_priority_alerted_this_scan`, `_sr_priority_this_cycle` all removed
- `high_priority=True` kwarg no longer passed to `deliver_underdog` from engine paths
- The 80-94 BQ `high_priority=` block removed from all 3 deliver calls
- `_format_95_priority_alert` function still defined (dead code) — harmless
- All alerts now flow through unified `🎯 ACTIONABLE BET PICK` format via `AlertDelivery.deliver_underdog()`
- alerts.py: `_CappedDecision` wrapper caps S-tier to A-tier display if MQ<80 or conf<80

### Gate removals (delivery paths)
- `_lc_strict_tier_ok` removed from lc `is_qualified`
- `score.stars >= config.min_stars_for_sport()` removed from lc, standing, stable-refresh, FPR delivery paths
- FPR strict-sport S-only gate removed
- `np_immediate` changed from `min_stars_for_sport()` to `UD_NON_STRICT_MIN_STARS` (uniform 2★ floor)
- Only retained: `min_stars_for_sport` in watchlist path (internal quality gate only)

### Standing path extension
- `_prev_eff_tier not in ("A", "S")` → `_prev_eff_tier not in ("A", "S", "B")` — B-tier now enters standing path
- score_total ≥ 50 → "B" fallback for null score_tier

### lc rejection labels
- `elif score.stars < config.min_stars_for_sport(...)` branch removed
- `elif not _lc_strict_tier_ok:` branch removed

### Watchlist /funnel display — REMOVED
- "👁 Watchlist Refresh" display block removed from `/funnel` output in commands.py

### Health server
- `if _health_runner is not None: return` idempotency guard added in main.py

**Why:** Per spec — all S/A/B tiers actionable for all sports; unified alert format; fast-fetch cadence preservation.

**How to apply:** If reverting any of these, update the corresponding test files — many tests were updated in tandem (test_cred_burning_protection.py, test_duplicate_dedup.py, test_gate_adjustment_final.py, test_live_gate_et_format.py, test_pipeline_fix.py, test_priority_override.py, test_scan_performance.py, test_stable_refresh.py, test_stale_line_guard.py, test_tier1_final_fix.py, test_underdog_job_integration.py, test_v34_star_system.py, test_v35_cold_start_lifecycle.py).
