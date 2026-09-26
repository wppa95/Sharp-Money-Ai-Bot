# Riot VAL-MATCH-V1 (official API reference)

URL: https://developer.riotgames.com/api-details/val-match-v1  
Retrieved: 2026-09-26 (direct public fetch; the web-search/web-fetch service returned HTTP 402).

The official reference documents:

* `GET /val/match/v1/matches/{matchId}` (“Get match by id”), returning `MatchDto`.
* `GET /val/match/v1/matchlists/by-puuid/{puuid}` (“Get matchlist by puuid”), returning `MatchlistDto`; the documented list contains match IDs and queue values.
* `GET /val/match/v1/recent-matches/by-queue/{queue}` (“Get recent matches”), returning `RecentMatchesDto`.

The `MatchDto` schema exposes match identity (`matchInfo.matchId`, game name/version/start time, map and queue), teams and players. Player records include `puuid`, game name/tag, team, character, stats and competitive tier. Player stats include kills, deaths, assists, score, rounds played, headshots, bodyshots, legshots, economy, ability casts, damage, player-round stats and locations. Match-level fields include `isCompleted`, `gameLengthMillis`, and round results; these are suitable completion/result gates only when the response is actually obtained from the authorized API.

The recent-match implementation note says live-region recent IDs cover only matches completed in the last 10 minutes; the `esports` routing value covers the last 12 hours. This is not a long-term history endpoint. The public reference does not state that ordinary PUUID history includes professional tournament matches, nor does it provide a tournament schedule/archive.

The reference requires an API key for requests. It does not document a public production quota on this page; Riot's getting-started/terms pages distinguish development/personal/production keys and require product registration/approval for higher access. No undocumented endpoint or sample request was used.