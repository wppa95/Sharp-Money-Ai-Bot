---
name: Evidence-only delivery ranking
description: Player-prop delivery ordering must use settled directional hit-rate evidence, never confidence or quality labels.
---

Directional delivery ranking uses the primary settled player-result window: higher OVER-perspective hit rate for OVER picks and lower OVER-perspective hit rate for UNDER picks. Evidence/sample availability may break ties; confidence, tiers, quality labels, and market-movement bonuses must not.

**Why:** The user requires Telegram picks to be selected from historical evidence rather than model confidence.

**How to apply:** Keep confidence out of every candidate-selection and delivery-ordering key, including fallback tie-breaks.