---
name: SQLite scan latency
description: Keep live Underdog cadence independent from bulk evidence and telemetry writes.
---

Large neutral candidate telemetry must use bounded batch commits, and monitoring
writes may be queued during evaluation but must be drained before a completed
scan releases its ownership slot.

**Why:** Per-row candidate commits plus a single giant persistence transaction
stretched a 2,304-prop cycle past its 120-second cadence. Bounded transactions
let independent jobs run between chunks while the final drain prevents queued
observations from racing the next scan.

**How to apply:** Keep the SQLite writer single-filed, split large history,
tracking, opportunity, and snapshot writes into short chunks, apply explicit
fallback backpressure when admission is full, and drain the queue before
reporting the cycle complete.