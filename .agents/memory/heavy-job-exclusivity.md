---
name: Heavy job exclusivity
description: Scheduler boundary for expensive monitoring and evidence jobs.
---

Cadence-critical Underdog monitoring and FPR must not wait behind one scheduler-wide exclusive lock. The live feed owns an in-process duplicate-scan guard; FPR defers/handoffs when the feed owns that slot, and may overlap feed scoring otherwise. All SQLite commits still pass through the process-wide writer gate. Provider/network-heavy player-history collection remains independent and its durable batches yield while the live feed owns the persistence window.

**Why:** The scheduler launches callbacks as tracked tasks, so a shared exclusive group can make a 20–30 second FPR pass or a long bookkeeping drain coalesce the next 90-second Underdog trigger. SQLite already serializes writers, while the monitoring/FPR handoff logic prevents duplicate full-feed work.

**How to apply:** Keep the Underdog and FPR scheduler callbacks independent; rely on `_ud_full_scan_running` and the FPR deferred handoff for full-scan coordination. Keep slow provider collection independent and add a cooperative persistence boundary before each history write batch. Do not use a scheduler-wide lock for report-only or cadence-critical work.