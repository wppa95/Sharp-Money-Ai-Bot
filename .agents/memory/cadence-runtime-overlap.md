---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The configured two-minute cadence is not a guarantee of two-minute scan completion. Stateful full-feed Underdog work and opportunity grading share one execution slot. Scheduler triggers are retained and queue behind their named or shared slot; they must not be coalesced, cancelled, skipped, or overlapped.

**Why:** Coalesced monitor/history triggers lost required work. Queued non-overlapping execution preserves every trigger while the scan’s dynamic grading budget prevents one cycle from monopolizing the shared slot.

**How to apply:** Keep per-job slots and the Underdog/history shared slot. Derive grading timeout from cadence minus actual scan elapsed time and headroom. Do not add overlap flags, max-instance workarounds, or scan-time provider hydration.

The production full-pool monitoring rotation uses the same cadence as the
active Underdog monitor; its legacy interval setting is only an enable/disable
compatibility switch.

**Why:** A separate full-pool interval made live logs report a 90-second
Underdog monitor alongside a 120-second monitoring rotation, obscuring the
actual cycle contract.

**How to apply:** Schedule full-pool work from `UNDERDOG_POLL_INTERVAL`, while
preserving only `FPR_INTERVAL=0` as the disable path.