---
name: V4 evidence and reporting populations
description: Boundaries between completed-game evidence, provider observations, and Telegram performance reporting.
---

Completed player game results are the preferred evidence for directional Telegram PLAYs and the only source for L5/L10/L20/L30 hit-rate displays. They may come from persisted ``player_game_results`` or terminal ``PropTrackingLog`` rows (HIT/MISS/PUSH with an actual value). When settled hit-rate data is unavailable, validated historical line evidence can provide a fallback direction from current line versus historical average; raw pending observations remain non-evidence. Each named window requires its full sample size.

**Why:** Treating line observations as games inflated history and allowed an apparently supported pick to reach delivery without actual player performance evidence. The bot also needs to avoid suppressing every evidence-backed prop solely because non-MLB settlement providers have not populated hit-rate windows.

**How to apply:** Prefer merged/deduped terminal tracking and player-game results for hit rates; never count PENDING rows. If those windows are absent or inconclusive, permit the historical-average fallback only when validation reports supporting evidence. Keep fallback provenance distinct from settled-result evidence in diagnostics and reports.