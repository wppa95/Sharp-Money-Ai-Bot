---
name: Scheduler ownership
description: FPR owns the only scheduled full-pool rotation; Stable Refresh is retained only as an unscheduled compatibility path.
---

The active scheduler has one Underdog monitor, one PrizePicks companion cycle
inside that monitor, and one FPR/full-pool rotation. Stable Refresh must not be
registered as an independent repeating job or given a replacement cadence.

**Why:** A second DB-backed rotation competed for SQLite and monitoring
capacity without adding a distinct current user-facing capability.

**How to apply:** When changing scheduler registrations, preserve the required
monitoring, CLV, grading, evidence, health, budget, pruning, and season jobs;
keep monitoring jobs single-instance; and audit legacy job names for duplicates.

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