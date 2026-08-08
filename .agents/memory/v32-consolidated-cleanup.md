---
name: V3.2 Consolidated Cleanup
description: Four targeted fixes — alert count consistency, funnel near-miss dedup, /picks tier gate, /health restart reason removed.
---

## Fixes applied (Aug 2026 — 3,675 tests passing)

### P1 — count_actionable_pick_records() OVER/UNDER filter
**Rule:** `count_actionable_pick_records()` must filter `recommendation.in_(["OVER","UNDER"])` to stay consistent with `get_alerted_opportunity_log()`.

**Why:** PropOpportunityLog uses UPSERT on (external_id, stat_type). A prop alerted as OVER that is later re-evaluated to PASS has alert_sent=True but recommendation=PASS. The old count query had no recommendation filter so it included these rows; the "shown" query already filtered OVER/UNDER. Both must use the same universe.

**How to apply:** Any new count query on PropOpportunityLog.alert_sent must also filter recommendation.in_(["OVER","UNDER"]).

### P2 — get_funnel_summary() near-miss dedup
**Rule:** Near-Misses must exclude props that have ANY ACCEPTED row in the same time window.

**Why:** `log_prop_candidate_batch` does plain INSERT (not upsert), so the same prop accumulates multiple rows across scan cycles. A prop accepted in scan N but rejected in scan N+1 (or vice versa) would appear as a Near-Miss even though it was properly handled. The fix: fetch all ACCEPTED (player_name, sport, stat_type) in the window, build a set, exclude from REJECTED results.

**How to apply:** Always deduplicate near-miss queries by excluding props with any ACCEPTED row in the window.

### P3 — get_top_ud_props_for_picks() tier gate
**Rule:** `score_tier.in_(["S", "A"])` — never `score_tier != "PASS"`.

**Why:** `!= "PASS"` allowed NULL-tier (unscored) props through, causing 30-confidence / Tier — props to appear in /picks. NULL passes `!= "PASS"` in SQLAlchemy (Python-level comparison), so unscored rows were included.

**How to apply:** Any /picks DB query must use .in_(["S","A"]) for the tier filter.

### P3b — cmd_picks confidence floor
**Rule:** In `_cmd_picks_inner` _sport_groups loop, skip props where `eff_conf < 55` (Tier — threshold).

**Why:** Belt-and-suspenders against stale PropLineHistory score_tier vs. current low snapshot confidence.

### P4 — Restart reason line removed from /health
**Rule:** The user-facing `f"Restart reason:   {reason_label}"` line is removed from cmd_health lines list.

**Why:** User requested removal. All underlying detection (was_unexpected_exit, last_startup_reason, crash_cause_label, _REASON_LABEL dict) remains intact in health.py and commands.py — only the display line is gone.

**How to apply:** Do not re-add "Restart reason:" to the cmd_health lines list. The test `test_restart_reason_section_removed` in test_health_stale_recovery.py guards this.
