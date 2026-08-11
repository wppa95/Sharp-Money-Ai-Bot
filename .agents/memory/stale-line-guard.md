---
name: Stale-line freshness guard
description: Root cause analysis and guard implementation for stale Underdog alert lines; config gate verification.
---

## Root Cause

Within a single `underdog_job` scan cycle, scoring and delivery use the **same `snap` object** — `line_val = snap.line or 0.0` is set once and passed directly to the formatter. There is no within-code stale line.

The observed stale-alert scenario (alert: 57.5, Underdog app: 59.5) is **Underdog API propagation lag**: the unofficial `/v3/over_under_lines` endpoint serves cached data that may not yet reflect a line move visible in Underdog's real-time app UI (which may use a different, more live endpoint).

**Why this matters:** There is nothing the bot can do to eliminate API-side lag. The guard makes the invariant explicit and future-proofs against refactors.

## Guard Implementation

**`_ud_line_fresh(candidate_line, player, stat_type, scan_line_map)`** — helper in `market_engine.py`. Returns True when candidate matches the latest scan snapshot line (tolerance 0.01 for float noise). Returns True for unknown players (not in map).

**`_current_scan_line_map`** — built once per scan from `ud_snaps` (same source as scoring), mapping `(player, stat_type) → line`. Built after `ud_snaps` is populated, before the main scoring loop.

**Guard insertion points (all 5 delivery paths):**
- NP 95+ override: `_np_95_fresh` — added to the inner `if` condition
- NP normal delivery: `not _ud_line_fresh(...)` sets `_np_bet_ready = False`
- LC 95+ override: `_lc_95_fresh` — added to the inner `if` condition
- SP 95+ override: `_sp_95_fresh` — added to the inner `if` condition
- SP normal delivery: `not _ud_line_fresh(...)` → `continue`

All guards log a WARNING if triggered (won't happen in normal operation — the check always passes since scoring and delivery use the same snap object).

## Alert Format Change

`_format_95_priority_alert` now appends:
`"⚠️ Verify current line on Underdog before placing."`

## Config Gate Finding (Issue #2)

`MIN_AI_CONFIDENCE = 60` — global scoring baseline (historical, now display-only; not used as a delivery gate anywhere in market_engine.py per grep: `min_confidence = 0`).

`UD_MIN_CONF_A = 70` — actual A-tier delivery gate (used by `min_conf_for_sport_tier()`).

`UD_NON_STRICT_MIN_CONF_A = 70` — same floor for non-strict (Tier 1) sports.

These are intentionally separate. The `/config` display of 60 does NOT weaken the V3.5 A-tier gate. No change needed.

**Why:** 60 is a legacy field never touched in delivery paths; 70 is what controls actionable alert delivery for A-tier props.

## Test Coverage

`test_stale_line_guard.py` — 26 tests:
- 8 unit tests for `_ud_line_fresh()`
- 10 source-inspection tests confirming guard at all 5 delivery points
- 2 dedup regression tests (dedup unchanged)
- 6 config gate verification tests
