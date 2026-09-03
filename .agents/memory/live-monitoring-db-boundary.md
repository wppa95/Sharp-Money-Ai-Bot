---
name: Live monitoring database boundary
description: Keep live monitoring feed-driven and reserve SQLite access for durable evidence workflows.
---

Live monitoring must compare the current provider feed with in-memory state. SQLite may be used for evidence, history, grading, and tracking, but not as a recurring active-pool scan or monitoring source.

**Why:** The Underdog snapshot archive grew large enough that database-backed monitoring reads and rewrites held the live cadence for minutes.

**How to apply:** Do not schedule full-pool or stable-refresh monitoring jobs that read the snapshot archive. Keep durable writes/reads only where they produce or consume evidence, historical records, grading results, or tracking state.