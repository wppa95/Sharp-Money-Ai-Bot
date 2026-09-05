---
name: PropLine cycle pacing
description: How the primary PrizePicks feed is scheduled without a competing job.
---

The primary PrizePicks monitor is a branch of the same scheduled monitoring
cycle as Underdog. Each Underdog scheduler trigger starts both branches
concurrently, and the shared cycle does not complete until both branches have
finished. It fetches one configured PrizePicks sport per cycle and must not call
the provider-wide all-sports fetch in production. The provider's own request
pacing remains internal to that branch.

**Why:** A heartbeat handoff created a second effective monitoring rhythm:
PropLine could still be processing an older cycle while Underdog started the
next one. Shared ownership makes cadence and completion explicit without
slowing either provider or changing the authorized client's pacing.

The result-metadata fetch adapter must follow the same one-sport limit as the
legacy scoped adapter; exposing fetch success metadata is not permission to
expand a cycle into an all-sports sweep.

**Why:** Expanding only the production result adapter held the shared scheduler
job open across multiple sports and prevented its completion telemetry from
being recorded promptly.

Scheduled player-history supplementation follows the same boundary: public
history collection may cover the full active target universe, but PropLine
fallbacks use a small rotating target window rather than one request per
active prop.

**Why:** Sparse public history can require PropLine without allowing a
full-book fallback sweep to queue behind the provider's shared limiter.

The base live feed retains its 180/hour pacing under the 5,000/day total cap.
Non-bulk endpoint calls have a separate 650/day and 27/hour allowance. Empty
sport snapshots are incomplete and enter a short cooldown so they do not
consume repeated feed requests; transport labels never occupy sport fields.

**Why:** The base feed uses 4,320 requests/day, leaving 680. Reserving 650 for
endpoint calls leaves roughly 30 unused while preventing history/reporting
lookups from exhausting the live-feed budget.

**How to apply:** Keep canonical provider sport keys, the configured-sport
round robin, and separate feed/endpoint admission. Do not report "paced" as a
sport or repeatedly request a known-empty sport during its cache TTL.