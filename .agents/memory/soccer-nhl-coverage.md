---
name: Soccer + NHL stat coverage
description: How soccer and NHL player stats are fetched, which APIs are accessible, and soccer transfer-handling behavior.
---

# Soccer + NHL stat coverage

## Network reality in this Replit environment

`site.api.espn.com` returns HTTP 403 from this container. This blocks all ESPN-based
historical stat providers (NBA, WNBA, NFL-ESPN, Soccer, old NHL). The bot still works
because MLB (statsapi.mlb.com), OpenDota, Sleeper, JeffSackmann CSVs, Underdog, and
the NHL official API are all reachable.

**Why:** ESPN actively blocks cloud-provider IPs. Other bot deployments on non-Replit
infra where ESPN is unblocked will work fine with the existing ESPN code paths.

## NHL — Real provider (api-web.nhle.com)

**Provider:** `bot/providers/nhl_stats.py` — `NHLStatsProvider`

**How it works:**
1. Bulk-loads all active skater + goalie bios from `api.nhle.com/stats/rest/en/skater/bios`
   and `…/goalie/bios` on first use → builds name→player_id registry (≈795 skaters + 63 goalies)
2. Fetches per-game logs from `api-web.nhle.com/v1/player/{id}/game-log/{season}/2`
   for the last two NHL regular seasons
3. Skater keys available: `goals, assists, points, shots, powerPlayGoals, powerPlayPoints, toi`
4. Goalie keys available: `shotsAgainst, goalsAgainst, savePctg, shutouts`
5. `saves` = `shotsAgainst − goalsAgainst` (computed; no direct `saves` field in game log)
6. `toi` stored as fractional minutes (parsed from "MM:SS" string)

**Wired in:** `player_stats.py` routes `sport_upper == "NHL"` to `_get_nhl_provider()`
**before** the ESPN catch-all, so ESPN is never called for NHL.

**Season ID format:** `YYYYYYYY` (e.g. `20252026`). Computed dynamically from UTC date.
Month ≥ 10 → `year → year+1`; Month < 10 → `year-1 → year`.

## Soccer — SoccerStatsProvider (football-data.org v4)

**Provider:** `bot/providers/soccer_stats.py` — `SoccerStatsProvider`

**NOT in `UD_ALERT_SPORTS` default** because the free API tier has no lineup/appearance
data, so DNPs (games the player missed) cannot be distinguished from zero-stat games.
Enable manually: `UD_ALERT_SPORTS=...,SOCCER` + `FOOTBALL_DATA_API_KEY`.

**Transfer handling:** `_find_player_info` now searches ALL supported competitions
(PL, PD, BL1, SA, FL1), not just the first one where the player is found. A player who
transferred mid-season (e.g. EPL → Bundesliga) appears in both leagues; `fetch_results`
merges results from all leagues, de-duplicating by game_date, preserving full history.

**Cache behaviour:** Positive results (list of `(competition, team)` tuples) are cached
for the process lifetime. Negative results are NOT cached so the next call re-searches
as new event data arrives during the season.

**Stats available:** goals, assists, goals + assists (g+a), yellow cards, red cards.
Source tag: `"football_data_org"`.

## Stat normalization (underdog_provider.py)

- `"shots"` → `"shots on goal"` (hockey alias — preserved; Underdog soccer uses `"shots on target"`)
- `"saves"` / `"goalkeeper saves"` → `"goalkeeper saves"` (soccer GK)
- Added: `assists, yellow cards, red cards, blocked shots, power play points, time on ice`
