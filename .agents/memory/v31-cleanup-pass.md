---
name: V3.1 Cleanup pass — 5 gate fixes
description: All 5 outstanding priorities from the requirements doc implemented Aug 2026; 3129 tests passing.
---

## Items completed

**#1 — MLB/NFL BQ ≥ 85 gate**
- `config.py`: `UD_STRICT_SPORT_MIN_BET_QUALITY = 85` (env-overridable).
- `market_engine.py`: gate added after `sport_tier_gate` in BOTH new-prop path AND standing path.
  Block condition: `sport in ud_strict_alert_sports AND decision.confidence < UD_STRICT_SPORT_MIN_BET_QUALITY`.

**#2 — Relax gates for non-MLB/NFL sports**
- `config.py`: added `UD_NON_STRICT_MIN_STARS=2`, `UD_NON_STRICT_MIN_CONF_A=60`, `UD_NON_STRICT_MIN_CONF_B=45`.
- `config.py`: added `min_stars_for_sport(sport)` and `min_conf_for_sport_tier(sport, tier)` helper methods.
- `market_engine.py`: stars gate in all 3 paths (new-prop `is_qualified`, standing, line-change `is_qualified`) now calls `config.min_stars_for_sport(snap.sport)`.
- `market_engine.py`: conf gates in new-prop and standing paths now call `config.min_conf_for_sport_tier(sport, tier)`.

**#5 — Game-live hard gate (all 3 paths)**
- `market_engine.py`: added module-level `_is_game_live_or_past(snap, now)` function.
  Checks `snap.game_status` attribute (LIVE/IN_PROGRESS/FINAL/COMPLETED/CLOSED) first,
  then falls back to `game_time < now`. If `game_time` is None → not blocked.
- Applied in: new-prop path (after MLB UNDER block), standing path (after BQ gate), line-change path (replaced old `game_time < now` check with new helper).

**#7 — ET time format in Telegram alerts**
- `alerts_multiplatform.py`: added `_format_game_time_et(dt)` → uses `zoneinfo.ZoneInfo("America/New_York")`, outputs `"8:40 PM ET"` (no leading zero via `%-I`). Falls back to HH:MM UTC on error.
- Fixed `format_underdog_change_alert` and `format_underdog_new_prop_alert` game_str lines.
  Label updated from `<b>Game:</b>` to `🕐 <b>Game starts:</b>`.
- The internal `format_market_move_detected` function intentionally left as UTC (internal only, never reaches Telegram).

**#18 — Explicit scheduler max_instances**
- `main.py`: `run_repeating(underdog_job, ..., job_kwargs={"max_instances": 1, "misfire_grace_time": 60})`.

## Tests added
- `bot/tests/test_live_gate_et_format.py`: 34 new tests covering all 5 items.
  - `TestIsGameLiveOrPast`: 10 tests (future/past/None/status field/Alex Bregman regression).
  - `TestFormatGameTimeEt`: 6 tests (output format, ET label, no UTC leak, no leading zero).
  - `TestConfigHelperMethods`: 12 tests (min_stars/min_conf sport routing, case insensitivity).
  - `TestStrictSportBQConfig`: 5 tests (default=85, boundary values, configurability).
  - `TestSchedulerMaxInstances`: 1 test (AST parse of main.py confirms `max_instances=1`).

## Test count
- Before: 3095 | After: 3129 (+34 all passing)

**Why:**
S-tier threshold uniform across all sports (it's already the strictest tier); only A/B relax for non-strict sports.
The `_is_game_live_or_past` function is forward-compatible: if Underdog adds a `game_status` field later, it works immediately without code changes.
