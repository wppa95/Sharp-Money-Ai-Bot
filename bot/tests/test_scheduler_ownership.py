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


def test_current_book_has_one_monitoring_owner_and_no_remainder_rotation():
    names = [name for name, _ in _scheduled_jobs()]

    assert names.count("underdog_monitor") == 1
    assert names.count("full_pool_rescan") == 0
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
        "opportunity_grader",
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


@pytest.mark.asyncio
async def test_launcher_discards_overlapping_trigger_without_queueing():
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

    launcher = coordinator.launch("test_launcher", callback)
    first_launcher = asyncio.create_task(launcher(None))
    await started.wait()
    await first_launcher

    # The active task is still blocked, but the second launcher returns
    # immediately and does not create a queued execution.
    await launcher(None)
    assert calls == 1
    assert len(coordinator._tasks) == 1

    release.set()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)
    assert calls == 1
    assert not coordinator._running
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_launcher_allows_different_job_names_to_run_independently():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    release = asyncio.Event()
    calls: list[str] = []

    async def callback_a(_context):
        calls.append("a")
        started["a"].set()
        await release.wait()

    async def callback_b(_context):
        calls.append("b")
        started["b"].set()
        await release.wait()

    await coordinator.launch("job_a", callback_a)(None)
    await coordinator.launch("job_b", callback_b)(None)
    await asyncio.gather(started["a"].wait(), started["b"].wait())
    assert sorted(calls) == ["a", "b"]

    release.set()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)
    assert not coordinator._running
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_launcher_exclusive_group_waits_for_other_heavy_job():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first_callback(_context):
        first_started.set()
        await release_first.wait()

    async def second_callback(_context):
        second_started.set()

    first = coordinator.launch(
        "underdog_monitor",
        first_callback,
        exclusive_group="underdog_monitoring",
    )
    second = coordinator.launch(
        "player_history_collector",
        second_callback,
        exclusive_group="underdog_monitoring",
    )

    await first(None)
    await first_started.wait()
    await second(None)
    await asyncio.sleep(0)
    assert not second_started.is_set()

    release_first.set()
    await second_started.wait()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)
    assert not coordinator._running
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_launcher_long_run_is_not_cancelled_by_later_trigger():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False

    async def callback(_context):
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    launcher = coordinator.launch("long_job", callback)
    await launcher(None)
    await started.wait()
    await launcher(None)
    await asyncio.sleep(0)
    assert not cancelled
    assert len(coordinator._tasks) == 1

    release.set()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_launcher_logs_exception_and_releases_coordinator(caplog):
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()

    async def callback(_context):
        raise RuntimeError("scheduled test failure")

    with caplog.at_level("ERROR"):
        await coordinator.launch("error_job", callback)(None)
        task = next(iter(coordinator._tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert "scheduled test failure" in caplog.text
    assert "error_job" not in coordinator._running
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_launcher_cancellation_is_awaited_and_releases_coordinator():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def callback(_context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    await coordinator.launch("cancel_job", callback)(None)
    await started.wait()
    task = next(iter(coordinator._tasks))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert finished.is_set()
    assert "cancel_job" not in coordinator._running
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_all_launched_tasks():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    finished = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def callback(name):
        async def _callback(_context):
            started[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                finished[name].set()
        return _callback

    await coordinator.launch("shutdown_a", await callback("a"))(None)
    await coordinator.launch("shutdown_b", await callback("b"))(None)
    await asyncio.gather(started["a"].wait(), started["b"].wait())

    await coordinator.shutdown()

    assert finished["a"].is_set()
    assert finished["b"].is_set()
    assert not coordinator._running
    assert not coordinator._tasks
