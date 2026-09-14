---
name: Direction-only prop delivery
description: Active prop delivery and strongest-pick selection are independent of tier, bet quality, market quality, and PASS labels.
---

The active delivery policy is recommendation plus legitimate historical evidence. Telegram keeps every eligible OVER pick at 70%+ confidence or UNDER pick at 35%- confidence, with no global or per-sport count cap; the middle band remains tracked without Telegram delivery. Tier, BQ, MQ, and PASS remain only as legacy compatibility metadata where older persistence or callers still require them; they must not qualify, rank, defer, or format current deliveries.

**Why:** Score-class gates caused qualified evidence-backed props to be rejected or inconsistently handled between scan paths and final Telegram delivery.

**How to apply:** New delivery paths should call the shared directional/evidence gate, apply the direction-specific confidence thresholds after the complete batch is evaluated, rank stronger directional confidence/evidence first, and represent no actionable direction as `None`.