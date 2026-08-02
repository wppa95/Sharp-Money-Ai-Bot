"""
engine/health.py — Lightweight bot health tracker.

A JSON-backed singleton that persists across imports within a process and
survives bot restarts (data written to bot/data/health.json).

Tracks:
  • restart count + timestamp list (last 20)
  • per-job last_run / last_fail / last_error
  • last Telegram message send time
  • last provider fetch time + last provider error
  • heartbeat timestamp (updated every ~60 s by the heartbeat job)
  • last_error (any source)
  • shutdown reason (pending_shutdown_reason) written before process exit
  • startup reason inferred from previous session's shutdown record

Shutdown → Startup reason mapping
  previous pending_shutdown_reason   → startup_reason recorded
  ─────────────────────────────────   ──────────────────────────────────────────
  "clean_shutdown"                   → "clean_restart"   (SIGTERM / SIGINT / PTB stop)
  "unexpected_exit"                  → "crash_detected"  (atexit fired; run_polling raised)
  missing / None                     → "unexpected_exit" (SIGKILL, OOM, hard crash)
  no previous session at all         → "first_start"

All methods are synchronous and thread-safe via a simple lock.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Bot Error Taxonomy (Framework v3.0 Layer 3 — bot-level side)
# ─────────────────────────────────────────────────────────────────────────────

class BotErrorType(str, enum.Enum):
    """
    Classification of bot-level failures (distinct from provider failures).

    Provider failures are typed by ``providers.base.FailureType``.
    Bot failures are typed here — covering code crashes, database errors,
    data-processing failures, and hard process crashes.

    Used by HealthTracker to classify recorded failures and surface them in
    /health output so operators can distinguish transient processing errors
    from crashes or database outages.

    CODE_FAILURE       — A Python exception was raised and caught in a job
                         handler or command handler (non-crash).
    DATABASE_FAILURE   — A database operation failed (read, write, or query).
    CRASH              — The process exited unexpectedly; detected by the
                         HealthTracker sidecar on the next startup.
    PROCESSING_FAILURE — A data-processing step failed (parsing, scoring,
                         normalization) but the bot continued running.
    """

    CODE_FAILURE       = "code_failure"
    DATABASE_FAILURE   = "database_failure"
    CRASH              = "crash"
    PROCESSING_FAILURE = "processing_failure"

_DEFAULT_PATH = Path(__file__).parent.parent / "data" / "health.json"
_RESTART_HISTORY_LIMIT = 20
_JOB_HISTORY_LIMIT     = 5

# Maps the shutdown reason written by the previous session to the startup
# reason label recorded at the beginning of the new session.
_SHUTDOWN_TO_STARTUP: dict[str, str] = {
    "clean_shutdown":   "clean_restart",   # PTB post_shutdown ran → clean exit
    "unexpected_exit":  "crash_detected",  # atexit fallback fired → run_polling raised
}


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _now_ts() -> float:
    return datetime.utcnow().timestamp()


def _age_str(ts: Optional[float]) -> str:
    """Human-readable age string from an epoch timestamp, or '—' if None."""
    if ts is None:
        return "—"
    secs = int(_now_ts() - ts)
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    rem = mins % 60
    return f"{hrs}h {rem}m ago"


def _secs_to_duration(secs: Optional[float]) -> str:
    """Convert a duration in seconds to a human-readable string."""
    if secs is None or secs < 0:
        return "—"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m {secs % 60}s"
    hrs = mins // 60
    rem = mins % 60
    return f"{hrs}h {rem}m"


class HealthTracker:
    """
    Singleton health tracker.

    Persist to / load from a JSON file so state survives restarts.
    All public methods are synchronous (threading.Lock); safe to call
    from async contexts via normal function calls.
    """

    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self._path  = path
        self._lock  = threading.Lock()
        self._state: dict = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = self._path.read_text()
                self._state = json.loads(raw)
                logger.debug("HealthTracker: loaded from %s", self._path)
            else:
                self._state = {}
        except Exception as exc:
            logger.warning("HealthTracker: could not load state (%s) — starting fresh", exc)
            self._state = {}

    def _save(self) -> None:
        """Write state to disk atomically.  Errors are non-fatal."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=2))
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning("HealthTracker: could not save state: %s", exc)

    # ── Shutdown tracking ─────────────────────────────────────────────────────

    def record_shutdown(self, reason: str) -> None:
        """
        Write shutdown reason to the sidecar before the process exits.

        Call from post_shutdown (for clean exits) or an atexit handler
        (for crash paths).  Safe to call multiple times — last write wins.

        The 'pending_shutdown_reason' field is consumed (and cleared) on
        the next startup by record_startup().
        """
        now_iso = _now_iso()
        now_ts  = _now_ts()
        with self._lock:
            self._state["pending_shutdown_reason"] = reason
            self._state["last_shutdown_at"]        = now_iso
            self._state["last_shutdown_ts"]        = now_ts
            self._save()
        logger.info("HealthTracker: shutdown recorded — reason=%s", reason)

    def record_shutdown_if_not_set(self, reason: str) -> None:
        """
        Write shutdown reason only if none has been recorded yet this
        session.  Used by the atexit fallback so it never overwrites a
        'clean_shutdown' written by post_shutdown.
        """
        with self._lock:
            if "pending_shutdown_reason" in self._state:
                return   # already recorded by a clean-shutdown path
        self.record_shutdown(reason)

    # ── Startup ───────────────────────────────────────────────────────────────

    def record_startup(self, reason: str = "normal") -> None:
        """
        Call once from post_init.  Increments restart count and infers
        the startup reason from the previous session's shutdown record.

        The explicit 'reason' parameter is kept for backwards-compat but
        is ignored — the actual reason is always inferred automatically.
        """
        now_iso   = _now_iso()
        now_ts    = _now_ts()

        with self._lock:
            # ── Infer startup reason from previous session ─────────────────
            prev_count   = self._state.get("restart_count", 0)
            pending      = self._state.get("pending_shutdown_reason")  # may be None
            shutdown_at  = self._state.get("last_shutdown_at")
            shutdown_ts  = self._state.get("last_shutdown_ts")
            startup_ts   = self._state.get("last_startup_ts")

            if prev_count == 0:
                startup_reason = "first_start"
            elif pending is None:
                # Nothing wrote before the process died → hard kill / OOM / very early crash
                startup_reason = "unexpected_exit"
            else:
                startup_reason = _SHUTDOWN_TO_STARTUP.get(pending, f"after_{pending}")

            # Session duration = time from last startup to this shutdown
            session_secs: Optional[float] = None
            if startup_ts is not None and shutdown_ts is not None and shutdown_ts > startup_ts:
                session_secs = shutdown_ts - startup_ts

            # ── Update state ───────────────────────────────────────────────
            self._state["restart_count"] = prev_count + 1

            history: list = self._state.setdefault("restart_history", [])
            history.append({
                "ts":            now_iso,
                "reason":        startup_reason,
                "shutdown_at":   shutdown_at,
                "session_secs":  session_secs,
            })
            if len(history) > _RESTART_HISTORY_LIMIT:
                self._state["restart_history"] = history[-_RESTART_HISTORY_LIMIT:]

            self._state["last_startup"]        = now_iso
            self._state["last_startup_ts"]     = now_ts
            self._state["last_startup_reason"] = startup_reason

            # Clear the pending shutdown fields — consumed
            self._state.pop("pending_shutdown_reason", None)
            self._state.pop("last_shutdown_at",        None)
            self._state.pop("last_shutdown_ts",        None)

            self._save()

        logger.info(
            "HealthTracker: startup #%d recorded (reason=%s)",
            self._state["restart_count"], startup_reason,
        )

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def update_heartbeat(self) -> None:
        """Call from the heartbeat job every ~60 s."""
        with self._lock:
            self._state["heartbeat"]    = _now_iso()
            self._state["heartbeat_ts"] = _now_ts()
            self._save()

    def heartbeat_age_seconds(self) -> Optional[float]:
        ts = self._state.get("heartbeat_ts")
        return (_now_ts() - ts) if ts is not None else None

    def heartbeat_age_str(self) -> str:
        return _age_str(self._state.get("heartbeat_ts"))

    # ── Job tracking ──────────────────────────────────────────────────────────

    def record_job_run(self, job_name: str) -> None:
        """Mark a successful job execution (resets fail_streak; auto-detects recovery)."""
        with self._lock:
            jobs: dict  = self._state.setdefault("jobs", {})
            entry: dict = jobs.setdefault(job_name, {})
            prev_streak = entry.get("fail_streak", 0)
            last_error  = entry.get("last_error", "")
            entry["last_run"]    = _now_iso()
            entry["last_run_ts"] = _now_ts()
            entry["run_count"]   = entry.get("run_count", 0) + 1
            entry["fail_streak"] = 0
            self._save()
        # Auto-detect and record recovery when transitioning out of a failure streak
        if prev_streak > 0:
            self.record_recovery_event(
                job_name,
                recovered_from=f"fail_streak={prev_streak}: {str(last_error)[:80]}",
            )

    def record_job_started(self, job_name: str) -> None:
        """
        Mark that a job has been dispatched (before completion or failure).

        Writes ``last_started`` / ``last_started_ts`` into the job dict and
        sets the top-level ``last_job_started`` / ``last_job_started_at`` fields
        so the health sidecar always shows which job was running last.
        """
        with self._lock:
            jobs:  dict = self._state.setdefault("jobs", {})
            entry: dict = jobs.setdefault(job_name, {})
            entry["last_started"]    = _now_iso()
            entry["last_started_ts"] = _now_ts()
            self._state["last_job_started"]    = job_name
            self._state["last_job_started_at"] = _now_iso()
            self._save()

    def record_job_fail(self, job_name: str, error: str) -> None:
        """Mark a failed job execution with error details."""
        with self._lock:
            jobs: dict  = self._state.setdefault("jobs", {})
            entry: dict = jobs.setdefault(job_name, {})
            entry["last_fail"]    = _now_iso()
            entry["last_fail_ts"] = _now_ts()
            entry["last_error"]   = str(error)[:200]
            entry["fail_count"]   = entry.get("fail_count", 0) + 1
            entry["fail_streak"]  = entry.get("fail_streak", 0) + 1
            fails: list = entry.setdefault("recent_fails", [])
            fails.append({"ts": _now_iso(), "error": str(error)[:200]})
            if len(fails) > _JOB_HISTORY_LIMIT:
                entry["recent_fails"] = fails[-_JOB_HISTORY_LIMIT:]
            self._state["last_error"]    = f"[{job_name}] {str(error)[:200]}"
            self._state["last_error_ts"] = _now_iso()
            self._save()
        logger.warning("HealthTracker: job %r failed: %s", job_name, str(error)[:100])

    def get_job_info(self, job_name: str) -> dict:
        return self._state.get("jobs", {}).get(job_name, {})

    def get_all_jobs(self) -> dict[str, dict]:
        return dict(self._state.get("jobs", {}))

    def job_last_run_str(self, job_name: str) -> str:
        return _age_str(self.get_job_info(job_name).get("last_run_ts"))

    def job_last_fail_str(self, job_name: str) -> str:
        return _age_str(self.get_job_info(job_name).get("last_fail_ts"))

    # ── Telegram ──────────────────────────────────────────────────────────────

    def record_underdog_scan(self, props_count: int, alerts_sent: int) -> None:
        """Record a completed successful Underdog scan cycle."""
        with self._lock:
            self._state["last_underdog_scan"]    = _now_iso()
            self._state["last_underdog_scan_ts"] = _now_ts()
            self._state["last_underdog_props"]   = props_count
            self._state["last_underdog_alerts"]  = alerts_sent
            self._save()

    def record_database_write(self, operation: str = "write") -> None:
        """Record a successful database write operation."""
        with self._lock:
            self._state["last_db_write"]     = _now_iso()
            self._state["last_db_write_ts"]  = _now_ts()
            self._state["last_db_operation"] = str(operation)[:64]
            self._save()

    def record_pipeline_fail(self, stage: str, module: str, error: str) -> None:
        """
        Record a pipeline stage failure with module attribution.

        Distinct from record_job_fail — this captures *where inside* a job
        the failure occurred so operators can distinguish e.g. a scoring failure
        from a delivery failure without reading raw logs.
        """
        with self._lock:
            self._state["last_pipeline_fail"] = {
                "stage":  str(stage)[:64],
                "module": str(module)[:64],
                "error":  str(error)[:200],
                "ts":     _now_iso(),
            }
            self._state["last_error"]    = f"[pipeline:{stage}:{module}] {str(error)[:150]}"
            self._state["last_error_ts"] = _now_iso()
            self._save()
        logger.warning(
            "HealthTracker: pipeline fail — stage=%s module=%s error=%s",
            stage, module, str(error)[:80],
        )

    def record_recovery_event(self, job_name: str, recovered_from: str = "") -> None:
        """
        Record a recovery event — called when a job succeeds after one or more failures.

        Stores the last recovery in a top-level ``last_recovery_event`` dict so
        /health can show operators when the system last self-healed.

        This is called automatically by ``record_job_run`` when it detects a
        non-zero fail_streak, so callers in market_engine.py do not need to
        call this directly.
        """
        with self._lock:
            event = {
                "job":     str(job_name)[:64],
                "reason":  str(recovered_from)[:128] if recovered_from else "job succeeded after failures",
                "ts":      _now_iso(),
                "ts_unix": _now_ts(),
            }
            self._state["last_recovery_event"] = event
            # Track a short history of recoveries (last 5)
            history: list = self._state.setdefault("recovery_history", [])
            history.append(event)
            if len(history) > 5:
                self._state["recovery_history"] = history[-5:]
            self._save()
        logger.info(
            "HealthTracker: recovery event — job=%s reason=%s",
            job_name, str(recovered_from)[:60],
        )

    def record_telegram_send(self) -> None:
        """Call after any successful outbound Telegram message."""
        with self._lock:
            self._state["last_telegram_send"]    = _now_iso()
            self._state["last_telegram_send_ts"] = _now_ts()
            self._save()

    def last_telegram_send(self) -> Optional[str]:
        return self._state.get("last_telegram_send")

    def last_telegram_send_age_str(self) -> str:
        return _age_str(self._state.get("last_telegram_send_ts"))

    # ── Provider ─────────────────────────────────────────────────────────────

    def record_provider_fetch(self, provider: str) -> None:
        with self._lock:
            providers: dict = self._state.setdefault("providers", {})
            entry: dict     = providers.setdefault(provider, {})
            entry["last_fetch"]    = _now_iso()
            entry["last_fetch_ts"] = _now_ts()
            entry["fetch_count"]   = entry.get("fetch_count", 0) + 1
            entry["error_streak"]  = 0
            self._save()

    def record_provider_error(self, provider: str, error: str) -> None:
        with self._lock:
            providers: dict = self._state.setdefault("providers", {})
            entry: dict     = providers.setdefault(provider, {})
            entry["last_error"]     = _now_iso()
            entry["last_error_msg"] = str(error)[:200]
            entry["error_count"]    = entry.get("error_count", 0) + 1
            entry["error_streak"]   = entry.get("error_streak", 0) + 1
            self._state["last_error"]    = f"[provider:{provider}] {str(error)[:150]}"
            self._state["last_error_ts"] = _now_iso()
            self._save()

    def get_provider_info(self, provider: str) -> dict:
        return self._state.get("providers", {}).get(provider, {})

    def provider_last_fetch_str(self, provider: str) -> str:
        return _age_str(self.get_provider_info(provider).get("last_fetch_ts"))

    # ── Uptime ────────────────────────────────────────────────────────────────

    def current_uptime_str(self) -> str:
        """Human-readable uptime since last startup."""
        return _age_str(self._state.get("last_startup_ts"))

    def current_uptime_secs(self) -> Optional[float]:
        ts = self._state.get("last_startup_ts")
        return (_now_ts() - ts) if ts is not None else None

    # ── Global summary ────────────────────────────────────────────────────────

    def restart_count(self) -> int:
        return self._state.get("restart_count", 0)

    def startup_reason(self) -> str:
        """
        Return the reason for the most recent startup.

        Values:
          "first_start"         — first ever run (no prior history).
          "unexpected_exit"     — process died without writing a shutdown record
                                  (crash, OOM, SIGKILL, Replit eviction).
          "after_clean_shutdown"— previous session ended cleanly (SIGTERM / restart).
          "unknown"             — state file absent or unreadable.
        """
        return self._state.get("last_startup_reason", "unknown")

    def last_startup(self) -> Optional[str]:
        return self._state.get("last_startup")

    def last_startup_reason(self) -> str:
        """Startup reason inferred for this session (set during record_startup)."""
        return self._state.get("last_startup_reason", "unknown")

    def last_session_duration_str(self) -> str:
        """
        Duration of the PREVIOUS session (from the history entry of THIS startup),
        or '—' if not available.
        """
        history = self._state.get("restart_history", [])
        if not history:
            return "—"
        latest = history[-1]
        return _secs_to_duration(latest.get("session_secs"))

    def last_session_secs(self) -> Optional[float]:
        history = self._state.get("restart_history", [])
        if not history:
            return None
        return history[-1].get("session_secs")

    def was_unexpected_exit(self) -> bool:
        """True if this startup followed a crash or unexpected kill."""
        reason = self.last_startup_reason()
        return reason in ("unexpected_exit", "crash_detected")

    def last_error(self) -> Optional[str]:
        return self._state.get("last_error")

    def last_error_ts(self) -> Optional[str]:
        return self._state.get("last_error_ts")

    def restart_history(self) -> list[dict]:
        return list(self._state.get("restart_history", []))

    def last_heartbeat(self) -> Optional[str]:
        return self._state.get("heartbeat")

    # ── New Phase 2 accessors ─────────────────────────────────────────────────

    def job_last_started_str(self, job_name: str) -> str:
        return _age_str(self.get_job_info(job_name).get("last_started_ts"))

    def last_underdog_scan(self) -> Optional[str]:
        return self._state.get("last_underdog_scan")

    def last_underdog_scan_age_str(self) -> str:
        return _age_str(self._state.get("last_underdog_scan_ts"))

    def last_underdog_props(self) -> Optional[int]:
        return self._state.get("last_underdog_props")

    def last_underdog_alerts(self) -> Optional[int]:
        return self._state.get("last_underdog_alerts")

    def last_db_write(self) -> Optional[str]:
        return self._state.get("last_db_write")

    def last_db_write_age_str(self) -> str:
        return _age_str(self._state.get("last_db_write_ts"))

    def last_pipeline_fail_info(self) -> Optional[dict]:
        """Return the last pipeline failure dict, or None."""
        return self._state.get("last_pipeline_fail")

    def last_job_started_name(self) -> Optional[str]:
        return self._state.get("last_job_started")

    def last_recovery_event(self) -> Optional[dict]:
        """Return the most recent recovery event dict, or None."""
        return self._state.get("last_recovery_event")

    def last_recovery_age_str(self) -> str:
        evt = self._state.get("last_recovery_event")
        if not evt:
            return "—"
        return _age_str(evt.get("ts_unix"))

    def recovery_history(self) -> list:
        """Return list of up to 5 recent recovery events (oldest first)."""
        return list(self._state.get("recovery_history", []))


# ── Module-level singleton ────────────────────────────────────────────────────

_tracker: Optional[HealthTracker] = None


def init_health_tracker(path: Path = _DEFAULT_PATH) -> HealthTracker:
    """Create (or replace) the module-level singleton and return it."""
    global _tracker
    _tracker = HealthTracker(path)
    return _tracker


def get_health_tracker() -> Optional[HealthTracker]:
    """Return the singleton, or None if init_health_tracker() has not been called."""
    return _tracker
