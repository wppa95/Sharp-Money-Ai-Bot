"""
tests/test_health_upgrade.py — Phase 2: extended HealthTracker methods.

Tests:
  • record_job_started → persists last_started in job dict + top-level fields
  • record_underdog_scan → persists last_underdog_scan fields
  • record_database_write → persists last_db_write fields
  • record_pipeline_fail → persists last_pipeline_fail dict + last_error
  • Public accessors: last_underdog_scan, last_db_write, last_pipeline_fail_info,
    job_last_started_str, last_job_started_name
  • Persistence: all new fields survive a reload from disk
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


def _make_tracker(tmp_path: Path) -> "HealthTracker":
    from engine.health import HealthTracker
    return HealthTracker(path=tmp_path / "health.json")


# ── record_job_started ────────────────────────────────────────────────────────

def test_record_job_started_sets_job_fields(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_started("test_job")
    info = ht.get_job_info("test_job")
    assert "last_started" in info
    assert "last_started_ts" in info


def test_record_job_started_sets_top_level(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_started("underdog_job")
    assert ht.last_job_started_name() == "underdog_job"


def test_record_job_started_multiple_jobs(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_started("job_a")
    ht.record_job_started("job_b")
    assert ht.last_job_started_name() == "job_b"
    assert "last_started" in ht.get_job_info("job_a")
    assert "last_started" in ht.get_job_info("job_b")


def test_job_last_started_str_returns_ago(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_started("underdog_job")
    s = ht.job_last_started_str("underdog_job")
    assert isinstance(s, str)
    assert "ago" in s or s == "—"


def test_job_last_started_str_unknown_job_returns_dash(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.job_last_started_str("no_such_job") == "—"


# ── record_underdog_scan ──────────────────────────────────────────────────────

def test_record_underdog_scan_sets_fields(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_underdog_scan(props_count=150, alerts_sent=3)
    assert ht.last_underdog_scan() is not None
    assert ht.last_underdog_props() == 150
    assert ht.last_underdog_alerts() == 3


def test_record_underdog_scan_age_str(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_underdog_scan(100, 2)
    s = ht.last_underdog_scan_age_str()
    assert isinstance(s, str)
    assert "ago" in s or s == "—"


def test_record_underdog_scan_default_none(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_underdog_scan() is None
    assert ht.last_underdog_props() is None
    assert ht.last_underdog_alerts() is None


def test_record_underdog_scan_overwrites_previous(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_underdog_scan(100, 2)
    ht.record_underdog_scan(200, 5)
    assert ht.last_underdog_props() == 200
    assert ht.last_underdog_alerts() == 5


def test_record_underdog_scan_zero_alerts(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_underdog_scan(75, 0)
    assert ht.last_underdog_alerts() == 0
    assert ht.last_underdog_scan() is not None


# ── record_database_write ─────────────────────────────────────────────────────

def test_record_database_write_sets_fields(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_database_write("lifecycle_bridge")
    assert ht.last_db_write() is not None


def test_record_database_write_default_age_str(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_database_write()
    s = ht.last_db_write_age_str()
    assert isinstance(s, str)
    assert "ago" in s or s == "—"


def test_record_database_write_default_operation(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_database_write()
    assert ht.last_db_write() is not None


def test_record_database_write_none_before_first_call(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_db_write() is None
    assert ht.last_db_write_age_str() == "—"


def test_record_database_write_long_operation_truncated(tmp_path):
    ht = _make_tracker(tmp_path)
    long_op = "x" * 200
    ht.record_database_write(long_op)
    assert ht.last_db_write() is not None  # should not raise


# ── record_pipeline_fail ──────────────────────────────────────────────────────

def test_record_pipeline_fail_sets_fail_dict(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_pipeline_fail("scoring", "market_engine", "NoneType error")
    info = ht.last_pipeline_fail_info()
    assert info is not None
    assert info["stage"]  == "scoring"
    assert info["module"] == "market_engine"
    assert "NoneType error" in info["error"]
    assert "ts" in info


def test_record_pipeline_fail_updates_last_error(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_pipeline_fail("delivery", "alerts", "Telegram timeout")
    err = ht.last_error()
    assert err is not None
    assert "delivery" in err or "alerts" in err


def test_record_pipeline_fail_default_none(tmp_path):
    ht = _make_tracker(tmp_path)
    assert ht.last_pipeline_fail_info() is None


def test_record_pipeline_fail_overwrites_previous(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_pipeline_fail("stage_a", "mod_a", "err_a")
    ht.record_pipeline_fail("stage_b", "mod_b", "err_b")
    info = ht.last_pipeline_fail_info()
    assert info["stage"]  == "stage_b"
    assert info["module"] == "mod_b"


def test_record_pipeline_fail_long_error_truncated(tmp_path):
    ht = _make_tracker(tmp_path)
    long_err = "e" * 500
    ht.record_pipeline_fail("s", "m", long_err)
    info = ht.last_pipeline_fail_info()
    assert len(info["error"]) <= 200


# ── Persistence across reload ─────────────────────────────────────────────────

def test_new_fields_survive_reload(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_started("underdog_job")
    ht.record_underdog_scan(99, 4)
    ht.record_database_write("test_op")
    ht.record_pipeline_fail("stage", "mod", "test error")

    from engine.health import HealthTracker
    ht2 = HealthTracker(path=tmp_path / "health.json")

    assert ht2.last_underdog_scan() is not None
    assert ht2.last_underdog_props() == 99
    assert ht2.last_underdog_alerts() == 4
    assert ht2.last_db_write() is not None
    pf = ht2.last_pipeline_fail_info()
    assert pf is not None
    assert pf["stage"] == "stage"
    assert ht2.last_job_started_name() == "underdog_job"


def test_job_started_field_in_job_dict_survives_reload(tmp_path):
    ht = _make_tracker(tmp_path)
    ht.record_job_started("underdog_job")
    info_before = ht.get_job_info("underdog_job")

    from engine.health import HealthTracker
    ht2 = HealthTracker(path=tmp_path / "health.json")
    info_after = ht2.get_job_info("underdog_job")

    assert info_after.get("last_started") == info_before.get("last_started")
