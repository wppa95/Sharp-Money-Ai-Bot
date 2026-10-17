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

**Verified capability boundary (2026-10-17):** Official V4 documentation says
the scores endpoint returns event completion and team scores, not player stats.
Historical odds endpoints return market quotes (including player labels, lines,
and prices), not actual player-game results; historical endpoints are paid-plan
only, and historical event-prop requests cost 10 quota credits per unique
market/region. Account plan and quota reset/remaining values cannot be inferred
from local config or stale local counters.

**Why:** Player-result grading and historical compatibility remain useful, but
the retired provider must not perform network requests.

**How to apply:** Future grading/provider work must preserve the generic
scaffold without restoring an Odds API client or polling path. Even after an
explicit restoration request, do not treat team scores or quoted prices as a
player result; require verified account-plan and quota-period details before
using paid historical endpoints.