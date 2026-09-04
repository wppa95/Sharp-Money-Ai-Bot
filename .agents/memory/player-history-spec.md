---
name: Player history job spec (authoritative)
description: Final confirmed spec for player_history_collector_job — full active-book collection on every cadence.
---

## Authoritative Spec

- **Supported sports only**: skip sports without a working `PlayerStatsProvider` path in both primary and fallback paths.
- **Player count: unlimited** — no `[:100]` or `[:1000]` cap on target building. Full active Tier-1 snapshot is used.
- **API calls: full active book per cycle** — every active supported target is attempted in the same cadence; `_API_CALL_TARGET` is only a compatibility/configuration floor and never truncates the target list.
- **Cadence: 120 seconds**, `max_instances=1`, `misfire_grace_time=120`.
- **Path**: `get_active_underdog_snapshot_per_prop()` → filter Tier-2 → `provider.fetch_results()` → `db.upsert_player_result()`.
- **Fallback**: `get_latest_props_for_provider("Underdog", since_hours=48)` with same Tier-2 filter (no count cap on iteration either).
- **Cadence policy**: fetch the full active book with bounded concurrency and await every scheduled target; no result queue, rotation cursor, or deferred target list is carried forward.

## Why

The user requires complete active-book coverage (currently approximately 5,200 props) every two-minute cadence. A cap or rotating cursor would leave current observations stale and make coverage depend on prior scheduler runs.

## How to Apply

Never add a player-count or API-call slice to the active target list or fetch loop. Keep provider concurrency and write batches bounded so the full book can complete without unbounded requests or SQLite transaction churn.

**Cadence rule:** The history collector is a fast monitor, not a backlog processor. Keep provider work concurrent so the full active book can complete inside the two-minute display/monitoring window; use bounded writer waits for lock diagnostics rather than silently dropping targets.