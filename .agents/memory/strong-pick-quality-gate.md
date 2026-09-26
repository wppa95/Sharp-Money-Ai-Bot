---
name: Strong pick quality gate
description: The quality boundary shared by player-prop alerts and the /picks command.
---

Automatic delivery, `/picks`, and `/slip` share the same complete L5/L10/L15/L20/L30/season history gate: OVER at least 70% and UNDER at most 35% OVER-rate in every window. No sport bypasses this user-facing pick gate.

**Why:** The user clarified that these cutoffs apply to manual picks and slips as well as automatic delivery. Earlier history-optional shortcuts could show directional picks without meeting the quality thresholds.

**How to apply:** Use the shared qualifying-history helper in the live market queue, final Telegram sender, `/picks`, and `/slip`. Preserve broad sport scope and retain raw observations, settlement evidence, and tracking rows when a candidate is filtered.