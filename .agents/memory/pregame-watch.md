---
name: Pregame market watch — active
description: How the continuous pregame watch system works, what activates alerts, and key design decisions.
---

## Rule
The pregame watch is a continuous all-day job (`_pregame_watch_job` in `main.py`), NOT a morning-only scan.
Runs every `PREGAME_SCAN_INTERVAL` seconds (default 300s = 5 min).

## Job flow (each cycle)
1. `morning_scan(db)` — discovers new Underdog props from the last 2h; records opening lines in `_watch_entries`.
2. `pregame_scan(db, bot, chat_ids)` — re-fetches current UD lines; bulk-fetches PP + DK/FD; builds `PlayerPropMarketComparison(min_confidence=60)` for each entry; fires alerts.
3. `clear_stale()` — removes entries for games that started >30 min ago.

## Alert conditions (in pregame_scan)
- **First detection**: `alert_key not in self._alerted_set` AND `comp is not None` (conf≥60).
- **Movement re-alert**: `entry.has_movement` AND current line differs from `_movement_alerted[alert_key]`.
- Alert key: `"{player}|{stat}|{game_id}"`.

## Alert format
`🟣 PREGAME PLAYER PROP OPPORTUNITY` with:
- Sport / Player / Market / Game in
- Available Lines (all 4 providers)
- Movement (Previous → Current with arrow)
- Best Available Line (OVER-friendly / UNDER-friendly split)
- Market Quality (N/4 providers) / Confidence / Reason

## _build_dk_fd_index
Module-level helper in `pregame_watch.py`. Parses OddsRecord `selection` field ("Player Name Over" / "Player Name Under") into `{(player_lower, sportsbook): line}`. Shared pattern with `commands.py::_build_dk_fd_index`.

**Why:** The pregame engine needs its own copy since it can't import from `commands.py` without circular imports.

## Config
- `PREGAME_SCAN_INTERVAL` = 300 (env override available)
- `PLAYER_PROP_SPORTS` = "MLB,NBA,WNBA,NFL" (expanded from NBA,MLB)

## How to apply
- Never add a separate "morning scan only" job — this engine runs all day.
- New sports Underdog carries appear automatically (no sport filter in morning_scan).
- DK/FD prop lines are gated by `PLAYER_PROP_SPORTS` in `_player_props_job` and season checker.
