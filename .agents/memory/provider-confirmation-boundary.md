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

The sportsbook corroboration path may be disabled independently of the
pick'em providers. When it is disabled, keep the DK/FD normalization and
matching helpers intact behind an explicit opt-in, while PropLine PrizePicks
confirmation remains available through the same read-only boundary.

**Why:** DK/FD support is intentionally paused without removing reusable
abstractions or coupling that pause to the Underdog or PropLine pipelines.

**How to apply:** Gate sportsbook confirmation before any DK/FD fetch or
persisted-row lookup. Do not use the sportsbook flags alone as proof that
confirmation is active.