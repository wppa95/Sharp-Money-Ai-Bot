---
name: Cold-start removal
description: Startup behavior for the Underdog monitoring and delivery pipeline.
---

The Underdog monitor has no startup-only eligibility mode. Every cycle uses the
same observation, scoring, evidence, qualification, deduplication, and delivery
rules; restart recovery restores durable state without suppressing or inventing
alerts.

**Why:** A disabled cold-start branch left stale gates, counters, bulk-save
paths, and tests that could diverge from the live pipeline and hide eligible
directional picks.

**How to apply:** Do not add a first-cycle latch or startup-specific delivery
exception. If restart behavior changes, preserve database evidence and dedup
state while keeping cycle processing uniform.