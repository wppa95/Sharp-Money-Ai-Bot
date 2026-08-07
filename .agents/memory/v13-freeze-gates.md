---
name: V1.3 Freeze Gates + DK/FD Removal
description: MLB UNDER block, C-tier gate fix, sport funnel breakdown, DK/FD connector removal — all implemented Aug 2026.
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

### DK/FD connector removal
**Why:** DraftKings and FanDuel connectors were enabled=False (never producing live data). Their snapshots stored in the DB but never fed Underdog confidence scores, so they improved 0 actionable picks. They used the shared OddsAPI cache, so cost was zero — but value was also zero.

**How to apply:** Do not re-add DK/FD connectors unless there is a clear pathway for their market data to improve Underdog confidence scores. The OddsAPI confirmation layer (`_get_odds_api_confirmation`) already covers S/A sportsbook cross-check without needing dedicated connectors.

**Files deleted:** `bot/connectors/draftkings.py`, `bot/connectors/fanduel.py`

**Files updated:**
- `bot/connectors/__init__.py` — removed DK/FD from imports and `__all__`
- `bot/main.py` — removed DK/FD connector imports and `registry.register()` calls
- `bot/tests/test_connectors.py` — removed `TestDraftKingsConnector`, `TestFanDuelConnector` classes and imports
- `bot/tests/test_sports_config.py` — removed `TestConnectorMappings` class and DK/FD assertions from `TestActiveSportsConfig`

`make_mock_dk` and `make_mock_fd` in `connectors/mock.py` are kept — they return `MockOddsConnector` instances and do NOT import from the deleted files.

## Test count
3070 tests passing (Aug 2026). New file: `bot/tests/test_alert_gates.py` (16 tests covering MLB UNDER gate logic, C-tier qualification, sport funnel breakdown DB, funnel command rendering, DK/FD removal verification).
