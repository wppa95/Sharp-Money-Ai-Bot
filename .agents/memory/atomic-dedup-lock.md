---
name: Atomic delivery dedup lock
description: _try_claim_delivery_slot pattern — how dedup check+record is made atomic, and why.
---

## Rule
All 4 delivery paths (delivery queue loop, SR, WL, FPR) must call `_try_claim_delivery_slot(player, sport, stat, line)` **before** calling `deliver_underdog`. No path may call `_record_prop_alerted` after delivery — the pre-claim already does it.

## Key identifiers
- `_prop_dedup_lock` — module-level `asyncio.Lock()` in `market_engine.py`
- `_try_claim_delivery_slot(player, sport, stat, line)` — async helper; acquires lock, calls `_is_prop_deduped`, calls `_record_prop_alerted` if slot is free, returns True/False
- `_prop_market_alerted` — existing module-level dict; key=(player,sport,stat), value=(ts,line)

**Why:** The Nimmo duplicate problem (same MLB Hits OVER 0.5 sent at 6:43/6:45/6:47/6:49 ET) was caused by non-atomic check+record across concurrent jobs (delivery loop, SR, WL, FPR all running simultaneously). The lock makes it atomic.

## lc-path dedup gate
The line-change path was also missing a `_is_prop_deduped` guard at *collection* time (marked with comment `# dedup_gate [lc]`). It was added after the flip-cooldown block. Without it, a prop could be queued for delivery despite being recently alerted.

## Tier 2 BQ/MQ threshold: 85
`_tier_delivery_gate(sport, direction, bq_score, mq_score)` — Tier 2 sports (NBA, MLB, NFL) require **both** `bq_score >= 85.0` AND `mq_score >= 85.0`. Any value below 85 (including the old 75 threshold) is blocked.

**Why:** Spec specified ≥85; prior session used 75 (incorrect).

## Test patterns
- `conftest.py` autouse fixture clears `_prop_market_alerted` between tests — prevents stale dedup entries from blocking delivery assertions in lifecycle/health tests.
- Tests that need `deliver_underdog` to fire and use sports with real market-quality computation (hit_rates=None) must patch both `_tier_delivery_gate` (return True) and `_try_claim_delivery_slot` (AsyncMock, return_value=True).
- Atomic dedup tests use `asyncio.run(...)` not `asyncio.get_event_loop().run_until_complete(...)` — the latter fails in full-suite ordering after pytest-asyncio tests.
- Generic new-prop / integration tests use `sport="NHL"` (Tier 1, no BQ/MQ gate) not `sport="NBA"` (Tier 2).
