---
name: Grader backlog fairness
description: Durable settlement backlogs need bounded prioritization so rows with available evidence are not delayed by unresolved sports.
---

The settlement grader must not use only ascending row IDs when the pending ledger contains a large unresolved sport population. A cursor can spend every cycle on rows that have historical player data but no result for the specific event, while later rows with completed official evidence remain untouched.

**Why:** A correlated `EXISTS` against the large player-results ledger was too slow for the long-lived SQLite database. A cheap sport-priority order restored grading throughput without weakening date or event-identity matching.

**How to apply:** Keep the cursor and strict settlement matching, but prioritize sports with an established official-results path before unresolved populations. Re-evaluate the priority list when provider coverage changes.