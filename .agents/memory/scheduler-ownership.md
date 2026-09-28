---
name: Scheduler ownership
description: FPR owns the only scheduled full-pool rotation; compatibility paths remain unscheduled
---

Each scheduled job owns an independent execution lane. Underdog, PropLine,
grading, history, and supporting jobs may overlap, but a second execution of
the same named job must always be coalesced.

Never chain one provider's work from another provider's job or assign different
names to a shared execution group. Reserve same-name admission atomically
before launching asynchronous work. The retired sportsbook/Odds API poller
must not be restored as a required source.

FPR may preserve directional opportunity records for stale/direct live-row
input, but pregame guards remain at the delivery boundary. Keep canonical
active queries as the production lifecycle boundary and retain defensive
provider-scope filters before scoring.