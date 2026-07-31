# Sharp Money +EV Detection Bot — v1.0

A professional sports-betting intelligence Telegram bot that monitors live sportsbook odds, detects steam moves and positive-expected-value opportunities, and delivers richly formatted alerts automatically.

---

## Quick start

- **Run:** `Sharp Money Bot` workflow (or `python bot/main.py`)
- **Required secret:** `TELEGRAM_TOKEN` (set in Replit Secrets)
- **Optional secret:** `ODDS_API_KEY` — The Odds API key for live data (without it the polling jobs no-op gracefully)
- The bot polls Telegram continuously; keep the workflow running

---

## Stack

| Layer | Library |
|---|---|
| Bot framework | python-telegram-bot 22 (async, APScheduler job queue) |
| Database | SQLAlchemy 2 + aiosqlite (async SQLite) |
| HTTP client | aiohttp (live odds fetch) |
| Runtime | Python 3.11 |
| Config | python-dotenv (local dev) |
| Tests | pytest + pytest-asyncio |

---

## File structure

```
bot/
├── main.py              # Entry point — Application lifecycle, background jobs
├── config.py            # All settings (env vars + typed properties)
├── models.py            # Shared dataclasses: OddsLine, EVOpportunity, SteamAlert, …
├── database.py          # Async SQLAlchemy ORM: OddsRecord, EVRecord, SteamRecord
├── alerts.py            # Alert formatting, risk factors, AlertDelivery pipeline
├── commands.py          # Telegram command handlers (/start /help /status /analyze /steam /ev)
├── engine/              # Analysis engine package (see below)
│   ├── __init__.py      # Re-exports AnalysisEngine
│   ├── analysis.py      # Orchestrator: VigRemover, EVCalculator, SteamDetector, AIConfidenceScorer
│   ├── fair_probability.py  # Devig methods: multiplicative, additive, power, odds-ratio
│   ├── ev.py            # EVResult, kelly_fraction, compute_ev, compute_ev_batch
│   ├── steam.py         # SteamMovement, SteamResult, compute_steam, compute_steam_simple
│   └── confidence.py    # ConfidenceResult, _ConfidenceScorer, compute_confidence
├── tests/
│   ├── __init__.py
│   └── test_pipeline.py # Full pipeline test suite (76 tests, all passing)
└── data/                # Auto-created at runtime — sharp_money.db lives here
```

---

## Completed modules — v1.0 baseline

### 1. Odds pipeline (`engine/analysis.py`, `main.py`)
- `fetch_live_odds(sport)` — async call to The Odds API (h2h, spreads, totals)
- `_poll_odds_job` — APScheduler job, runs every `ODDS_POLL_INTERVAL` seconds (default 60s)
- Stores each fetched line as an `OddsRecord` in SQLite with timestamp

### 2. Fair probability engine (`engine/fair_probability.py`, `engine/analysis.py → VigRemover`)
- Four devig methods: **multiplicative** (default), additive, power-iteration, odds-ratio
- `american_to_implied`, `implied_to_american`, `overround`, `vig_percentage`, `hold_percentage`
- `compute_fair_probability(odds_list, method)` — works for two-way and multi-way markets
- `compute_fair_market(lines)` — batch fair-prob across a full market snapshot
- `no_vig_line` — returns the fair American odds for both sides of a two-way market

### 3. EV engine (`engine/ev.py`, `engine/analysis.py → EVCalculator`)
- `expected_value_pct(fair_probability, market_odds)` — core EV formula
- `kelly_fraction` / half-Kelly sizing
- `edge_pct`, `break_even_probability`, `fair_vs_market_diff`
- `EVResult` — full result object with rating, confidence flags, sizing
- `compute_ev_batch` — vectorised over a list of lines
- `EVRating` enum: STRONG / MODERATE / MARGINAL / NEGATIVE / AVOID

### 4. Steam engine (`engine/steam.py`, `engine/analysis.py → SteamDetector`)
- `compute_steam(movements)` — detects cross-book consensus line movement
- `compute_steam_simple(movement, books_moved)` — lightweight scorer used in background jobs
- Steam score 0–100 from: odds change magnitude (+40 max), books moved (+30 max), line movement (+20 max, placeholder +10)
- `SteamTier`: ELITE / STRONG / MODERATE / WEAK
- `MovementDirection`: UP / DOWN / FLAT / MIXED

### 5. Confidence engine (`engine/confidence.py`, `engine/analysis.py → AIConfidenceScorer`)
Five live signals, 100-pt ceiling:

| Signal | Max pts | Source |
|---|---|---|
| EV Edge | 25 | `ev_percentage` tier (≥3/5/7/10%) |
| Steam Score | 25 | `steam_score × 0.25` |
| Sharp Book Presence | 20 | `books_moved ∩ config.sharp_books` (≥1/2/3) |
| Line Shopping Efficiency | 20 | `\|side_a_odds − side_b_odds\|` (≥5/10/20) |
| Market Tightness | 10 | `vig_pct` (≤0 / ≤3 / ≤6) |

Star bands (from confidence score):
- 90–100 → ★★★★★ Elite
- 75–89  → ★★★★☆ Strong
- 60–74  → ★★★☆☆ Good
- 40–59  → ★★☆☆☆ Marginal
- 0–39   → ★☆☆☆☆ Weak

### 6. Analysis engine (`engine/analysis.py → AnalysisEngine`)
Single entry-point that orchestrates all sub-engines:
```
analyze_line(sport, market_type, event, selection, side_a_odds, side_b_odds, …)
  → EVOpportunity(ev_result, steam_alert, ai_confidence, recommendation, stars, reason_codes)
```
- `Recommendation`: STRONG_BET / BET / LEAN / PASS / FADE
- Requires both EV ≥ threshold AND confidence ≥ threshold to recommend betting
- Also owns `fetch_live_odds()` and `fetch_prizepicks_lines()` (stub)

### 7. Risk system (`alerts.py → compute_ev_risk_factors / compute_steam_risk_factors`)
- `RiskFactor(level, description, icon)` — HIGH 🔴 / MEDIUM 🟡 / LOW 🔵
- EV risk checks: vig quality, steam confirmation, odds extremity, AI confidence, edge thinness, Kelly sizing
- Steam risk checks: book count, movement size, sharp book presence
- `identify_sharp_books(books)` — filters against `config.sharp_books` (Pinnacle, Circa, Bookmaker.eu, Heritage, BetOnline, CRIS, 5Dimes, …)

### 8. Alert delivery (`alerts.py → AlertDelivery`)
Full pipeline in a single call — no inline logic needed in background jobs:
```
AlertDelivery(db, bot, chat_ids, min_ev, min_confidence, min_steam, ev_dedup_window, steam_dedup_window)
  .deliver_ev(opp)     → DeliveryResult(filtered, filtered_reason, sent, deduped, recipients_sent, …)
  .deliver_steam(alert) → DeliveryResult(…)
```
Steps: **filter** → **dedup** (DB-backed, configurable window) → **format** (rich HTML, risk factors, sharp books) → **send** → **log** (`EVRecord` / `SteamRecord`)

`format_ev_alert` HTML fields: alert type, sport/league, event, player/market, sportsbook, offered odds, fair odds, fair probability, EV%, Kelly, steam score, books moving, sharp books, AI confidence, star rating, recommendation, risk factors.

### 9. Telegram integration (`main.py`, `commands.py`)
Background jobs:
- `_poll_odds_job` (every 60s) — fetch → store → detect +EV → `AlertDelivery.deliver_ev()`
- `_steam_check_job` (every 30s) — window query → group by book → detect steam → `AlertDelivery.deliver_steam()`

Commands:
| Command | What it does |
|---|---|
| `/start` | Welcome message with feature overview |
| `/help` | Full command reference |
| `/status` | Uptime, DB record counts, config summary |
| `/analyze [sport] [selection] [odds] [opp_odds]` | On-demand vig removal + EV + Kelly |
| `/steam` | Latest sharp moves from DB |
| `/ev` | Latest +EV opportunities from DB |

### 10. Multi-Platform Market Engine (`bot/connectors/`, `bot/engine/consensus.py`, `bot/engine/clv.py`, `bot/market_engine.py`)

Modular connector framework + cross-book analysis engines:

**Connectors** (`bot/connectors/`):
- `BaseConnector` — abstract interface: `fetch()`, `health_check()`, `enabled`, `is_pickem`
- `DraftKingsConnector` — DraftKings odds via The Odds API, tracks opening vs. current per session
- `FanDuelConnector` — FanDuel odds via The Odds API, same normalization layer as DraftKings
- `UnderdogConnector` — Underdog Fantasy pick'em projections; detects line movement, value changes, removals
- `ConnectorRegistry` — manages all connectors, fetches in parallel, routes sportsbook vs. pick'em
- All output normalized to `MarketSnapshot` (sport, event, market_type, selection, odds, sportsbook, timestamp, game_time, opening_odds, is_pickem)

**Consensus Engine** (`bot/engine/consensus.py`):
- `compute_consensus(snapshots)` → `list[ConsensusResult]` — groups by market key, computes median odds/line across books
- `find_inefficiencies(snapshots)` → books deviating beyond threshold flagged as `MarketInefficiency`
- `build_multi_book_steam_inputs(snapshots)` → prepares per-market movement data for steam detection

**CLV Engine** (`bot/engine/clv.py`):
- `compute_clv(bet_odds, closing_odds, ...)` → `CLVResult` — with de-vigged fair probability comparison when counterpart odds available
- `build_clv_opportunity(snapshot, consensus_snaps)` → `CLVOpportunity` — flags when current price leads projected close
- CLV grades: Excellent (≥5%) / Strong (≥2%) / Neutral / Weak / Bad

**Market Engine Jobs** (`bot/market_engine.py`):
- `connector_poll_job` — fetches all sportsbook connectors, stores `MarketSnapshotRecord`, updates in-memory cache
- `consensus_check_job` — runs consensus engine, sends `MarketInefficiency` alerts and multi-book steam alerts through `AlertDelivery`
- `clv_check_job` — detects CLV leads, alerts when current price > projected close by `MIN_CLV_LEAD` cents
- `underdog_job` — fetches Underdog projections, alerts on line changes and removed props

**New alert types**: `MULTI_BOOK_STEAM`, `MARKET_INEFFICIENCY`, `CLV_OPPORTUNITY`, `UNDERDOG_LINE_CHANGE`, `UNDERDOG_REMOVED`  
**New commands**: `/market` (cross-book consensus), `/clv` (CLV performance history)  
**Pick'em isolation**: Underdog/PrizePicks output stays in pick'em domain — never mixed into sportsbook moneyline analysis

### 11. Provider Abstraction Layer (`bot/providers/prop_provider.py`)

Normalised, provider-agnostic player-prop model so PrizePicks (DataDome-protected) can be plugged in later without touching any alert or DB code:

- **`PlayerProp`** — normalised dataclass (`provider`, `sport`, `player_name`, `team`, `stat_type`, `line_value`, `game_time`, `external_id`, `game_id`, `fetched_at`). `prop_key` tuple uniquely identifies a (provider, player, sport, stat) combination.
- **`PropProviderBase`** — abstract base class. Subclasses must implement `provider_name`, `sport_keys`, `fetch_props()`. Default `normalize_stat()` lower-strips; `is_available()` returns `True`.
- **`PropComparison`** — result of comparing a pick'em prop to a sportsbook line. Fields: `sb_line`, `sb_over_odds`, `sb_under_odds`, `fair_prob_over/under` (multiplicative vig removal), `edge_over/under` (vs. 50% break-even), `best_side`, `best_edge`, `sportsbook`, `detected_at`. Properties: `has_edge`, `line_diff`, passthrough player info.
- **`PropComparisonEngine`** — `compare()` / `compare_many()` / `filter_edges()`. Direction rule: provider_line > sb_line → UNDER edge; provider_line < sb_line → OVER edge. `filter_edges()` returns sorted-best-first list above `min_edge_pct` threshold.

### 12. Performance Dashboard Engine (`bot/engine/dashboard.py`)

Full cross-alert-type aggregation engine powering the `/dashboard` command:

- **`DashboardReport`** — dataclass with `total_all_alerts`, `avg_ev_pct`, `avg_clv_pct`, `clv_beat_close_rate`, `ud_tier_breakdown`, `by_sport` (`list[SportPerf]`), `by_market` (`list[MarketPerf]`), `daily_trend` (`list[DailyTrend]`, always 7 days), `best_sport`, `worst_sport`, `best_market`. `to_telegram()` renders full HTML.
- **`DashboardEngine.gather(db)`** — async class method. Independently queries ev_records, steam_records, underdog_snapshots, pp_edge_records, clv_records. Each sub-query is try/except-wrapped so partial DB failures never break the dashboard.
- **`TierPerf`** — tier breakdown with `tier_emoji` property (🔥/🟢/🟡/⚪).
- Win-rate only shown when `n ≥ 5` resolved records to avoid misleading small-sample stats.

### 13. CLV Seed Pipeline (`bot/database.py`)

Infrastructure for closing-line value tracking across all alert types (EV, Underdog, PP):

- **`PropLineHistory`** ORM model — provider-agnostic prop snapshot table (`provider`, `sport`, `player_name`, `team`, `stat_type`, `line_value`, `game_time`, `external_id`, `game_id`, `fetched_at`). DB methods: `save_prop_line_history()`, `save_prop_line_history_bulk()`, `get_prop_line_history()`, `get_latest_props_for_provider()`, `count_prop_line_history()`.
- **`AlertCLVSeed`** ORM model — CLV seed table (`source_table`, `source_id`, `alert_type`, `sport`, `market_type`, `event`, `selection`, `bet_odds`, `counterpart_odds`, `tier`, `game_time`, `alerted_at`, `clv_pct`, `clv_computed`). UNIQUE on `(source_table, source_id)` — duplicate-safe. DB methods: `save_alert_clv_seed()`, `get_pending_clv_seeds()`, `count_pending_clv_seeds()`, `mark_clv_seed_computed()`, `get_clv_seed_for_source()`.
- **`seed_clv_from_ev_records()`** — scans alerted EVRecords not yet seeded, creates AlertCLVSeed entries. Idempotent.
- **`seed_clv_from_ud_snapshots()`** — same for Underdog snapshots. Idempotent.
- **`_tier_from_confidence(score)`** — maps 0–100 ai_confidence to S/A/B/PASS tier label.
- **`_clv_seed_job`** background task — runs every 15 min; calls both seed methods; logs count of new seeds only.

### 15. PrizePicks concrete provider (`bot/providers/prizepicks.py`)

Implements the roadmap-specified `providers/prizepicks.py` file — a concrete `PropProviderBase` that normalises PrizePicks data into the shared `PlayerProp` model:

- **`PrizePicksProvider`** — live provider (wraps `PrizePicksClient` from `bot/prizepicks.py`). `is_available()` returns `False` until DataDome is resolved. `fetch_props()` raises `NotImplementedError` to make the limitation explicit rather than silently failing.
- **`PrizePicksManualProvider`** — manual/test-feed provider. Accepts raw `dict` records or `PrizePicksLine` objects. Supports `from_dicts(data)` class-method for JSON-friendly ingestion. Handles datetime parsing, stat normalisation, and invalid-value defaults. `is_available()` = True when data is loaded.
- **`pp_line_to_player_prop(ln)`** — boundary adapter: PrizePicksLine fields → canonical PlayerProp. Only place in the codebase where PrizePicks-specific field names appear.
- **`_normalize_stat(raw)`** — shared stat normalisation map (pts→points, reb→rebounds, hr→home runs, etc.).

### 16. Underdog concrete provider + bridge (`bot/providers/underdog_provider.py`)

Maps Underdog data into the same normalized model as PrizePicks so both feed a shared `PropLineHistory`:

- **`UnderdogProvider`** — concrete `PropProviderBase` wrapping `UnderdogSnapshotRecord` rows. Skips `removed=True` props. Applies optional sport filter. Used by the bot's Underdog job to feed PropLineHistory.
- **`ud_snapshot_to_player_prop(snap)`** — adapter: UnderdogSnapshotRecord → PlayerProp. Handles None fields, stat normalisation.
- **`Database.sync_underdog_snapshots_to_prop_history()`** — bridge method. Reads recent non-removed Underdog snapshot records and bulk-inserts them into `PropLineHistory` (provider="Underdog"), deduped by (player_name, sport, stat_type, external_id). Idempotent — safe to call repeatedly.

### 17. Test suite (`bot/tests/`)
**Full pipeline coverage across all modules — 1427 tests, all passing.**

| File | Tests | Covers |
|---|---|---|
| `test_pipeline.py` | 68 | Full EV/steam/confidence/alert/delivery pipeline |
| `test_prop_provider.py` | 56 | `PlayerProp`, `PropProviderBase` ABC, `PropComparisonEngine`, vig removal |
| `test_dashboard.py` | 115 | `DashboardReport.to_telegram()`, `DashboardEngine.gather()` (empty + seeded DB) |
| `test_alert_clv_seed.py` | 84 | `PropLineHistory` CRUD, `AlertCLVSeed` upsert + dedup, seed methods |
| `test_prizepicks_provider.py` | 61 | `_normalize_stat`, `_parse_dt`, `pp_line_to_player_prop`, `PrizePicksProvider`, `PrizePicksManualProvider` |
| `test_underdog_provider.py` | 43 | `_normalize_stat`, `ud_snapshot_to_player_prop`, `UnderdogProvider`, `sync_underdog_snapshots_to_prop_history` |
| (+ other test files) | ~1000 | Connectors, consensus, CLV engine, AI ranking, backtesting, PP/UD providers |

Run: `python -m pytest bot/tests/ -q`

---

## Configuration

All settings in `bot/config.py` — override via environment variables:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | — | **Required.** Bot token from @BotFather |
| `ODDS_API_KEY` | — | The Odds API key (live data) |
| `ALLOWED_USER_IDS` | (empty) | Comma-separated Telegram user IDs for alerts |
| `ODDS_POLL_INTERVAL` | `60` | Seconds between odds fetches |
| `STEAM_CHECK_INTERVAL` | `30` | Seconds between steam scans |
| `MIN_EV_THRESHOLD` | `3.0` | Minimum EV% to alert |
| `MIN_STEAM_SCORE` | `70` | Minimum steam score (0–100) to alert |
| `MIN_AI_CONFIDENCE` | `60` | Minimum confidence score (0–100) to alert |
| `EV_DEDUP_WINDOW` | `1800` | Seconds before re-alerting same EV opportunity |
| `STEAM_DEDUP_WINDOW` | `3600` | Seconds before re-alerting same steam move |
| `SHARP_BOOKS` | Pinnacle, Circa, Bookmaker.eu, … | Comma-separated sharp book names |
| `ACTIVE_SPORTS` | americanfootball_nfl, … | Sports to monitor |
| `BOT_DATABASE_URL` | `sqlite+aiosqlite:///bot/data/sharp_money.db` | Database URL |

> `DATABASE_URL` is reserved by Replit for managed Postgres — always use `BOT_DATABASE_URL` for the bot's SQLite.

---

## Architecture decisions

- **`engine/` package shadows `engine.py`** — Python resolves packages before modules. `AnalysisEngine` lives in `engine/analysis.py`, exported via `engine/__init__.py`. The original `engine.py` is dead code (tracked as task #5).
- **`AlertDelivery` centralises all delivery logic** — background jobs call a single method; no inline dedup/format/send logic in `main.py`.
- **Dedup via DB** — `has_recent_ev_alert` / `has_recent_steam_alert` query the ORM with a configurable time window instead of in-memory state (survives restarts).
- **`run_polling()` owns the event loop** — PTB v20+; never wrap in `asyncio.run()`. Async DB setup uses the `post_init` lifecycle hook.
- **Stars map directly from confidence score** — not a composite formula. The old `ev × 0.5 + confidence × 0.3 + steam × 0.2` composite made 5★ mathematically unreachable (ceiling was 51/70 threshold).

---

## User preferences

- Documentation changes do not require calling `markTaskInProgress`
- Test suite is the source of truth for pipeline correctness — run before merging any engine changes
- Sharp book list is configurable via `SHARP_BOOKS` env var; defaults cover the primary sharp/respected books
- **Alert source priority: PrizePicks (primary, daily use) → Underdog (secondary) → DraftKings/FanDuel MLB ML/Totals (tertiary)**
- Do not spend development effort optimising sportsbook alerts before PrizePicks analysis is complete
- PrizePicks features ship first; Underdog enhancements second; sportsbook scope changes last
