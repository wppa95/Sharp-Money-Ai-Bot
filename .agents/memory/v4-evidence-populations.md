---
name: V4 evidence and reporting populations
description: Boundaries between completed-game evidence, provider observations, and Telegram performance reporting.
---

Completed player game results are the preferred evidence for directional Telegram PLAYs and the only source for L5/L10/L20/L30 hit-rate displays. They may come from persisted ``player_game_results`` or terminal ``PropTrackingLog`` rows (HIT/MISS/PUSH with an actual value). When settled hit-rate data is unavailable, validated historical line evidence can provide a fallback direction from current line versus historical average; raw pending observations remain non-evidence. Each named window requires its full sample size.

For ``/grade``, a source-less PrizePicks outcome may be corroborated by PropLine only when one trusted result matches the exact event, player, sport, stat, actual value, and directional outcome. The line-feed and completed-result ledgers can use different external IDs, so this report-only cross-check does not require those IDs to match. It must not rewrite stored provenance or make a grade learning-eligible.

**Why:** Treating line observations as games inflated history and allowed an apparently supported pick to reach delivery without actual player performance evidence. The bot also needs to avoid suppressing every evidence-backed prop solely because non-MLB settlement providers have not populated hit-rate windows. PrizePicks line IDs and PropLine result IDs may be separate namespaces even when the completed event and player result match exactly.

**How to apply:** Prefer merged/deduped terminal tracking and player-game results for hit rates; never count PENDING rows. If those windows are absent or inconclusive, permit the historical-average fallback only when validation reports supporting evidence. Keep fallback provenance distinct from settled-result evidence in diagnostics and reports. For PrizePicks report reconciliation, allow only trusted PropLine result sources and exact event/player/sport/stat/actual/direction matches; keep the evidence count separate and leave stored grade source and learning state unchanged.