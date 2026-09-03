---
name: Live monitoring database boundary
description: Keep live monitoring feed-driven and reserve SQLite access for durable evidence workflows.
---

Live monitoring must compare the current provider feed with in-memory state. SQLite may be used for evidence, history, grading, and tracking, but not as a recurring active-pool scan or monitoring source.

**Why:** The Underdog snapshot archive grew large enough that database-backed monitoring reads and rewrites held the live cadence for minutes.

**How to apply:** Do not schedule full-pool or stable-refresh monitoring jobs that read the snapshot archive. Keep durable writes/reads only where they produce or consume evidence, historical records, grading results, or tracking state.

PropLine sport-catalog failures must remain distinguishable from a valid empty catalog. A temporary 429 should fall back to the provider's known non-football namespace; a successful empty catalog should remain empty.

**Why:** The client catches provider errors internally and otherwise exposes both rate-limited and genuinely empty catalog responses as `[]`, which can silently stop every non-football request.

**How to apply:** Preserve an explicit discovery-failure signal through the client/adapter boundary before choosing a compatibility sport fallback.