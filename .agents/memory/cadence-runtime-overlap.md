---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The polling interval is a schedule target, not a guarantee that a full-feed
cycle completes within it. Cycle time includes feed evaluation and database
persistence, so cadence changes must leave headroom for shared-database work.

**Why:** A recent full-feed scan took nearly as long as the configured cadence.
Tightening it without measured headroom can queue scans and increase database
contention instead of improving line freshness.

**How to apply:** Choose the interval from observed completed-cycle timings,
including persistence; keep the existing single full-feed rotation and avoid
overlapping scans or scan-time provider hydration.

The production full-pool monitoring rotation uses the same cadence as the
active Underdog monitor; its legacy interval setting is only an enable/disable
compatibility switch.

**Why:** A separate full-pool interval made live logs report a 90-second
Underdog monitor alongside a 120-second monitoring rotation, obscuring the
actual cycle contract.

**How to apply:** Schedule full-pool work from `UNDERDOG_POLL_INTERVAL`, while
preserving only `FPR_INTERVAL=0` as the disable path.

The live cycle must reserve scheduler headroom rather than waiting for the
full configured interval. Required bulk feed writes stay ordered in the
persistence worker; bridge and non-critical candidate telemetry follow that
order without extending the monitoring coroutine past its budget.

**Why:** Waiting exactly 90 seconds caused a few milliseconds of scheduler
overlap and false cadence failures, while synchronous bridge/candidate drains
turned a 16-second scan into a 112-second job.

**How to apply:** Leave explicit headroom in the final persistence wait,
preserve raw-feed → bridge/lifecycle → candidate ordering, and let queued bulk
writes finish asynchronously after the live scan returns.