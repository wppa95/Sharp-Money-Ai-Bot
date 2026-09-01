---
name: Provider confirmation boundary
description: The safety boundary for cross-provider corroboration.
---

Cross-provider confirmation is informational and read-only. It may reuse
already-polled OddsAPI/cache observations and normalize them through the pure
matching helper, but it must not create a new scheduler, alter quantitative
scoring, persist a second monitoring record, or independently deliver an alert.

**Why:** Provider expansion must improve corroboration without reintroducing
duplicate polling cycles or allowing external data to fabricate or override an
Underdog decision.

**How to apply:** Add new providers or markets at the normalization and
confirmation boundary. Keep failures, missing data, ambiguous identity, and
stale rows explicitly unconfirmed, and leave the existing monitoring pipeline
as the sole owner of scoring, persistence, and delivery.

For PropLine/PrizePicks specifically, confirmation reads recent persisted
`PropLineHistory` snapshots and never calls the live provider fetch methods.
The temporary shared request allowance is capped at 1,000 requests while the
client's normal 120-second pacing remains authoritative.

**Why:** Confirmation runs from multiple Underdog alert paths. A live fetch
there multiplies provider traffic, can exhaust a temporary allowance, and
creates a competing polling cycle.

**How to apply:** Keep PropLine HTTP requests inside the existing companion
scan. Treat missing, stale, removed, or ambiguous persisted snapshots as no
confirmation rather than refreshing them inline.

The sportsbook corroboration path may be disabled independently of the
pick'em providers. When it is disabled, keep the DK/FD normalization and
matching helpers intact behind an explicit opt-in, while PropLine PrizePicks
confirmation remains available through the same read-only boundary.

**Why:** DK/FD support is intentionally paused without removing reusable
abstractions or coupling that pause to the Underdog or PropLine pipelines.

**How to apply:** Gate sportsbook confirmation before any DK/FD fetch or
persisted-row lookup. Do not use the sportsbook flags alone as proof that
confirmation is active.

For PrizePicks display matching, read the freshness-bounded current
PropLineHistory pool and compare normalized player, sport, and stat identity.
Provider line differences are informational output, not an identity rejection;
only stale, removed, or ambiguous source rows remain unconfirmed.

**Why:** Stored provider labels vary in capitalization and composite-stat
spacing, while different lines are the exact cross-provider comparison users
need to see.

**How to apply:** Never use exact raw stat text or a near-equal line threshold
as the prerequisite for showing a verified PrizePicks line beside Underdog.