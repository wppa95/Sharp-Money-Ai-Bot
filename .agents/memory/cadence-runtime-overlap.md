---
name: Cadence runtime overlap
description: Observed runtime behavior when the Underdog full scan exceeds its configured scheduler interval.
---

The configured two-minute cadence is not a guarantee of two-minute scan completion. Stateful full-feed Underdog work and opportunity grading share one single-flight cadence owner. Grading must use only the time remaining after the scan, with release headroom, and paced settlement fallbacks must have a small per-cycle target cap. PrizePicks fetch/persistence remains independent.

**Why:** A paced PropLine settlement fallback once held the cadence owner for several minutes after a completed scan. Repeating monitor triggers coalesced and the serialized history collector could not hydrate non-MLB evidence. A fixed grading allowance was also insufficient because scan runtime varies.

**How to apply:** Keep scan and grading single-flight, but derive the grading timeout from cadence minus actual scan elapsed time and headroom. Rotate unfinished settlements to later cycles. Do not restore unbounded per-row PropLine grading or move provider hydration into live candidate evaluation.