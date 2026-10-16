---
name: Auto-sent slip outcomes
description: Historical-only outcome and provenance rules for previously sent combinations.
---

New multi-pick generation and delivery are disabled by the user's equal-primary-platform correction. These rules apply only to previously saved combinations.

Historical automatic slip results are summaries of the saved legs, not official wager or payout records. Grade each leg from an explicitly settled PropLine result or a final PropLine player-game stat matched to the saved event, player, sport, and stat. A different event-ID namespace is acceptable only when the shared resolver proves a unique player/stat/date/team/opponent match; player-game stats never use date-only matching. Grade against the saved send-time line and direction. A combination is HIT only when every leg hits, MISS when any leg misses, and PUSH when no leg misses but at least one leg pushes. Unresolved legs stay pending; explicit provider voids remain distinct from pushes. Older sends without saved leg snapshots cannot be reconstructed unless the exact same combination is still available.

**Why:** The bot sends recommendations, not wagers. Final numeric stats can determine a saved directional leg, but they do not establish provider voids or platform payout behavior. Strict team/opponent reconciliation can safely bridge provider event-ID namespaces without treating a date-only player match as proof.

**How to apply:** Keep recent leg detail in `/slip journal` and aggregate counts separate from manual-slip ROI in `/slipstats`. Require an exact event ID or the shared resolver's validated unique fingerprint for player-game stats; preserve source provenance and both event IDs. Do not use line movement, generic history, date-only, or guessed event matches.