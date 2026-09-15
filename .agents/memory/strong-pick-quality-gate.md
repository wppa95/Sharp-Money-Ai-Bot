---
name: Strong pick quality gate
description: The quality boundary shared by player-prop alerts and the /picks command.
---

The strongest-pick product path requires complete L5/L10/L15/L20/ L30/season directional history: OVER needs at least 70% in every window, and UNDER needs at most 30% in every window. Historical-average fallback or a single positive window is not strong-pick evidence.

**Why:** Direction-only qualification allowed mediocre rows such as 40–60% MLB Hits histories to appear in both Telegram alerts and `/picks`, especially when downstream intelligence was unavailable.

**How to apply:** Keep this gate in the shared selector used by alert delivery and `/picks`. Preserve raw observations, settlement evidence, and tracking rows even when a candidate is filtered from user-facing picks.