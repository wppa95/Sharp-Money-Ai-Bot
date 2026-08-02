---
name: Crash Diagnosis Patch
description: Global exception capture, crash cause classification, and improved Telegram restart alert.
---

## What was added

### engine/health.py
- `record_crash_detail(exc_type_name, exc_msg, tb_text, active_job, active_module, active_function)` — persists to `last_crash_detail` in health sidecar. Called only from the global excepthook, never from normal job error handling.
- `last_crash_detail()` — accessor; returns dict or None.
- `crash_cause_label()` — classifies crash from sidecar state:
  - `"Database Lock"` — OperationalError + "locked" in exc_msg (case-insensitive)
  - `"Python Exception"` — any other exception with crash detail present
  - `"Memory Kill / Host Restart"` — `unexpected_exit` startup reason + no crash detail (SIGKILL/OOM path)
  - `"Python Exception (no detail captured)"` — `crash_detected` + no detail
  - `"Unknown Exit"` — none of the above

### main.py
- `_install_excepthook()` — installs `sys.excepthook` that:
  1. Extracts exc type / msg / full traceback / innermost frame (module + function)
  2. Reads `last_job_started_name()` from health sidecar as active job
  3. Calls `ht.record_crash_detail(...)` to persist
  4. Calls `ht.record_shutdown_if_not_set("unexpected_exit")` — keeps atexit as no-op
  5. Delegates to original hook (traceback still prints)
  6. Never raises — all errors silently swallowed
- Called in `main()` before `_register_atexit_fallback()` and before `run_polling`
- `_send_startup_notification()` updated: replaces hardcoded "Unexpected Exit" with `crash_cause_label()`; adds "Crash Details" block (exc type/msg + module basename + function + active job) for crash restarts; clean starts unaffected.

### tests/test_crash_diagnosis.py
- 31 tests covering: record_crash_detail, last_crash_detail, crash_cause_label for all 4 cause types, _install_excepthook wiring, _send_startup_notification crash/clean/memory-kill formats.

## Key rules
- `record_crash_detail` is ONLY for the global excepthook — never call it from job handlers.
- `crash_cause_label()` is purely read-only; it never writes state.
- The excepthook must always call the original hook at the end (finally block).
- Async test helpers must use `asyncio.new_event_loop()` + close in finally (not `asyncio.get_event_loop()`).
- Crash detail is NOT cleared on `record_startup()` — notification reads it after startup is recorded.

## Shutdown state machine (complete)
| Event | pending_shutdown_reason written | Crash detail written | Next startup_reason |
|---|---|---|---|
| SIGTERM / clean stop | `clean_shutdown` (post_shutdown) | No | `clean_restart` |
| run_polling raises | `unexpected_exit` (excepthook → atexit) | Yes (excepthook) | `crash_detected` |
| SIGKILL / OOM | Nothing | Nothing | `unexpected_exit` |
| Early crash (pre-HealthTracker) | Nothing | Nothing | `unexpected_exit` |
