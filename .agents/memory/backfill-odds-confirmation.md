---
name: Backfill grading
description: /backfill command and direction-aware pending-prop grading
---

`cmd_backfill` in `bot/commands.py` fetches supported player stats for pending
opportunities (cutoff_hours=4), upserts `player_game_results`, and grades
direction-aware HIT/MISS/PUSH outcomes. It is registered as `/backfill` and
reuses `get_pending_opportunities(cutoff_hours=4)`.

It does not fetch future-game stats. Missing or unresolved official results
remain unavailable rather than being inferred from projected lines or removals.