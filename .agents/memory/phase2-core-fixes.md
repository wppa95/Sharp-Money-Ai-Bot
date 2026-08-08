---
name: Phase 2 Core fixes
description: Four targeted fixes applied in Phase 2 Core — props=0, /restarts removal, auto-grading, decision_tier clarity
---

## #114 props=0 — FIXED
`market_engine.py`: `_n_ud_snaps_this_cycle = len(ud_snaps)` captured immediately after ud_snaps populated (line ~968). `_health.record_underdog_scan(props_count=_n_ud_snaps_this_cycle, ...)` uses captured value. `ud_snaps.clear()` at line ~2276 (OOM fix) no longer causes props=0.

**Why:** .clear() emptied the list 170 lines before health recording. Old code read len(ud_snaps) after clear → always 0.

## #115 /restarts removed — FIXED
`commands.py`: `cmd_restarts` function deleted entirely.
`main.py`: import removed, `CommandHandler("restarts", cmd_restarts)` removed.

**Why:** Restart classification confused dev restarts (SIGKILL race with PTB graceful shutdown) with genuine crashes. Removal requested by Phase 2 Core spec; persistent DB state is never erased by restarts.

## #116 auto-grading — FIXED
`main.py` `_grade_opportunities_job`: fetch-results loop (mirrors /backfill) added BEFORE grading loop. Uses `PlayerStatsProvider.fetch_results()` + `upsert_player_result()` per unique (player, sport, stat_type) with dedup set `_grade_fetched`. Fetch errors caught/logged per-player — do not abort grading.

**Why:** Grader only read PlayerResult rows already in DB from prior scans. Props removed from Underdog before game completion never got a result row → stayed PENDING forever.

## #113 decision_tier clarity — FIXED
Both `_scored_props.append({...})` dicts in `market_engine.py` (new-prop path ~line 1349, line-change path ~line 1648) now include `"decision_tier": (decision.decision_tier if decision is not None else None)`.

**Why:** score.tier (UDPropScore composite) and decision.decision_tier (bet-decision confidence tier S≥85/A≥70) are independent systems. MLB gate uses decision_tier. Storing both in PropCandidateLog makes mlb_tier_blocked rejections legible ("Composite S, Decision A → blocked").

## Test counts
Phase 2 Core: 22 new tests in test_phase2_core_fixes.py
Phase 2 lifecycle (#112): 22 tests in test_phase2_lifecycle_fixes.py (1 test updated: source-code scan anchor changed from "record_underdog_scan" to "_health.record_underdog_scan(" to avoid matching the new comment)
Total: 3,256 passing, 0 failures. Bot startup #177 clean.
