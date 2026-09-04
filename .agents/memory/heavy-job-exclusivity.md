---
name: Heavy job exclusivity
description: Scheduler boundary for expensive monitoring and evidence jobs.
---

Underdog monitoring and player-history collection must share an exclusive scheduler group. A history trigger may be created while a scan is active, but it must wait until the scan, database persistence barrier, and monitoring-cycle completion have finished.

**Why:** The scheduler launches callbacks as tracked tasks, so per-job coalescing alone does not prevent different heavy jobs from overlapping. Overlap causes SQLite writer contention and makes cadence logs misleading.

**How to apply:** Put any future heavy job that reads or writes the active Underdog universe behind the same group, and log whether it waited. Do not use this gate for lightweight heartbeat, reporting, or alert-policy work.