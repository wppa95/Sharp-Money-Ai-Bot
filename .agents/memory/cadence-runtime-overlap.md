---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The configured two-minute cadence is not a guarantee of two-minute scan completion. Stateful full-feed Underdog work and opportunity grading now share one single-flight cadence owner: the monitor finishes before grading starts, and no independent grader trigger can overlap, skip, or queue. PrizePicks fetch/persistence remains independent.

**Why:** A restart verification run showed one cold-start scan completing in about 170 seconds and a subsequent scan still active after several minutes. The coordinator stayed healthy and prevented duplicate heavy work, but the effective refresh cadence degraded and status counters could temporarily be unavailable.

**How to apply:** Treat interval changes and overlap logs separately from crash health. Keep both the full-feed scan and grading single-flight under the Underdog cadence owner. Persist provider observations at fetch time so slow evaluation cannot lose them. Avoid concurrent full scans or separate grading timers because SQLite contention and scheduler backlog are already significant.