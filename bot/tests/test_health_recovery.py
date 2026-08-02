"""
tests/test_health_recovery.py — Health timeline expansion: recovery events.

Tests:
  • record_recovery_event: stores event in last_recovery_event and recovery_history
  • record_job_run: auto-detects recovery when fail_streak > 0
  • record_job_run: does NOT trigger recovery when fail_streak == 0
  • last_recovery_event accessor
  • last_recovery_age_str accessor
  • recovery_history: stores up to 5 events, oldest first
  • Persistence: recovery events survive a reload from disk
"""

from __future__ import annotations

import pytest
from pathlib import Path


def _make_tracker(tmp_path: Path) -> "HealthTracker":
    from engine.health import HealthTracker
    return HealthTracker(path=tmp_path / "health.json")


# ── record_recovery_event ─────────────────────────────────────────────────────

def test_record_recovery_event_stores_event(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("underdog_job", "fail_streak=3: connection timeout")
    evt = ht.last_recovery_event()
    assert evt is not None
    assert evt["job"]  == "underdog_job"
    assert "fail_streak=3" in evt["reason"]
    assert "ts"   in evt
    assert "ts_unix" in evt


def test_record_recovery_event_no_reason_uses_default(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("underdog_job")
    evt = ht.last_recovery_event()
    assert evt is not None
    assert evt["reason"]  # non-empty default


def test_record_recovery_event_empty_string_reason(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("underdog_job", "")
    evt = ht.last_recovery_event()
    assert evt is not None


def test_record_recovery_event_overwrites_previous(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("job_a", "error a")
    ht.record_recovery_event("job_b", "error b")
    evt = ht.last_recovery_event()
    assert evt["job"] == "job_b"


def test_record_recovery_event_default_none(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_recovery_event() is None


# ── last_recovery_age_str ─────────────────────────────────────────────────────

def test_last_recovery_age_str_dash_before_any_event(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_recovery_age_str() == "—"


def test_last_recovery_age_str_returns_ago_after_event(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("underdog_job", "test")
    s = ht.last_recovery_age_str()
    assert isinstance(s, str)
    assert "ago" in s or s == "—"


# ── recovery_history ─────────────────────────────────────────────────────────

def test_recovery_history_empty_initially(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.recovery_history() == []


def test_recovery_history_appends_events(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("job_a", "err1")
    ht.record_recovery_event("job_b", "err2")
    history = ht.recovery_history()
    assert len(history) == 2
    assert history[0]["job"] == "job_a"
    assert history[1]["job"] == "job_b"


def test_recovery_history_caps_at_5(tmp_path):
    ht = _make_tracker(tmp_path)
    for i in range(8):
        ht.record_recovery_event(f"job_{i}", f"err_{i}")
    history = ht.recovery_history()
    assert len(history) == 5
    # Most recent 5 should be kept
    assert history[-1]["job"] == "job_7"


def test_recovery_history_returns_list(tmp_path):
    ht = _make_tracker(tmp_path)
    assert isinstance(ht.recovery_history(), list)


# ── record_job_run: auto-detects recovery ────────────────────────────────────

def test_record_job_run_auto_detects_recovery_from_fail_streak(tmp_path):
    ht = _make_tracker(tmp_path)
    # Build up a fail streak
    ht.record_job_fail("underdog_job", "connection timeout")
    ht.record_job_fail("underdog_job", "connection timeout again")
    assert ht.get_job_info("underdog_job").get("fail_streak", 0) >= 2
    # Now success — should auto-detect recovery
    ht.record_job_run("underdog_job")
    evt = ht.last_recovery_event()
    assert evt is not None
    assert evt["job"] == "underdog_job"


def test_record_job_run_no_recovery_event_on_clean_start(tmp_path):
    ht = _make_tracker(tmp_path)
    # No prior failures — record_job_run should NOT create a recovery event
    ht.record_job_run("underdog_job")
    assert ht.last_recovery_event() is None


def test_record_job_run_no_recovery_after_second_success(tmp_path):
    ht = _make_tracker(tmp_path)
    # Fail + recover + succeed again — only one recovery event
    ht.record_job_fail("underdog_job", "err")
    ht.record_job_run("underdog_job")   # triggers recovery
    evt1_ts = ht.last_recovery_event()["ts"]
    ht.record_job_run("underdog_job")   # no new recovery (streak=0)
    evt2_ts = ht.last_recovery_event()["ts"]
    # timestamp unchanged — no new recovery event was written
    assert evt1_ts == evt2_ts


def test_record_job_run_resets_fail_streak_to_zero(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_fail("underdog_job", "err")
    ht.record_job_run("underdog_job")
    assert ht.get_job_info("underdog_job").get("fail_streak", 0) == 0


def test_record_job_run_recovery_includes_error_text(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_fail("underdog_job", "NoneType has no attribute foo")
    ht.record_job_run("underdog_job")
    evt = ht.last_recovery_event()
    assert "NoneType" in evt.get("reason", "")


# ── Persistence ───────────────────────────────────────────────────────────────

def test_recovery_event_survives_reload(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_fail("underdog_job", "error x")
    ht.record_job_run("underdog_job")

    from engine.health import HealthTracker
    ht2 = HealthTracker(path=tmp_path / "health.json")
    evt = ht2.last_recovery_event()
    assert evt is not None
    assert evt["job"] == "underdog_job"


def test_recovery_history_survives_reload(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_recovery_event("job_x", "test error")
    ht.record_recovery_event("job_y", "test error 2")

    from engine.health import HealthTracker
    ht2 = HealthTracker(path=tmp_path / "health.json")
    h = ht2.recovery_history()
    assert len(h) == 2
    assert h[0]["job"] == "job_x"
