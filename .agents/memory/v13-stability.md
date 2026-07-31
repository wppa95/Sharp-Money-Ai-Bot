---
name: v1.3 Stability architecture
description: Health-monitoring design decisions for the v1.3 freeze — what every scheduled job must record and why.
---

# v1.3 Stability — durable design decisions

## Rule: every scheduled job invocation records exactly one outcome

Every job (including the heartbeat) must end with exactly one of:
- `_ht.record_job_run(name)` — successful execution, including idle no-ops
- `_ht.record_job_fail(name, reason)` — any exception, import failure, or critical persistence failure

**Why:** `/health` is the sole liveness indicator; jobs that exit without recording leave the command unable to distinguish "never ran" from "crashed silently."

**How to apply:** Guard clauses (e.g. `if _db is None: return`) must call `record_job_run` before returning (they are healthy no-ops, not failures). Import errors and processing exceptions must call `record_job_fail`.

## Rule: persistence failures are job failures

In `underdog_job`, the PropLineHistory bridge and lifecycle-state updates are *persistence stages* — failure must be recorded as `record_job_fail`, not silently absorbed.

**Why:** A job that successfully fetches and alerts but fails to persist lifecycle state has partial work outstanding; `/health` must surface this.

## Rule: clamp_score at every persistence boundary

`clamp_score(value, label, min_, max_)` must wrap any score or confidence value before it reaches a DB model, including:
- `UnderdogSnapshotRecord.score_total` (0–100), `.score_stars` (0–5), `.bet_confidence` (0–100)
- `EVRecord.ai_confidence` (0–100) — in `alerts.AlertDelivery._log_ev`

**Why:** Out-of-range values stored to the DB corrupt backtesting and calibration; a logged WARNING surfaces the source before the value is persisted.

## Rule: lifecycle queue applied AFTER bridge

`_lifecycle_alerted` / `_lifecycle_removed` are populated during the `underdog_job` loop, then applied via `update_prop_lifecycle_state` only *after* `sync_underdog_snapshots_to_prop_history` completes.

**Why:** The bridge creates/updates `PropLineHistory` rows; applying lifecycle state before the bridge would target rows that don't exist yet.

## Rule: re-entry detection requires both known_keys AND prev_record checks

A prop in `known_keys` (which includes removed rows) with `prev_record = None` (from `get_latest_underdog_snapshot_per_prop` which is non-removed only) is a **re-entry** — the prop returned after removal. Treat with `new_prop=True` to bypass the timing filter and fire an alert.

**Why:** Without this, re-entering props silently skip alerting because `line_changed=False` (no prev_record to compare against). Users miss the re-appearance.

**How to apply:** `is_reentry = not is_removed and not is_cold_start and prev_record is None`

## Rule: Player Prop Market Engine is the active alert framework (replacing pp_reference)

`bot/engine/player_prop_market.py` — `run_player_prop_market_cycle` — is the live path. `_prop_market_alerted` is the module-level dedup set. Old `run_pp_reference_cycle` is dead code and must not be re-enabled.

**Why:** The new framework shows all 4 providers (PP/UD/DK/FD), labels confidence as "Proxy Match Confidence" (not just "Confidence"), and supports multi-provider market view per spec.

## Rule: command handlers must have try/except + error_handler must reply

Every `cmd_*` handler needs a top-level `try/except` block that catches all exceptions and sends a visible error reply. The global `error_handler` also replies to the user.

**Why:** Without this, failed commands appear to silently succeed — the user gets no response and has no way to know something went wrong.
