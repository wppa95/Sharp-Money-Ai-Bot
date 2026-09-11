---
name: Direction-only prop delivery
description: Active prop delivery and strongest-pick selection are independent of tier, bet quality, market quality, and PASS labels.
---

The active delivery policy is recommendation plus legitimate historical evidence. Each completed delivery batch keeps only the strongest eligible OVER and strongest eligible UNDER; all other candidates remain tracked without Telegram delivery. Tier, BQ, MQ, and PASS remain only as legacy compatibility metadata where older persistence or callers still require them; they must not qualify, rank, defer, or format current deliveries.

**Why:** Score-class gates caused qualified evidence-backed props to be rejected or inconsistently handled between scan paths and final Telegram delivery.

**How to apply:** New delivery paths should call the shared directional/evidence gate, rank only evidence and stable identity fields, and represent no actionable direction as `None`.