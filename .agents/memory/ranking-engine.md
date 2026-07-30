---
name: AI Ranking & Backtesting Engine
description: Design decisions for engine/ranking.py, engine/backtesting.py, and the /performance + /backtest commands.
---

## Architecture

`engine/ranking.py` wraps `compute_confidence()` — it does NOT re-implement any confidence signal logic. It adds a historical adjustment layer (±10 pts max) on top of the 0-100 confidence score.

## Tier thresholds
S ≥ 95, A ≥ 85, B ≥ 75, Pass < 75 — same numeric floor as `RankingTier.from_score(score)`.

## Historical adjustment budget (±10 pts total)
- Win rate adj: ±5 pts (≥60% WR → +5; 38% WR → -5)
- CLV adj: ±3 pts (≥3% avg CLV → +3; below -6% → -3)
- Market-type adj: ±2 pts (market WR ≥58% → +2; <42% → -2)
- ML override stub: 0 pts until a model is trained

**Why:** Historical signals confirm live market intelligence but must never override it. Capping at ±10 ensures a weak live signal can't be rescued by past performance alone.

## TAKE gate
TAKE requires: tier ≥ B AND zero HIGH-severity confidence warnings.
HIGH-severity warnings: SINGLE_BOOK, GAME_IMMINENT, LOW_LIQUIDITY.

**Why:** A single-book move is unconfirmed; betting into it despite good history is a leak.

## Backtesting
`BacktestEngine.run(ev_records)` uses stored `ai_confidence` as the ranking score directly — avoids reconstructing 9 inputs from partial historical data.
TAKE threshold = 75 (tier ≥ B). PASS records are excluded from win rate / CLV stats.
`DimensionStats.win_rate` returns None when fewer than 5 resolved bets exist (same as MIN_SAMPLE_SIZE).

## CLV threshold boundaries (important for tests)
```
avg_clv >= 3.0  → +3
avg_clv >= 1.5  → +2
avg_clv >= 0.0  → +1
avg_clv >= -2.0 → 0
avg_clv >= -4.0 → -1
avg_clv >= -6.0 → -2   # -5.5% lands HERE
below -6.0      → -3   # need < -6.0 (e.g. -7.0%) to get -3
```

## DB methods added
- `get_ev_records_with_results(sport, market_type, limit, include_pending)`
- `update_ev_record_result(record_id, result, clv)`

## Known gaps (tracked as separate tasks)
- `analysis.py` still uses old `AIConfidenceScorer` (5-signal); `compute_ranking()` is a separate call on top → Task #12.
- `minutes_to_game` not derived from `event_start` yet → Task #14.
- format_ev_alert injects ranking block via optional `ranking_result` kwarg; live wiring to the main alert pipeline → Task #13.
