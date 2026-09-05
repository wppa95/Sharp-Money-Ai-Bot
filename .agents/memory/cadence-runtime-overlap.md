---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The configured two-minute cadence is not a guarantee of two-minute scan completion. Stateful full-feed Underdog work and opportunity grading share one execution slot. Scheduler triggers are retained and queue behind their named or shared slot; they must not be coalesced, cancelled, skipped, or overlapped.

**Why:** Coalesced monitor/history triggers lost required work. Queued non-overlapping execution preserves every trigger while the scan’s dynamic grading budget prevents one cycle from monopolizing the shared slot.

**How to apply:** Keep per-job slots and the Underdog/history shared slot. Derive grading timeout from cadence minus actual scan elapsed time and headroom. Do not add overlap flags, max-instance workarounds, or scan-time provider hydration.