---
name: Tier-1 direction gate (Weak direction fix)
description: ud_bet_decision.py uses sport-aware threshold to allow "Weak" direction for Tier-1 props
---

## Rule
`make_ud_bet_decision` now accepts an optional `sport: str = ""` parameter.

- **Tier-2** (MLB/NBA/NFL or empty sport ""):  `_B_RATE = 0.60` → rate ≥ 0.60 OVER, ≤ 0.40 UNDER
- **Tier-1** (any other known sport): `_B_RATE_TIER1 = 0.55` → rate ≥ 0.55 OVER, ≤ 0.45 UNDER, 0.45–0.55 PASS

All 7 call sites in `market_engine.py` (lines ~1526, 1850, 1952, 2531, 3494, 3540, 3836, 4239) now pass `sport=<sport_var>`.

Stable refresh, watchlist, and FPR paths also updated.

**Why:** Tier-1 sports like WNBA/TENNIS/CS have smaller samples and wider natural variance. "Weak" directional props (rate 0.55-0.60) should reach the scoring/delivery pipeline for Tier-1; downstream BQ/MQ/conf gates remain strict.

**How to apply:** All new `make_ud_bet_decision` calls must pass `sport=` for correct tier routing. Empty string falls back to Tier-2 threshold (backward compatible).
