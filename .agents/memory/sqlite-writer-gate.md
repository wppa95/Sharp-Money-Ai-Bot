---
name: Shared SQLite writer gate
description: Runtime SQLite sessions from multiple Database pools must share one process-wide serialization boundary.
---

All in-process Database objects that point to the same SQLite URL must share a
URL-scoped async session gate. A lock stored only on one Database instance is
not sufficient when the bot keeps a separate history pool.

**Why:** SQLite has one writer slot per database file, and the monitoring and
history pools otherwise race even when each pool appears isolated.

**How to apply:** Keep the gate outside provider/network work and verify it
with a cross-pool concurrency regression before changing the pool layout.