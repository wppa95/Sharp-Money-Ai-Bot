---
name: Scheduler ownership
description: FPR owns the only scheduled full-pool rotation; Stable Refresh is retained only as an unscheduled compatibility path.
---

Each scheduled job owns an independent execution lane. Underdog, PrizePicks,
grading, history, and supporting jobs may overlap each other, but a second
execution of the same named job must always be coalesced.

**Why:** Shared scheduler ownership let one slow provider or evidence job delay
unrelated work. Same-job overlap still risks duplicate requests and writes.

**How to apply:** Never chain one provider's work from another provider's job or
assign different names to a shared execution group. Reserve same-name admission
atomically before launching asynchronous work.

The sportsbook player-prop poller is a required, separate OddsAPI confirmation
source rather than a second pick'em monitor. It must remain registered only once
and use explicit single-instance protection.

**Why:** Removing that poller would deprive the existing PrizePicks/CLV
confirmation path of its supporting sportsbook observations, while an
unprotected repeat could overlap the provider monitor.

**How to apply:** Keep the player-prop fetch job separate from Underdog and
PrizePicks monitoring, but configure `max_instances=1`; leave legacy
PrizePicks, connector, consensus, CLV-check, and Stable Refresh registrations
disabled.

FPR evaluation may preserve directional opportunity records for a stale/direct
live-row input, but the pregame guard must remain at the delivery boundary.
Futures are excluded before FPR batch construction just like football aliases.

**Why:** Evaluation and grading are independent from Telegram delivery, while
non-player futures and gridiron markets must never enter expensive monitoring
work.

**How to apply:** Keep the canonical active query as the production lifecycle
boundary; retain the FPR defensive filters and never move live-game or provider
scope checks after scoring.