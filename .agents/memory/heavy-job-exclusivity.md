---
name: Heavy job exclusivity
description: Scheduler boundary for expensive monitoring and evidence jobs.
---

Writer-heavy monitoring jobs should share an exclusive scheduler group, but provider/network-heavy player-history collection must not hold that group for its full callback. Its durable write batches must yield while the live Underdog scan owns the persistence window.

**Why:** The scheduler launches callbacks as tracked tasks, so per-job coalescing alone does not prevent different writer jobs from overlapping. Holding the group across a long history-provider pass instead causes the next two-minute Underdog trigger to coalesce and breaks feed cadence.

**How to apply:** Put database-mutating monitoring jobs behind the shared group, keep slow provider collection independent, and add a cooperative persistence boundary before each history write batch. Do not use this gate for lightweight heartbeat, reporting, or alert-policy work.