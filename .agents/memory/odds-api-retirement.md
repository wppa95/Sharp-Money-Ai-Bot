---
name: The Odds API retirement
description: Durable rule for the retired sportsbook-odds provider.
---

The Odds API is retired. Do not add or reintroduce Odds API clients, requests,
schedulers, quota or pacing paths, confirmations, CLV fetches, or live-market
ingestion unless the user explicitly asks for that provider again.

Existing persisted odds, evidence, and CLV rows remain readable for historical
reports and compatibility. They are not permission to resume provider writes
or network requests. Underdog and PropLine own live evaluation, observations,
and provider-neutral settlement/evidence paths.

Keep the generic game-results and grading interfaces importable. The retired
score adapter remains an import-compatible no-network no-op.

**Why:** Player-result grading and historical compatibility remain useful, but
the retired provider must not perform network requests.

**How to apply:** Future grading/provider work must preserve the generic
scaffold without restoring an Odds API client or polling path.