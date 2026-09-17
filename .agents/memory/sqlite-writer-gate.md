---
name: Shared SQLite writer gate
description: Runtime SQLite sessions from multiple Database pools must share one process-wide serialization boundary.
---

All in-process Database objects that point to the same SQLite URL must share a
URL-scoped async writer gate. Reads may proceed concurrently, but DML, flush,
and commit activity must serialize across the monitoring and history pools.
Prefer one SQLAlchemy engine/pool for the bot's live SQLite file; a gate alone
does not eliminate connection-level contention between independent pools.

**Why:** SQLite has one writer slot per database file. Serializing entire
sessions made reports and target discovery wait behind large writes, while
allowing concurrent writes lets the pools race and produce lock failures. In
production, two independent pools could still surface a lock after the
in-process gate had been acquired.

**How to apply:** Keep the gate outside provider/network work, hold it for the
write transaction rather than the whole session, share the live engine where
possible, and verify short external-lock recovery plus cross-pool concurrency
before changing the pool layout.