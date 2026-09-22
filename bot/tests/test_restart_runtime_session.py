"""Regression coverage for restart-time monitoring session isolation."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.health import HealthTracker
import market_engine


def test_startup_clears_sidecar_runtime_state_but_keeps_restart_history(tmp_path: Path):
    path = tmp_path / "health.json"
    tracker = HealthTracker(path=path)
    tracker.record_startup()
    tracker.set_stable_refresh_cursor(17)
    tracker.set_wl_refresh_cursor(23)
    tracker.set_opportunity_grader_cursor(41)
    tracker.set_fpr_cursor(53)
    tracker.set_fpr_rotation(8)
    tracker.set_fpr_stats({"pool_size": 99, "cursor_end": 53})
    tracker.record_scan_checkpoint()
    tracker.record_job_run("underdog_job")
    tracker.record_provider_fetch("Underdog")
    first_session = tracker.monitoring_session_id()

    tracker.record_shutdown("clean_shutdown")
    tracker = HealthTracker(path=path)
    tracker.record_startup()

    assert tracker.monitoring_session_id() == first_session + 1
    assert tracker.get_stable_refresh_cursor() == 0
    assert tracker.get_wl_refresh_cursor() == 0
    assert tracker.get_opportunity_grader_cursor() == 0
    assert tracker.get_fpr_cursor() == 0
    assert tracker.get_fpr_rotation() == 1
    assert tracker.get_fpr_stats() == {}
    assert tracker.get_scan_checkpoint_age_minutes() is None
    assert tracker.get_all_jobs() == {}
    assert tracker.get_provider_info("Underdog") == {}
    assert tracker.heartbeat_age_seconds() is None
    assert len(tracker.restart_history()) == 2

    raw = json.loads(path.read_text())
    assert raw["restart_count"] == 2
    assert "fpr_cursor" not in raw
    assert "fpr_rotation" not in raw
    assert "opportunity_grader_cursor" not in raw
    assert "last_scan_checkpoint_ts" not in raw


@pytest.mark.asyncio
async def test_runtime_reset_cancels_child_tasks_and_clears_process_state():
    original = {
        "_prizepicks_cycle_task": market_engine._prizepicks_cycle_task,
        "_fpr_handoff_task": market_engine._fpr_handoff_task,
        "_prizepicks_processing_tasks": set(market_engine._prizepicks_processing_tasks),
        "_active_jobs": dict(market_engine._active_jobs),
        "_ud_scan_state": dict(market_engine._ud_scan_state),
        "_ud_known_keys": set(market_engine._ud_known_keys),
    }

    async def wait_forever():
        await asyncio.Event().wait()

    child_tasks = {
        asyncio.create_task(wait_forever()),
        asyncio.create_task(wait_forever()),
        asyncio.create_task(wait_forever()),
    }
    market_engine._prizepicks_cycle_task = next(iter(child_tasks))
    market_engine._fpr_handoff_task = next(iter(child_tasks - {market_engine._prizepicks_cycle_task}))
    market_engine._prizepicks_processing_tasks = (
        child_tasks
        - {
            market_engine._prizepicks_cycle_task,
            market_engine._fpr_handoff_task,
        }
    )
    market_engine._active_jobs["underdog"] = 1.0
    market_engine._ud_scan_state[("player", "stat")] = object()
    market_engine._ud_known_keys.add(("player", "stat"))

    try:
        await market_engine.reset_monitoring_runtime_state()
        assert all(task.cancelled() for task in child_tasks)
        assert market_engine._prizepicks_cycle_task is None
        assert market_engine._fpr_handoff_task is None
        assert not market_engine._prizepicks_processing_tasks
        assert not market_engine._active_jobs
        assert not market_engine._ud_scan_state
        assert not market_engine._ud_known_keys
        assert market_engine._prizepicks_sport_cursor == 0
        assert market_engine._fpr_deferred_pending is False
        assert market_engine._fpr_execution_running is False
    finally:
        # Avoid changing module state for tests that run after this one.
        market_engine._prizepicks_cycle_task = original["_prizepicks_cycle_task"]
        market_engine._fpr_handoff_task = original["_fpr_handoff_task"]
        market_engine._prizepicks_processing_tasks = original[
            "_prizepicks_processing_tasks"
        ]
        market_engine._active_jobs.update(original["_active_jobs"])
        market_engine._ud_scan_state.update(original["_ud_scan_state"])
        market_engine._ud_known_keys.update(original["_ud_known_keys"])