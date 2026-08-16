---
name: Player history job spec (authoritative)
description: Final confirmed spec for player_history_collector_job — Tier-1 only, unlimited player count, 250-call cap.
---

## Authoritative Spec

- **Tier-1 only**: filter `_TIER2_SPORTS = {"MLB","NBA","NFL"}` from target list in both primary and fallback paths.
- **Player count: unlimited** — no `[:100]` or `[:1000]` cap on target building. Full active Tier-1 snapshot is used.
- **API call cap: 250 per cycle** — `targets[:_API_CALL_TARGET]` where `_API_CALL_TARGET = 250`. This limits API spend, not player count.
- **Cadence: 120 seconds**, `max_instances=1`, `misfire_grace_time=120`.
- **Path**: `get_active_underdog_snapshot_per_prop()` → filter Tier-2 → `provider.fetch_results()` → `db.upsert_player_result()`.
- **Fallback**: `get_latest_props_for_provider("Underdog", since_hours=48)` with same Tier-2 filter (no count cap on iteration either).

## Why

250 is an API-call budget, not a player-count limit. Maximizing Tier-1 player coverage is the goal; the cap only protects credit spend per 2-min cycle.

## How to Apply

Never add a player-count slice (`[:N]`) to the target-building loop. Only `targets[:_API_CALL_TARGET]` in the fetch loop.
