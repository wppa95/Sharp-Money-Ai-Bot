# Research Notes: Completed-Game Data for Active Sports

**Status:** complete
**Depth:** Quick

## Plan

- **Question:** Which public or licensed feeds document completed per-player game results for KBO, NPB, LoL, Valorant, table tennis, and badminton?
- **Scope:** Verify sport coverage, player/game identity, result fields, completion status, access, published quotas, and history limits. Do not integrate or guess provider keys.
- **Audience:** Technical, focused on selecting a safe history source for the bot.
- **Deliverable:** A source comparison with a practical recommendation and unresolved access checks.

## Focus Areas

| # | Area | Status | Sources |
|---|---|---|---|
| 1 | KBO and NPB baseball | documented per-game stats; coverage-level check remains | [@sportradar-global-baseball-overview], [@sportradar-global-baseball-sport-event-summary], [@sportradar-global-baseball-player-summaries], [@sportradar-global-baseball-faq] |
| 2 | League of Legends and Valorant esports | LoL fields verified; Valorant endpoint verified, payload fields still need a sample | [@pandascore-plan-reference], [@pandascore-lol-game-data-sample], [@pandascore-matches-lifecycle], [@pandascore-authentication], [@pandascore-rate-and-connections-limits] |
| 3 | Table tennis and badminton | completed match results documented; detailed per-player performance stats not verified | [@sportradar-table-tennis-overview], [@sportradar-table-tennis-sport-event-summary], [@sportradar-table-tennis-competitor-summaries], [@sportradar-table-tennis-statistics-summary], [@sportradar-table-tennis-faq], [@sportradar-badminton-overview], [@sportradar-badminton-sport-event-summary], [@sportradar-badminton-competitor-summaries], [@sportradar-badminton-statistics-summary], [@sportradar-badminton-faq] |

## Coverage Checklist

- [x] KBO: Global Baseball documents coverage and game-level player statistics; verify the required KBO competition and fields in the licensed feed before use.
- [x] NPB: Global Baseball documents coverage and game-level player statistics; verify the required NPB competition and fields in the licensed feed before use.
- [x] LoL: historical match-player stats, player/game/match IDs, and game completion fields are documented.
- [x] Valorant: historical match-player stats endpoint and plan are documented; exact returned stat fields need a sample.
- [x] Table tennis: completed competitor match history, winner, and set scores are documented; detailed performance stats are not.
- [x] Badminton: completed competitor match history, winner, and set scores are documented; detailed performance stats are not.
- [x] Access and quotas: PandaScore publishes token authentication and plan limits; Sportradar requires an API key, but the reviewed pages do not publish a request quota.

## Findings Log

### KBO and NPB

- Global Baseball lists both leagues and directs customers to the Coverage Matrix for exact competitions and data offered [@sportradar-global-baseball-overview].
- Sport Event Summary documents game-level player and team statistics, including stable event/player identity and batter/pitcher measures such as hits, home runs, RBI, strikeouts, walks, and innings pitched [@sportradar-global-baseball-sport-event-summary].
- Player Summaries provides stats and results for a player's 10 most recent games; deeper history would require collecting event IDs and querying event summaries [@sportradar-global-baseball-player-summaries].
- Prefer `status=closed` for confirmed results. `ended` means the match is over but does not by itself mean the result is confirmed [@sportradar-global-baseball-faq].
- Access is authenticated and coverage-specific. A public per-hour quota was not found in the reviewed baseball docs.

### LoL and Valorant

- PandaScore documents historical match-player stats endpoints for both games; the match-stat endpoints require the Historical plan (or eligible real-time access) [@pandascore-plan-reference].
- LoL's documented player fields include kills, deaths, assists, and creep score; game, match, tournament, and player IDs are provided in the sample [@pandascore-lol-game-data-sample].
- Use match `status=finished` and `complete=true` before consuming post-game stats; `finished` alone can precede complete stats [@pandascore-matches-lifecycle].
- All requests require token authentication. The published REST limits distinguish 1,000 requests/hour for schedules/results/context from 10,000/hour for historical/post-match data [@pandascore-authentication], [@pandascore-rate-and-connections-limits].
- Valorant's endpoint is documented, but the exact payload fields and active competition coverage remain to be checked against a sample.

### Table tennis and badminton

- Sportradar documents competitor identity and up to 30 most recent completed matches in Competitor Summaries for both sports [@sportradar-table-tennis-competitor-summaries], [@sportradar-badminton-competitor-summaries].
- Event summaries expose competitor IDs, winner IDs, final match scores, per-set scores, and event status. The statistics summaries document player identity fields, but do not establish a separate set of detailed player-performance metrics [@sportradar-table-tennis-sport-event-summary], [@sportradar-table-tennis-statistics-summary], [@sportradar-badminton-sport-event-summary], [@sportradar-badminton-statistics-summary].
- Overview pages say match statistics are available “when available”; competition and data coverage must be confirmed in the vendor Coverage Matrix. Authentication is required [@sportradar-table-tennis-overview], [@sportradar-badminton-overview].
- Use `closed` as confirmed-result status; `ended` only means the match is over [@sportradar-table-tennis-faq], [@sportradar-badminton-faq].

## Conflicts & Open Questions

- The table-tennis and badminton overview pages mention statistics “when available,” but the reviewed event and statistics schemas establish match/set scores and player identity—not guaranteed detailed per-player stat fields. Treat these as result-only until a sample proves the needed markets.
- PandaScore documents Valorant match-player stats endpoints and historical access, but this pass did not verify a concrete Valorant payload schema.
- Sportradar pages do not publish a request quota or production-use terms. Exact competition coverage and permitted use require vendor confirmation.

## Gaps

- Before integration, obtain authorized sample responses for KBO and NPB, then verify competition IDs, player stats, and result status.
- Obtain a Valorant historical match-player stats sample and confirm desired fields and competition coverage.
- Confirm the exact table-tennis and badminton stats required by the bot against licensed sample payloads; the public docs establish match results and set scores only.
- Verify Sportradar quotas, retention, and production-use rights with the vendor.
- Research used public documentation fetched directly because the web-search service returned HTTP 402; no authenticated API samples were available.

## Public-Alternatives Follow-Up — 2026-09-26

**Status:** complete  
**Depth:** Quick

### Plan

- **Question:** Which public alternatives can replace the inaccessible licensed feeds for KBO, NPB, LoL, and Valorant?
- **Scope:** Check player/game identity, stat fields, completion evidence, coverage/history, access requirements, quotas, and reuse limits. Do not integrate undocumented APIs or assume keys.
- **Audience:** Technical, focused on safe completed-history inputs for the bot.
- **Deliverable:** A cited addendum ranking usable public sources and clearly marking gaps.

### Focus Areas

| # | Area | Status | Sources |
|---|---|---|---|
| 1 | KBO and NPB public datasets | complete; no bot-ready source verified | [@official-kbo-player-stats], [@official-kbo-schedule], [@official-npb-players], [@official-npb-games], [@dentearl-kbo-data], [@dentearl-npb-data], [@armstjc-npb-repository] |
| 2 | LoL professional match exports | complete; Oracle's Elixir is a lead, not cleared for bot use | [@oracles-elixir-downloads-official], [@oracles-elixir-riot-policy-link], [@riot-match-v5-official-portal], [@riot-developer-portal] |
| 3 | Valorant documented APIs and public feeds | complete; documented fields, but pro-history and reuse remain unconfirmed | [@riot-val-match-v1], [@riot-api-terms-and-getting-started], [@henrikdev-unofficial-valorant-api-readme] |

### Coverage Checklist

- [x] Identify public KBO/NPB sources and whether they include per-player game statistics.
- [x] Assess available LoL pro-match documentation for player fields, completion, history, and access; record schema gaps.
- [x] Verify whether Valorant public sources cover pro matches or only ordinary player match history.
- [x] Record access, quotas, freshness, and reuse/license limitations.
- [x] Recommend which sources are safe enough to pilot without unauthorized provider accounts.

### Findings Log

Source-key citations below map to `research/sources.json`.

### Conflicts & Open Questions

- The built-in web-search/page-fetch services returned HTTP 402. Findings were checked against direct public official pages and GitHub REST/raw content; no authenticated feed samples were available.

### Gaps

- No checked anonymous public source met the data-rights, completion, stable-identity, and coverage requirements for bot ingestion. No source was integrated.

### Valorant follow-up findings — 2026-09-26

- Four independent web-search attempts returned errors consistent with the reported HTTP 402; direct Riot pages and GitHub REST/raw content were used instead.
- Official Riot VAL-MATCH-V1 documents match-by-ID, matchlist-by-PUUID, and recent-by-queue routes. Its match schema exposes `isCompleted` and per-player combat/economy/ability/damage fields [@riot-val-match-v1].
- Riot’s recent route is only a short discovery window (10 minutes for live regions; 12 hours for `esports` routing). This does not prove durable professional-match history, and the public reference does not establish that ordinary PUUID history contains tournament matches [@riot-val-match-v1].
- Riot access requires an API key; terms/getting-started/policy pages distinguish key types and product registration/approval. Exact quota and production approval are account-specific [@riot-developer-portal], [@riot-api-terms-and-getting-started].
- HenrikDev is explicitly unofficial. Its README states consent and key/rate limits but warns about large analytics/production use and does not establish tournament coverage or bot reuse rights. Treat it as ordinary player/account history unless the provider says otherwise [@henrikdev-unofficial-valorant-api-readme].
- Public GitHub visibility or a public page is not a license. No anonymous, documented source met all professional-coverage, final-result, stable-field, quota, and reuse requirements.

### Public-alternatives follow-up findings

- Four web-search attempts were made on 2026-09-26; all failed with the known HTTP 402 behavior. Direct public HTML and GitHub REST/raw fetches were used instead.
- KBO's public pages expose aggregate player records and schedule/results, but the captured pages provide no documented per-game player API, identity contract, quota, cadence/history policy, or bot reuse permission. The footer says all rights reserved [@official-kbo-player-stats], [@official-kbo-schedule].
- NPB's public pages expose annual player records and a games area, but not a documented per-player-per-game feed. The player page expressly prohibits secondary use and unauthorized reproduction [@official-npb-players], [@official-npb-games].
- `dentearl/kbo-data` and `dentearl/npb-data` are team-level score/result snapshots, not player-stat feeds; public visibility does not establish reuse rights [@dentearl-kbo-data], [@dentearl-npb-data].
- `armstjc/Nippon-Baseball-Data-Repository` lists a Player Game Stats release and identifies SPAIA as a data source. Its README does not establish rights to SPAIA-sourced data, so obtain permission before use [@armstjc-npb-repository].
- Oracle's Elixir provides professional LoL match-data CSV downloads and says they are refreshed daily, but its page does not define a completion protocol or bot license; Riot's linked policy applies [@oracles-elixir-downloads-official], [@oracles-elixir-riot-policy-link].
- Riot Match-V5 is ordinary player match history rather than a verified professional archive. Its public portal documents key and usage constraints; do not use it for tournament history without confirmation [@riot-match-v5-official-portal], [@riot-developer-portal].
- Result: no checked public source satisfies all required criteria for a bot. Public pages/repositories remain cross-check or research leads only.