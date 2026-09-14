---
name: Direction-only prop delivery
description: Active prop delivery and strongest-pick selection are independent of tier, bet quality, market quality, and PASS labels.
---

The active delivery policy is recommendation plus legitimate historical evidence. Each completed delivery batch keeps the strongest eligible directional pick for each sport, whether OVER or UNDER; all other candidates remain tracked without Telegram delivery. Tier, BQ, MQ, and PASS remain only as legacy compatibility metadata where older persistence or callers still require them; they must not qualify, rank, defer, or format current deliveries.

**Why:** Score-class gates caused qualified evidence-backed props to be rejected or inconsistently handled between scan paths and final Telegram delivery.

**How to apply:** New delivery paths should call the shared directional/evidence gate, select one winner per sport after the complete cross-sport batch is evaluated, rank only evidence and stable identity fields, and represent no actionable direction as `None`.