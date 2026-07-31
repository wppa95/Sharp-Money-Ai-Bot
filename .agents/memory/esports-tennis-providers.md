---
name: Esports & Tennis stat providers
description: How CS/DOTA/Tennis game-result history is sourced; data availability and approximation decisions.
---

## DOTA 2 — OpenDota API
- Free, no key required. `https://api.opendota.com/api/`
- Player lookup: `/search?q={name}` → account_id, then `/players/{id}/recentMatches`
- Underdog player names often prefixed "None " (no first name) — stripped before search.
- Per-match fields: kills, deaths, assists, last_hits, gold_per_min (no hero/series metadata).
- Fantasy points approximated: `kills×4 + assists×2 + deaths×(−2) + last_hits×0.15 + gpm×0.05`
  - Produces ~40–130 per game; exact Underdog formula is proprietary.

## Multi-map stat scaling
- "Kills on Maps 1+2" = cumulative over 2 games. OpenDota gives per-game data.
- **Decision**: store `per_game_value × map_count` so the stored value is directly comparable to the cumulative line.
  - e.g. 8 kills in one game → stored 16 for "Maps 1+2" at line 10.5 → OVER ✓
  - Approximation: two games in a series may differ; over many games the directional signal is correct.

## CS2 — PandaScore API (optional)
- Requires `PANDASCORE_API_KEY` env var. Returns [] gracefully without it.
- Endpoint: `/csgo/matches?filter[opponent_id]={player_id}&sort=-begin_at&per_page=30`
- Per-game fields from PandaScore games[].teams[].players[].stats: kills, assists, deaths, headshots.

## Tennis — JeffSackmann CSV
- Free, no key. `github.com/JeffSackmann/tennis_atp` and `tennis_wta` repositories.
- URL pattern: `https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv`
- Data lag: 1–3 days after match completion.
- Score string parsing handles: straight sets, three-set, five-set, tiebreaks "(N)".
- Skips: retirements (RET), walkovers (W/O), unfinished matches.
- Computed stats from score: total games won (sum each set's games), sets won (count won sets).
- CSV columns: w_ace/l_ace, w_df/l_df, w_1stIn/l_1stIn, w_svpt/l_svpt.

## Config
- `UD_ALERT_SPORTS` default changed from `"MLB,WNBA"` to `"MLB,WNBA,DOTA,TENNIS,CS"`.
- CS props still self-suppress via decision engine (PASS) when no PandaScore key is set.

## Routing
- `PlayerStatsProvider.fetch_results()` delegates CS/DOTA to `EsportsStatsProvider` and TENNIS to `TennisStatsProvider` via lazy singletons `_get_esports_provider()` / `_get_tennis_provider()`.
- Singletons persist ID cache across calls within the same process lifetime.

**Why:** CS2 pros use esports handles not real names; the "None " prefix is from Underdog's null first_name. OpenDota search uses Steam personaname (handle), so fuzzy matching on the handle part works better than full name.
