---
name: V4 evidence and reporting populations
description: Boundaries between completed-game evidence, provider observations, and Telegram performance reporting.
---

Completed player game results are the only evidence that can qualify a directional Telegram PLAY or populate L5/L10/L20/L30 hit-rate displays. Those results may come from persisted ``player_game_results`` or terminal ``PropTrackingLog`` rows (HIT/MISS/PUSH with an actual value); raw pending observations are monitoring context, not games. Each named window requires its full sample size.

**Why:** Treating line observations as games inflated history and allowed an apparently supported pick to reach delivery without actual player performance evidence. The bot also has a separate durable neutral-tracking population, so settled non-MLB rows must not be stranded outside the hit-rate loader.

**How to apply:** Merge/dedupe terminal tracking results with player-game results before computing hit rates; never count PENDING rows. Keep proxy/provider observations available for monitoring and scoring context, and label validation evidence separately from settled-result evidence in diagnostics. Reports must label delivery-independent historical grading separately from records marked as delivered Telegram PLAYs.