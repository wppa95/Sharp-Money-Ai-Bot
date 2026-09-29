---
name: Underdog grading source
description: Project rule for grading picks sourced from Underdog.
---

Use official Underdog settlement results for native grading. In this project, the wired Underdog-bookmaker event-settlement lookup is served through PropLine; there is no separate Underdog settlement client. Do not claim or invent an independent source. Final PropLine player-game stats may also support directional HIT/MISS/PUSH grades when the saved event ID and the result event ID match exactly and player, sport, and stat match. Never use those stats for VOID, or use projected-line movement, disappearance, or unrelated history. A numeric zero is not proof of a void; only an explicit provider settlement can mark one.

Persist the result-row source on each terminal opportunity grade. Results without verifiable provenance, including legacy rows whose source was never stored, are unverified and must not enter hit-rate or adaptive-learning samples. Hit rate is HIT/(HIT+MISS); PUSH is reported separately.

**Why:** Exact final player-game stats are distinct from explicit settlement outcomes but can resolve a saved directional line without inventing a result. The current event-settlement path is still backed by PropLine, so it cannot recover an outcome when that feed returns no settlement. Zero is a valid completed stat, while only an explicit provider settlement establishes a void.

**How to apply:** Persist the exact result source and event identity. Never date-match `propline_player_games`; keep explicit event settlements higher priority, and keep the existing football-data exclusion unless a separate grading-only boundary is approved. If a direct Underdog feed is requested, require its documented authorized source before adding an adapter. Never silently upgrade legacy grades.