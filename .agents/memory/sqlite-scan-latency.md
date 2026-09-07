---
name: SQLite scan latency
description: Keep live Underdog cadence independent from bulk evidence and telemetry writes.
---

Large neutral candidate telemetry must use bounded batch commits, and non-critical
monitoring writes must be queued rather than awaited by the live-feed scan.

**Why:** Per-row candidate commits plus a full persistence barrier stretched a
2,304-prop cycle past its 120-second cadence, causing the next feed trigger to
coalesce and leaving monitoring data stale.

**How to apply:** Preserve all writes and drain the queue during shutdown or
explicit failure handling, but do not make the normal successful scan wait for
telemetry/history persistence when the queue can safely serialize it.