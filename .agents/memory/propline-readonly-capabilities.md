---
name: PropLine read-only capability boundary
description: Rules for using PropLine’s expanded endpoint surface without contaminating live betting identity or incomplete game evidence.
---

PropLine’s read-only endpoints supplement shared player history. Explicitly final/completed player-game rows from every supported sport, including NFL and CFB, enter the shared result store. Directional grading still requires exact saved/result event, player, sport, and stat identity; `propline_player_games` never uses date-only fallback. Live or incomplete values cannot enter completed-result history. Resolved PropLine history remains a separate explicit-settlement path.

**Why:** Supported-sport history must remain complete, including NFL and CFB, while in-progress or mismatched results must not grade saved lines or become actionable alert evidence.

**How to apply:** Do not exclude supported sports from shared-history persistence. Require explicit final status and identified player-game rows, then require exact event/player/sport/stat identity when grading. Route PropLine calls through the shared rate limiter/cache/error envelope; keep movement, EV, projections, payout, freshness, coverage, SGP, and CLV informational.