---
name: Telegram pick and result semantics
description: Separates delivered picks from evaluated opportunities and documents the directional result contract.
---

## Contract
- Only unique, identity-complete successful ACTIONABLE_PICK Telegram audits count as delivered picks; `PropOpportunityLog.alert_sent` is compatibility bookkeeping, not proof of delivery.
- Join delivery reports and sent-only performance on provider, source, event, player, sport, market, original line, and direction. PrizePicks also requires the exact PropLine line variant.
- `PropOpportunityLog.result` is recommendation-oriented: HIT means the recorded OVER or UNDER pick won, and MISS means it lost.
- Performance hit rate is HIT / (HIT + MISS + PUSH); pushes remain separately visible and are included in the denominator.
- Do not invert UNDER results again in reports, calibration, analyst grading, or backtests.
- Recommendation-less `PropTrackingLog` rows are neutral OVER-baseline settlements and must remain a separate population.

**Why:** evaluated opportunities include picks that were never sent, while legacy alert flags can lack a matching successful audit. Neutral tracking rows have no recommendation. The user wants all verified terminal outcomes reflected in the hit-rate denominator, including pushes. Earlier readers also reversed UNDER outcomes a second time, making winning UNDER picks appear as misses.

**How to apply:** label the population being reported, use identity-matched successful audits for Telegram-only counts and accuracy, use directional opportunity results directly, and include pushes in every H/(H+M+P) report denominator. Pending and unverified outcomes remain outside that denominator.
