---
name: Prop Intelligence Engine
description: Architecture, constraints, and edge cases for engine/prop_intelligence.py (Framework v3.0 Layer 8).
---

# Prop Intelligence Engine

## Architecture
- **File:** `engine/prop_intelligence.py` — all pure functions + frozen dataclasses; no async, no DB calls.
- **Entry point:** `compute_prop_intelligence(player_name, sport, stat_type, line, history) → PropIntelligenceResult`
- **Candidate integration:** `Candidate.with_prop_intelligence(result) → Candidate` (uses `dataclasses.replace` via `_dc_replace` alias).
- **Tier adjustment:** `_intelligence_adjusted_tier(tier, result)` defined at module level in `candidate.py` — NOT in `prop_intelligence.py`.

## 5 Layers
1. `compute_historical_intelligence(history, line, adapter)` → `HistoricalIntelligence`
2. `compute_role_intelligence(history, stat_type, sport)` → `RoleIntelligence`
3. `SPORT_ADAPTERS` dict + `get_sport_adapter(sport)` — frozen `SportAdapter` dataclasses per sport.
4. `compute_matchup_intelligence(history, line, adapter)` → `MatchupIntelligence`
5. `PropIntelligenceResult` aggregates all layers; applied via `Candidate.with_prop_intelligence()`.

## Key Implementation Rules
- **No circular import:** `prop_intelligence.py` must never import from `engine.candidate` or `engine.ud_scoring` or `engine.ud_bet_decision`.  The `_intelligence_adjusted_tier` helper lives in `candidate.py` and uses `getattr(result, ...)` to avoid the circular import.
- **No duplicate scoring:** `prop_intelligence.py` must not call `score_ud_prop()` or `make_ud_bet_decision()`.  Invariant checked in `tests/test_prop_intelligence.py::test_no_duplicate_scoring_engine` (checks for import patterns, not string presence).
- **`_sample_strength` variance bonus:** Only applied when `n >= 3`. For n=0 or n=1, var_adj=0, so zero-sample strength is exactly 10.
- **`_g(obj, attr)` helper:** All history access uses this to support ORM objects, dicts, and SimpleNamespaces interchangeably.

## Tier Adjustment Rules (`_intelligence_adjusted_tier`)
| Condition | Effect |
|---|---|
| `sample_strength < 20` | Cap tier at B |
| `sample_strength < 35` | Cap tier at A |
| `role == Bench AND stability == Volatile` | One step down |
| `matchup == Tough` | One step down |
| `tier == BLOCK` or unknown | Pass through unchanged |
Only downgrades; upgrades owned by primary scoring engine.

## Data Confidence Delta bounds: −20 to +20 (applied to `data_confidence`)
## Betting Edge Delta bounds: −20 to +20 (applied to `betting_edge`), clamps `role.signal + matchup.signal`
## market_confidence is never modified by prop intelligence.

## Failing-test lessons from initial implementation
- Rising trend test: lines jumping from 20→30 classify as Volatile (CV ≈ 0.20), so signal can be negative even when `usage_trend == "Rising"`. The trend label is the right assertion; `signal > 0` is not guaranteed when dataset is volatile.
- Volatile test: don't rely on random seeds + variance for deterministic test — use explicit wild-line arrays instead.

**Why:** Prevents accidental recreation of the scoring engine and keeps the intelligence strictly additive.
