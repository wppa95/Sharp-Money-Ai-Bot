---
name: PropLine read-only capability boundary
description: Rules for using PropLine’s expanded endpoint surface without contaminating live betting identity or incomplete game evidence.
---

PropLine is a shared supplemental feed for all sports monitored by V4, not a basketball-only source. Dynamically discover its catalog and retain each sport’s observations; only use history or grade markets with exact verified stat mappings. Explicitly final/completed player-game rows enter shared history while preserving provider/source identity. Directional grading requires exact saved/result event, player, sport, and stat identity; `propline_player_games` never uses date-only fallback. Unknown schemas, unsupported markets, and incomplete values remain insufficient rather than being guessed or silently discarded. Resolved PropLine history remains a separate explicit-settlement path.

**Why:** The user intends PropLine to supply supplemental data for most V4-monitored sports, not just basketball. Coverage must expand safely without treating unknown schemas or in-progress/mismatched results as verified player evidence.

**How to apply:** Use the returned catalog and bounded shared request rotation across monitored sports; do not silently skip a sport solely because its schema differs. Require explicit final status and identified player-game rows, then exact event/player/sport/stat identity for grading. Route calls through the shared rate limiter/cache/error envelope; keep movement, EV, projections, payout, freshness, coverage, SGP, and CLV informational.