---
name: Provider abstraction, CLV pipeline, and calibration system
description: Frozen stable v1.2 baseline — all modules, key decisions, and test count as of July 31 2026.
---

## Frozen baseline — v1.2 (July 31 2026)

**Test count: 1528 tests, 0 failures.**

## Module inventory

| Module | Status | Key note |
|---|---|---|
| `providers/prop_provider.py` | ✅ stable | `PlayerProp`, `PropProviderBase` ABC, `PropComparisonEngine` |
| `providers/prizepicks.py` | ✅ stable | `PrizePicksProvider` (DataDome blocked) + `PrizePicksManualProvider` |
| `providers/underdog_provider.py` | ✅ stable | `UnderdogProvider` wrapping `UnderdogSnapshotRecord` rows |
| `engine/dashboard.py` | ✅ stable | `DashboardEngine`, `DashboardReport.to_telegram()` |
| `engine/calibration.py` | ✅ NEW | `CalibrationEngine`, `CalibrationReport` — detection vs recommendation split |
| `database.py` — PropLineHistory | ✅ NEW lifecycle | `first_seen`, `last_seen`, `change_count`, `prev_line`, `removed` columns (migration) |
| `database.py` — upsert_prop_line_lifecycle | ✅ NEW | Returns `(row, event)` where event ∈ ADDED/CHANGED/REMOVED/RETURNED/UNCHANGED |
| `database.py` — sync_underdog…` | ✅ NEW lifecycle | Lifecycle-aware upsert; wired into `underdog_job` after each fetch cycle |
| `database.py` — CLV stats/harvest | ✅ NEW | `get_clv_stats_by_dimension`, `get_clv_seeds_by_tier_stats`, `mark_clv_seed_expired` |
| `database.py` — get_pending_clv_seeds | ✅ UPDATED | Now includes game_time=None seeds after 24h stale threshold |
| `main.py` — `_clv_harvest_job` | ✅ NEW | Every 3600s, first=300; processes pending seeds, expires stale ones |
| `commands.py` — `/calibration` | ✅ NEW | Calls CalibrationEngine, returns full report |
| `commands.py` — `/pp_import` | ✅ NEW | Pipe-delimited multi-line PP import; lifecycle events per prop |
| `commands.py` — `/status` | ✅ UPDATED | Now shows PropLineHistory count with provider breakdown |

## Key design decisions

### Calibration — detection ≠ recommendation
**Rule:** Line-movement detection accuracy and betting recommendation accuracy are tracked SEPARATELY. A sharp move (correctly detected) does NOT automatically mean a profitable bet. The two are shown in separate sections of `/calibration`.

**Why:** Conflating them would mislead: you can detect that a move happened correctly (the line kept moving) but the market may have already priced in the fair value by the time a bet executes.

**How to apply:** Any new tracking that tries to measure "was the alert correct?" must choose which question it's answering. Never combine them into a single accuracy score.

### PropLineHistory lifecycle approach
**Rule:** `upsert_prop_line_lifecycle()` is the single write path for any provider inserting into PropLineHistory. Direct `save_prop_line_history()` may still be used for bulk bridges but lifecycle tracking requires the upsert method.

**Why:** Each upsert computes ADDED/CHANGED/REMOVED/RETURNED/UNCHANGED by comparing against the most-recent row for that (provider, player, sport, stat). Without this, lifecycle events are lost.

**How to apply:** `/pp_import` uses `upsert_prop_line_lifecycle()`. The Underdog sync uses the lifecycle-aware `sync_underdog_snapshots_to_prop_history()`. Future providers should use the same upsert path.

### CLV seeds with game_time=None
**Rule:** EV records currently don't store `game_time`, so their seeds get `game_time=None`. These seeds appear in `get_pending_clv_seeds()` only after `alerted_at < now - 24h` (stale threshold). The harvest job expires them immediately (no closing odds available without game_time).

**Why:** The original `get_pending_clv_seeds()` required `game_time.isnot(None)`, which meant EV seeds were never processed. The stale-hours fallback allows the harvest job to clean them up.

**How to apply:** When sportsbook polling is re-enabled and EV records start storing `game_time`, the harvest job will automatically use real closing odds instead of expiring.

### OddsRecord column naming
**Rule:** `OddsRecord` uses `recorded_at` (not `fetched_at`) for its timestamp column.

**Why:** Discovered during harvest job implementation — `get_last_odds_for_event()` must ORDER BY `OddsRecord.recorded_at`.

### PropLineHistory lifecycle columns — migration-based
**Rule:** The lifecycle columns (`first_seen`, `last_seen`, `change_count`, `prev_line`, `removed`) are defined in BOTH the ORM model AND a migration method (`_migrate_prop_line_history()`). The ORM definition is the authoritative source for new DBs; the migration handles existing databases.

**Why:** Without ORM definition, SQLAlchemy's `sa_update(...).values(last_seen=..., change_count=...)` raises `CompileError: Unconsumed column names`.
