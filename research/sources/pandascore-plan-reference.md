<!-- Source URL: https://developers.pandascore.co/docs/plan-reference.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2026-08-18T07:47:15.000Z
---

Fetch the complete documentation index at: https://developers.pandascore.co/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Plan reference

This page covers all game-specific endpoints for Counter-Strike, League of Legends, Dota 2, and Valorant with the plan required to access each one indicated.

## Plan reference

| Plan label used below | What it means                  |
| --------------------- | ------------------------------ |
| **All plans**         | Free / Fixtures plan and above |
| **Historical**        | Historical plan and above      |
| **Historical Pro**    | Historical Pro plan only       |
| **Basic Live**        | Live Basic plan and above      |
| **Pro Live**          | Live Pro plan only             |

***

## 🔫 Counter-Strike (CS2)

### Match & game data

| What you need                      | Endpoint                                                                                                            | Plan              |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------- |
| List upcoming CS matches           | [`GET /csgo/matches/upcoming`](https://developers.pandascore.co/reference/get_csgo_matches_upcoming-1)              | All plans         |
| List past CS matches               | [`GET /csgo/matches/past`](https://developers.pandascore.co/reference/get_csgo_matches_past-1)                      | All plans         |
| List running CS matches            | [`GET /csgo/matches/running`](https://developers.pandascore.co/reference/get_csgo_matches_running-1)                | All plans         |
| Get a specific CS match            | [`GET /csgo/matches/{id}`](https://developers.pandascore.co/reference/get_csgo_matches_matchidorslug-1)             | Historical        |
| List games within a CS match       | [`GET /csgo/matches/{id}/games`](https://developers.pandascore.co/reference/get_csgo_matches_matchidorslug_games-1) | Historical        |
| Get a specific CS game             | [`GET /csgo/games/{id}`](https://developers.pandascore.co/reference/get_csgo_games_csgogameid-1)                    | Historical        |
| CS post-game rounds                | [`GET /csgo/games/{id}/rounds`](https://developers.pandascore.co/reference/get_csgo_games_csgogameid_rounds-1)      | Basic Live        |
| CS post-game events (play-by-play) | [`GET /csgo/games/{id}/events`](https://developers.pandascore.co/reference/get_csgo_games_csgogameid_events-1)      | **Pro Live only** |

### Player & team stats

| What you need                                 | Endpoint                                                                                                                                                             | Plan       |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| Get stats for a CS player (overall)           | [`GET /csgo/players/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_players_playeridorslug_stats-1)                                                 | Historical |
| Get stats for a CS team (overall)             | [`GET /csgo/teams/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_teams_teamidorslug_stats-1)                                                       | Historical |
| Get stats for all CS players on a match       | [`GET /csgo/matches/{id}/players/stats`](https://developers.pandascore.co/reference/get_csgo_matches_matchidorslug_players_stats-1)                                  | Historical |
| Get stats for a specific CS player on a match | [`GET /csgo/matches/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_matches_matchidorslug_players_playeridorslug_stats-1)              | Historical |
| Get stats for a CS team on a match            | [`GET /csgo/matches/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_matches_matchidorslug_teams_teamidorslug_stats-1)                    | Historical |
| Get stats for a CS player on a tournament     | [`GET /csgo/tournaments/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_tournaments_tournamentidorslug_players_playeridorslug_stats-1) | Historical |
| Get stats for a CS team on a tournament       | [`GET /csgo/tournaments/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_tournaments_tournamentidorslug_teams_teamidorslug_stats-1)       | Historical |
| Get stats for a CS player on a series         | [`GET /csgo/series/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_series_serieidorslug_players_playeridorslug_stats-1)                | Historical |
| Get stats for a CS team on a series           | [`GET /csgo/series/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_csgo_series_serieidorslug_teams_teamidorslug_stats-1)                      | Historical |

### Context & reference data

| What you need            | Endpoint                                                                                                       | Plan      |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- | --------- |
| List CS maps             | [`GET /csgo/maps`](https://developers.pandascore.co/reference/get_csgo_maps-1)                                 | All plans |
| Get a specific CS map    | [`GET /csgo/maps/{id}`](https://developers.pandascore.co/reference/get_csgo_maps_csgomapid-1)                  | All plans |
| List CS weapons          | [`GET /csgo/weapons`](https://developers.pandascore.co/reference/get_csgo_weapons-1)                           | All plans |
| Get a specific CS weapon | [`GET /csgo/weapons/{id}`](https://developers.pandascore.co/reference/get_csgo_weapons_csgoweaponidorslug-1)   | All plans |
| List CS players          | [`GET /csgo/players`](https://developers.pandascore.co/reference/get_csgo_players-1)                           | All plans |
| List CS teams            | [`GET /csgo/teams`](https://developers.pandascore.co/reference/get_csgo_teams-1)                               | All plans |
| List CS leagues          | [`GET /csgo/leagues`](https://developers.pandascore.co/reference/get_csgo_leagues-1)                           | All plans |
| List CS tournaments      | [`GET /csgo/tournaments/upcoming`](https://developers.pandascore.co/reference/get_csgo_tournaments_upcoming-1) | All plans |
| List CS series           | [`GET /csgo/series`](https://developers.pandascore.co/reference/get_csgo_series-1)                             | All plans |

### CS live data?

📖 Live data reference: [CS in-game data sample](https://developers.pandascore.co/docs/data-sample-csgo) | [WebSockets overview](https://developers.pandascore.co/docs/websockets-overview)

**Frames** (Basic Live and Pro Live — every \~2 seconds via WebSocket):

* Game overview: map, round number, elapsed round time, bomb planted, paused state, finished/winner
* Team stats: score (match series), round score, side (CT/T)
* Player stats: kills, deaths, economy (in-game currency), alive status

**Pro Live-only fields within frames:**

* Player `primary_weapon`, `secondary_weapon`, `hp`

**Events** (Pro Live only — sent as they occur via WebSocket):

* `kill` — killer, killed, weapon used, round number, elapsed round time
* `round_start` — round number, team scores per side
* `round_end` — outcome (e.g. eliminated), winning team, round number

📖 Full field breakdown with examples: [CS in-game data sample](https://developers.pandascore.co/docs/data-sample-csgo)

***

## 🏆 League of Legends

### Match & game data

| What you need                       | Endpoint                                                                                                        | Plan       |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------- | ---------- |
| List upcoming LoL matches           | [`GET /lol/matches/upcoming`](https://developers.pandascore.co/reference/get_lol_matches_upcoming)              | All plans  |
| List past LoL matches               | [`GET /lol/matches/past`](https://developers.pandascore.co/reference/get_lol_matches_past)                      | All plans  |
| List running LoL matches            | [`GET /lol/matches/running`](https://developers.pandascore.co/reference/get_lol_matches_running)                | All plans  |
| Get a specific LoL match            | [`GET /lol/matches/{id}`](https://developers.pandascore.co/reference/get_lol_matches_matchidorslug)             | Historical |
| List games within a LoL match       | [`GET /lol/matches/{id}/games`](https://developers.pandascore.co/reference/get_lol_matches_matchidorslug_games) | Historical |
| Get a specific LoL game             | [`GET /lol/games/{id}`](https://developers.pandascore.co/reference/get_lol_games_lolgameid)                     | Historical |
| LoL post-game frames                | [`GET /lol/games/{id}/frames`](https://developers.pandascore.co/reference/get_lol_games_lolgameid_frames)       | Basic Live |
| LoL post-game events (play-by-play) | [`GET /lol/games/{id}/events`](https://developers.pandascore.co/reference/get_lol_games_lolgameid_events)       | Basic Live |

### Player & team stats

| What you need                                  | Endpoint                                                                                                                                                         | Plan       |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| Get stats for a LoL player (overall)           | [`GET /lol/players/{id}/stats`](https://developers.pandascore.co/reference/get_lol_players_playeridorslug_stats)                                                 | Historical |
| Get stats for a LoL team (overall)             | [`GET /lol/teams/{id}/stats`](https://developers.pandascore.co/reference/get_lol_teams_teamidorslug_stats)                                                       | Historical |
| Get stats for all LoL players on a match       | [`GET /lol/matches/{id}/players/stats`](https://developers.pandascore.co/reference/get_lol_matches_matchidorslug_players_stats)                                  | Historical |
| Get stats for a specific LoL player on a match | [`GET /lol/matches/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_lol_matches_matchidorslug_players_playeridorslug_stats)              | Historical |
| Get stats for a LoL team on a match            | [`GET /lol/matches/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_lol_matches_matchidorslug_teams_teamidorslug_stats)                    | Historical |
| Get stats for a LoL player on a tournament     | [`GET /lol/tournaments/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_lol_tournaments_tournamentidorslug_players_playeridorslug_stats) | Historical |
| Get stats for a LoL team on a tournament       | [`GET /lol/tournaments/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_lol_tournaments_tournamentidorslug_teams_teamidorslug_stats)       | Historical |
| Get stats for a LoL player on a series         | [`GET /lol/series/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_lol_series_serieidorslug_players_playeridorslug_stats)                | Historical |
| Get stats for a LoL team on a series           | [`GET /lol/series/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_lol_series_serieidorslug_teams_teamidorslug_stats)                      | Historical |

### Context & reference data

| What you need        | Endpoint                                                                                                   | Plan      |
| -------------------- | ---------------------------------------------------------------------------------------------------------- | --------- |
| List LoL players     | [`GET /lol/players`](https://developers.pandascore.co/reference/get_lol_players)                           | All plans |
| List LoL teams       | [`GET /lol/teams`](https://developers.pandascore.co/reference/get_lol_teams)                               | All plans |
| List LoL leagues     | [`GET /lol/leagues`](https://developers.pandascore.co/reference/get_lol_leagues)                           | All plans |
| List LoL tournaments | [`GET /lol/tournaments/upcoming`](https://developers.pandascore.co/reference/get_lol_tournaments_upcoming) | All plans |
| List LoL series      | [`GET /lol/series`](https://developers.pandascore.co/reference/get_lol_series)                             | All plans |

### LoL live data

📖 Live data reference: [LoL in-game data sample](https://developers.pandascore.co/docs/data-sample-league-of-legends)  | [WebSockets overview](https://developers.pandascore.co/docs/websockets-overview)

**Frames** (Basic Live and Pro Live — every \~2 seconds via WebSocket):

* Game overview: game timestamp, finished/winner
* Team stats (red/blue): towers, kills, drakes, Nashors, inhibitors, gold, score
* Player stats: champion, KDA (kills/deaths/assists), CS (creep score), level, summoner spells, items

**Pro Live-only fields within frames:**

* Team: `herald`, `voidgrubs`, `paused`
* Player: `items`, `hp`

**Events** (Pro Live only — sent as they occur via WebSocket):

* Kill feed covering: player kills, tower kills, inhibitor kills, drake kills, Baron Nashor kills, Rift Herald kills
* Each event includes killer, killed entity type, assists, and in-game timestamp

📖 Full field breakdown with examples: [LoL in-game data sample](https://developers.pandascore.co/docs/data-sample-league-of-legends)

***

## 🛡️ Dota 2

### Match & game data

| What you need                    | Endpoint                                                                                                            | Plan                    |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| List upcoming Dota 2 matches     | [`GET /dota2/matches/upcoming`](https://developers.pandascore.co/reference/get_dota2_matches_upcoming)              | All plans               |
| List past Dota 2 matches         | [`GET /dota2/matches/past`](https://developers.pandascore.co/reference/get_dota2_matches_past)                      | All plans               |
| List running Dota 2 matches      | [`GET /dota2/matches/running`](https://developers.pandascore.co/reference/get_dota2_matches_running)                | All plans               |
| Get a specific Dota 2 match      | [`GET /dota2/matches/{id}`](https://developers.pandascore.co/reference/get_dota2_matches_matchidorslug)             | Historical              |
| List games within a Dota 2 match | [`GET /dota2/matches/{id}/games`](https://developers.pandascore.co/reference/get_dota2_matches_matchidorslug_games) | Historical              |
| Get a specific Dota 2 game       | [`GET /dota2/games/{id}`](https://developers.pandascore.co/reference/get_dota2_games_dota2gameid)                   | Historical              |
| Dota 2 post-game frames          | [`GET /dota2/games/{id}/frames`](https://developers.pandascore.co/reference/get_dota2_games_dota2gameid_frames)     | **Historical Pro only** |

### Player & team stats

| What you need                                 | Endpoint                                                                                                                                                             | Plan       |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| Get stats for a Dota 2 player (overall)       | [`GET /dota2/players/{id}/stats`](https://developers.pandascore.co/reference/get_dota2_players_playeridorslug_stats)                                                 | Historical |
| Get stats for a Dota 2 team (overall)         | [`GET /dota2/teams/{id}/stats`](https://developers.pandascore.co/reference/get_dota2_teams_teamidorslug_stats)                                                       | Historical |
| Get stats for all Dota 2 players on a match   | [`GET /dota2/matches/{id}/players/stats`](https://developers.pandascore.co/reference/get_dota2_matches_matchidorslug_players_stats)                                  | Historical |
| Get stats for a Dota 2 player on a tournament | [`GET /dota2/tournaments/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_dota2_tournaments_tournamentidorslug_players_playeridorslug_stats) | Historical |
| Get stats for a Dota 2 team on a tournament   | [`GET /dota2/tournaments/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_dota2_tournaments_tournamentidorslug_teams_teamidorslug_stats)       | Historical |
| Get stats for a Dota 2 player on a series     | [`GET /dota2/series/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_dota2_series_serieidorslug_players_playeridorslug_stats)                | Historical |
| Get stats for a Dota 2 team on a series       | [`GET /dota2/series/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_dota2_series_serieidorslug_teams_teamidorslug_stats)                      | Historical |
| List finished games for a Dota 2 team         | [`GET /dota2/teams/{id}/games`](https://developers.pandascore.co/reference/get_dota2_teams_teamidorslug_games)                                                       | Historical |

### Context & reference data

| What you need           | Endpoint                                                                                                       | Plan      |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- | --------- |
| List Dota 2 heroes      | [`GET /dota2/heroes`](https://developers.pandascore.co/reference/get_dota2_heroes)                             | All plans |
| Get a specific hero     | [`GET /dota2/heroes/{id}`](https://developers.pandascore.co/reference/get_dota2_heroes_dota2heroidorslug)      | All plans |
| List Dota 2 items       | [`GET /dota2/items`](https://developers.pandascore.co/reference/get_dota2_items)                               | All plans |
| Get a specific item     | [`GET /dota2/items/{id}`](https://developers.pandascore.co/reference/get_dota2_items_dota2itemidorslug)        | All plans |
| List Dota 2 abilities   | [`GET /dota2/abilities`](https://developers.pandascore.co/reference/get_dota2_abilities)                       | All plans |
| List Dota 2 players     | [`GET /dota2/players`](https://developers.pandascore.co/reference/get_dota2_players)                           | All plans |
| List Dota 2 teams       | [`GET /dota2/teams`](https://developers.pandascore.co/reference/get_dota2_teams)                               | All plans |
| List Dota 2 leagues     | [`GET /dota2/leagues`](https://developers.pandascore.co/reference/get_dota2_leagues)                           | All plans |
| List Dota 2 tournaments | [`GET /dota2/tournaments/upcoming`](https://developers.pandascore.co/reference/get_dota2_tournaments_upcoming) | All plans |

***

## 🎯 Valorant

<Callout icon="ℹ️" theme="info">
  ### Valorant does not have a live/real-time data plan. Events and rounds data is available post-game, with the depth depending on your plan tier.
</Callout>

### Match & game data

| What you need                  | Endpoint                                                                                                                 | Plan                             |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------- |
| List upcoming Valorant matches | [`GET /valorant/matches/upcoming`](https://developers.pandascore.co/reference/get_valorant_matches_upcoming)             | All plans                        |
| List past Valorant matches     | [`GET /valorant/matches/past`](https://developers.pandascore.co/reference/get_valorant_matches_past)                     | All plans                        |
| List running Valorant matches  | [`GET /valorant/matches/running`](https://developers.pandascore.co/reference/get_valorant_matches_running)               | All plans                        |
| Get a specific Valorant game   | [`GET /valorant/games/{id}`](https://developers.pandascore.co/reference/get_valorant_games_valorantgameid)               | Valorant Historical              |
| Valorant post-game events      | [`GET /valorant/games/{id}/events`](https://developers.pandascore.co/reference/get_valorant_games_valorantgameid_events) | **Valorant Historical Pro only** |
| Valorant post-game rounds      | [`GET /valorant/games/{id}/rounds`](https://developers.pandascore.co/reference/get_valorant_games_valorantgameid_rounds) | **Valorant Historical Pro only** |

### Player & team stats

| What you need                                   | Endpoint                                                                                                                                                                   | Plan                |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| Get stats for a Valorant player (overall)       | [`GET /valorant/players/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_players_playeridorslug_stats)                                                 | Valorant Historical |
| Get stats for a Valorant team (overall)         | [`GET /valorant/teams/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_teams_teamidorslug_stats)                                                       | Valorant Historical |
| Get stats for all Valorant players on a match   | [`GET /valorant/matches/{id}/players/stats`](https://developers.pandascore.co/reference/get_valorant_matches_matchidorslug_players_stats)                                  | Valorant Historical |
| Get stats for a Valorant team on a match        | [`GET /valorant/matches/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_matches_matchidorslug_teams_teamidorslug_stats)                    | Valorant Historical |
| Get stats for a Valorant player on a tournament | [`GET /valorant/tournaments/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_tournaments_tournamentidorslug_players_playeridorslug_stats) | Valorant Historical |
| Get stats for a Valorant team on a tournament   | [`GET /valorant/tournaments/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_tournaments_tournamentidorslug_teams_teamidorslug_stats)       | Valorant Historical |
| Get stats for a Valorant player on a series     | [`GET /valorant/series/{id}/players/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_series_serieidorslug_players_playeridorslug_stats)                | Valorant Historical |
| Get stats for a Valorant team on a series       | [`GET /valorant/series/{id}/teams/{id}/stats`](https://developers.pandascore.co/reference/get_valorant_series_serieidorslug_teams_teamidorslug_stats)                      | Valorant Historical |

### Context & reference data

| What you need             | Endpoint                                                                                                             | Plan      |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------- | --------- |
| List Valorant players     | [`GET /valorant/players`](https://developers.pandascore.co/reference/get_valorant_players)                           | All plans |
| List Valorant teams       | [`GET /valorant/teams`](https://developers.pandascore.co/reference/get_valorant_teams)                               | All plans |
| List Valorant leagues     | [`GET /valorant/leagues`](https://developers.pandascore.co/reference/get_valorant_leagues)                           | All plans |
| List Valorant tournaments | [`GET /valorant/tournaments/upcoming`](https://developers.pandascore.co/reference/get_valorant_tournaments_upcoming) | All plans |

### What's in the Valorant Historical Pro data?

The **rounds** endpoint includes detailed per-round data:

* Player agent, assists, deaths, weapon and shield type, combat score
* Player economy at the start and end of each preparation phase
* Team score and side per round
* Round number, outcome, and winner

The **events** endpoint provides a play-by-play event feed for each game.

***

## Quick plan summary

|                                            | Free / Fixtures | Historical | Basic Live | Pro Live |
| ------------------------------------------ | --------------- | ---------- | ---------- | -------- |
| Schedules, results, rosters                | ✅               | ✅          | ✅          | ✅        |
| Post-game stats (players & teams)          | ❌               | ✅          | ✅          | ✅        |
| Games within a match                       | ❌               | ✅          | ✅          | ✅        |
| Live frames (CS & LoL)                     | ❌               | ❌          | ✅          | ✅        |
| Live frames — all fields (CS & LoL)        | ❌               | ❌          | ❌          | ✅        |
| Live frames (Dota 2)                       | ❌               | ❌          | ✅          | ✅        |
| Post-game rounds (CS)                      | ❌               | ❌          | ✅          | ✅        |
| Post-game events / play-by-play (CS & LoL) | ❌               | ❌          | ❌          | ✅        |
| Sandbox (CS & LoL replay)                  | ❌               | ❌          | ✅          | ✅        |

|                                      | Valorant Historical | Valorant Historical Pro |
| ------------------------------------ | ------------------- | ----------------------- |
| Post-game player & team stats        | ✅                   | ✅                       |
| Post-game events & rounds (detailed) | ❌                   | ✅                       |

***

## Need help?

Join our Slack community for direct support — we'll set up a private channel for you and your team.

👉 [Join the PandaScore Slack](https://join.slack.com/t/pandascore/shared_invite/zt-3ljcjj4mo-5q0ON2~qdef5umvzqq3mZw)