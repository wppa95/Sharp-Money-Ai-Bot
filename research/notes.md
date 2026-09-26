# Research Notes: Completed-Game Data for Active Sports

**Status:** researching
**Depth:** Quick

## Plan

- **Question:** Which public or licensed feeds document completed per-player game stats for KBO, NPB, LoL, Valorant, table tennis, and badminton?
- **Scope:** Verify sport coverage, raw player-game fields, final status and identity fields, access requirements, and published limits. Do not integrate or guess provider keys.
- **Audience:** Technical, focused on selecting a safe history source for the bot.
- **Deliverable:** A source comparison with a practical recommendation and unresolved access checks.

## Focus Areas

| # | Area | Status | Sources |
|---|---|---|---|
| 1 | KBO and NPB baseball | pending | -- |
| 2 | League of Legends and Valorant esports | pending | -- |
| 3 | Table tennis and badminton | pending | -- |

## Coverage Checklist

- [ ] Verify a documented KBO endpoint returns completed player-game stats and stable game/player identity.
- [ ] Verify a documented NPB endpoint returns completed player-game stats and stable game/player identity.
- [ ] Verify LoL match/game history exposes player-level results and completion status.
- [ ] Verify Valorant match/game history exposes player-level results and completion status.
- [ ] Verify table-tennis completed matches expose player-level stats, not just scores.
- [ ] Verify badminton completed matches expose player-level stats, not just scores.
- [ ] Identify authentication, quota, licensing, and history-depth constraints for each candidate.

## Findings Log

Research has not yet established a verified provider for any remaining sport.

## Conflicts & Open Questions

- Some feeds may expose match scores but not the per-player stat fields needed by the history pipeline.
- Official or commercial feeds may require account approval or paid licensing; public docs do not necessarily imply production-use rights.

## Gaps

- Search and documentation access are being checked after the web-search service returned HTTP 402.