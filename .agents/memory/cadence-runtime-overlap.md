---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The configured two-minute cadence is not a guarantee of two-minute scan completion. Stateful full-feed Underdog work remains single-flight, while the opportunity grader uses independent tracked workers so a slow provider lookup does not suppress later grading batches. PrizePicks fetch/persistence runs independently of both.

**Why:** A restart verification run showed one cold-start scan completing in about 170 seconds and a subsequent scan still active after several minutes. The coordinator stayed healthy and prevented duplicate heavy work, but the effective refresh cadence degraded and status counters could temporarily be unavailable.

**How to apply:** Treat interval changes and overlap logs separately from crash health. Keep stateful full-feed scans single-flight, but allow cursor/batch-based settlement work to overlap when it has independent worker state. Persist provider observations at fetch time so slow evaluation cannot lose them. Avoid concurrent full scans because SQLite contention and memory use are already significant.