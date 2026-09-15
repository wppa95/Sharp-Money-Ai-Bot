---
name: Odds API settlement-only scope
description: The retained role and pacing boundary for The Odds API.
---

The Odds API is an explicitly enabled, strictly paced settlement/evidence
source only. PropLine and Underdog own live market, season metadata, and
player-prop ingestion. The bot must not spend Odds API quota on `/sports`,
live odds, sportsbook player props, or bulk player statistics.

**Why:** The daily allowance is intentionally small, and using it for
monitoring or corroboration can starve the hit/miss/push evidence path. The
Odds API scores endpoint is team-level, so player-value grading still belongs
to the existing player-stats provider.

**How to apply:** Keep `OddsApiResultsProvider` available for explicit
settlement/evidence requests, and route every real request through the shared
persistent quota pacer. Do not construct or schedule season metadata refreshes
in the production bot, and do not inject the analysis engine's live Odds API
fetch methods into alert processing.