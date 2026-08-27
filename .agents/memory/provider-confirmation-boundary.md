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