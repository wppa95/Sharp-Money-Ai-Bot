"""Focused scheduler-registration regression tests."""

from __future__ import annotations

import ast
from pathlib import Path


MAIN_SOURCE = Path(__file__).parents[1].joinpath("main.py").read_text()
MAIN_TREE = ast.parse(MAIN_SOURCE)


def _registered_repeating_jobs() -> list[tuple[str, str, ast.Call]]:
    jobs = []
    for node in ast.walk(MAIN_TREE):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run_repeating"
        ):
            continue
        name_kw = next(
            (keyword for keyword in node.keywords if keyword.arg == "name"),
            None,
        )
        if (
            name_kw is not None
            and isinstance(name_kw.value, ast.Constant)
            and isinstance(name_kw.value.value, str)
        ):
            jobs.append((
                name_kw.value.value,
                ast.unparse(node.args[0]) if node.args else "",
                node,
            ))
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


def test_stable_refresh_is_not_independently_scheduled():
    jobs = _registered_repeating_jobs()

    assert all(name != "stable_refresh" for name, _, _ in jobs)
    assert all(function != "_stable_refresh_job" for _, function, _ in jobs)
    assert "_stable_refresh_job" not in MAIN_SOURCE


def test_fpr_is_the_only_registered_full_pool_rotation():
    jobs = _registered_repeating_jobs()

    assert [
        (name, function)
        for name, function, _ in jobs
        if "pool" in name.lower() or "rescan" in name.lower()
    ] == [("full_pool_rescan", "_full_pool_rescan_job")]


def test_required_monitoring_and_maintenance_jobs_remain_registered():
    names = {name for name, _, _ in _registered_repeating_jobs()}

    assert {
        "odds_poller",
        "player_props_fetcher",
        "underdog_monitor",
        "full_pool_rescan",
        "clv_seeder",
        "clv_harvester",
        "opportunity_grader",
        "player_history_collector",
        "budget_checker",
        "prop_history_pruner",
        "heartbeat",
    } <= names


def test_registered_job_names_are_unique_and_no_legacy_monitors_are_active():
    jobs = _registered_repeating_jobs()
    names = [name for name, _, _ in jobs]

    assert len(names) == len(set(names))
    assert "prizepicks_monitor" not in names
    assert "connector_poller" not in names
    assert "consensus_checker" not in names
    assert "clv_checker" not in names
    assert "steam_checker" not in names


def test_monitoring_jobs_keep_single_instance_scheduler_protection():
    jobs = {
        name: call for name, _, call in _registered_repeating_jobs()
    }

    for name in ("odds_poller", "underdog_monitor", "full_pool_rescan"):
        kwargs = _job_kwargs(jobs[name])
        assert isinstance(kwargs["max_instances"], ast.Constant)
        assert kwargs["max_instances"].value == 1