---
name: V3.2 Tier 1 stable actionable fix
description: Standing path score_tier=NULL fallback so stable Tier 1 props aren't silently dropped after a no-change cycle.
---

## The rule
In `market_engine.underdog_job` (standing path candidate filter), when the latest snapshot has `score_tier=None` (stored during a no-change cycle without re-scoring), derive effective tier from `score_total` using S≥80 / A≥65 thresholds before applying the A/S quality floor.

**Only applies when `score_tier is None`.**  Explicit "B" or "PASS" tiers are NOT promoted — the fallback guard is `if _prev_eff_tier is None`.

## Why
No-change cycles store a `UnderdogSnapshotRecord` with `score=None → score_tier=NULL`.  A stable esports/WNBA prop that previously scored S-tier would then be invisible to the standing path (the old gate was `score_tier not in ("A","S")`), so it never reached Telegram even though it was "qualified" in /funnel.

## How to apply
- Variable: `_prev_eff_tier` (introduced in the fix)
- Gate check: `if _prev_eff_tier not in ("A", "S"): continue`
- Existing test `test_score_tier_gate_still_in_standing_path` updated to check `_prev_eff_tier not in ("A", "S")`
- All other standing-path gates (validation, supporting_data, direction, conf, BQ, live-game, 24h dedup) unchanged
- MLB/NFL strict gates (S-tier + BQ≥95) unchanged
- `MIN_UNDERDOG_LINE_CHANGE=0.5` unchanged
- 3369 tests passing Aug 2026
