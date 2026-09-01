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
async def test_shared_cycle_starts_both_branches_and_waits_for_both(monkeypatch):
    events = []
    underdog_release = asyncio.Event()
    propline_release = asyncio.Event()
    completed = []

    class Health:
        def record_monitoring_cycle_complete(self):
            completed.append("complete")

    async def propline_branch(_context):
        events.append("propline-start")
        await propline_release.wait()
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

    assert set(events) == {"propline-start", "underdog-start"}
    assert not task.done()

    underdog_release.set()
    await asyncio.sleep(0)
    assert "underdog-end" in events
    assert "propline-end" not in events
    assert not task.done()
    assert completed == []

    propline_release.set()
    await task
    assert set(events) == {
        "propline-start",
        "underdog-start",
        "underdog-end",
        "propline-end",
    }
    assert completed == ["complete"]


def test_scheduler_has_one_shared_monitor_owner_and_no_heartbeat_handoff():
    main_source = inspect.getsource(__import__("main").post_init)
    heartbeat_source = inspect.getsource(__import__("main")._heartbeat_job)

    assert "name=\"underdog_monitor\"" in main_source
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