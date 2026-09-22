---
name: Live post-scan boundaries
description: Performance and correctness boundaries for complete-feed Underdog scans and their post-scan bridge.
---

Live full-feed scans must retain every raw observation, but their bookkeeping must use bounded executemany writes and explicit current-cycle timestamp slices. The live MQ update and lifecycle bridge must not rebuild append-only history by expanding thousands of source-identity predicates, especially when legacy rows lack external IDs.

**Why:** A complete feed can contain more than 5,000 observations. Re-reading historical rows or generating one predicate per source-less prop can hold SQLite long enough that a cycle appears to stop before its post-scan completion record.

**How to apply:** Keep maintenance callers on all-history behavior, pass an explicit cycle boundary for live MQ/bridge reads, keep raw snapshot transactions bounded, and preserve a distinct post-scan bridge/completion phase after candidate and scan-cycle telemetry.

The scheduler cadence must exceed the measured complete-feed runtime with operational headroom. The current six-minute default follows a roughly five-minute full cycle and should be revisited if feed size or write contention changes materially.

**Why:** A 150-second trigger was shorter than the observed 302-second complete cycle, causing the scheduler cadence to report misses even when persistence eventually completed.

**How to apply:** Base cadence changes on `underdog_cycle_timing` completion data, not only provider fetch time; preserve the complete-feed contract instead of reducing the feed to fit a shorter interval.