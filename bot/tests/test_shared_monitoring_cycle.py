"""Regression coverage for the shared Underdog/PropLine cadence boundary."""

import asyncio
import inspect
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import commands
import market_engine


@pytest.mark.asyncio
async def test_prizepicks_evaluates_before_slow_underdog_finishes(monkeypatch):
    release_ud = asyncio.Event()
    pp_evaluated = asyncio.Event()
    prop = SimpleNamespace(player_name="Peer Player")
    monkeypatch.setattr(market_engine, "_propline_ingest_task", None)
    monkeypatch.setattr(market_engine, "_prizepicks_cycle_task", None)
    monkeypatch.setattr(market_engine, "_propline_pending_props", {})

    async def ingestion(_context):
        market_engine._propline_pending_props["peer"] = prop
        return True

    async def evaluate(_context, **kwargs):
        assert kwargs["_fetched_props"] == [prop]
        assert not release_ud.is_set()
        pp_evaluated.set()

    async def underdog(_context):
        await release_ud.wait()

    monkeypatch.setattr(market_engine, "propline_request_scan_job", ingestion)
    monkeypatch.setattr(market_engine, "_run_prizepicks_independent_cycle", evaluate)
    task = asyncio.create_task(
        market_engine._shared_monitoring_cycle(underdog)(SimpleNamespace())
    )
    try:
        await asyncio.wait_for(pp_evaluated.wait(), timeout=1)
        assert not task.done()
    finally:
        release_ud.set()
        await task


@pytest.mark.asyncio
async def test_busy_prizepicks_worker_retains_next_batch_without_extra_workers(monkeypatch):
    release = asyncio.Event()
    batches = []
    fetched = []
    monkeypatch.setattr(market_engine, "_propline_ingest_task", None)
    monkeypatch.setattr(market_engine, "_prizepicks_cycle_task", None)
    monkeypatch.setattr(market_engine, "_propline_pending_props", {})
    monkeypatch.setattr(market_engine, "_PROPLINE_HANDOFF_GRACE_SECONDS", 0.001)

    async def ingestion(_context):
        prop = SimpleNamespace(player_name=f"Player {len(fetched)}")
        fetched.append(prop)
        market_engine._propline_pending_props[prop.player_name] = prop
        return True

    async def evaluate(_context, **kwargs):
        batches.append(kwargs["_fetched_props"])
        if len(batches) == 1:
            await release.wait()

    async def underdog(_context):
        return None

    monkeypatch.setattr(market_engine, "propline_request_scan_job", ingestion)
    monkeypatch.setattr(market_engine, "_run_prizepicks_independent_cycle", evaluate)
    cycle = market_engine._shared_monitoring_cycle(underdog)
    await cycle(SimpleNamespace())
    worker = market_engine._prizepicks_cycle_task
    try:
        await cycle(SimpleNamespace())
        assert market_engine._prizepicks_cycle_task is worker
        assert len(batches) == 1
    finally:
        release.set()
        await worker
    assert batches == [[fetched[0]], [fetched[1]]]
    assert not market_engine._propline_pending_props


@pytest.mark.asyncio
async def test_shared_cycle_keeps_propline_internal_to_ingestion(monkeypatch):
    events = []
    underdog_release = asyncio.Event()
    ingestion_release = asyncio.Event()
    completed = []

    class Health:
        def record_monitoring_cycle_complete(self):
            completed.append("complete")

    async def propline_ingestion(_context):
        events.append("ingestion-start")
        await ingestion_release.wait()
        events.append("ingestion-end")

    async def propline_branch(_context, **_kwargs):
        events.append("propline-start")
        events.append("propline-end")

    async def underdog_branch(_context):
        events.append("underdog-start")
        await underdog_release.wait()
        events.append("underdog-end")

    monkeypatch.setattr(market_engine, "_run_prizepicks_independent_cycle", propline_branch)
    monkeypatch.setattr(
        market_engine, "propline_request_scan_job", propline_ingestion
    )
    monkeypatch.setattr(market_engine, "get_health_tracker", lambda: Health())

    shared_cycle = market_engine._shared_monitoring_cycle(underdog_branch)
    task = asyncio.create_task(shared_cycle(SimpleNamespace()))
    for _ in range(5):
        await asyncio.sleep(0)

    assert "ingestion-start" in events
    assert "underdog-start" in events
    assert "propline-start" not in events
    assert not task.done()

    underdog_release.set()
    await asyncio.sleep(0)
    assert "underdog-end" in events
    assert not task.done()

    ingestion_release.set()
    await task
    assert set(events) == {
        "ingestion-start",
        "ingestion-end",
        "underdog-start",
        "underdog-end",
    }
    assert completed == ["complete"]


@pytest.mark.asyncio
async def test_shared_cycle_does_not_create_empty_propline_opportunity_batch(monkeypatch):
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

    assert calls == []


@pytest.mark.asyncio
async def test_shared_cycle_handoffs_propline_props_to_shared_evaluator(monkeypatch):
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

    async def propline_ingestion(_context):
        market_engine._propline_pending_props["prop"] = prop
        return True

    monkeypatch.setattr(
        market_engine,
        "_propline_pending_props",
        {},
    )
    monkeypatch.setattr(
        market_engine, "propline_request_scan_job", propline_ingestion
    )
    monkeypatch.setattr(
        market_engine,
        "_run_prizepicks_independent_cycle",
        propline_branch,
    )

    await market_engine._shared_monitoring_cycle(underdog_branch)(SimpleNamespace())
    await asyncio.sleep(0)

    assert len(calls) == 1
    assert calls[0][0] == [prop]
    assert calls[0][1] == "shared"
    assert calls[0][2] is True


@pytest.mark.asyncio
async def test_shared_cycle_bounds_cancellation_resistant_enrichment(monkeypatch):
    """A stalled optional lookup cannot delay cycle completion bookkeeping."""
    cleanup_finished = asyncio.Event()
    completed = []

    class Health:
        def record_monitoring_cycle_complete(self):
            completed.append("complete")

    async def stalled_enrichment():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            # Model an aiosqlite/provider worker that needs time to unwind.
            await asyncio.sleep(0.08)
            cleanup_finished.set()
            return "late"

    async def underdog_branch(_context):
        started = time.monotonic()
        bounded, result = await market_engine._await_task_without_cancellation_cleanup(
            stalled_enrichment(),
            timeout=0.01,
        )
        assert bounded is False
        assert result is None
        assert time.monotonic() - started < 0.06
        return {"observations_persisted": 2, "enrichment": "deferred"}

    async def propline_ingestion(_context):
        return None

    monkeypatch.setattr(market_engine, "propline_request_scan_job", propline_ingestion)
    monkeypatch.setattr(market_engine, "get_health_tracker", lambda: Health())

    result = await market_engine._shared_monitoring_cycle(underdog_branch)(
        SimpleNamespace()
    )

    assert result == {"observations_persisted": 2, "enrichment": "deferred"}
    assert completed == ["complete"]
    await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_funnel_renders_scan_cycle_when_optional_report_read_stalls(monkeypatch):
    """Funnel uses recorded scan evidence instead of turning a timeout into zero."""
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = SimpleNamespace(args=[])
    cleanup_finished = asyncio.Event()
    db = MagicMock()
    db.get_scan_cycle_summary = AsyncMock(
        return_value={
            "cycles": 1,
            "fetched": 4,
            "active": 3,
            "analyzed": 1,
            "qualified": 1,
            "alert_delivered": 0,
            "first_scan_ts": "2026-09-09T12:00:00+00:00",
            "last_scan_ts": "2026-09-09T12:00:00+00:00",
            "enrichment_status": "deferred",
            "enrichment_deferred": 2,
        }
    )

    async def stalled_funnel_read(*_args, **_kwargs):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.08)
            cleanup_finished.set()
            return None

    db.get_funnel_summary = stalled_funnel_read
    monkeypatch.setattr(commands, "_db", db)
    monkeypatch.setattr(commands, "_check_allowed", lambda _update: True)
    monkeypatch.setattr(commands, "_COMMAND_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(commands.cmd_funnel(update, context), timeout=0.2)
    message = update.message.reply_text.await_args_list[-1].args[0]

    assert "Fetched from Underdog:          <b>4</b>" in message
    assert "Optional evidence enrichment:</b> <b>deferred</b>" in message
    assert "2 observations deferred" in message
    assert "UNAVAILABLE" not in message
    await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)


def test_scheduler_has_one_underdog_monitor_owner_and_no_heartbeat_handoff():
    main_source = inspect.getsource(__import__("main").post_init)
    heartbeat_source = inspect.getsource(__import__("main")._heartbeat_job)

    assert "name=\"underdog_monitor\"" in main_source
    assert "name=\"prizepicks_monitor\"" not in main_source
    assert "_shared_monitoring_cycle" in main_source
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