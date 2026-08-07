---
name: Market-Signal Bypass (Gate 1)
description: Decision engine Gate 1 bypass for high-confidence props without game history — market-signal direction used when score≥70 and line moved ≥2%.
---

## Problem solved
Props scoring ≥70/100 (B-tier or better) were being labeled decision_pass because Gate 1 in `ud_bet_decision.py` requires real game history (`hit_rates.has_real_data=True`). For CS/LOL/Tennis/DOTA players, no game history exists yet → all props → PASS → 0 alerts.

The root cause also included a bug: `hit_rates=[]` was passed to `make_ud_bet_decision` at cold-start (line ~1338 in `market_engine.py`), causing `'list' object has no attribute 'has_real_data'` errors captured by the health sidecar.

## Fix

### P0 — cold-start bug (market_engine.py)
Changed `hit_rates = []` → `hit_rates = None` at the cold-start `make_ud_bet_decision` call. The defensive guard in the decision engine already handles None.

### P1 — Gate 1 market-signal bypass (ud_bet_decision.py)
When `hit_rates is None or not hit_rates.has_real_data`:
1. Compute `_score_total = getattr(score, "total", None)` defensively (handles MagicMock in tests).
2. If `isinstance(_score_total, (int, float)) and _score_total >= 70 AND score.tier in (S/A/B/C) AND |avg_vs_line_pct| >= 0.02`:
   - Direction: `avg_vs_line_pct > 0` → OVER (line moved down from historical avg), `< 0` → UNDER
   - Tier: `score.tier` (mirrors scoring tier)
   - Confidence: `min(int(score.total * 0.90), 85)` — capped lower since no game history
   - Reason: explicitly says "no game history yet"
3. Otherwise: fall through to the original PASS path.

## Why
- `avg_vs_line_pct = (avg_line - current_line) / avg_line`
  - > 0 → Underdog lowered the line from historical average → OVER is easier → OVER signal
  - < 0 → Underdog raised the line → OVER is harder → UNDER signal
- 2% threshold filters noise from tiny line adjustments
- 85 confidence cap reflects the absence of verified game results

## Rules
- Decision Pass (score < 70) is unchanged — low-confidence props still get PASS.
- Props with no directional signal (no avg_line, |pct| < 2%) still get PASS even if score ≥ 70.
- All window fields (l5, l10, etc.) are None on market picks — no game data to show.
- The `getattr` defensive guard is critical: `_score_mock()` in tests doesn't set `s.total`, so `score.total >= 70` would raise TypeError without it.

## C-tier display
Added `"C": "▪️ C-Tier"` to `tier_display()` in `UDBetDecision` — C-tier was previously falling through to "—".

## Test count
3090 tests passing (Aug 2026). New file: `bot/tests/test_decision_market_signal.py` (20 tests).
