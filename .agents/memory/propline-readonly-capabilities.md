---
name: PropLine read-only capability boundary
description: Rules for using PropLine’s expanded endpoint surface without contaminating live betting identity or incomplete game evidence.
---

PropLine’s expanded endpoint surface is read-only context. Raw player-game data may supplement history only after an explicit final/completed status; live cumulative values and football-related sports must be excluded before persistence. Resolved PropLine history remains the settlement/reconciliation path.

**Why:** PropLine exposes broad box-score, market, movement, pricing, and calibration data, but the bot’s live monitoring identities and alert qualification remain provider-specific. Treating raw/live context as settled or actionable would create false history and cross-provider attribution.

**How to apply:** Route new PropLine GET capabilities through the shared rate limiter/cache/error envelope. Keep event context, stats, movement, EV, projections, payout, freshness, and coverage responses informational unless a separate, tested storage boundary explicitly accepts them.