"""
tests/test_crash_forensics.py

Tests for the crash forensics black box recorder (Phase 2-4):
- Persistent crash history (append, not overwrite)
- Crash ID counter increments
- Full runtime snapshot fields
- Backward-compat last_crash_detail
- Accessor methods
- Restart notification shows crash ID
- /health shows crash history
"""

import asyncio
import json
import tempfile
from pathlib import Path
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_tracker(tmp_path: Path = None):
    from engine.health import HealthTracker
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    path = tmp_path / "health.json"
    return HealthTracker(path=path)


def _seed_startup_state(ht):
    """Simulate a bot that has started and run for a while."""
    ht.record_startup()
    ht.update_heartbeat()
    ht.record_job_started("underdog_job")
    ht.record_underdog_scan(props_count=42, alerts_sent=3)
    ht.record_database_write("insert")
    ht.record_telegram_send()


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Crash ID counter
# ─────────────────────────────────────────────────────────────────────────────

def test_crash_id_starts_at_one(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_crash_id() is None
    assert ht.current_crash_id_counter() == 0

    ht.record_crash_detail("ValueError", "bad value", "tb\nline2")
    assert ht.last_crash_id() == 1
    assert ht.current_crash_id_counter() == 1


def test_crash_id_increments(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ValueError", "msg1", "tb1")
    ht.record_crash_detail("RuntimeError", "msg2", "tb2")
    ht.record_crash_detail("KeyError", "msg3", "tb3")
    assert ht.last_crash_id() == 3
    assert ht.current_crash_id_counter() == 3


def test_crash_id_persists_across_reload(tmp_path):
    from engine.health import HealthTracker
    path = tmp_path / "health.json"
    ht = HealthTracker(path=path)
    ht.record_crash_detail("ValueError", "err", "tb")
    ht.record_crash_detail("TypeError", "err2", "tb2")

    # Reload — simulates bot restart reading previous state
    ht2 = HealthTracker(path=path)
    assert ht2.current_crash_id_counter() == 2
    assert ht2.last_crash_id() == 2


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Crash history (append, not overwrite)
# ─────────────────────────────────────────────────────────────────────────────

def test_crash_history_empty_initially(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.crash_history() == []


def test_crash_history_appends_records(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ValueError", "msg1", "tb1")
    ht.record_crash_detail("RuntimeError", "msg2", "tb2")
    hist = ht.crash_history()
    assert len(hist) == 2
    assert hist[0]["exc_type"] == "ValueError"
    assert hist[1]["exc_type"] == "RuntimeError"


def test_crash_history_does_not_overwrite_previous(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("Error1", "first", "tb1")
    first_record = ht.crash_history()[0]
    ht.record_crash_detail("Error2", "second", "tb2")
    hist = ht.crash_history()
    # First record must still be there
    assert hist[0]["exc_type"] == "Error1"
    assert hist[0]["exc_msg"] == "first"
    assert len(hist) == 2


def test_crash_history_trimmed_to_limit(tmp_path):
    from engine import health as _h
    original = _h._CRASH_HISTORY_LIMIT
    _h._CRASH_HISTORY_LIMIT = 3
    try:
        ht = _make_tracker(tmp_path)
        for i in range(5):
            ht.record_crash_detail(f"Err{i}", f"msg{i}", "tb")
        hist = ht.crash_history()
        assert len(hist) == 3
        # Should keep the most recent
        assert hist[-1]["exc_type"] == "Err4"
    finally:
        _h._CRASH_HISTORY_LIMIT = original


def test_crash_history_persists_across_reload(tmp_path):
    from engine.health import HealthTracker
    path = tmp_path / "health.json"
    ht = HealthTracker(path=path)
    ht.record_crash_detail("ValueError", "msg", "tb")
    ht.record_crash_detail("RuntimeError", "msg2", "tb2")

    ht2 = HealthTracker(path=path)
    hist = ht2.crash_history()
    assert len(hist) == 2
    assert hist[0]["crash_id"] == 1
    assert hist[1]["crash_id"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Snapshot fields
# ─────────────────────────────────────────────────────────────────────────────

def test_crash_snapshot_has_required_fields(tmp_path):
    ht = _make_tracker(tmp_path)
    _seed_startup_state(ht)
    ht.record_crash_detail(
        "ImportError", "no module named X", "traceback...",
        active_job="underdog_job",
        active_module="/bot/engine/market.py",
        active_function="_parse_prop",
    )
    cd = ht.last_crash_detail()
    assert cd is not None

    required_keys = [
        "crash_id", "ts", "ts_unix", "startup_number", "uptime_secs",
        "shutdown_reason", "exc_type", "exc_msg", "tb_text",
        "active_job", "active_module", "active_function",
        "last_job_started", "last_heartbeat", "last_successful_job",
        "last_underdog_scan", "last_db_write", "last_telegram_send",
        "last_error",
    ]
    for key in required_keys:
        assert key in cd, f"Missing key: {key}"


def test_crash_snapshot_captures_active_job(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ValueError", "msg", "tb", active_job="underdog_job")
    cd = ht.last_crash_detail()
    assert cd["active_job"] == "underdog_job"


def test_crash_snapshot_captures_module_and_function(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail(
        "KeyError", "key", "tb",
        active_module="/bot/engine/analyst.py",
        active_function="build_analyst_from_alert_parts",
    )
    cd = ht.last_crash_detail()
    assert "analyst.py" in cd["active_module"]
    assert cd["active_function"] == "build_analyst_from_alert_parts"


def test_crash_snapshot_uptime_secs_present_after_startup(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_startup()
    ht.record_crash_detail("ValueError", "msg", "tb")
    cd = ht.last_crash_detail()
    # uptime_secs should be a non-negative number
    assert cd["uptime_secs"] is not None
    assert cd["uptime_secs"] >= 0


def test_crash_snapshot_startup_number(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_startup()
    ht.record_crash_detail("ValueError", "msg", "tb")
    cd = ht.last_crash_detail()
    assert cd["startup_number"] == 1


def test_crash_snapshot_last_underdog_scan_captured(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_startup()
    ht.record_underdog_scan(props_count=10, alerts_sent=2)
    ht.record_crash_detail("ValueError", "msg", "tb")
    cd = ht.last_crash_detail()
    assert cd["last_underdog_scan"] != ""


def test_crash_snapshot_last_db_write_captured(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_database_write("insert_prop")
    ht.record_crash_detail("ValueError", "msg", "tb")
    cd = ht.last_crash_detail()
    assert cd["last_db_write"] != ""


def test_crash_snapshot_last_heartbeat_captured(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.update_heartbeat()
    ht.record_crash_detail("ValueError", "msg", "tb")
    cd = ht.last_crash_detail()
    assert cd["last_heartbeat"] != ""


def test_crash_snapshot_tb_truncated_at_2000(tmp_path):
    ht = _make_tracker(tmp_path)
    long_tb = "x" * 5000
    ht.record_crash_detail("ValueError", "msg", long_tb)
    cd = ht.last_crash_detail()
    assert len(cd["tb_text"]) <= 2000


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Alert tracking
# ─────────────────────────────────────────────────────────────────────────────

def test_record_alert_generated_stores_info(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_alert_generated("Jazz Chisholm", "hits", "A")
    ag = ht.last_alert_generated()
    assert ag is not None
    assert ag["player"] == "Jazz Chisholm"
    assert ag["stat"] == "hits"
    assert ag["tier"] == "A"


def test_record_alert_generated_captured_in_crash_snapshot(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_alert_generated("Aaron Judge", "home_runs", "S")
    ht.record_crash_detail("ValueError", "msg", "tb")
    cd = ht.last_crash_detail()
    assert cd["last_alert_player"] == "Aaron Judge"
    assert cd["last_alert_stat"] == "home_runs"
    assert cd["last_alert_tier"] == "S"


def test_last_alert_age_str_returns_string(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_alert_age_str() == "—"
    ht.record_alert_generated("Player", "stat", "B")
    age = ht.last_alert_age_str()
    assert isinstance(age, str)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Backward compat: last_crash_detail still works
# ─────────────────────────────────────────────────────────────────────────────

def test_last_crash_detail_returns_most_recent(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_crash_detail("ValueError", "first", "tb1")
    ht.record_crash_detail("RuntimeError", "second", "tb2")
    cd = ht.last_crash_detail()
    assert cd["exc_type"] == "RuntimeError"
    assert cd["exc_msg"] == "second"


def test_last_crash_detail_none_when_no_crash(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_crash_detail() is None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Restart notification includes crash ID
# ─────────────────────────────────────────────────────────────────────────────

def test_startup_notification_contains_crash_id():
    """_send_startup_notification should show Crash ID when a crash detail exists."""
    import asyncio, types, html as _h

    # Patch the tracker to return a known crash detail
    class _FakeHT:
        def startup_reason(self): return "crash_detected"
        def restart_count(self): return 7
        def last_heartbeat(self): return "2026-08-02 10:00:00 UTC"
        def last_job_started_name(self): return "underdog_job"
        def crash_cause_label(self): return "Python Exception"
        def last_crash_detail(self):
            return {
                "crash_id": 4,
                "exc_type": "ValueError",
                "exc_msg": "bad prop line",
                "active_job": "underdog_job",
                "active_module": "/bot/engine/market.py",
                "active_function": "_parse",
                "memory_mb": 128.5,
                "uptime_secs": 3750,
            }
        def last_crash_id(self): return 4

    sent: list[str] = []

    async def _run():
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from main import _send_startup_notification

        class _FakeBot:
            async def send_message(self, chat_id, text, parse_mode=None):
                sent.append(text)

        await _send_startup_notification(_FakeBot(), [12345], _FakeHT())
    

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert len(sent) == 1
    msg = sent[0]
    assert "Crash ID" in msg
    assert "#4" in msg


def test_startup_notification_crash_snapshot_saved_label():
    import asyncio

    class _FakeHT:
        def startup_reason(self): return "unexpected_exit"
        def restart_count(self): return 3
        def last_heartbeat(self): return None
        def last_job_started_name(self): return None
        def crash_cause_label(self): return "Memory Kill / Host Restart"
        def last_crash_detail(self): return None
        def last_crash_id(self): return None

    sent: list[str] = []

    async def _run():
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from main import _send_startup_notification

        class _FakeBot:
            async def send_message(self, chat_id, text, parse_mode=None):
                sent.append(text)

        await _send_startup_notification(_FakeBot(), [12345], _FakeHT())


    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert len(sent) == 1
    msg = sent[0]
    assert "Resumed Monitoring" in msg


def test_startup_notification_module_basename_shown():
    """Module field should show basename, not full path."""
    import asyncio

    class _FakeHT:
        def startup_reason(self): return "crash_detected"
        def restart_count(self): return 2
        def last_heartbeat(self): return None
        def last_job_started_name(self): return "underdog_job"
        def crash_cause_label(self): return "Python Exception"
        def last_crash_detail(self):
            return {
                "crash_id": 1,
                "exc_type": "ImportError",
                "exc_msg": "no module",
                "active_job": "underdog_job",
                "active_module": "/home/runner/workspace/bot/engine/market_engine.py",
                "active_function": "_load",
                "memory_mb": None,
                "uptime_secs": None,
            }
        def last_crash_id(self): return 1

    sent = []

    async def _run():
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from main import _send_startup_notification
        class _FakeBot:
            async def send_message(self, chat_id, text, parse_mode=None):
                sent.append(text)
        await _send_startup_notification(_FakeBot(), [12345], _FakeHT())

    

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    msg = sent[0]
    assert "market_engine.py" in msg
    # Full path should not appear
    assert "/home/runner" not in msg


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — /health command shows crash forensics block
# ─────────────────────────────────────────────────────────────────────────────

def test_cmd_health_crash_section_shown(tmp_path):
    """cmd_health output must contain the Crash Forensics block."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ht = _make_tracker(tmp_path)
    ht.record_startup()
    ht.update_heartbeat()
    ht.record_crash_detail(
        "ValueError", "bad line", "traceback text",
        active_job="underdog_job",
        active_module="/bot/engine/market.py",
        active_function="_parse",
    )

    from engine import health as _hmod
    _hmod._tracker = ht

    replies = []

    async def _run():
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from commands import cmd_health

        class _Msg:
            async def reply_text(self, text, parse_mode=None):
                replies.append(text)

        class _Upd:
            message = _Msg()
            def effective_user(self): return None

        import commands as _cmd
        _cmd._ALLOWED_USER_IDS = set()  # allow all for test

        # patch _check_allowed to always return True
        orig = _cmd._check_allowed
        _cmd._check_allowed = lambda u: True
        try:
            await cmd_health(_Upd(), None)
        finally:
            _cmd._check_allowed = orig

    loop.run_until_complete(_run())
    loop.close()

    assert replies, "No reply from cmd_health"
    text = replies[0]
    assert "Crash Forensics" in text
    assert "crash ID" in text.lower() or "Crash ID" in text or "#1" in text


def test_cmd_health_no_crash_shows_none_recorded(tmp_path):
    """When no crash has occurred, health should say so clearly."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ht = _make_tracker(tmp_path)
    ht.record_startup()

    from engine import health as _hmod
    _hmod._tracker = ht

    replies = []

    async def _run():
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from commands import cmd_health
        import commands as _cmd

        class _Msg:
            async def reply_text(self, text, parse_mode=None):
                replies.append(text)

        class _Upd:
            message = _Msg()

        _cmd._check_allowed = lambda u: True
        await cmd_health(_Upd(), None)

    loop.run_until_complete(_run())
    loop.close()

    text = replies[0]
    # When no crash has occurred the Crash Forensics section is omitted entirely
    # so the health output is clean and does not alarm the user with stale data.
    assert "Crash Forensics" not in text
    assert "💥" not in text


def test_cmd_health_crash_log_shows_multiple_entries(tmp_path):
    """When multiple crashes exist, /health shows a mini crash log."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ht = _make_tracker(tmp_path)
    ht.record_startup()
    ht.record_crash_detail("Error1", "msg1", "tb1")
    ht.record_crash_detail("Error2", "msg2", "tb2")
    ht.record_crash_detail("Error3", "msg3", "tb3")

    from engine import health as _hmod
    _hmod._tracker = ht

    replies = []

    async def _run():
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from commands import cmd_health
        import commands as _cmd
        class _Msg:
            async def reply_text(self, text, parse_mode=None):
                replies.append(text)
        class _Upd:
            message = _Msg()
        _cmd._check_allowed = lambda u: True
        await cmd_health(_Upd(), None)

    loop.run_until_complete(_run())
    loop.close()

    text = replies[0]
    # Should show crash log with #1, #2, #3
    assert "#1" in text
    assert "#2" in text
    assert "#3" in text
