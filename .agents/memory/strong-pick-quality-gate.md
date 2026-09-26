---
name: Strong pick quality gate
description: The quality boundary shared by player-prop alerts and the /picks command.
---

Delivery and analyst views use different eligibility policies. Automatic delivery requires complete L5/L10/L15/L20/L30/season history: OVER at least 70% and UNDER at most 35% OVER-rate in every window. `/picks` retains its separate history-quality behavior, including its optional-sport path; `/slip` remains directional-only.

**Why:** The user explicitly limited the 70%/35% cutoffs to automatic delivery. Applying them to analyst commands would change their existing purpose and filtering behavior.

**How to apply:** Enforce the 70%/35% rule in the live market queue and final Telegram sender, across all supported sports. Keep `/picks` and `/slip` on their own existing rules, and preserve raw observations, settlement evidence, and tracking rows when delivery filters a candidate.