---
name: Stale recovery display fix
description: /health showed a 61h-old recovery event as the active/current recovery. Added a 6-hour staleness gate mirroring the existing global-error and pipeline-failure gates.
---

## Problem

`cmd_health` displayed `last_recovery_event` unconditionally, so a recovery from
August 6 appeared as "Last recovery: ~61h ago · underdog_job ↳ fail_streak=3:
'list' object has no attribute 'has_real_data'" even when the current session was
completely healthy.

The global-error and pipeline-failure sections already had 2-hour staleness gates;
the recovery section had none.

## Fix

**`engine/health.py`** — Added `last_recovery_age_hours() → Optional[float]`:
- Uses the `ts_unix` float stored by `record_recovery_event` (avoids ISO-string
  timezone parsing).
- Returns `None` if no recovery event or `ts_unix` missing.

**`commands.py` — `cmd_health`** — Added `_RECOVERY_STALE_HOURS = 6.0` gate:
- `_rec_age_h >= 6.0`: shows `ℹ️ Last recovery: ~Xh ago · job: Y (historical — no recent failures)`
- `_rec_age_h < 6.0`: shows `✅ Last recovery: ~Xm ago · job: Y` + reason sub-line (unchanged)
- No recovery recorded: unchanged — `✅ Last recovery: No recovery events recorded`

## Why

A recovery this old is resolved history, not current health signal. Presenting it
with ✅ alongside current job status misleads operators into thinking a failure
just resolved. The 6-hour threshold was chosen to be meaningfully longer than the
2-hour gates used elsewhere, while still keeping the display fresh for genuinely
recent self-heals.

## How to apply

If /health shows a recovery event that looks stale, check `last_recovery_age_hours()`.
The gate is in `cmd_health` (not in `HealthTracker`) so the underlying event is
always preserved in the JSON sidecar for debugging.

## Test count

3509 passing Aug 2026. Focused tests in `tests/test_health_stale_recovery.py`
(35 tests covering: age_hours method, stale label, recent display, no-recovery
fallback, source-level guards, regression guards).
