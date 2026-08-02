---
name: Phase 2 Alert + Tracking Upgrades
description: Prop Intelligence Phase 2 batch — alert enrichment, DB schema, confidence gate, health telemetry, /rollups command.
---

## What was added

### Section 1 — Rich Intelligence Alert
- `alerts_multiplatform.py`: `_format_intelligence_block(trace)` renders role/matchup intelligence inline.  
  Both `format_underdog_new_prop_alert` and `format_underdog_change_alert` accept 3 new optional kwargs:
  `opponent`, `intelligence_trace`, `opening_line` (change alert only).
- `alerts.py` `deliver_underdog`: same 3 new params forwarded to formatters.
- `market_engine.py`: imports `_compute_intel`; new-prop / line-change / standing paths each compute
  an intelligence trace and pass it through.  Line-change path guards with `try/except NameError`
  because `ud_history` may be unbound in removal-only code paths.

### Section 2 — opening_line persistence
- `PropLineHistory.opening_line` column added.  Set to `line_value` on first INSERT; never updated.
- `_migrate_prop_line_history_v2()` migration added (safe / idempotent).

### Section 3 — Results tracking
- `PropOpportunityLog`: 4 new columns: `stars`, `risk_level`, `explanation`, `void_reason`.
- `log_prop_opportunity`: accepts `stars`, `risk_level`, `explanation` optional kwargs.
- `grade_opportunity`: accepts extended result codes (VOID/CANCELLED/INJURY_VOID/GAME_INTERRUPTED)
  and `void_reason` kwarg.
- `_migrate_prop_opportunity_log_v2()` migration added (safe / idempotent).

### Section 4 — Learning rollups
- `database.py`: `get_learning_rollups()` returns `{by_tier, by_sport, by_stat_type, by_error_type,
  player_trend, total_graded}`.  Only graded (HIT/MISS/PUSH) PLAY (OVER/UNDER) rows counted.
- `commands.py`: `cmd_rollups` added.
- `main.py`: `/rollups` handler registered.

### Section 5 — Quality gate
- `config.py`: `UD_MIN_CONF_S=75`, `UD_MIN_CONF_A=65`, `UD_MIN_CONF_B=55`, `ENABLE_LEARNING_UPDATES=False`.
- Gate applied in all 3 `deliver_underdog` call sites in `market_engine.py`.
  Removal alerts bypass the gate (`is_removed=True`).

### Section 6 — Crash recovery expansion
- `engine/health.py`: 4 new methods: `record_job_started`, `record_underdog_scan`,
  `record_database_write`, `record_pipeline_fail` + public accessors.
- `market_engine.py`: `record_job_started` at job start; `record_underdog_scan` + `record_database_write`
  on success; `record_pipeline_fail` in both success-persistence-fail and exception-handler paths.
- `cmd_health` expanded to show Underdog scan telemetry, last DB write, last pipeline failure.

## Key quirks

**Why:**
- `ud_history` is assigned only in branches where scoring runs; removal-only paths skip scoring.
  Intelligence trace block uses `try/except NameError` to guard this.
- `get_learning_rollups` Win/Loss accounting: OVER+HIT = W, OVER+MISS = L; UNDER+MISS = W, UNDER+HIT = L
  (because "under cleared" = the over-line failed).
- `ENABLE_LEARNING_UPDATES` defaults False — rollups are display-only; no weight mutations yet.

## Test coverage
- `tests/test_intelligence_alert.py` — formatter rendering, signature checks
- `tests/test_health_upgrade.py`     — all 4 new HealthTracker methods + persistence
- `tests/test_db_phase2.py`          — ORM columns, migrations, grade extended codes, rollups
- `tests/test_quality_gate.py`       — config defaults and gate logic
