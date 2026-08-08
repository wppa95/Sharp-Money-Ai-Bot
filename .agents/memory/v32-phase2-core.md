---
name: V3.2 Phase 2 Core
description: Four targeted fixes — esports picks unblocked, /health display lines removed, display tier fallback for no-cross-provider-data sports.
---

## Fixes applied (Aug 2026 — 3,733 tests passing)

### P1 — CS/LOL/esports S-tier picks were blocked (removed eff_conf < 55 gate)
**Rule:** Never gate /picks display loop on bet_confidence or proxy_match_confidence. The DB query score_tier.in_(["S","A"]) is the authoritative confidence gate.

**Why:** The eff_conf < 55 gate added in the prior cleanup pass used bet_confidence (from UnderdogSnapshotRecord) as a secondary filter. For esports (CS/LOL/VAL/ESPORTS), bet_confidence is low due to limited historical data (n_history < 5 → data_conf ≈ 40), even when score_total = 85 (S-tier). This blocked all CS/LOL picks from /picks even though they correctly qualified in the funnel. The prop scoring and bet_confidence are DIFFERENT values from different pipeline stages.

**How to apply:** If adding a confidence gate to /picks in the future, use plh.score_confidence (engine's own confidence from scoring), not bet_confidence or proxy_match_confidence. The DB filter score_tier.in_(["S","A"]) already enforces the scoring confidence floor.

### P1b — _render_pick_entry: tier/confidence display fallback for esports
**Rule:** When proxy_match_confidence < 30 AND plh.score_tier is S/A/B AND plh.score_confidence is not None, use plh.score_tier and plh.score_confidence for tier/stars/confidence display instead of proxy_match_confidence.

**Why:** For CS/LOL/VAL/ESPORTS, there is no PrizePicks/DK/FD cross-provider data (DK/FD disabled, PP doesn't offer esports). proxy_match_confidence is always 0 for these sports. Without the fallback, S-tier esports props displayed as "Tier —" with 1 star. The threshold < 30 (not < 55) is intentional: at 30+ proxy conf, cross-provider data exists and should be used for display.

### P4 — "Previous session:" and "Crash detected:" removed from /health
**Rule:** cmd_health lines list must not include "Previous session:" or "Crash detected:" display lines. All underlying HealthTracker methods (was_unexpected_exit, last_startup_reason, crash_cause_label, last_session_duration_str) remain intact.

**Why:** User requested removal of noisy display lines. This follows the prior-pass removal of "Restart reason:".

**How to apply:** Do not re-add any of these three display lines to cmd_health: "Restart reason:", "Previous session:", "Crash detected:". Tests in test_health_stale_recovery.py guard all three.

### Prior-pass fixes still intact
- count_actionable_pick_records() OVER/UNDER filter
- get_funnel_summary() near-miss accepted-key dedup
- get_top_ud_props_for_picks() score_tier.in_(["S","A"])
- _prop_market_alerted dedup restored from DB on restart
- _fmt_user_ts() 12h AM/PM format
- Stale-recovery _RECOVERY_STALE_HOURS gate
