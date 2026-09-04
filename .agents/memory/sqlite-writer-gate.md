---
name: Shared SQLite writer gate
description: Runtime SQLite sessions from multiple Database pools must share one process-wide serialization boundary.
---

All in-process Database objects that point to the same SQLite URL must share a
URL-scoped async writer gate. Reads may proceed concurrently, but DML, flush,
and commit activity must serialize across the monitoring and history pools. A
lock stored only on one Database instance is not sufficient when the bot keeps
a separate history pool.

**Why:** SQLite has one writer slot per database file. Serializing entire
sessions made reports and target discovery wait behind large writes, while
allowing concurrent writes lets the pools race and produce lock failures.

**How to apply:** Keep the gate outside provider/network work, hold it for the
write transaction rather than the whole session, and verify it with a
cross-pool concurrency regression before changing the pool layout.