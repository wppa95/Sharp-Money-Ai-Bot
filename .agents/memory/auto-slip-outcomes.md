---
name: Auto-sent slip outcomes
description: Outcome and provenance rules for reporting automatically sent player-prop combinations.
---

Automatic slip results are summaries of the saved legs, not official wager or payout records. Grade each leg only from an explicitly settled PropLine result matched to its saved event identity and send-time line/direction. A combination is HIT only when every leg hits, MISS when any leg misses, and PUSH when no leg misses but at least one leg pushes. Unresolved legs stay pending; explicit provider voids remain distinct from pushes. Older sends without saved leg snapshots cannot be reconstructed unless the exact same combination is still available.

**Why:** The bot sends recommendations, not wagers. Treating a combined result as a cash settlement would claim platform payout behavior the bot does not observe.

**How to apply:** Keep recent leg detail in `/slip journal` and aggregate counts separate from manual-slip ROI in `/slipstats`. Do not add polling or use line movement, generic player history, or guessed event matches to settle auto slips.