"""
Tests for engine/health.py — HealthTracker singleton.
"""
import json
import os
import sys
import time
import asyncio
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.health import HealthTracker, init_health_tracker, get_health_tracker


def _make_tracker() -> tuple[HealthTracker, Path]:
    """Return a fresh HealthTracker writing to a tmp file."""
    tmp = Path(tempfile.mktemp(suffix=".json"))
    ht = HealthTracker(path=tmp)
    return ht, tmp


# ── Startup ───────────────────────────────────────────────────────────────────

class TestRecordStartup:
    def test_increments_restart_count(self):
        ht, _ = _make_tracker()
        assert ht.restart_count() == 0
        ht.record_startup()
        assert ht.restart_count() == 1
        ht.record_startup()
        assert ht.restart_count() == 2

    def test_persists_to_file(self, tmp_path):
        p = tmp_path / "h.json"
        ht = HealthTracker(path=p)
        ht.record_startup()
        assert p.exists()
        raw = json.loads(p.read_text())
        assert raw["restart_count"] == 1
        # First-ever startup is always inferred as "first_start";
        # explicit reason kwarg is ignored (auto-detection takes precedence).
        assert raw["restart_history"][0]["reason"] == "first_start"

    def test_loads_persisted_state(self, tmp_path):
        p = tmp_path / "h.json"
        ht1 = HealthTracker(path=p)
        ht1.record_startup()
        ht2 = HealthTracker(path=p)  # reloads from file
        assert ht2.restart_count() == 1

    def test_last_startup_set(self):
        ht, _ = _make_tracker()
        ht.record_startup()
        assert ht.last_startup() is not None

    def test_restart_history_capped_at_20(self):
        ht, _ = _make_tracker()
        for _ in range(25):
            ht.record_startup()
        assert len(ht.restart_history()) == 20


# ── Heartbeat ─────────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_heartbeat_age_none_before_first_update(self):
        ht, _ = _make_tracker()
        assert ht.heartbeat_age_seconds() is None
        assert ht.heartbeat_age_str() == "—"

    def test_heartbeat_updated(self):
        ht, _ = _make_tracker()
        ht.update_heartbeat()
        age = ht.heartbeat_age_seconds()
        assert age is not None
        assert age < 5  # should be near-zero

    def test_last_heartbeat_returns_iso_string(self):
        ht, _ = _make_tracker()
        ht.update_heartbeat()
        hb = ht.last_heartbeat()
        assert hb is not None
        assert "UTC" in hb


# ── Job tracking ──────────────────────────────────────────────────────────────

class TestJobTracking:
    def test_record_job_run(self):
        ht, _ = _make_tracker()
        ht.record_job_run("test_job")
        info = ht.get_job_info("test_job")
        assert info["run_count"] == 1
        assert info["fail_streak"] == 0

    def test_record_job_fail(self):
        ht, _ = _make_tracker()
        ht.record_job_fail("test_job", "boom")
        info = ht.get_job_info("test_job")
        assert info["fail_count"] == 1
        assert info["fail_streak"] == 1
        assert "boom" in info["last_error"]

    def test_fail_streak_resets_on_run(self):
        ht, _ = _make_tracker()
        ht.record_job_fail("test_job", "err1")
        ht.record_job_fail("test_job", "err2")
        assert ht.get_job_info("test_job")["fail_streak"] == 2
        ht.record_job_run("test_job")
        assert ht.get_job_info("test_job")["fail_streak"] == 0

    def test_job_last_run_str_unknown_before_run(self):
        ht, _ = _make_tracker()
        assert ht.job_last_run_str("nonexistent") == "—"

    def test_get_all_jobs(self):
        ht, _ = _make_tracker()
        ht.record_job_run("job_a")
        ht.record_job_run("job_b")
        jobs = ht.get_all_jobs()
        assert "job_a" in jobs
        assert "job_b" in jobs

    def test_last_error_set_on_fail(self):
        ht, _ = _make_tracker()
        ht.record_job_fail("myjob", "something went wrong")
        assert "something went wrong" in (ht.last_error() or "")


# ── Provider tracking ─────────────────────────────────────────────────────────

class TestProviderTracking:
    def test_record_provider_fetch(self):
        ht, _ = _make_tracker()
        ht.record_provider_fetch("Underdog")
        info = ht.get_provider_info("Underdog")
        assert info["fetch_count"] == 1
        assert info["error_streak"] == 0

    def test_record_provider_error(self):
        ht, _ = _make_tracker()
        ht.record_provider_error("Underdog", "timeout")
        info = ht.get_provider_info("Underdog")
        assert info["error_count"] == 1
        assert info["error_streak"] == 1

    def test_error_streak_resets_on_fetch(self):
        ht, _ = _make_tracker()
        ht.record_provider_error("Underdog", "err")
        ht.record_provider_error("Underdog", "err")
        ht.record_provider_fetch("Underdog")
        assert ht.get_provider_info("Underdog")["error_streak"] == 0


# ── Telegram tracking ─────────────────────────────────────────────────────────

class TestTelegramTracking:
    def test_last_send_none_initially(self):
        ht, _ = _make_tracker()
        assert ht.last_telegram_send() is None
        assert ht.last_telegram_send_age_str() == "—"

    def test_record_telegram_send(self):
        ht, _ = _make_tracker()
        ht.record_telegram_send()
        assert ht.last_telegram_send() is not None
        age = ht.last_telegram_send_age_str()
        assert age != "—"


# ── Module singleton ──────────────────────────────────────────────────────────

class TestModuleSingleton:
    def test_get_before_init_returns_none(self):
        # Reset singleton
        import engine.health as _hmod
        _hmod._tracker = None
        assert get_health_tracker() is None

    def test_init_returns_tracker(self, tmp_path):
        p = tmp_path / "h.json"
        ht = init_health_tracker(path=p)
        assert ht is not None
        assert get_health_tracker() is ht

    def test_corrupt_file_handled_gracefully(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        ht = HealthTracker(path=p)
        # Should not raise; starts with empty state
        assert ht.restart_count() == 0
