---
name: Auto-sent slip outcomes
description: Outcome and provenance rules for reporting automatically sent player-prop combinations.
---

Automatic slip results are summaries of the saved legs, not official wager or payout records. Grade each leg from an explicitly settled PropLine result or a final PropLine player-game stat matched to the saved event, player, sport, and stat; player-game stats never use date-only matching. Grade against the saved send-time line and direction. A combination is HIT only when every leg hits, MISS when any leg misses, and PUSH when no leg misses but at least one leg pushes. Unresolved legs stay pending; explicit provider voids remain distinct from pushes. Older sends without saved leg snapshots cannot be reconstructed unless the exact same combination is still available.

**Why:** The bot sends recommendations, not wagers. Final numeric stats can determine a saved directional leg, but they do not establish provider voids or platform payout behavior.

**How to apply:** Keep recent leg detail in `/slip journal` and aggregate counts separate from manual-slip ROI in `/slipstats`. Require explicit final status and exact identity for player-game stats; do not use line movement, generic history, or guessed event matches.