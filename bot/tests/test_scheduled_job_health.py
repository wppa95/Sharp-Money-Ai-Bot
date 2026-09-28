"""
Tests for health-recording correctness in scheduled bot jobs.

Reviewer requirement: every scheduled job must record exactly one run or fail per
invocation — including no-op/idle paths and all early-return exits.
"""

from __future__ import annotations

import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import main as m
from engine.health import HealthTracker


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_health() -> HealthTracker:
    tmp = Path(tempfile.mktemp(suffix=".json"))
    return HealthTracker(path=tmp)


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    return ctx


# ── _heartbeat_job ────────────────────────────────────────────────────────────

class TestHeartbeatJobHealth:
    """
    _heartbeat_job must call record_job_run on every successful tick and
    record_job_fail when update_heartbeat raises.
    """

    @pytest.mark.asyncio
    async def test_successful_tick_records_job_run(self):
        """Normal heartbeat tick records a successful run."""
        health = _make_health()
        ctx    = _make_context()
        with patch.object(m, "get_health_tracker", return_value=health):
            await m._heartbeat_job(ctx)

        info = health.get_job_info("heartbeat_job")
        assert info.get("run_count", 0) >= 1, (
            "record_job_run not called after successful _heartbeat_job tick. job_info=%s" % info
        )
        assert info.get("fail_count", 0) == 0

    @pytest.mark.asyncio
    async def test_update_heartbeat_failure_records_job_fail(self):
        """When update_heartbeat raises, record_job_fail is called."""
        health = _make_health()
        ctx    = _make_context()

        original_run = health.record_job_run

        # Patch update_heartbeat to raise
        health.update_heartbeat = MagicMock(side_effect=RuntimeError("heartbeat storage error"))

        with patch.object(m, "get_health_tracker", return_value=health):
            await m._heartbeat_job(ctx)

        info = health.get_job_info("heartbeat_job")
        assert info.get("fail_count", 0) >= 1, (
            "record_job_fail not called when update_heartbeat raises. job_info=%s" % info
        )
        assert info.get("run_count", 0) == 0


# ── _clv_seed_job guard ───────────────────────────────────────────────────────

class TestClvSeedJobGuard:
    """_clv_seed_job must record a no-op run when _db is None."""

    @pytest.mark.asyncio
    async def test_no_db_records_job_run(self):
        """When _db is None (startup/teardown) the job records a successful no-op run."""
        health = _make_health()
        ctx    = _make_context()
        with patch.object(m, "_db", None):
            with patch.object(m, "get_health_tracker", return_value=health):
                await m._clv_seed_job(ctx)

        info = health.get_job_info("_clv_seed_job")
        assert info.get("run_count", 0) >= 1, (
            "record_job_run not called when _db is None in _clv_seed_job. job_info=%s" % info
        )
        assert info.get("fail_count", 0) == 0


# ── _clv_harvest_job guard ────────────────────────────────────────────────────

class TestClvHarvestJobGuard:
    """_clv_harvest_job must record a no-op run when _db is None."""

    @pytest.mark.asyncio
    async def test_no_db_records_job_run(self):
        """When _db is None (startup/teardown) the job records a successful no-op run."""
        health = _make_health()
        ctx    = _make_context()
        with patch.object(m, "_db", None):
            with patch.object(m, "get_health_tracker", return_value=health):
                await m._clv_harvest_job(ctx)

        info = health.get_job_info("_clv_harvest_job")
        assert info.get("run_count", 0) >= 1, (
            "record_job_run not called when _db is None in _clv_harvest_job. job_info=%s" % info
        )
        assert info.get("fail_count", 0) == 0


class TestClvHarvestJobHealth:
    """
    _clv_harvest_job must record a successful run on idle (no pending seeds)
    and record a failure on import error or processing exception.
    """

    def _make_db(self, pending_seeds=None) -> MagicMock:
        db = MagicMock()
        db.get_pending_clv_seeds    = AsyncMock(return_value=pending_seeds or [])
        db.mark_clv_seed_expired    = AsyncMock()
        db.mark_clv_seeds_expired_bulk = AsyncMock()
        db.mark_clv_seed_computed   = AsyncMock()
        db.get_last_odds_for_event  = AsyncMock(return_value=None)
        db.save_clv_record          = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_idle_no_seeds_records_job_run(self):
        """When there are no pending seeds the job records a successful no-op run."""
        health = _make_health()
        db     = self._make_db(pending_seeds=[])
        ctx    = _make_context()

        with patch.object(m, "_db", db):
            with patch.object(m, "get_health_tracker", return_value=health):
                await m._clv_harvest_job(ctx)

        info = health.get_job_info("_clv_harvest_job")
        assert info.get("run_count", 0) >= 1, (
            "record_job_run not called for idle _clv_harvest_job (no pending seeds). "
            "job_info=%s" % info
        )
        assert info.get("fail_count", 0) == 0

    @pytest.mark.asyncio
    async def test_import_error_records_job_fail(self):
        """An ImportError from the clv module records a job failure."""
        health = _make_health()
        db     = self._make_db()
        ctx    = _make_context()

        with patch.object(m, "_db", db):
            with patch.object(m, "get_health_tracker", return_value=health):
                # Simulate the import failing inside _clv_harvest_job
                with patch.dict("sys.modules", {"engine.clv": None}):
                    await m._clv_harvest_job(ctx)

        info = health.get_job_info("_clv_harvest_job")
        assert info.get("fail_count", 0) >= 1, (
            "record_job_fail not called after ImportError in _clv_harvest_job. "
            "job_info=%s" % info
        )
        assert info.get("run_count", 0) == 0

    @pytest.mark.asyncio
    async def test_processing_exception_records_job_fail(self):
        """A DB exception after import succeeds records a job failure."""
        health = _make_health()
        db     = self._make_db()
        db.get_pending_clv_seeds = AsyncMock(side_effect=RuntimeError("DB gone"))
        ctx    = _make_context()

        with patch.object(m, "_db", db):
            with patch.object(m, "get_health_tracker", return_value=health):
                await m._clv_harvest_job(ctx)

        info = health.get_job_info("_clv_harvest_job")
        assert info.get("fail_count", 0) >= 1, (
            "record_job_fail not called after DB exception in _clv_harvest_job"
        )
        assert info.get("run_count", 0) == 0

    @pytest.mark.asyncio
    async def test_successful_harvest_records_job_run(self):
        """
        When seeds exist and are all expired (no closing odds), the job records
        a successful run.
        """
        from datetime import datetime, timedelta

        health = _make_health()
        now    = datetime.utcnow()

        # One seed well past grace period — will be expired
        seed = MagicMock()
        seed.id         = 1
        seed.game_time  = now - timedelta(hours=10)
        seed.bet_odds   = -110
        seed.alert_type = "SPORTSBOOK"
        seed.event      = "game-001"
        seed.selection  = "Team A ML"

        db  = self._make_db(pending_seeds=[seed])
        ctx = _make_context()

        with patch.object(m, "_db", db):
            with patch.object(m, "get_health_tracker", return_value=health):
                await m._clv_harvest_job(ctx)

        info = health.get_job_info("_clv_harvest_job")
        assert info.get("run_count", 0) >= 1, (
            "record_job_run not called after successful _clv_harvest_job with seeds. "
            "job_info=%s" % info
        )
        assert info.get("fail_count", 0) == 0
