"""Regression coverage for the shared Underdog/PropLine cadence boundary."""

import asyncio
import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import commands
import market_engine


@pytest.mark.asyncio
async def test_shared_cycle_starts_both_branches_without_waiting_for_propline(monkeypatch):
    events = []
    underdog_release = asyncio.Event()
    completed = []

    class Health:
        def record_monitoring_cycle_complete(self):
            completed.append("complete")

    async def propline_branch(_context):
        events.append("propline-start")
        events.append("propline-end")

    async def underdog_branch(_context):
        events.append("underdog-start")
        await underdog_release.wait()
        events.append("underdog-end")

    monkeypatch.setattr(market_engine, "_run_prizepicks_independent_cycle", propline_branch)
    monkeypatch.setattr(market_engine, "get_health_tracker", lambda: Health())

    shared_cycle = market_engine._shared_monitoring_cycle(underdog_branch)
    task = asyncio.create_task(shared_cycle(SimpleNamespace()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert set(events) == {
        "propline-start",
        "propline-end",
        "underdog-start",
    }
    assert not task.done()

    underdog_release.set()
    await asyncio.sleep(0)
    assert "underdog-end" in events
    await task
    assert set(events) == {
        "propline-start",
        "underdog-start",
        "underdog-end",
        "propline-end",
    }
    assert completed == ["complete"]


@pytest.mark.asyncio
async def test_shared_cycle_uses_empty_paced_batch_without_fallback_fetch(monkeypatch):
    calls = []

    async def propline_branch(
        _context,
        *,
        _fetched_props=None,
        _fetched_sport=None,
        background_processing=False,
    ):
        calls.append((_fetched_props, _fetched_sport, background_processing))

    async def underdog_branch(_context):
        return None

    monkeypatch.setattr(
        market_engine,
        "_propline_pending_props",
        {},
    )
    monkeypatch.setattr(
        market_engine,
        "_run_prizepicks_independent_cycle",
        propline_branch,
    )

    await market_engine._shared_monitoring_cycle(underdog_branch)(SimpleNamespace())
    await asyncio.sleep(0)

    assert calls == [([], "paced", True)]


@pytest.mark.asyncio
async def test_shared_cycle_marks_paced_props_as_already_persisted(monkeypatch):
    calls = []
    prop = SimpleNamespace(player_name="Paced Player")

    async def propline_branch(
        _context,
        *,
        _fetched_props=None,
        _fetched_sport=None,
        _fetched_persisted=False,
        background_processing=False,
    ):
        calls.append((_fetched_props, _fetched_sport, _fetched_persisted))

    async def underdog_branch(_context):
        return None

    monkeypatch.setattr(
        market_engine,
        "_propline_pending_props",
        {"prop": prop},
    )
    monkeypatch.setattr(
        market_engine,
        "_run_prizepicks_independent_cycle",
        propline_branch,
    )

    await market_engine._shared_monitoring_cycle(underdog_branch)(SimpleNamespace())
    await asyncio.sleep(0)

    assert calls == [([prop], "paced", True)]


def test_scheduler_has_one_shared_monitor_owner_and_no_heartbeat_handoff():
    main_source = inspect.getsource(__import__("main").post_init)
    heartbeat_source = inspect.getsource(__import__("main")._heartbeat_job)

    assert "name=\"underdog_monitor\"" in main_source
    assert "_shared_monitoring_cycle(underdog_job)" in main_source
    assert "_start_prizepicks_cycle" not in main_source
    assert "_start_prizepicks_cycle" not in heartbeat_source
    assert "record_monitoring_cycle_complete" in inspect.getsource(
        market_engine._shared_monitoring_cycle
    )


def test_status_reports_shared_cycle_not_provider_subsections():
    source = inspect.getsource(commands.cmd_status)

    assert "Latest monitoring cycle" in source
    assert "last_monitoring_cycle_age_str" in source
    assert "PrizePicks Pipeline" not in source
    assert "Latest Underdog scan" not in source


@pytest.mark.asyncio
async def test_production_database_writes_are_deferred_and_shutdown_drains():
    calls = []
    database = market_engine.Database.__new__(market_engine.Database)

    async def write():
        calls.append("written")

    database.write = write
    await market_engine._persist_db_write(
        database,
        "test deferred write",
        lambda: database.write(),
    )

    assert calls == []
    await market_engine.shutdown_db_persistence_worker()
    assert calls == ["written"]
    assert market_engine._db_persistence_worker_task is None
    assert market_engine._db_persistence_queue is None


@pytest.mark.asyncio
async def test_database_double_keeps_awaited_compatibility_behavior():
    calls = []

    class DatabaseDouble:
        async def write(self):
            calls.append("written")

    database = DatabaseDouble()
    await market_engine._persist_db_write(
        database,
        "test compatibility write",
        lambda: database.write(),
    )

    assert calls == ["written"]