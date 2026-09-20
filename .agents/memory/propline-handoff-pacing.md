---
name: PropLine handoff pacing
description: The shared monitoring cycle must remain responsive while PropLine requests wait on provider pacing.
---

The shared Underdog/PropLine cycle may start PropLine ingestion concurrently, but a quota-paced or slow fetch must not hold the live monitoring cycle open. Give the handoff only a short grace window for fast completion; leave slower work running and preserve its completed observations for the next cycle.

**Why:** PropLine quota pacing can legitimately sleep for seconds while the live Underdog scan needs to finish on its own cadence. Waiting indefinitely caused full-suite timing failures and could make production monitoring appear stalled.

**How to apply:** Keep provider pacing and ingestion ownership inside PropLine. Bound only the cycle-to-cycle handoff wait, and never clear a completed ingestion batch when starting the replacement request.