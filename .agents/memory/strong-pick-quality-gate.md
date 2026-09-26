---
name: Strong pick quality gate
description: The quality boundary shared by player-prop alerts and the /picks command.
---

The strongest-pick product path requires complete L5/L10/L15/L20/L30/season directional history: OVER needs at least 70% in every window, and UNDER needs at most 35% OVER-rate in every window. Apply this across sports and user-facing pick surfaces; missing windows do not pass.

**Why:** The user explicitly set the cutoff at 70% or higher for OVER and 35% or lower for UNDER, with nothing between. An optional-sport shortcut had allowed directional picks without proving this quality.

**How to apply:** Enforce the same complete-window rule in the live market queue, final Telegram delivery, `/picks`, and `/slip`. Preserve broad sport scope and retain raw observations, settlement evidence, and tracking rows when a candidate is filtered.