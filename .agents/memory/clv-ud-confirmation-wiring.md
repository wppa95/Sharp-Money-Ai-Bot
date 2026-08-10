---
name: CLV UD confirmation wiring
description: How OddsAPI confirmation avg_odds feeds into AlertCLVSeed for Underdog S/A pick CLV tracking.
---

## Rule
When an Underdog S/A alert fires and `_get_odds_api_confirmation()` returns `avg_odds`,
call `db.seed_clv_from_ud_confirmation(...)` to create/upgrade an AlertCLVSeed with real
sportsbook bet_odds. Without this, all UD seeds have `bet_odds=None` and the harvest job
immediately expires them.

## Harvest guard change
`main._clv_harvest_job` guard was:
```python
if not seed.bet_odds or seed.alert_type == "UNDERDOG":
```
Changed to:
```python
if not seed.bet_odds:
```
This allows UD seeds that DO have bet_odds (from OddsAPI confirmation) to proceed to
closing-line lookup.

**Why:** UD pick'em has no sportsbook odds, but when OddsAPI confirms an S/A prop we
have a real market price (avg_odds across books) that serves as the bet_odds proxy.

## Key gap still open
`CLVRecord` creation in `main._clv_harvest_job` at line ~971 uses `clv_result.clv_lead`
but `CLVResult` has `clv_proxy` not `clv_lead`. This will AttributeError when closing
odds are actually found. Fix: change `clv_result.clv_lead` → `clv_result.clv_proxy` in
both main.py and tests/test_clv_harvest.py `_run_harvest_logic`.

## Where the 3 call sites are
All in `bot/market_engine.py`, after `ud_result.sent` in:
1. New-prop path — `_np_odds_confirm`
2. Line-change path — `_lc_odds_confirm`  
3. Standing path — `_s_odds_confirm`

## DB method
`Database.seed_clv_from_ud_confirmation()` in `bot/database.py`.
Uses ON CONFLICT DO UPDATE SET bet_odds=avg_odds WHERE bet_odds IS NULL —
so calling twice doesn't overwrite a confirmed seed.
