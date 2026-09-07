---
name: PropLine history pacing
description: Which PropLine endpoints are safe inside the two-minute player-history collector.
---

The scheduled player-history collector must use PropLine's fast market-specific resolved-history endpoint. Raw player-game and event-stat endpoints share slower endpoint pacing and can wait beyond the collector's per-target budget.

**Why:** Calling the raw endpoints first caused the five-second target timeout to fire before resolved history, making the collector appear healthy while silently producing no PropLine coverage.

**How to apply:** Keep raw games and event stats on supplemental/backfill paths with their own pacing. When diagnosing coverage, report both the sports actually attempted and the returned status; an empty usable-row report alone is ambiguous.