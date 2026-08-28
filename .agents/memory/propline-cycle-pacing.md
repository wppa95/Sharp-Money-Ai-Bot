---
name: PropLine cycle pacing
description: How the primary PrizePicks feed is scheduled without a competing job.
---

The primary PrizePicks monitor uses the existing Underdog cadence as a
companion task and fetches one configured PrizePicks sport per cycle. It must
not call the provider-wide all-sports fetch in production.

**Why:** The authorized PropLine client enforces a five-minute request interval;
serially fetching the full discovered sport list delays persistence and
evaluation for hours.

**How to apply:** Keep the configured-sport round robin and the existing
companion-task guard. Do not add a separate PrizePicks scheduler or bypass the
client's pacing just to increase coverage.