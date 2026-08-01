"""
Tests for HealthTracker shutdown tracking and startup reason detection.

Covers:
  - record_shutdown writes pending_shutdown_reason, last_shutdown_at, last_shutdown_ts
  - record_shutdown_if_not_set is a no-op when already recorded
  - record_startup infers "first_start" on the very first run
  - record_startup infers "clean_restart" after "clean_shutdown"
  - record_startup infers "crash_detected" after "unexpected_exit"
  - record_startup infers "unexpected_exit" when no pending_shutdown_reason
  - Session duration is computed from last_startup_ts to last_shutdown_ts
  - History entries include reason, shutdown_at, session_secs
  - pending_shutdown_reason is cleared after record_startup
  - was_unexpected_exit returns True only for crash/unexpected reasons
  - _secs_to_duration helper
  - Uptime helpers exist
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from engine.health import HealthTracker, _secs_to_duration


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _fresh(tmp_path: Path) -> HealthTracker:
    """Return a HealthTracker backed by a fresh temp file (no prior state)."""
    p = tmp_path / "health.json"
    return HealthTracker(path=p)


def _load_raw(ht: HealthTracker) -> dict:
    """Read the raw JSON from the sidecar."""
    return json.loads(ht._path.read_text())


# ── record_shutdown ───────────────────────────────────────────────────────────

class TestRecordShutdown:
    def test_writes_pending_reason(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()  # first start
        ht.record_shutdown("clean_shutdown")
        raw = _load_raw(ht)
        assert raw["pending_shutdown_reason"] == "clean_shutdown"
        assert "last_shutdown_at" in raw
        assert isinstance(raw["last_shutdown_ts"], float)

    def test_last_write_wins(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_shutdown("unexpected_exit")   # overwrite
        raw = _load_raw(ht)
        assert raw["pending_shutdown_reason"] == "unexpected_exit"

    def test_if_not_set_is_noop_when_already_recorded(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_shutdown_if_not_set("unexpected_exit")   # must not overwrite
        raw = _load_raw(ht)
        assert raw["pending_shutdown_reason"] == "clean_shutdown"

    def test_if_not_set_writes_when_not_recorded(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        # No record_shutdown called before this
        ht.record_shutdown_if_not_set("unexpected_exit")
        raw = _load_raw(ht)
        assert raw["pending_shutdown_reason"] == "unexpected_exit"


# ── record_startup reason inference ──────────────────────────────────────────

class TestStartupReasonInference:
    def test_first_start(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        assert ht.last_startup_reason() == "first_start"

    def test_clean_restart_after_clean_shutdown(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()          # startup #1 — first_start
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()          # startup #2 — should see clean_restart
        assert ht.last_startup_reason() == "clean_restart"

    def test_crash_detected_after_unexpected_exit(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("unexpected_exit")
        ht.record_startup()
        assert ht.last_startup_reason() == "crash_detected"

    def test_unexpected_exit_when_no_pending_reason(self, tmp_path: Path) -> None:
        """Simulate SIGKILL: pending_shutdown_reason never written."""
        ht = _fresh(tmp_path)
        ht.record_startup()   # first_start
        # Do NOT call record_shutdown — simulates hard kill
        ht.record_startup()   # should infer unexpected_exit
        assert ht.last_startup_reason() == "unexpected_exit"

    def test_explicit_reason_param_is_ignored(self, tmp_path: Path) -> None:
        """The 'reason' kwarg kept for compat must not override auto-detection."""
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_startup(reason="normal")   # legacy call
        assert ht.last_startup_reason() == "clean_restart"

    def test_pending_reason_cleared_after_startup(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        raw = _load_raw(ht)
        assert "pending_shutdown_reason" not in raw
        assert "last_shutdown_at"        not in raw
        assert "last_shutdown_ts"        not in raw


# ── Session duration ──────────────────────────────────────────────────────────

class TestSessionDuration:
    def test_session_secs_in_history(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        time.sleep(0.05)            # ensure measurable gap
        ht.record_shutdown("clean_shutdown")
        time.sleep(0.01)
        ht.record_startup()

        history = ht.restart_history()
        latest  = history[-1]
        assert latest["session_secs"] is not None
        assert latest["session_secs"] > 0.0

    def test_session_secs_none_when_no_prior_startup_ts(self, tmp_path: Path) -> None:
        """First-ever startup has no prior startup_ts → session_secs should be None."""
        ht = _fresh(tmp_path)
        ht.record_startup()
        history = ht.restart_history()
        assert history[0]["session_secs"] is None

    def test_last_session_duration_str(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        time.sleep(0.05)
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        dur = ht.last_session_duration_str()
        assert dur != "—"           # something was recorded
        assert isinstance(dur, str)

    def test_shutdown_at_stored_in_history(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        history = ht.restart_history()
        latest  = history[-1]
        assert latest["shutdown_at"] is not None


# ── History entries ───────────────────────────────────────────────────────────

class TestHistory:
    def test_history_includes_reason(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        history = ht.restart_history()
        reasons = [e["reason"] for e in history]
        assert "first_start"   in reasons
        assert "clean_restart" in reasons

    def test_history_grows_correctly(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        for _ in range(5):
            ht.record_startup()
            ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        assert len(ht.restart_history()) == 6

    def test_history_capped_at_20(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        for _ in range(25):
            ht.record_startup()
            ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        assert len(ht.restart_history()) <= 20


# ── was_unexpected_exit ───────────────────────────────────────────────────────

class TestWasUnexpectedExit:
    def test_false_on_first_start(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        assert ht.was_unexpected_exit() is False

    def test_false_after_clean_restart(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        assert ht.was_unexpected_exit() is False

    def test_true_after_crash_detected(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        ht.record_shutdown("unexpected_exit")
        ht.record_startup()
        assert ht.was_unexpected_exit() is True

    def test_true_after_sigkill(self, tmp_path: Path) -> None:
        """No pending_shutdown_reason = SIGKILL path."""
        ht = _fresh(tmp_path)
        ht.record_startup()
        # no record_shutdown
        ht.record_startup()
        assert ht.was_unexpected_exit() is True


# ── _secs_to_duration helper ──────────────────────────────────────────────────

class TestSecsToDuration:
    @pytest.mark.parametrize("secs, expected", [
        (0,      "0s"),
        (45,     "45s"),
        (60,     "1m 0s"),
        (90,     "1m 30s"),
        (3600,   "1h 0m"),
        (3661,   "1h 1m"),
        (7384,   "2h 3m"),
        (None,   "—"),
        (-5,     "—"),
    ])
    def test_format(self, secs, expected) -> None:
        assert _secs_to_duration(secs) == expected


# ── restart_count and accessors ───────────────────────────────────────────────

class TestAccessors:
    def test_restart_count_increments(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        assert ht.restart_count() == 0
        ht.record_startup()
        assert ht.restart_count() == 1
        ht.record_shutdown("clean_shutdown")
        ht.record_startup()
        assert ht.restart_count() == 2

    def test_last_startup_is_set(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        assert ht.last_startup() is None
        ht.record_startup()
        assert ht.last_startup() is not None

    def test_current_uptime_str(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        uptime = ht.current_uptime_str()
        assert isinstance(uptime, str)
        assert uptime != "—"

    def test_current_uptime_secs(self, tmp_path: Path) -> None:
        ht = _fresh(tmp_path)
        ht.record_startup()
        secs = ht.current_uptime_secs()
        assert secs is not None
        assert secs >= 0
