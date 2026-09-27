---
name: Telegram pick and result semantics
description: Separates delivered picks from evaluated opportunities and documents the directional result contract.
---

## Contract
- Only successfully sent ACTIONABLE PICK alerts count as Telegram-delivered picks; use the delivery marker for sent-only performance.
- `PropOpportunityLog.result` is recommendation-oriented: HIT means the recorded OVER or UNDER pick won, and MISS means it lost.
- Do not invert UNDER results again in reports, calibration, analyst grading, or backtests.
- Recommendation-less `PropTrackingLog` rows are neutral OVER-baseline settlements and must remain a separate population.

**Why:** evaluated opportunities include picks that were never sent, while neutral tracking rows have no recommendation. Earlier readers also reversed UNDER outcomes a second time, making winning UNDER picks appear as misses.

**How to apply:** label the population being reported, filter to successful deliveries for Telegram-only accuracy, and use directional opportunity HIT/MISS values directly.
