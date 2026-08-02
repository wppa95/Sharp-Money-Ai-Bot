---
name: Final Completion Batch
description: Final Prop Intelligence completion — B-tier calibration, AI Analyst inline, health recovery events, matchup expansion.
---

## What was added

### Section 1 — B-Tier Calibration
- `config.py`: `UD_MIN_STARS_TO_ALERT` default changed from 4 → 3.
  B-tier (3 stars) now passes the star gate; filtered by `UD_MIN_CONF_B=55` confidence gate.
  C-tier (2★) and D-tier (1★) remain blocked by the star gate.

### Section 2 — Matchup Intelligence Expansion
- `_format_intelligence_block` enhanced:
  - Adds L5/L10/L20 hit rate row from `intelligence_trace.historical.windows`
  - Shows sample_strength next to hit rates
  - Expanded matchup reasoning bullets from 2 → 3
- All underlying data (hit rates, trend, opponent, opening line, market quality, model data)
  was already present from Phase 2; this surfaces more of it directly in the intelligence block.

### Section 3 — Health Timeline Expansion
- `engine/health.py`: Added `record_recovery_event(job_name, recovered_from)` method
- `record_job_run` auto-detects recovery: if `fail_streak > 0` before the success, it calls
  `record_recovery_event` automatically — market_engine.py needs no direct calls.
- Added `last_recovery_event()`, `last_recovery_age_str()`, `recovery_history()` accessors.
- `recovery_history` stores up to 5 most recent recovery events (oldest-first list).
- `cmd_health` in `commands.py` shows "Last recovery" section with age + job + reason.

### Section 4 — AI Analyst Layer
- `engine/analyst.py`: Added `build_analyst_from_alert_parts(...)` — pure function, no Candidate
  needed; builds AnalystNarrative from raw score/decision/intelligence parts.
- Added `format_analyst_alert_block(...)` — compact HTML block for inline alert appending;
  returns "" for PASS/None; catches all exceptions silently.
- `alerts_multiplatform.py`: Added `_format_analyst_inline_block(...)` helper.
- Both `format_underdog_new_prop_alert` and `format_underdog_change_alert` now append
  the analyst block for directional (OVER/UNDER) non-removal alerts.
- Risk level derived from stars: 5★→LOW, 4★→LOW, 3★→MEDIUM, <3→HIGH.

## Key rules
- Analyst block appended IN the alert (not a separate message).
- Removal alerts never get analyst block (`removed=True` guard).
- PASS decisions never get analyst block (`format_analyst_alert_block` returns "" for PASS).
- `record_job_run` auto-detects recovery — no separate calls needed in market_engine.
- Intelligence block now shows 3 matchup bullets (tests updated to expect 3, not 2).

## Test files
- `tests/test_btier_calibration.py`  — B-tier star/confidence gate logic
- `tests/test_health_recovery.py`    — recovery event methods + auto-detection + persistence
- `tests/test_analyst_inline.py`     — analyst narrative + alert block + formatter integration
