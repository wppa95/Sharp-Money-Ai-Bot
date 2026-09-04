---
name: Full-feed snapshot batching
description: Safe persistence pattern for multi-thousand-row live Underdog snapshot feeds.
---

Large live-feed snapshot batches must be committed in bounded transactions rather than one ORM flush. The full scan may keep its ownership barrier until every batch completes, and the returned count must aggregate successful batch writes.

**Why:** A single multi-thousand-row SQLite ORM transaction can monopolize the writer, create a very large WAL burst, and block independent player-evidence writes even when the scan itself has a cadence deadline.

**How to apply:** Keep evaluation limits separate from persistence limits. Batch the complete observation feed, aggregate written/retry/failure metrics, and verify the next cycle has zero intentionally skipped observations and no SQLite lock errors.