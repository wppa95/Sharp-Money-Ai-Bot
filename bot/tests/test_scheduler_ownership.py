"""Regression tests for background-job ownership and scheduler safety."""

from __future__ import annotations

import ast
import asyncio
import re
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
            and node.func.attr in {"run_repeating", "run_once"}
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


def test_current_book_has_underdog_monitoring_and_full_pool_rotation():
    names = [name for name, _ in _scheduled_jobs()]

    assert names.count("underdog_monitor") == 1
    assert names.count("full_pool_rescan") == 1
    assert "stable_refresh" not in names
    assert "stable_refresh_job" not in names
    assert "prizepicks_monitor" not in names
    assert "prizepicks_job" not in names
    assert "connector_poller" not in names
    assert "consensus_checker" not in names


def test_required_jobs_remain_registered_with_underdog_and_grader_timers():
    names = {name for name, _ in _scheduled_jobs()}

    assert {
        "clv_seeder",
        "clv_harvester",
        "player_history_collector",
        "prop_history_pruner",
        "heartbeat",
        "opportunity_grader",
        "full_pool_rescan",
    } <= names


def test_scheduled_jobs_use_coordinator_without_max_instances():
    jobs = dict(_scheduled_jobs())

    for name in (
        "underdog_monitor",
        "full_pool_rescan",
    ):
        kwargs = _job_kwargs(jobs[name])
        assert "max_instances" not in kwargs

    source = MAIN_SOURCE.read_text()
    assert "_ScheduledExecutionCoordinator" in source
    assert "_coordinated_job(" in source


def test_monitoring_and_grading_have_distinct_cycle_owners():
    source = MAIN_SOURCE.read_text()

    assert "async def _underdog_monitor_cycle" in source
    assert "await underdog_job(context)" in source
    assert "async def _opportunity_grader_tick" in source
    grader_tick = source.split(
        "async def _opportunity_grader_tick(context):", 1
    )[1].split("\n        grader_callback =", 1)[0]
    assert "grade_auto_slip_notification_legs" in grader_tick
    assert "finally:" in grader_tick
    assert "async def _propline_scan_tick" not in source
    assert re.search(
        r'_coordinated_job\(\s*"underdog_monitor",\s*_underdog_monitor_cycle',
        source,
    )
    assert "prizepicks_monitor" not in source
    assert "prizepicks_job" not in source
    assert (
        '_coordinated_job(' in source
        and '"full_pool_rescan"' in source
        and '_full_pool_rescan_job' in source
        and 'exclusive_group=_SQLITE_WRITE_JOB_GROUP' in source
    )


def test_full_pool_monitoring_uses_underdog_cadence():
    source = MAIN_SOURCE.read_text()
    fpr_block = source[
        source.index('if config.FPR_INTERVAL > 0:')
        :source.index('        # Grading owns an independent one-shot schedule.')
    ]

    assert "interval=config.UNDERDOG_POLL_INTERVAL" in fpr_block
    assert "first=config.UNDERDOG_POLL_INTERVAL" in fpr_block


@pytest.mark.asyncio
async def test_coordinator_queues_active_trigger_and_allows_next_trigger():
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
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert calls == 2

    # A later trigger is admitted after the queued execution finishes.
    release.clear()
    third = asyncio.create_task(wrapped(None))
    await asyncio.sleep(0)
    assert calls == 3
    release.set()
    await third


@pytest.mark.asyncio
async def test_underdog_cycle_does_not_run_independent_grader(monkeypatch):
    import main

    calls: list[str] = []

    async def fake_monitor(_context):
        calls.append("monitor_start")
        await asyncio.sleep(0)
        calls.append("monitor_end")

    async def fake_grader(_context):
        calls.append("grader_should_not_run")

    monkeypatch.setattr(main, "underdog_job", fake_monitor)
    monkeypatch.setattr(main, "_grade_opportunities_job", fake_grader)

    await main._underdog_monitor_cycle(None)

    assert calls == ["monitor_start", "monitor_end"]


@pytest.mark.asyncio
async def test_launcher_queues_overlapping_trigger_without_overlap():
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

    # The active task is still blocked; the second same-name trigger is
    # queued instead of overlapping the first execution.
    second_launcher = asyncio.create_task(launcher(None))
    await second_launcher
    assert calls == 1
    assert len(coordinator._tasks) == 2

    release.set()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)
    assert calls == 2
    assert not coordinator._running
    assert not coordinator._tasks


@pytest.mark.asyncio
async def test_launcher_never_allows_same_job_overlap():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    started = []
    release = asyncio.Event()

    async def callback(_context):
        started.append(len(started) + 1)
        await release.wait()

    launcher = coordinator.launch("parallel_job", callback, allow_overlap=True)
    await launcher(None)
    await launcher(None)
    await asyncio.sleep(0)

    assert started == [1]
    assert len(coordinator._tasks) == 2

    release.set()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)
    assert started == [1, 2]
    assert not coordinator._running
    assert not coordinator._active_counts


@pytest.mark.asyncio
async def test_simultaneous_same_name_launch_is_admitted_atomically():
    from main import _ScheduledExecutionCoordinator

    coordinator = _ScheduledExecutionCoordinator()
    release = asyncio.Event()
    calls = 0

    async def callback(_context):
        nonlocal calls
        calls += 1
        await release.wait()

    launcher = coordinator.launch("atomic_job", callback)
    await asyncio.gather(launcher(None), launcher(None))
    await asyncio.sleep(0)

    assert calls == 1
    assert len(coordinator._tasks) == 2

    release.set()
    await asyncio.gather(*list(coordinator._tasks))
    assert calls == 2


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
async def test_launcher_allows_different_job_names_to_run_independently():
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
    )
    second = coordinator.launch(
        "player_history_collector",
        second_callback,
    )

    await first(None)
    await first_started.wait()
    await second(None)
    await asyncio.sleep(0)
    assert second_started.is_set()

    release_first.set()
    await asyncio.gather(*list(coordinator._tasks))
    await asyncio.sleep(0)
    assert not coordinator._running
    assert not coordinator._tasks


def test_production_database_writers_share_one_ownership_group():
    import inspect
    import main

    source = inspect.getsource(main.post_init)
    assert 'name="underdog_monitor"' in source
    assert 'name="player_history_collector"' in source
    assert 'exclusive_group=_SQLITE_WRITE_JOB_GROUP' in source
    assert "sqlite_write_jobs" in inspect.getsource(main)
    history_registration = source[
        source.index('"player_history_collector"'):
        source.index('"player_history_collector"') + 220
    ]
    assert "exclusive_group" not in history_registration


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
    assert len(coordinator._tasks) == 2

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
