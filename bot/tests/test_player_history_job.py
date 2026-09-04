from __future__ import annotations

import sys
import os
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from player_history_job import (
    _SUPPORTED_HISTORY_SPORTS,
    _fair_cycle_targets,
    player_history_collector_job,
    stop_player_history_collector,
)
import player_history_job
import market_engine


def test_fair_cycle_targets_round_robins_all_sports():
    targets = [
        ("nfl-1", "NFL", "passing yards"),
        ("nfl-2", "NFL", "passing yards"),
        ("nba-1", "NBA", "points"),
        ("mlb-1", "MLB", "hits"),
    ]
    selected = _fair_cycle_targets(targets, 3)
    assert [target[1] for target in selected] == ["MLB", "NBA", "NFL"]


def test_fair_cycle_targets_preserves_all_when_under_limit():
    targets = [("nba-1", "NBA", "points"), ("wnba-1", "WNBA", "assists")]
    assert _fair_cycle_targets(targets, 100) == [
        ("nba-1", "NBA", "points"),
        ("wnba-1", "WNBA", "assists"),
    ]


def test_fifa_uses_existing_soccer_history_path():
    assert "FIFA" in _SUPPORTED_HISTORY_SPORTS


def test_football_aliases_are_not_history_targets():
    assert not (_SUPPORTED_HISTORY_SPORTS & {"NFL", "NCAAF", "CFB"})


def test_history_collector_is_not_part_of_monitoring_activity_or_health():
    source = inspect.getsource(player_history_collector_job)

    assert "_job_activity_start" not in source
    assert "_job_activity_end" not in source
    assert "record_job_started" not in source
    assert "record_job_run" not in source
    assert "record_job_fail" not in source


@pytest.mark.asyncio
async def test_storage_collector_can_be_preempted_before_scan():
    task = asyncio.create_task(asyncio.sleep(60))
    player_history_job._collector_task = task

    await stop_player_history_collector()

    assert task.cancelled()
    assert player_history_job._collector_task is None


def test_all_active_scan_entry_points_preempt_storage_enrichment():
    assert "_preempt_storage_enrichment" in inspect.getsource(
        market_engine.underdog_job
    )
    assert "_preempt_storage_enrichment" in inspect.getsource(
        market_engine._stable_refresh_job
    )
    assert "_preempt_storage_enrichment" in inspect.getsource(
        market_engine._full_pool_rescan_job
    )


@pytest.mark.asyncio
async def test_player_history_skips_football_before_provider_fetch():
    football_row = SimpleNamespace(
        player_name="Football Player",
        sport="NFL",
        stat_type="passing yards",
    )
    db = MagicMock()
    db.get_all_active_prop_line_history_by_line = AsyncMock(
        return_value={("Underdog", "Football Player", "NFL", "passing yards"): football_row}
    )
    db.get_latest_props_for_provider = AsyncMock(return_value=[])
    context = SimpleNamespace(bot_data={"db": db})
    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=[])

    with (
        patch("player_history_job._underdog_scan_running", return_value=False),
        patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_yields_when_underdog_claims_slot_after_active_read():
    """The fallback query/provider work must not start after a live scan claims."""
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value={})
    db.get_latest_props_for_provider = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("player_history_job._underdog_scan_running", side_effect=[False, True]),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await player_history_collector_job(context)

    db.get_latest_props_for_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_stops_before_db_write_when_underdog_claims_after_fetch():
    """A completed stats request must not trigger writes into the hot scan."""
    snapshot = SimpleNamespace(player="Player One", sport="NBA", selection="Points 10.5")
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Player One", "Points"): snapshot}
    )
    db.upsert_player_result = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=[SimpleNamespace()])
    with (
        patch("player_history_job._underdog_scan_running", side_effect=[False, False, False, True]),
        patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
            "Player One", "NBA", "points"
    )
    db.upsert_player_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_collects_prizepicks_only_active_target():
    """PrizePicks-only rows use the same shared historical provider path."""
    pp_row = SimpleNamespace(
        player_name="PP Player",
        sport="NBA",
        stat_type="Points",
        line_value=18.5,
    )
    db = MagicMock()
    db.get_all_active_prop_line_history_by_line = AsyncMock(
        return_value={
            ("PrizePicks", "PP Player", "NBA", "Points", 18.5, "pp-1"): pp_row,
        }
    )
    db.upsert_player_result = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=[])
    with (
        patch("player_history_job._underdog_scan_running", return_value=False),
        patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
        "PP Player", "NBA", "points"
    )
