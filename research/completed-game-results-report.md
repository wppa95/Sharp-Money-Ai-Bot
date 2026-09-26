# Completed Per-Player Game-Result Sources

**Research Date:** September 26, 2026  
**Depth:** Quick  
**Sources Consulted:** 33

## Executive Summary

The best-documented options cover four sports: Sportradar’s Global Baseball API documents KBO and NPB coverage plus per-game player statistics, with a 10-game player-history endpoint and a confirmed-result status. PandaScore documents historical player-stat endpoints for League of Legends and Valorant; LoL’s fields and completion markers are documented directly, while Valorant still needs a sample payload. [[1]](https://developer.sportradar.com/baseball/reference/global-baseball-overview) [[2]](https://developer.sportradar.com/baseball/reference/global-baseball-sport-event-summary) [[3]](https://developer.sportradar.com/baseball/reference/global-baseball-player-summaries) [[4]](https://developer.sportradar.com/baseball/reference/global-baseball-faq) [[5]](https://developers.pandascore.co/docs/plan-reference) [[6]](https://developers.pandascore.co/docs/data-sample-league-of-legends) [[7]](https://developers.pandascore.co/docs/matches-lifecycle) [[8]](https://developers.pandascore.co/docs/authentication) [[9]](https://developers.pandascore.co/docs/rate-and-connections-limits)

These licensed-provider documents describe capabilities, not this bot’s authorization. Do not use Sportradar or PandaScore without an applicable account, competition coverage, and explicit production-use rights.

The public-alternatives follow-up found no bot-ready open baseball feed: the official KBO/NPB pages are public, but the checked community datasets either contain only team results or have unresolved source-data rights. For professional LoL, Oracle’s Elixir offers free daily CSV downloads, but the publisher points users to Riot policy and does not define a completion protocol. Riot’s official VAL-MATCH-V1 documents per-player statistics and `isCompleted`, but its short recent-match window and uncertain tournament-history access do not make it a verified public esports archive. [[20]](https://developer.riotgames.com/apis#match-v5) [[21]](https://developer.riotgames.com/docs/portal) [[22]](https://oracleselixir.com/tools/downloads) [[23]](https://developer.riotgames.com/policies/general) [[24]](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) [[25]](https://www.koreabaseball.com/Schedule/Schedule.aspx) [[26]](https://npb.jp/bis/players/) [[27]](https://npb.jp/games/) [[28]](https://github.com/dentearl/kbo-data) [[29]](https://github.com/dentearl/npb-data) [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository) [[31]](https://developer.riotgames.com/api-details/val-match-v1) [[32]](https://developer.riotgames.com/terms) [[33]](https://github.com/Henrik-3/unofficial-valorant-api)

Sportradar also documents player-linked match histories for table tennis and badminton: up to 30 most recent completed matches, with competitor identity, match winner, match score, and set scores. The reviewed schemas do not establish detailed per-player performance statistics beyond those match results. These feeds are suitable for winner/set-score history, but not yet verified for other player-stat markets. [[10]](https://developer.sportradar.com/table-tennis/reference/overview) [[11]](https://developer.sportradar.com/table-tennis/reference/sport-event-summary) [[12]](https://developer.sportradar.com/table-tennis/reference/competitor-summaries) [[13]](https://developer.sportradar.com/table-tennis/reference/statistics-summary) [[14]](https://developer.sportradar.com/table-tennis/reference/faq) [[15]](https://developer.sportradar.com/badminton/reference/badminton-overview) [[16]](https://developer.sportradar.com/badminton/reference/badminton-sport-event-summary) [[17]](https://developer.sportradar.com/badminton/reference/badminton-competitor-summaries) [[18]](https://developer.sportradar.com/badminton/reference/badminton-statistics-summary) [[19]](https://developer.sportradar.com/badminton/reference/badminton-faq)

These are first-party provider documents, which are authoritative for documented endpoints but do not independently prove live data quality, completeness, or production-use rights. No authenticated sample API calls were available during this research.

## Coverage Comparison

| Sport | Documented result source | Per-game fields and identity | Completion signal | Access and limits |
|---|---|---|---|---|
| KBO | Sportradar Global Baseball | Game-level player/team stats; player and event IDs. Player Summaries returns the last 10 games. Exact stats vary by coverage. [[1]](https://developer.sportradar.com/baseball/reference/global-baseball-overview) [[2]](https://developer.sportradar.com/baseball/reference/global-baseball-sport-event-summary) [[3]](https://developer.sportradar.com/baseball/reference/global-baseball-player-summaries) | Status and winner ID are documented. `closed` means results confirmed; `ended` means only that the game is over. [[4]](https://developer.sportradar.com/baseball/reference/global-baseball-faq) | API key and appropriate package/competition coverage required. No public per-hour quota found in reviewed docs. |
| NPB | Sportradar Global Baseball | Same documented game-level baseball statistics and player history route; NPB is explicitly listed in coverage. [[1]](https://developer.sportradar.com/baseball/reference/global-baseball-overview) [[2]](https://developer.sportradar.com/baseball/reference/global-baseball-sport-event-summary) | Prefer `closed` for confirmed results. [[4]](https://developer.sportradar.com/baseball/reference/global-baseball-faq) | Same access and quota caveats as KBO. |
| LoL | PandaScore Historical match-player stats | Historical endpoint returns per-match player stats. Documented fields include kills, deaths, assists, and creep score; player/game/match/tournament IDs are present in sample data. [[5]](https://developers.pandascore.co/docs/plan-reference) [[6]](https://developers.pandascore.co/docs/data-sample-league-of-legends) | Require match `status=finished` and `complete=true` before using post-game stats. A game frame also exposes `finished` and `winner_id`. [[6]](https://developers.pandascore.co/docs/data-sample-league-of-legends) [[7]](https://developers.pandascore.co/docs/matches-lifecycle) | Token authentication. Historical/post-match plan limit: 10,000 requests/hour; schedules/results/context: 1,000/hour. [[8]](https://developers.pandascore.co/docs/authentication) [[9]](https://developers.pandascore.co/docs/rate-and-connections-limits) |
| Valorant | PandaScore Historical match-player stats | A per-match player-stats route is documented under the Valorant Historical plan. The exact response fields were not verified in this pass. [[5]](https://developers.pandascore.co/docs/plan-reference) | Apply the same `finished` + `complete=true` match gate. [[7]](https://developers.pandascore.co/docs/matches-lifecycle) | Token authentication; historical/post-match limit is 10,000 requests/hour. Exact competition coverage and fields need a sample. [[8]](https://developers.pandascore.co/docs/authentication) [[9]](https://developers.pandascore.co/docs/rate-and-connections-limits) |
| Table tennis | Sportradar Competitor Summaries and Sport Event Summary | Up to 30 recent completed matches per competitor; competitor/event IDs, winner, match score, and per-set scores. Detailed individual performance metrics were not established. [[11]](https://developer.sportradar.com/table-tennis/reference/sport-event-summary) [[12]](https://developer.sportradar.com/table-tennis/reference/competitor-summaries) [[13]](https://developer.sportradar.com/table-tennis/reference/statistics-summary) | `closed` means confirmed; `ended` is not equivalent to confirmed. [[14]](https://developer.sportradar.com/table-tennis/reference/faq) | API authentication required; exact competition/data coverage is in the Coverage Matrix. Public request quota not found. [[10]](https://developer.sportradar.com/table-tennis/reference/overview) |
| Badminton | Sportradar Competitor Summaries and Sport Event Summary | Up to 30 recent completed matches per competitor; competitor/event IDs, winner, match score, and per-set scores. Detailed individual performance metrics were not established. [[16]](https://developer.sportradar.com/badminton/reference/badminton-sport-event-summary) [[17]](https://developer.sportradar.com/badminton/reference/badminton-competitor-summaries) [[18]](https://developer.sportradar.com/badminton/reference/badminton-statistics-summary) | `closed` means confirmed; `ended` is not equivalent to confirmed. [[19]](https://developer.sportradar.com/badminton/reference/badminton-faq) | API authentication required; exact competition/data coverage is in the Coverage Matrix. Public request quota not found. [[15]](https://developer.sportradar.com/badminton/reference/badminton-overview) |

## Findings

### KBO and NPB

Sportradar’s Global Baseball documentation explicitly lists NPB and KBO and points customers to a Coverage Matrix for the exact competitions and data products. Its Sport Event Summary is described as returning game-level player and team statistics. Documented player measures include at-bats, hits, home runs, RBI, strikeouts, walks, runs, stolen bases, and pitching measures such as innings pitched and earned runs. Event and player identifiers are part of the response model. [[1]](https://developer.sportradar.com/baseball/reference/global-baseball-overview) [[2]](https://developer.sportradar.com/baseball/reference/global-baseball-sport-event-summary)

The Player Summaries endpoint returns results and statistics for a player’s 10 most recent games. That is useful for recent history, but it does not alone cover longer windows. A longer history would require enumerating covered events and requesting their summaries; the reviewed public pages do not establish retention depth or the request budget for that workflow. Use the event status field conservatively: `ended` means the match is over, while `closed` means the result has been confirmed. [[3]](https://developer.sportradar.com/baseball/reference/global-baseball-player-summaries) [[4]](https://developer.sportradar.com/baseball/reference/global-baseball-faq)

**Assessment:** strongest documented candidate for KBO/NPB. Before building against it, confirm each required league/season and stat in the Coverage Matrix and in an authorized sample response. Do not invent league IDs or assume every listed competition has the same stat coverage.

### League of Legends and Valorant

PandaScore’s plan reference lists historical match-level player-stat endpoints for LoL and Valorant. The LoL sample exposes player IDs and per-game stats such as kills, deaths, assists, and creep score. Match and game IDs distinguish a map/game from the larger series. The docs distinguish a finished match from a complete post-game record: wait for `status=finished` and `complete=true` before using the full statistics. [[5]](https://developers.pandascore.co/docs/plan-reference) [[6]](https://developers.pandascore.co/docs/data-sample-league-of-legends) [[7]](https://developers.pandascore.co/docs/matches-lifecycle)

PandaScore requires token authentication. Its published limits are 1,000 requests/hour for schedules, results, and context, and 10,000/hour for historical/post-match data. The historical stats endpoints require the Historical plan or eligible real-time access. [[5]](https://developers.pandascore.co/docs/plan-reference) [[8]](https://developers.pandascore.co/docs/authentication) [[9]](https://developers.pandascore.co/docs/rate-and-connections-limits)

**Assessment:** LoL is a strong documented candidate. Valorant has the required match-player endpoint and plan documented, but no concrete stat payload was verified here; test an authorized sample before mapping any Valorant market to it.

### Table tennis and badminton

Sportradar documents a Competitor Summaries feed that returns the 30 most recent completed matches for a specified competitor in both sports. Event summaries include event and competitor identities, winner IDs, final match scores, status, and per-set scores. This provides a useful result history for match-winner and set-score outcomes. [[11]](https://developer.sportradar.com/table-tennis/reference/sport-event-summary) [[12]](https://developer.sportradar.com/table-tennis/reference/competitor-summaries) [[16]](https://developer.sportradar.com/badminton/reference/badminton-sport-event-summary) [[17]](https://developer.sportradar.com/badminton/reference/badminton-competitor-summaries)

The overview pages say statistics are available “when available,” but the reviewed statistics summaries and match schemas do not document a dependable set of additional per-player performance fields. Both feeds require authentication, and exact competition/data coverage is delegated to the Coverage Matrix. The public pages reviewed do not state request quotas. [[10]](https://developer.sportradar.com/table-tennis/reference/overview) [[13]](https://developer.sportradar.com/table-tennis/reference/statistics-summary) [[15]](https://developer.sportradar.com/badminton/reference/badminton-overview) [[18]](https://developer.sportradar.com/badminton/reference/badminton-statistics-summary)

**Assessment:** verified for completed match outcomes and set scores, not verified for richer prop statistics. For both sports, use `closed` as the confirmed-result state; treat `ended` as provisional until confirmation. [[14]](https://developer.sportradar.com/table-tennis/reference/faq) [[19]](https://developer.sportradar.com/badminton/reference/badminton-faq)

## Limitations

This research reviewed public provider documentation, not authenticated feed responses. Coverage can vary by competition and package, and first-party docs do not prove that every event or stat is populated consistently. Sportradar’s reviewed pages did not publish request quotas or production-use terms. PandaScore’s Valorant endpoint is documented, but its response fields were not sampled. The web-search service returned HTTP 402, so source discovery used direct public documentation pages; no source claim relies on search snippets.

## Recommendations

1. **For KBO and NPB, wait for an authorized source.** None of the checked public alternatives meets the bot’s player-stat, completion, history, and reuse requirements.
2. **Treat Oracle’s Elixir as a research lead for LoL, not an approved feed.** Before any use, obtain Riot/publisher permission and verify a current CSV schema, coverage, update/correction behavior, and completion rule. Match-V5 is not a substitute for pro results.
3. **Do not use an anonymous Valorant source for pro history.** Confirm Riot product approval, tournament coverage, quotas, and a historical player-stat sample first.
4. **Use Sportradar table-tennis/badminton only for result-based history after authorization**, unless a sample proves the required richer stats. Do not infer unreturned performance fields.
5. **Before any production use, verify each source’s license, competition coverage, history depth, finalization/correction semantics, and access limits.** Public documentation alone does not settle those terms.

## Sources

1. [Global Baseball v2 Overview](https://developer.sportradar.com/baseball/reference/global-baseball-overview) — Updated 2026-08-20, Tier 3. Evidence: `research/sources/sportradar-global-baseball-overview.md`
2. [Global Baseball Sport Event Summary](https://developer.sportradar.com/baseball/reference/global-baseball-sport-event-summary) — Updated 2026-05-29, Tier 3. Evidence: `research/sources/sportradar-global-baseball-sport-event-summary.md`
3. [Global Baseball Player Summaries](https://developer.sportradar.com/baseball/reference/global-baseball-player-summaries) — Updated 2026-05-29, Tier 3. Evidence: `research/sources/sportradar-global-baseball-player-summaries.md`
4. [Global Baseball FAQs](https://developer.sportradar.com/baseball/reference/global-baseball-faq) — Updated 2026-07-15, Tier 3. Evidence: `research/sources/sportradar-global-baseball-faq.md`
5. [PandaScore Plan Reference](https://developers.pandascore.co/docs/plan-reference) — Updated 2026-08-18, Tier 3. Evidence: `research/sources/pandascore-plan-reference.md`
6. [League of Legends In-Game Data Sample](https://developers.pandascore.co/docs/data-sample-league-of-legends) — Updated 2025-09-16, Tier 3. Evidence: `research/sources/pandascore-lol-game-data-sample.md`
7. [PandaScore Matches Lifecycle](https://developers.pandascore.co/docs/matches-lifecycle) — Updated 2025-09-16, Tier 3. Evidence: `research/sources/pandascore-matches-lifecycle.md`
8. [PandaScore Authentication](https://developers.pandascore.co/docs/authentication) — Updated 2025-09-16, Tier 3. Evidence: `research/sources/pandascore-authentication.md`
9. [PandaScore Rate and Connection Limits](https://developers.pandascore.co/docs/rate-and-connections-limits) — Updated 2025-09-16, Tier 3. Evidence: `research/sources/pandascore-rate-and-connections-limits.md`
10. [Table Tennis API Overview](https://developer.sportradar.com/table-tennis/reference/overview) — Updated 2026-08-20, Tier 3. Evidence: `research/sources/sportradar-table-tennis-overview.md`
11. [Table Tennis Sport Event Summary](https://developer.sportradar.com/table-tennis/reference/sport-event-summary) — Updated 2025-09-23, Tier 3. Evidence: `research/sources/sportradar-table-tennis-sport-event-summary.md`
12. [Table Tennis Competitor Summaries](https://developer.sportradar.com/table-tennis/reference/competitor-summaries) — Updated 2025-09-23, Tier 3. Evidence: `research/sources/sportradar-table-tennis-competitor-summaries.md`
13. [Table Tennis Statistics Summary](https://developer.sportradar.com/table-tennis/reference/statistics-summary) — Updated 2026-06-30, Tier 3. Evidence: `research/sources/sportradar-table-tennis-statistics-summary.md`
14. [Table Tennis FAQs](https://developer.sportradar.com/table-tennis/reference/faq) — Updated 2026-07-16, Tier 3. Evidence: `research/sources/sportradar-table-tennis-faq.md`
15. [Badminton API Overview](https://developer.sportradar.com/badminton/reference/badminton-overview) — Updated 2026-08-20, Tier 3. Evidence: `research/sources/sportradar-badminton-overview.md`
16. [Badminton Sport Event Summary](https://developer.sportradar.com/badminton/reference/badminton-sport-event-summary) — Updated 2026-05-27, Tier 3. Evidence: `research/sources/sportradar-badminton-sport-event-summary.md`
17. [Badminton Competitor Summaries](https://developer.sportradar.com/badminton/reference/badminton-competitor-summaries) — Updated 2026-05-27, Tier 3. Evidence: `research/sources/sportradar-badminton-competitor-summaries.md`
18. [Badminton Statistics Summary](https://developer.sportradar.com/badminton/reference/badminton-statistics-summary) — Updated 2026-06-30, Tier 3. Evidence: `research/sources/sportradar-badminton-statistics-summary.md`
19. [Badminton FAQs](https://developer.sportradar.com/badminton/reference/badminton-faq) — Updated 2026-07-15, Tier 3. Evidence: `research/sources/sportradar-badminton-faq.md`

20. [Riot Match-V5 API Reference](https://developer.riotgames.com/apis#match-v5) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/riot-match-v5-official-portal.md`.

21. [Riot Developer Portal: API key types and rate limits](https://developer.riotgames.com/docs/portal) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/riot-developer-portal.md`.
22. [Oracle's Elixir Match Data Downloads](https://oracleselixir.com/tools/downloads) — fetched 2026-09-26, Tier 3. Evidence: `research/sources/oracles-elixir-downloads-official.md`.
23. [Riot General Policy Linked by Oracle's Elixir](https://developer.riotgames.com/policies/general) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/oracles-elixir-riot-policy-link.md`.
24. [KBO Player Records](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/official-kbo-player-stats.html`.
25. [KBO Schedule and Results](https://www.koreabaseball.com/Schedule/Schedule.aspx) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/official-kbo-schedule.html`.
26. [NPB Individual Annual Records](https://npb.jp/bis/players/) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/official-npb-players.html`.
27. [NPB Games](https://npb.jp/games/) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/official-npb-games.html`.
28. [dentearl/kbo-data](https://github.com/dentearl/kbo-data) — fetched 2026-09-26, Tier 2. Evidence: `research/sources/dentearl-kbo-data-README.md`.
29. [dentearl/npb-data](https://github.com/dentearl/npb-data) — fetched 2026-09-26, Tier 2. Evidence: `research/sources/dentearl-npb-data-README.md`.
30. [Nippon Baseball Data Repository](https://github.com/armstjc/Nippon-Baseball-Data-Repository) — fetched 2026-09-26, Tier 2. Evidence: `research/sources/armstjc-Nippon-Baseball-Data-Repository-README.md`.
31. [Riot VAL-MATCH-V1 API reference](https://developer.riotgames.com/api-details/val-match-v1) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/riot-val-match-v1.md`.
32. [Riot API Terms and Access Documentation](https://developer.riotgames.com/terms) — fetched 2026-09-26, Tier 1. Evidence: `research/sources/riot-api-terms-and-getting-started.md`.
33. [Henrik-3 unofficial Valorant API README](https://github.com/Henrik-3/unofficial-valorant-api) — fetched 2026-09-26, Tier 2. Evidence: `research/sources/henrikdev-unofficial-valorant-api-readme.md`.

## League of Legends public-alternatives addendum

**Research date:** 2026-09-26. The built-in search/fetch service returned HTTP 402, so this pass used direct public fetches and the official Riot portal; no undocumented endpoint was used. Four independent discovery searches were attempted, but all returned 402 and therefore supplied no evidence.

### Key facts

| Candidate | What it is | Per-player/game identity and fields | Completion/history | Access, limits, and reuse |
|---|---|---|---|---|
| **Riot Match-V5** | Official Riot API for League match history | PUUID-based history lookup returns Riot `matchId`s; a match response contains participant identities and per-player game statistics/results. It is ordinary player match history, not a professional-esports archive. | A completed match response is the relevant post-game record, but Riot's public portal does not promise a retention period or a separate “complete” workflow. | API key required. Development key expires after 24 hours. Personal key limits are 20 requests/second and 100 requests/2 minutes per region; personal keys may not serve a public-consumption application. Production use needs a product/application approval. Rate and policy limits remain applicable. [[20]](https://developer.riotgames.com/apis#match-v5) [[21]](https://developer.riotgames.com/docs/portal) |
| **Oracle's Elixir downloadable match data** | Publisher-curated professional LoL esports CSV downloads | The current files and definitions were not inspected in this pass; per-player row granularity, IDs, and the relevant stat fields remain unverified. | The publisher says downloads are updated once per day, but does not document a final/complete marker, correction policy, history retention, or SLA. | Public Google Drive downloads; no API key or request quota is stated. The page says Riot owns game statistics and use must follow Riot terms/policies. Public access/free charge is not a bot or redistribution license. [[22]](https://oracleselixir.com/tools/downloads) [[23]](https://developer.riotgames.com/policies/general) |

### Notable claims requiring cross-reference

* Treat Riot Match-V5 as a source for ordinary player history, not as a substitute for professional match results. Riot's PUUID history model and Oracle's explicit pro-league list answer different questions. [[20]](https://developer.riotgames.com/apis#match-v5) [[22]](https://oracleselixir.com/tools/downloads)
* Oracle's page says the files are “updated once per day,” but this is a publisher freshness statement, not a guarantee that every competition, row, or stat is present. [[22]](https://oracleselixir.com/tools/downloads)
* “Free of charge” and a public Google Drive link do not settle permission to operate a bot, redistribute rows, or commercialize derived results. Cross-check the current Riot policy and obtain publisher permission for the actual product. [[22]](https://oracleselixir.com/tools/downloads) [[23]](https://developer.riotgames.com/policies/general)
* The publisher page does not specify a complete-state field or completion protocol. Do not treat a listed row as final without checking the current file format and correction practices. [[22]](https://oracleselixir.com/tools/downloads)
* Exact Oracle column names, ID semantics, historical start date, and missing-game policy require inspection of the current file and definitions. They are intentionally not asserted from a stale or guessed CSV URL. [[22]](https://oracleselixir.com/tools/downloads)

### Source quality assessment

* **Tier 1 / first-party, current fetch:** Riot Developer Portal and Riot policy pages (`research/sources/riot-match-v5-official-portal.md`, `research/sources/oracles-elixir-riot-policy-link.md`). Strong for authentication, policy, and rate-limit claims. The Match-V5 details are dynamically rendered, so the saved evidence records that limitation. [[20]](https://developer.riotgames.com/apis#match-v5) [[21]](https://developer.riotgames.com/docs/portal) [[23]](https://developer.riotgames.com/policies/general)
* **Tier 3 / data publisher, current fetch:** Oracle's Elixir downloads page (`research/sources/oracles-elixir-downloads-official.md`). Strong for what the publisher offers, listed pro leagues, daily update cadence, intended audience, and policy caveat. Weak for proving a license, complete historical coverage, exact schema, or completion protocol. [[22]](https://oracleselixir.com/tools/downloads)
* **No independent license or data-quality audit was found in this pass.** A public page or GitHub repository would not have been treated as permission by itself.

### Gaps and recommendation

No candidate is bot-ready without authorization. Match-V5 requires an appropriate Riot product/key and is the wrong coverage class for professional esports. Oracle's Elixir is the closer professional-history alternative, but the team must confirm Riot/publisher reuse rights, inspect a current header/definitions file, quantify historical coverage and corrections, and define a snapshot freshness/completion rule. Do not integrate either source solely because its page is public. [[20]](https://developer.riotgames.com/apis#match-v5) [[21]](https://developer.riotgames.com/docs/portal) [[22]](https://oracleselixir.com/tools/downloads) [[23]](https://developer.riotgames.com/policies/general)

## Public-alternatives addendum: KBO and NPB

**Research date:** 2026-09-26. Four web-search calls were attempted (KBO official box scores, NPB official player/game results, and the two dentearl repositories plus the armstjc repository); each returned an error consistent with the previously observed HTTP 402. The evidence below therefore comes from read-only direct HTTPS pages and GitHub REST/raw URLs, not search snippets.

### Key facts

* **Official KBO pages are public, but not a documented reusable data API.** The player-record page shows season/aggregate stats, and the schedule page is a public schedule/results view. The captured page says `Copyrightⓒ KBO, All Rights Reserved.` The reviewed pages do not provide a public API specification, per-game identity contract, update/history policy, quota, or bot/republication permission. They are useful for human cross-checking, not a bot-ready source. [[24]](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) [[25]](https://www.koreabaseball.com/Schedule/Schedule.aspx)
* **Official NPB pages are public, but do not document a per-player-per-game feed.** NPB exposes annual player records and a games/results area. The player page expressly restricts secondary use and unauthorized reproduction. No public API contract, stable per-game identity guarantee, quota, retention policy, or bot-use grant was found in the pages reviewed. [[26]](https://npb.jp/bis/players/) [[27]](https://npb.jp/games/)
* **`dentearl/kbo-data` and `dentearl/npb-data` are public snapshots, not player-stat feeds.** Their READMEs describe daily-updated regular-season schedules and game results, including scores and standings, but not per-player statistics. The reviewed project materials do not establish bot reuse rights; public visibility is not permission to reuse the data. These are useful team-result cross-checks, not substitutes for completed player-game history. [[28]](https://github.com/dentearl/kbo-data) [[29]](https://github.com/dentearl/npb-data)
* **`armstjc/Nippon-Baseball-Data-Repository` is a technical NPB lead, not a safe bot source.** Its README lists a Player Game Stats release and identifies SPAIA as a major data source. The available data may be useful for research, but the repository does not establish permission from SPAIA or other rightsholders to redistribute or use those records in a bot. Do not treat its scraping implementation as a documented SPAIA API. [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository)

### Notable claims requiring cross-reference

1. A repository's update activity is evidence of maintenance only; it does not establish complete game coverage, corrected final results, or permission for bot reuse. [[28]](https://github.com/dentearl/kbo-data) [[29]](https://github.com/dentearl/npb-data) [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository)
2. The armstjc README identifies a Player Game Stats release and SPAIA as a source, but identity stability, corrections, retention, and upstream reuse rights were not verified. [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository)
3. Do not treat a public repository's result labels or update cadence as the league's official finalization contract. The reviewed repositories describe their own transformed outputs, while the official pages do not document a bot-facing API contract. [[24]](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) [[25]](https://www.koreabaseball.com/Schedule/Schedule.aspx) [[26]](https://npb.jp/bis/players/) [[27]](https://npb.jp/games/) [[28]](https://github.com/dentearl/kbo-data) [[29]](https://github.com/dentearl/npb-data)
4. This review used public pages and repository documentation only; browser-service calls observed on official sites would not be treated as approved APIs without documentation or permission. [[24]](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) [[25]](https://www.koreabaseball.com/Schedule/Schedule.aspx) [[26]](https://npb.jp/bis/players/) [[27]](https://npb.jp/games/)

### Source quality assessment

| Source | Tier/date | What is established | Main limitation |
|---|---:|---|---|
| KBO official player/schedule pages | Tier 1, fetched 2026-09-26 | Public aggregate player records and schedule/results; KBO copyright notice | No documented per-game player API, quotas, cadence, identity contract, or reuse grant [[24]](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) [[25]](https://www.koreabaseball.com/Schedule/Schedule.aspx) |
| NPB official player/games pages | Tier 1, fetched 2026-09-26 | Public annual player records and games area; explicit secondary-use restriction | No documented per-game player API; reuse restriction conflicts with bot ingestion [[26]](https://npb.jp/bis/players/) [[27]](https://npb.jp/games/) |
| dentearl/kbo-data | Tier 2, fetched 2026-09-26 | Maintained schedule/result snapshots | Team-level only; reuse terms not established [[28]](https://github.com/dentearl/kbo-data) |
| dentearl/npb-data | Tier 2, fetched 2026-09-26 | Maintained schedule/result snapshots | Team-level only; reuse terms not established [[29]](https://github.com/dentearl/npb-data) |
| armstjc/Nippon-Baseball-Data-Repository | Tier 2, fetched 2026-09-26 | Public release identified as player-game stats; SPAIA is a stated source | Reuse rights, accuracy, corrections, and documented upstream API not established [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository) |

### Gaps and decision

No checked public source meets all required criteria (per-player-per-game stats, stable identity, final-result confirmation, history/corrections, cadence, access limits, and explicit bot reuse terms) for both KBO and NPB. **Do not integrate any of these sources without written permission or a clearly applicable data license.** The armstjc release is a candidate for a human-reviewed NPB backfill only after SPAIA/rightsholder permission; dentearl repositories can cross-check team results but cannot satisfy per-player props. [[24]](https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx) [[25]](https://www.koreabaseball.com/Schedule/Schedule.aspx) [[26]](https://npb.jp/bis/players/) [[27]](https://npb.jp/games/) [[28]](https://github.com/dentearl/kbo-data) [[29]](https://github.com/dentearl/npb-data) [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository)

## Valorant public-alternatives addendum

### Key facts

**Official Riot VAL-MATCH-V1 (documented, but not public-without-approval):** Riot documents match-by-ID, player matchlist-by-PUUID, and recent-by-queue routes. The match schema includes match/player/team identity, per-player combat/economy/ability/damage statistics, and explicit `isCompleted`. The recent route is a short discovery window (10 minutes for live regions, 12 hours for `esports` routing), not a durable history store. Riot requires an API key; key type and product approval affect access and limits. [[31]](https://developer.riotgames.com/api-details/val-match-v1) [[21]](https://developer.riotgames.com/docs/portal) [[32]](https://developer.riotgames.com/terms)

Riot’s documentation distinguishes the `esports` recent-match routing value from live regions, but the public reference does **not** establish that PUUID matchlists contain tournament matches or that match-by-ID can discover an arbitrary professional event. Treat ordinary ranked history and professional esports coverage as separate until Riot confirms competition coverage. Do not infer a tournament archive from the recent route. [[31]](https://developer.riotgames.com/api-details/val-match-v1)

**HenrikDev:** The maintainer’s public README documents an unofficial API, consent requirement, API keys and 30/90 requests-per-minute tiers, and warns against large analytics/production use. It does not establish tournament coverage, a licensed professional-match archive, or bot reuse rights. Treat it as unofficial ordinary account/player history unless the provider confirms otherwise; do not assume it supplies tournament matches. [[33]](https://github.com/Henrik-3/unofficial-valorant-api)

**No safe anonymous replacement was verified:** public GitHub code/data and public web pages are leads, not permission. No checked source provides all of a documented match-by-ID route, final status semantics, professional coverage, stable per-player fields, quotas, and explicit bot reuse rights without authorization. [[31]](https://developer.riotgames.com/api-details/val-match-v1) [[32]](https://developer.riotgames.com/terms) [[33]](https://github.com/Henrik-3/unofficial-valorant-api)

### Notable claims requiring cross-reference

1. `isCompleted` is documented by Riot’s schema, but the reference does not define correction/rollback behavior or prove that every response represents a final settled result; retain a provider-confirmed finalization rule. [[31]](https://developer.riotgames.com/api-details/val-match-v1)
2. Riot’s 12-hour esports recent window proves short-lived discovery only, not historical tournament retention. Professional match coverage and ordinary ranked history must not be conflated. [[31]](https://developer.riotgames.com/api-details/val-match-v1)
3. Riot policy/terms and a developer key are not the same as approval for this bot’s production or commercial use; confirm product registration, quotas, RSO/privacy requirements and redistribution terms. [[21]](https://developer.riotgames.com/docs/portal) [[32]](https://developer.riotgames.com/terms)
4. HenrikDev’s rate limits and consent language are claims in its own README, not Riot guarantees or a license grant. The README does not document tournament coverage. [[33]](https://github.com/Henrik-3/unofficial-valorant-api)
5. No applicable data license or permission was established for the unofficial repository reviewed; public visibility by itself does not establish reuse rights. [[30]](https://github.com/armstjc/Nippon-Baseball-Data-Repository) [[33]](https://github.com/Henrik-3/unofficial-valorant-api)

### Source quality assessment

| Source | Tier/date | Established | Limitation |
|---|---:|---|---|
| Riot VAL-MATCH-V1 reference | Tier 1, fetched 2026-09-26 | Official routes, schema fields, completion field, esports recent-window note | Key/approval required; no durable esports archive or public quota in reference [[31]](https://developer.riotgames.com/api-details/val-match-v1) |
| Riot Developer Portal and terms | Tier 1, fetched 2026-09-26 | Key types, registration/verification and policy context | Current approval/quota outcome is account-specific [[21]](https://developer.riotgames.com/docs/portal) [[32]](https://developer.riotgames.com/terms) |
| Henrik-3 unofficial API README | Tier 2, fetched 2026-09-26 | Provider-stated consent, key tiers, limits and analytics warning | Unofficial; no tournament coverage or reuse license established [[33]](https://github.com/Henrik-3/unofficial-valorant-api) |

### Gaps and decision

Do not integrate a public Valorant source on the evidence reviewed. For an authorized Riot pilot, request confirmation of tournament match availability, matchlist retention, finalization/correction semantics, required product/RSO approval, quotas, and permitted storage/redistribution. For HenrikDev, obtain written permission and an explicit statement of competition coverage and production use; absent that, classify it as unofficial ordinary player history only. The web-search and web-fetch callbacks returned HTTP 402, so this addendum used direct public Riot pages and GitHub REST/raw content; uncertainty is explicitly retained. [[21]](https://developer.riotgames.com/docs/portal) [[31]](https://developer.riotgames.com/api-details/val-match-v1) [[32]](https://developer.riotgames.com/terms) [[33]](https://github.com/Henrik-3/unofficial-valorant-api)

### Valorant source references

See sources 31–33 in the consolidated source list above.