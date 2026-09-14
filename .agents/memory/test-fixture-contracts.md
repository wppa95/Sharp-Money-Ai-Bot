---
name: Test fixture contracts
description: Non-obvious fixture rules for time-sensitive feeds, active-sport pacing, and strict delivery history.
---

Time-sensitive provider tests must control the current-season boundary and avoid live network dependence; active-sport pacing tests must patch the config used by the lazy priority resolver; strict directional-delivery fixtures need all six history windows, including L15.

**Why:** Wall-clock changes and configuration-driven behavior can make otherwise valid tests disagree with the production contract or reach live provider data unexpectedly.

**How to apply:** When adding or repairing tests, seed current-season cache entries or mock provider fetches, set the active sports explicitly for priority assertions, and populate L5/L10/L15/L20/L30/Season before expecting Telegram delivery.