---
name: Live monitoring database boundary
description: Keep live monitoring feed-driven and reserve SQLite access for durable evidence workflows.
---

Live monitoring must compare the current provider feed with in-memory state. Player-result evidence, line history, confirmation, and delivery dedup are keyed memory lookups during a scan; SQLite remains durable storage for background hydration, grading, reporting, and tracking.

**Why:** Per-candidate evidence, confirmation, and dedup reads made warmed scans suspend inside SQLite even after active-pool restoration moved to memory.

**How to apply:** Background collectors bulk-hydrate bounded caches after the scan releases its slot. Candidate paths use direct identity lookups; a cache miss yields no evidence/PASS and never falls back to SQLite or a remote provider.

History collection is a storage/enrichment worker, not a monitoring job. It must not register in monitoring activity or health/display state, wait behind scans and start late, or overlap any scan/rescan.

**Why:** A collector shown as active made monitoring displays appear incomplete, and long provider fallbacks could survive into the next live cadence even though the scan itself used memory.

**How to apply:** Skip the collector when monitoring is active, bound its independent work window and provider calls, and preempt any active collector before live scan, Stable Refresh, or full-pool rescan activity begins.

Live scan history snapshots must remain cache/DB-only even when the cache is empty. Remote primary-provider fetches belong exclusively to the independent history collector; authenticated PropLine player-history supplementation is disabled because its documented endpoints are one-player-at-a-time. A scan miss yields an evidence-free result for that cycle.

**Why:** Per-player enrichment inside new-prop, line-change, re-entry, standing, Stable Refresh, full-pool, PrizePicks, or read-only rotation paths lets monitoring consume history quota and makes latency depend on provider response time.

**How to apply:** Pass `ensure_real_results=False` to scan-time snapshots and `allow_remote=False` to scan-time hit-rate computation. Keep primary-provider collection and grading evidence batched/bulk-persisted; do not schedule any singular PropLine player-history family or per-opportunity settlement lookup.

Production writes emitted by a live scan must be serialized outside the scan coroutine, drained during graceful shutdown, and discarded when an event loop is replaced.

**Why:** SQLite persistence can outlive the monitoring work, but asyncio queues and tasks belong to the loop that created them; carrying them across loops risks stale tasks and lost shutdown writes.

**How to apply:** Queue scan writes through the persistence worker, keep compatibility doubles awaited, and reset the worker after draining before the database closes.

PropLine sport-catalog failures must remain distinguishable from a valid empty catalog. A temporary 429 should fall back to the provider's known non-football namespace; a successful empty catalog should remain empty.

**Why:** The client catches provider errors internally and otherwise exposes both rate-limited and genuinely empty catalog responses as `[]`, which can silently stop every non-football request.

**How to apply:** Preserve an explicit discovery-failure signal through the client/adapter boundary before choosing a compatibility sport fallback.