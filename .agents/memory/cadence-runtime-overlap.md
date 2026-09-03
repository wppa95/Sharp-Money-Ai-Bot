---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The configured two-minute cadence is not a guarantee of two-minute scan completion. A live Underdog scan can hold the shared full-scan slot across several interval triggers, causing the coordinator to skip those triggers and defer FPR, grading, and history work.

**Why:** A restart verification run showed one cold-start scan completing in about 170 seconds and a subsequent scan still active after several minutes. The coordinator stayed healthy and prevented duplicate heavy work, but the effective refresh cadence degraded and status counters could temporarily be unavailable.

**How to apply:** Treat interval changes and overlap logs separately from crash health. Before claiming a smooth cadence, measure end-to-end scan duration and the specific stage holding the slot; avoid simply adding concurrent full scans because SQLite contention and memory use are already significant.