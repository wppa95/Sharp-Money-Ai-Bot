"""
tests/test_crash_diagnosis.py — Crash diagnosis: record_crash_detail,
crash_cause_label, and Telegram restart alert content.

Tests:
  • record_crash_detail: persists all fields to sidecar
  • last_crash_detail: readable across HealthTracker instances (persistence)
  • crash_cause_label: correct classification for each crash type
  • _send_startup_notification: shows actual cause, crash block, recovery line
"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_tracker(tmp_path: Path) -> "HealthTracker":
    from engine.health import HealthTracker
    return HealthTracker(path=tmp_path / "health.json")


def _tracker_after_restart(tmp_path: Path, startup_reason: str) -> "HealthTracker":
    """Return a fresh HealthTracker whose state reflects a given startup reason."""
    from engine.health import HealthTracker
    ht = HealthTracker(path=tmp_path / "health.json")
    # Simulate having been through one startup with the given reason
    with ht._lock:
        ht._state["restart_count"]       = 2
        ht._state["last_startup_reason"] = startup_reason
    return ht


# ── record_crash_detail ───────────────────────────────────────────────────────

def test_record_crash_detail_stores_all_fields(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail(
        exc_type_name   = "ValueError",
        exc_msg         = "bad value in scoring",
        tb_text         = "  File foo.py, line 42\n    raise ValueError(...)",
        active_job      = "underdog_job",
        active_module   = "/bot/market_engine.py",
        active_function = "_run_cycle",
    )
    d = ht.last_crash_detail()
    assert d is not None
    assert d["exc_type"]        == "ValueError"
    assert d["exc_msg"]         == "bad value in scoring"
    assert "foo.py" in d["tb_text"]
    assert d["active_job"]      == "underdog_job"
    assert "market_engine" in d["active_module"]
    assert d["active_function"] == "_run_cycle"
    assert "ts"     in d
    assert "ts_unix" in d


def test_record_crash_detail_truncates_long_traceback(tmp_path):
    ht = _make_tracker(tmp_path)
    long_tb = "x" * 5000
    ht.record_crash_detail("RuntimeError", "msg", long_tb)
    d = ht.last_crash_detail()
    assert len(d["tb_text"]) <= 1500


def test_record_crash_detail_truncates_long_msg(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("RuntimeError", "y" * 500, "")
    d = ht.last_crash_detail()
    assert len(d["exc_msg"]) <= 256


def test_last_crash_detail_none_on_fresh_tracker(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_crash_detail() is None


def test_record_crash_detail_persists_to_disk(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ZeroDivisionError", "division by zero", "tb text")

    from engine.health import HealthTracker
    ht2 = HealthTracker(path=tmp_path / "health.json")
    d = ht2.last_crash_detail()
    assert d is not None
    assert d["exc_type"] == "ZeroDivisionError"


def test_record_crash_detail_overwrites_previous(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ValueError", "first", "")
    ht.record_crash_detail("RuntimeError", "second", "")
    d = ht.last_crash_detail()
    assert d["exc_type"] == "RuntimeError"


def test_record_crash_detail_with_empty_optional_fields(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ImportError", "cannot import", "")
    d = ht.last_crash_detail()
    assert d["active_job"]      == ""
    assert d["active_module"]   == ""
    assert d["active_function"] == ""


def test_record_crash_detail_does_not_affect_record_job_fail(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_fail("underdog_job", "some error")
    ht.record_crash_detail("RuntimeError", "crash", "tb")
    # Job fail streak should still be there
    assert ht.get_job_info("underdog_job")["fail_streak"] == 1
    # Crash detail also present
    assert ht.last_crash_detail() is not None


# ── crash_cause_label ─────────────────────────────────────────────────────────

def test_crash_cause_label_database_lock(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail(
        "OperationalError", "database is locked", "tb",
        active_job="underdog_job",
    )
    with ht._lock:
        ht._state["last_startup_reason"] = "crash_detected"
    assert ht.crash_cause_label() == "Database Lock"


def test_crash_cause_label_python_exception(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ValueError", "bad input", "tb")
    with ht._lock:
        ht._state["last_startup_reason"] = "crash_detected"
    assert ht.crash_cause_label() == "Python Exception"


def test_crash_cause_label_memory_kill_no_detail(tmp_path):
    ht = _tracker_after_restart(tmp_path, "unexpected_exit")
    # No crash detail → memory kill / SIGKILL
    assert ht.crash_cause_label() == "Memory Kill / Host Restart"


def test_crash_cause_label_crash_detected_no_detail(tmp_path):
    ht = _tracker_after_restart(tmp_path, "crash_detected")
    # Crash detected but no detail captured
    assert "Python Exception" in ht.crash_cause_label() or "no detail" in ht.crash_cause_label()


def test_crash_cause_label_unknown_on_clean_restart(tmp_path):
    ht = _tracker_after_restart(tmp_path, "clean_restart")
    # Not a crash — should not return DB lock or Python exception
    label = ht.crash_cause_label()
    assert label not in ("Database Lock", "Python Exception", "Memory Kill / Host Restart")


def test_crash_cause_label_db_lock_case_insensitive(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("OperationalError", "Database Is Locked", "tb")
    with ht._lock:
        ht._state["last_startup_reason"] = "unexpected_exit"
    assert ht.crash_cause_label() == "Database Lock"


def test_crash_cause_label_runtime_error_is_python_exception(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("RuntimeError", "something went very wrong", "tb")
    with ht._lock:
        ht._state["last_startup_reason"] = "unexpected_exit"
    assert ht.crash_cause_label() == "Python Exception"


def test_crash_cause_label_operational_error_not_locked_is_python_exception(tmp_path):
    """OperationalError that isn't 'locked' (e.g. disk full) → Python Exception."""
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("OperationalError", "disk I/O error", "tb")
    with ht._lock:
        ht._state["last_startup_reason"] = "crash_detected"
    assert ht.crash_cause_label() == "Python Exception"


# ── _install_excepthook ───────────────────────────────────────────────────────

def test_install_excepthook_replaces_sys_excepthook(tmp_path):
    import sys
    orig = sys.excepthook
    try:
        from main import _install_excepthook
        _install_excepthook()
        assert sys.excepthook is not orig
    finally:
        sys.excepthook = orig   # restore


def test_install_excepthook_captures_crash_on_unhandled_exc(tmp_path):
    """
    Simulating an unhandled exception: call the hook directly with a
    real exception and verify crash detail is persisted.
    """
    import sys, types
    from engine.health import init_health_tracker
    from main import _install_excepthook

    orig_hook   = sys.excepthook
    orig_tracker = None
    try:
        ht = init_health_tracker(path=tmp_path / "health_hook.json")

        _install_excepthook()

        # Synthesise an exception context
        try:
            raise RuntimeError("simulated unhandled error")
        except RuntimeError:
            import sys as _sys
            exc_type, exc_val, exc_tb = _sys.exc_info()

        # Call the hook directly (don't actually crash the process)
        with patch("sys.excepthook.__wrapped__", create=True):
            pass
        # Call the installed hook
        try:
            sys.excepthook(exc_type, exc_val, exc_tb)
        except SystemExit:
            pass
        except Exception:
            pass

        d = ht.last_crash_detail()
        assert d is not None
        assert d["exc_type"] == "RuntimeError"
        assert "simulated unhandled error" in d["exc_msg"]
    finally:
        sys.excepthook = orig_hook


def test_install_excepthook_always_calls_original(tmp_path):
    """The original sys.excepthook must be called after crash detail is captured."""
    import sys
    orig_called = []
    orig_hook = sys.excepthook
    try:
        def _mock_orig(et, ev, etb):
            orig_called.append(True)
        sys.excepthook = _mock_orig

        from main import _install_excepthook
        _install_excepthook()

        try:
            raise ValueError("test")
        except ValueError:
            import sys as _sys
            et, ev, etb = _sys.exc_info()

        sys.excepthook(et, ev, etb)
        assert orig_called, "original excepthook was not called"
    finally:
        sys.excepthook = orig_hook


def test_install_excepthook_safe_when_no_health_tracker(tmp_path):
    """Hook must not raise even if health tracker is not initialised."""
    import sys
    from engine import health as _h
    orig_tracker = _h._tracker
    orig_hook    = sys.excepthook
    try:
        _h._tracker = None   # simulate unintialised tracker
        from main import _install_excepthook
        _install_excepthook()

        try:
            raise KeyError("no tracker")
        except KeyError:
            import sys as _sys
            et, ev, etb = _sys.exc_info()

        try:
            sys.excepthook(et, ev, etb)   # must not raise
        except SystemExit:
            pass
        except Exception as exc:
            pytest.fail(f"excepthook raised: {exc}")
    finally:
        _h._tracker  = orig_tracker
        sys.excepthook = orig_hook


# ── _send_startup_notification — crash alert format ──────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_ht(startup_reason: str, crash_detail: dict | None = None,
             last_error: str | None = None, hb: str | None = None):
    ht = MagicMock()
    ht.startup_reason.return_value      = startup_reason
    ht.last_startup_reason.return_value = startup_reason
    ht.restart_count.return_value       = 3
    ht.last_heartbeat.return_value      = hb or "2026-08-02 01:00:00 UTC"
    ht.last_error.return_value          = last_error
    ht.last_crash_detail.return_value   = crash_detail
    ht.last_job_started_name.return_value = "underdog_job"
    ht._state = {"last_job_run": {"job": "underdog_job"}}
    if crash_detail:
        ht.crash_cause_label.return_value = (
            "Database Lock" if "OperationalError" in crash_detail.get("exc_type","") and
                               "locked" in crash_detail.get("exc_msg","").lower()
            else "Python Exception"
        )
    else:
        ht.crash_cause_label.return_value = (
            "Memory Kill / Host Restart" if startup_reason == "unexpected_exit"
            else "Python Exception (no detail captured)"
        )
    return ht


async def _send(ht, startup_reason: str) -> str:
    """Call _send_startup_notification and capture the sent message."""
    from main import _send_startup_notification
    sent: list[str] = []

    async def _fake_send(chat_id, text, parse_mode=None):
        sent.append(text)

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=_fake_send)

    await _send_startup_notification(bot=bot, chat_ids=[12345], ht=ht)
    return sent[0] if sent else ""


def test_crash_alert_shows_actual_cause_not_unexpected_exit(tmp_path):
    detail = {
        "exc_type": "ValueError", "exc_msg": "bad line value",
        "tb_text": "  File market_engine.py, line 99\n",
        "active_job": "underdog_job", "active_module": "/bot/market_engine.py",
        "active_function": "_parse_line",
    }
    ht = _mock_ht("unexpected_exit", crash_detail=detail)
    msg = _run(_send(ht, "unexpected_exit"))
    assert "Unexpected Exit" not in msg
    assert "Python Exception" in msg


def test_crash_alert_shows_crash_details_block(tmp_path):
    detail = {
        "exc_type": "RuntimeError", "exc_msg": "connection refused",
        "tb_text": "  File connectors/underdog.py, line 55\n",
        "active_job": "underdog_job", "active_module": "/bot/connectors/underdog.py",
        "active_function": "fetch_pickem",
    }
    ht = _mock_ht("unexpected_exit", crash_detail=detail)
    msg = _run(_send(ht, "unexpected_exit"))
    assert "Crash Details" in msg
    assert "RuntimeError" in msg
    assert "connection refused" in msg


def test_crash_alert_shows_active_module(tmp_path):
    detail = {
        "exc_type": "KeyError", "exc_msg": "player_name",
        "tb_text": "", "active_job": "",
        "active_module": "/bot/market_engine.py", "active_function": "_run_cycle",
    }
    ht = _mock_ht("crash_detected", crash_detail=detail)
    msg = _run(_send(ht, "crash_detected"))
    assert "market_engine" in msg


def test_crash_alert_shows_active_function(tmp_path):
    detail = {
        "exc_type": "IndexError", "exc_msg": "list index out of range",
        "tb_text": "", "active_job": "",
        "active_module": "/bot/engine/prop_intelligence.py",
        "active_function": "_compute_intel",
    }
    ht = _mock_ht("crash_detected", crash_detail=detail)
    msg = _run(_send(ht, "crash_detected"))
    assert "_compute_intel" in msg


def test_crash_alert_shows_recovery_status(tmp_path):
    ht = _mock_ht("unexpected_exit")
    msg = _run(_send(ht, "unexpected_exit"))
    assert "Resumed Monitoring" in msg
    assert "✅" in msg


def test_crash_alert_shows_last_active_task(tmp_path):
    ht = _mock_ht("unexpected_exit")
    msg = _run(_send(ht, "unexpected_exit"))
    assert "Last Active Task" in msg
    assert "underdog" in msg.lower()


def test_crash_alert_db_lock_shows_database_lock_reason(tmp_path):
    detail = {
        "exc_type": "OperationalError", "exc_msg": "database is locked",
        "tb_text": "  File database.py, line 500\n",
        "active_job": "_clv_seed_job",
        "active_module": "/bot/database.py", "active_function": "save_alert_clv_seed",
    }
    ht = _mock_ht("crash_detected", crash_detail=detail)
    msg = _run(_send(ht, "crash_detected"))
    assert "Database Lock" in msg


def test_crash_alert_memory_kill_shows_appropriate_reason(tmp_path):
    """No crash detail → Memory Kill / Host Restart label shown."""
    ht = _mock_ht("unexpected_exit", crash_detail=None)
    msg = _run(_send(ht, "unexpected_exit"))
    # Should NOT show "Unexpected Exit" (old generic label)
    assert "Unexpected Exit" not in msg
    assert "Memory Kill" in msg or "Host Restart" in msg


def test_normal_start_notification_unaffected(tmp_path):
    """First start and clean restart notifications must not show crash info."""
    ht_first   = _mock_ht("first_start")
    ht_restart = _mock_ht("clean_restart")

    msg_first   = _run(_send(ht_first,   "first_start"))
    msg_restart = _run(_send(ht_restart, "clean_restart"))

    for msg in (msg_first, msg_restart):
        assert "Crash Details" not in msg
        assert "Resumed Monitoring" not in msg   # only on crash path
        assert "Sharp Money Bot Online" in msg


def test_crash_alert_html_safe(tmp_path):
    """Exception message with HTML chars must be escaped."""
    detail = {
        "exc_type": "ValueError",
        "exc_msg": "<script>alert('xss')</script>",
        "tb_text": "", "active_job": "",
        "active_module": "", "active_function": "",
    }
    ht = _mock_ht("unexpected_exit", crash_detail=detail)
    msg = _run(_send(ht, "unexpected_exit"))
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg or "script" in msg  # escaped or included safely


def test_crash_alert_startup_number_included(tmp_path):
    ht = _mock_ht("unexpected_exit")
    msg = _run(_send(ht, "unexpected_exit"))
    assert "Startup #3" in msg or "#3" in msg
