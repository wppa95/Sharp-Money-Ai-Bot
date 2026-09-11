---
name: PropLine history pacing
description: Which PropLine endpoints are safe inside the two-minute player-history collector.
---

The scheduled player-history collector must use PropLine's fast market-specific resolved-history endpoint. Raw player-game and event-stat endpoints share slower endpoint pacing and can wait beyond the collector's per-target budget.

**Why:** Calling the raw endpoints first caused the five-second target timeout to fire before resolved history, making the collector appear healthy while silently producing no PropLine coverage.

**How to apply:** Keep raw games and event stats on supplemental/backfill paths with their own pacing. The scheduled PropLine history cursor rotates across every non-football target, including MLB; gridiron sports remain excluded. When diagnosing coverage, report both the sports actually attempted and the returned status; an empty usable-row report alone is ambiguous.

PropLine's documented graded esports player coverage is CS2 map 1-2 kills and headshots only; Dota 2, LoL, and Valorant remain odds-only for player props. Sport-aware market routing is required because several non-MLB markets use provider-specific keys.

**Why:** Generic esports mappings previously made unsupported titles look eligible and routed CS2 lines to obsolete market names.

**How to apply:** Use the official PropLine market catalog for NBA, NHL, Tennis, UFC, soccer, and CS2 mappings; return unsupported rather than guessing when the catalog only documents game-line coverage.