---
name: Grader date reconciliation
description: Reconcile UTC event timestamps with provider-local player-result dates.
---

Settlement lookup must try the event's UTC calendar date first and the immediately
preceding provider calendar date second, while retaining event/player/stat identity.

**Why:** Underdog event times are UTC, but MLB and similar result feeds use local
game dates; exact-date-only grading converted valid results into persistent no-data
rows.

**How to apply:** Never search by player/stat alone. Use the shifted date only
after an exact-date miss, preserve game-ID/provider constraints, and report
shifted matches separately from genuinely unresolved rows.