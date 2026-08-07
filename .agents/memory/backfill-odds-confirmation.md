---
name: Backfill grading and OddsAPI confirmation
description: /backfill command + non-blocking OddsAPI confirmation layer for S/A picks
---

## /backfill command (P2)
- `cmd_backfill` in `bot/commands.py` — fetches stats via `PlayerStatsProvider` for all PENDING opps (cutoff_hours=4), upserts to `player_game_results`, then grades direction-aware HIT/MISS/PUSH.
- Registered in `main.py` as `/backfill`.
- No new DB method needed — reuses `get_pending_opportunities(cutoff_hours=4)`.
- Does NOT fetch stats for future games — only props where game_time < now-4h.

## OddsAPI confirmation layer (P6)
- `_UD_TO_ODDS_API_MARKET` dict in `market_engine.py` maps Underdog stat names → OddsAPI market keys (NBA and MLB only — those are the only sports in `_SPORT_PLAYER_PROP_MARKETS`).
- `_get_odds_api_confirmation(sport, player, stat_type, direction, line)` in `market_engine.py`:
  - Non-blocking (5s timeout, returns None on any failure).
  - Uses cached `fetch_player_prop_lines()` — no extra API quota if already fetched this cycle.
  - Fuzzy surname match: "Caminero" matches "Junior Caminero".
  - Returns `{num_books, avg_line, notes, confirmed}` or None.
- `init_odds_confirmation(engine)` sets module-level `_analysis_engine`; called from `main.py` after `init_market_engine()`.
- Wired at all 3 alert paths (new-prop, line-change, standing) — S/A tier only, non-removal.
- Rendered as `📡 Market Check: 3 books · avg 25.5 ✅` in both change and new-prop alert formats.
- `deliver_underdog()` in `alerts.py` accepts `market_confirmation: Optional[dict] = None`.
- Both `format_underdog_change_alert` and `format_underdog_new_prop_alert` in `alerts_multiplatform.py` accept `market_confirmation`.

**Why:** OddsAPI confirmation is a non-blocking signal only. It must never block picks — if it fails, alert proceeds without it. Only NBA/MLB are wired because those are the only sports with `_SPORT_PLAYER_PROP_MARKETS` configured. Adding a new sport requires both a `_SPORT_PLAYER_PROP_MARKETS` entry and a `_UD_TO_ODDS_API_MARKET` mapping entry.
