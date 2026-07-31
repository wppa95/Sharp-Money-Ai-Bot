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
