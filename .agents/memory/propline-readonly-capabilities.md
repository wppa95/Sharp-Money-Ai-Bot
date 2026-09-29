---
name: PropLine read-only capability boundary
description: Rules for using PropLine’s expanded endpoint surface without contaminating live betting identity or incomplete game evidence.
---

PropLine’s expanded endpoint surface is informational context. Final player-game rows may support directional grading only after explicit final/completed status and exact event/player/sport/stat matching; they must not use date-only fallback. Live cumulative values and football-related sports remain excluded from the shared history store. Resolved PropLine history remains the settlement/reconciliation path.

**Why:** PropLine exposes broad box-score, market, movement, pricing, and calibration data, but live monitoring identities and alert qualification remain provider-specific. Exact final stats can grade a saved line without making raw context actionable; football data needs an isolated grading boundary to avoid leaking into shared history.

**How to apply:** Route new PropLine GET/POST capabilities through the shared rate limiter/cache/error envelope. Keep event context, movement, EV, projections, payout, freshness, coverage, SGP, and CLV informational. Preserve exact final-stats identity and do not remove the football exclusion without an isolated grading-only design.