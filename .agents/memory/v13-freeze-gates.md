---
name: V1.3 Freeze Gates
description: MLB UNDER block, C-tier gate fix, and sport funnel breakdown
---

## Rules

### MLB UNDER block (all 3 alert paths)
MLB alerts are OVER-only. The block is applied at all 3 alert dispatch points in `market_engine.py`:
- **New-prop path** — standalone `if _np_bet_ready and decision.recommendation == "UNDER"` gate after the MLB tier gate.
- **Line-change path** — wired into `_lc_mlb_ok`: requires `tier in mlb_alert_tiers AND recommendation != "UNDER"`.
- **Standing path** — standalone `if _ssport.upper() == "MLB" and _sdec.recommendation == "UNDER": continue`.

Tracking and grading are unaffected — this is an alert-delivery filter only.

### C-tier now allowed in line-change gate
Changed `decision.decision_tier in ("S", "A", "B")` → `("S", "A", "B", "C")` in `is_qualified` (line-change path ~line 1414). The new-prop and standing paths were already implicitly permissive (no explicit tier tuple).

### Sport funnel breakdown (/funnel)
`get_funnel_summary()` in `database.py` now returns a `by_sport` key — a list of dicts `{sport, scanned, accepted, watchlist, rejected, removed}`, sorted by scanned descending. Aggregation is done in Python (sport × gate_decision GROUP BY rows → defaultdict). `cmd_funnel` renders a monospace table under "⚽ Sport Funnel Breakdown".

## Test count
3070 tests passing (Aug 2026). New file: `bot/tests/test_alert_gates.py` (16 tests covering MLB UNDER gate logic, C-tier qualification, sport funnel breakdown DB, funnel command rendering, DK/FD removal verification).
