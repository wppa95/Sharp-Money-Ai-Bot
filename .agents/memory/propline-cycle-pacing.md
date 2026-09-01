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

**How to apply:** Keep one active companion task per shared cycle, await it
alongside Underdog, and rely on the existing single-instance scheduler for
overlap protection. Keep the configured-sport round robin and provider pacing;
do not add a PrizePicks scheduler or heartbeat-triggered handoff.