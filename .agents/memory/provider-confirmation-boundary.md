---
name: Provider confirmation boundary
description: Safety boundary for provider-neutral and PropLine corroboration
---

Confirmation is informational and read-only. It must not create a scheduler,
alter quantitative scoring, persist a duplicate monitoring record, or
independently deliver an alert. Current corroboration uses freshness-bounded
PropLine history and never refreshes a live provider inline.

Keep HTTP requests inside the existing companion scan and treat missing,
stale, removed, or ambiguous rows as unconfirmed. On-demand PropLine analytics
may compose authorized results/history endpoints for reports, but remains an
evidence facade rather than a monitoring, scoring, or delivery path.

Historical provider labels and persisted odds remain available for reporting
only. The retired Odds API must not be reintroduced as a confirmation source.