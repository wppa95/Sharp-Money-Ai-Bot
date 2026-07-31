---
name: Provider Abstraction, CLV Pipeline & Dashboard — stable baseline
description: Architecture added for generic prop provider layer, CLV seed pipeline, and performance dashboard. Documents the shape of new ORM models, background jobs, and test coverage.
---

## What was added

### `bot/providers/prop_provider.py`
- `PlayerProp` dataclass — provider-agnostic prop snapshot; `prop_key` tuple = (provider, player, sport, stat)
- `PropProviderBase` ABC — subclass with `provider_name`, `sport_keys`, `fetch_props()`
- `PropComparisonEngine` — line-gap direction rule: provider > sb → UNDER edge; provider < sb → OVER edge. Edge formula uses multiplicative vig removal vs. 50% break-even. Min-edge filter + sorted output.
- **Key test gotcha**: `_fair_probs(-110, -110)` returns exactly 0.5/0.5 → edge = 0.0. Tests that expect `edge > 0` must use asymmetric odds (e.g. +120/-140) not -110/-110.

### `bot/engine/dashboard.py`
- `DashboardEngine.gather(db)` — async classmethod; each sub-query is try/except-wrapped for resilience
- `DashboardReport.to_telegram()` — full HTML; `total_all_alerts` sums all 4 types; win_rate only shown when `n ≥ 5` resolved
- Always returns 7 DailyTrend entries (zeros on empty DB)

### `bot/database.py` additions
- `PropLineHistory` ORM + CRUD methods
- `AlertCLVSeed` ORM — UNIQUE on `(source_table, source_id)` — use `on_conflict_do_nothing` upsert
- `_tier_from_confidence(score)` helper: ≥95=S, ≥85=A, ≥75=B, else PASS
- `seed_clv_from_ev_records()` / `seed_clv_from_ud_snapshots()` — idempotent; safe to call repeatedly
- `_migrate_clv_records()` — adds sport/market_type/alert_type/tier columns to existing clv_records (idempotent)

### `bot/main.py`
- `_clv_seed_job` — runs every 900s (first=120), calls both seed methods, logs only when new seeds created

### `/dashboard` command
- Sends two messages: DashboardEngine report (all-alert stats) + PP live top-pick + resolved tier perf

## Test counts
- `test_prop_provider.py` — 56 tests
- `test_dashboard.py` — 115 tests (uses module-level `_loop = asyncio.new_event_loop()` + `_run()` helper; never `asyncio.get_event_loop()` which breaks on 3.11 after loop teardown)
- `test_alert_clv_seed.py` — 84 tests
- Total suite: 1323 tests, 0 failures as of July 31 2026

**Why:** Any future test file using aiosqlite in Python 3.11 must use a module-level `asyncio.new_event_loop()` and call `asyncio.set_event_loop(_loop)` at module level — `asyncio.get_event_loop()` fails after any other test file has closed a loop.
