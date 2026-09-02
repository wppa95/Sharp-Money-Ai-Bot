"""Regression tests for background-job ownership and scheduler safety."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

MAIN_SOURCE = Path(__file__).resolve().parents[1] / "main.py"


def _scheduled_jobs() -> list[tuple[str, ast.Call]]:
    tree = ast.parse(MAIN_SOURCE.read_text())
    jobs: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "run_repeating"
        ):
            continue
        name = next(
            (
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ),
            "",
        )
        if name:
            jobs.append((name, node))
    return jobs


def _job_kwargs(call: ast.Call) -> dict[str, ast.AST]:
    keyword = next(
        (keyword for keyword in call.keywords if keyword.arg == "job_kwargs"),
        None,
    )
    if keyword is None or not isinstance(keyword.value, ast.Dict):
        return {}
    return {
        key.value: value
        for key, value in zip(keyword.value.keys, keyword.value.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_single_owner_for_monitoring_and_full_pool_rotation():
    names = [name for name, _ in _scheduled_jobs()]

    assert names.count("underdog_monitor") == 1
    assert names.count("full_pool_rescan") == 1
    assert "stable_refresh" not in names
    assert "stable_refresh_job" not in names
    assert "prizepicks_monitor" not in names
    assert "prizepicks_job" not in names
    assert "connector_poller" not in names
    assert "consensus_checker" not in names


def test_required_independent_jobs_remain_registered():
    names = {name for name, _ in _scheduled_jobs()}

    assert {
        "odds_poller",
        "player_props_fetcher",
        "clv_seeder",
        "clv_harvester",
        "opportunity_grader",
        "player_history_collector",
        "budget_checker",
        "prop_history_pruner",
        "heartbeat",
        "season_checker",
    } <= names


def test_scheduled_jobs_use_coordinator_without_max_instances():
    jobs = dict(_scheduled_jobs())

    for name in (
        "odds_poller",
        "player_props_fetcher",
        "underdog_monitor",
        "full_pool_rescan",
        "opportunity_grader",
        "player_history_collector",
    ):
        kwargs = _job_kwargs(jobs[name])
        assert "max_instances" not in kwargs

    source = MAIN_SOURCE.read_text()
    assert "_ScheduledExecutionCoordinator" in source
    assert "_coordinated_job(" in source


@pytest.mark.asyncio
async def test_coordinator_coalesces_active_trigger_and_allows_next_trigger():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def callback(_context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    wrapped = coordinator.wrap("test_workflow", callback)
    first = asyncio.create_task(wrapped(None))
    await started.wait()
    second = asyncio.create_task(wrapped(None))
    await asyncio.sleep(0)

    assert calls == 1
    assert second.done()
    release.set()
    await first

    # A later trigger is not queued or lost after the active execution ends.
    release.clear()
    third = asyncio.create_task(wrapped(None))
    await asyncio.sleep(0)
    assert calls == 2
    release.set()
    await third
