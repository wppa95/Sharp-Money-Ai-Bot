---
name: Live monitoring database boundary
description: Keep live monitoring feed-driven and reserve SQLite access for durable evidence workflows.
---

Live monitoring must compare the current provider feed with in-memory state. SQLite may be used for evidence, history, grading, and tracking, but not as a recurring active-pool scan or monitoring source.

**Why:** The Underdog snapshot archive grew large enough that database-backed monitoring reads and rewrites held the live cadence for minutes.

**How to apply:** Do not schedule full-pool or stable-refresh monitoring jobs that read the snapshot archive. Keep durable writes/reads only where they produce or consume evidence, historical records, grading results, or tracking state.

Production writes emitted by a live scan must be serialized outside the scan coroutine, drained during graceful shutdown, and discarded when an event loop is replaced.

**Why:** SQLite persistence can outlive the monitoring work, but asyncio queues and tasks belong to the loop that created them; carrying them across loops risks stale tasks and lost shutdown writes.

**How to apply:** Queue scan writes through the persistence worker, keep compatibility doubles awaited, and reset the worker after draining before the database closes.

PropLine sport-catalog failures must remain distinguishable from a valid empty catalog. A temporary 429 should fall back to the provider's known non-football namespace; a successful empty catalog should remain empty.

**Why:** The client catches provider errors internally and otherwise exposes both rate-limited and genuinely empty catalog responses as `[]`, which can silently stop every non-football request.

**How to apply:** Preserve an explicit discovery-failure signal through the client/adapter boundary before choosing a compatibility sport fallback.