---
name: Grader backlog fairness
description: Durable settlement backlogs need bounded prioritization so rows with available evidence are not delayed by unresolved sports.
---

The settlement grader must not use only ascending row IDs when the pending ledger contains a large unresolved sport population. A cursor can spend every cycle on rows that have historical player data but no result for the specific event, while later rows with completed official evidence remain untouched.

Bulk grading is a pipeline property, not just a larger query limit: pending-row selection, settlement evidence resolution, and terminal writes must all be bulk operations. Remote fallback fetches need their own small time-bounded budget so they cannot hold the durable grading pass open. Per-sport cursors must interleave forward progress with bounded retries of unresolved rows.

**Why:** A correlated `EXISTS` against the large player-results ledger was too slow for the long-lived SQLite database. A cheap sport-priority order restored grading throughput without weakening date or event-identity matching.

**How to apply:** Keep strict date/event matching. Use independent per-sport cursors with interleaved pages so one backlog cannot monopolize grading, and retry unresolved rows so late-arriving official results are not skipped. Use chunked evidence reads and executemany settlement writes, while bounding provider fallback work separately. Do not grade from player/stat history alone.