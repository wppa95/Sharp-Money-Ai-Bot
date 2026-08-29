---
name: Scheduler ownership
description: FPR owns the only scheduled full-pool rotation; Stable Refresh is retained only as an unscheduled compatibility path.
---

The active scheduler has one Underdog monitor, one PrizePicks companion cycle
inside that monitor, and one FPR/full-pool rotation. Stable Refresh must not be
registered as an independent repeating job or given a replacement cadence.

**Why:** A second DB-backed rotation competed for SQLite and monitoring
capacity without adding a distinct current user-facing capability.

**How to apply:** When changing scheduler registrations, preserve the required
monitoring, CLV, grading, evidence, health, budget, pruning, and season jobs;
keep monitoring jobs single-instance; and audit legacy job names for duplicates.