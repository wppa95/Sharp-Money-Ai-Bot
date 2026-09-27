---
name: Strong pick quality gate
description: The quality boundary shared by player-prop alerts and the /picks command.
---

Automatic delivery, `/picks`, and `/slip` share the same complete L5/L10/L15/L20/L30/season history gate: OVER at least 75% and UNDER at most 25% OVER-rate in every window, with at least five completed results. Apply the same thresholds across all sports.

**Why:** On 2026-09-27, the user said alert volume was too high and chose the moderate 75%/25% cutoff to reduce volume while preserving evidence-based selection.

**How to apply:** Use the shared qualifying-history helper in the live market queue, final Telegram sender, `/picks`, and `/slip`. Do not add global or per-sport caps; retain raw observations, settlement evidence, and tracking rows when a candidate is filtered.