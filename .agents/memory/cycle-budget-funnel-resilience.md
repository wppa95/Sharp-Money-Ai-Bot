---
name: Monitoring cycle budget and funnel resilience
description: Optional player-history enrichment must never hold the shared monitoring cycle or funnel report open.
---

The complete Underdog feed is persisted before optional scoring enrichment. A stalled enrichment call consumes the cycle's bounded evidence budget and the remaining observations are retained as persisted/observation-only data rather than launching more slow lookups.

**Why:** Per-operation timeouts can still stall an event loop when cancellation cleanup waits on SQLite or provider work; repeated enrichment attempts then prevent cycle completion and leave `/funnel` without fresh scan evidence.

**How to apply:** Use hard task waits that return without awaiting slow cancellation cleanup, stop optional enrichment after the first timeout, and keep reporting paths able to render persisted scan-cycle data when normalized aggregates are unavailable.