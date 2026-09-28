---
name: Strong pick quality gate
description: The quality boundary shared by player-prop alerts and the /picks command.
---

MLB, NBA, NFL, WNBA, and NHL require all six L5/L10/L15/L20/L30/season windows, each with at least five completed results: OVER must be at least 75%, and UNDER must be at most 25% OVER-rate, in every window. Every other named sport qualifies with an explicit OVER/UNDER decision and confidence of at least 70; completed-result history is not required.

**Why:** The user selected a split policy: main leagues must show strong completed-result evidence, while other sports may qualify on a strong directional confidence score.

**How to apply:** Use the shared sport-aware qualification helper in automatic delivery, the final Telegram sender, `/picks`, and `/slip`. Persist and consume the actual directional decision confidence; never substitute a market-quality or bet-quality score. Do not add global or per-sport caps; retain raw observations, settlement evidence, and tracking rows when a candidate is filtered. Missing optional-sport history must remain unavailable in displays rather than being replaced with synthetic results.