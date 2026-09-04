from __future__ import annotations

import sys
import os
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from player_history_job import (
    _API_CALL_TARGET,
    _SUPPORTED_HISTORY_SPORTS,
    _fair_cycle_targets,
    player_history_collector_job,
)


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


def test_history_api_target_budget_is_bounded():
    assert _API_CALL_TARGET == 250


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


def test_history_collector_has_no_scan_coordination_hooks():
    source = inspect.getsource(player_history_collector_job)

    assert "_underdog_scan_running" not in source
    assert "stop_player_history_collector" not in source
    assert "time_budget" not in source


@pytest.mark.asyncio
async def test_history_collector_uses_its_dedicated_database_pool():
    main_db = MagicMock()
    history_db = MagicMock()
    history_db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value={})
    history_db.get_latest_props_for_provider = AsyncMock(return_value=[])
    context = SimpleNamespace(
        bot_data={"db": main_db, "history_db": history_db}
    )

    with patch("providers.player_stats.PlayerStatsProvider"):
        await player_history_collector_job(context)

    history_db.get_active_underdog_snapshot_per_prop.assert_awaited_once()
    assert not main_db.mock_calls


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
        patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider),
    ):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_fallback_runs_independently():
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value={})
    db.get_latest_props_for_provider = AsyncMock(return_value=[])
    context = SimpleNamespace(bot_data={"db": db})

    with patch("providers.player_stats.PlayerStatsProvider"):
        await player_history_collector_job(context)

    db.get_latest_props_for_provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_player_history_persists_completed_result_independently():
    snapshot = SimpleNamespace(player="Player One", sport="NBA", selection="Points 10.5")
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Player One", "Points"): snapshot}
    )
    db.upsert_player_result = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=[SimpleNamespace()])
    with patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
            "Player One", "NBA", "points"
    )
    db.upsert_player_result.assert_awaited_once()


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
    with patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
        "PP Player", "NBA", "points"
    )


@pytest.mark.asyncio
async def test_player_history_flushes_results_in_small_durable_batches():
    snapshot = SimpleNamespace(
        player_name="Batch Player",
        sport="NBA",
        stat_type="points",
    )
    results = [
        SimpleNamespace(
            player_name="Batch Player",
            sport="NBA",
            stat_type="points",
            game_date=f"2026-08-{(i % 28) + 1:02d}",
            actual_value=float(i),
            opponent="OPP",
            source="api",
            game_id=f"game-{i}",
        )
        for i in range(26)
    ]
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Batch Player", "points"): snapshot}
    )
    db.upsert_player_results_bulk = AsyncMock()
    db.upsert_player_result = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=results)
    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=fake_provider,
    ):
        await player_history_collector_job(context)

    assert [len(call.args[0]) for call in db.upsert_player_results_bulk.await_args_list] == [
        25,
        1,
    ]
    assert not db.upsert_player_result.await_args_list


@pytest.mark.asyncio
async def test_player_history_rotates_to_next_target_on_following_run():
    snapshots = {
        (f"Player {index:03d}", "points"): SimpleNamespace(
            player_name=f"Player {index:03d}",
            sport="NBA",
            stat_type="points",
        )
        for index in range(_API_CALL_TARGET + 2)
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    db.upsert_player_result = AsyncMock()
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[])
    context = SimpleNamespace(bot_data={"db": db})

    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=provider,
    ):
        await player_history_collector_job(context)
        first_run = [call.args for call in provider.fetch_results.await_args_list]
        provider.fetch_results.reset_mock()
        await player_history_collector_job(context)
        second_run = [call.args for call in provider.fetch_results.await_args_list]

    assert len(first_run) == _API_CALL_TARGET
    assert len(second_run) == _API_CALL_TARGET
    assert second_run[:2] == [
        ("Player 250", "NBA", "points"),
        ("Player 251", "NBA", "points"),
    ]
    assert second_run[:2] not in first_run


@pytest.mark.asyncio
async def test_player_history_commits_each_target_batch_before_next_fetch():
    first_result = SimpleNamespace()
    second_result = SimpleNamespace()
    snapshots = {
        ("First Player", "points"): SimpleNamespace(
            player_name="First Player", sport="NBA", stat_type="points"
        ),
        ("Second Player", "points"): SimpleNamespace(
            player_name="Second Player", sport="NBA", stat_type="points"
        ),
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    db.upsert_player_results_bulk = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[first_result])

    events: list[str] = []

    async def fetch(*args):
        events.append("fetch")
        result = first_result if len(events) == 1 else second_result
        return [result]

    async def commit(batch):
        events.append(f"commit:{len(batch)}")

    provider.fetch_results.side_effect = fetch
    db.upsert_player_results_bulk.side_effect = commit
    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=provider,
    ):
        await player_history_collector_job(context)

    assert events == ["fetch", "commit:1", "fetch", "commit:1"]


@pytest.mark.asyncio
async def test_player_history_logs_deferred_and_failed_targets(caplog):
    snapshots = {
        ("Good Player", "points"): SimpleNamespace(
            player_name="Good Player", sport="NBA", stat_type="points"
        ),
        ("Bad Player", "points"): SimpleNamespace(
            player_name="Bad Player", sport="NBA", stat_type="points"
        ),
        ("Deferred Player", "points"): SimpleNamespace(
            player_name="Deferred Player", sport="NBA", stat_type="points"
        ),
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    context = SimpleNamespace(bot_data={"db": db})
    provider = MagicMock()
    provider.fetch_results = AsyncMock(
        side_effect=[[], RuntimeError("provider unavailable")]
    )
    with (
        patch("player_history_job._API_CALL_TARGET", 2),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=provider,
        ),
        caplog.at_level("INFO"),
    ):
        await player_history_collector_job(context)

    assert "processed=2" in caplog.text
    assert "remaining=1" in caplog.text
    assert "errors=1" in caplog.text


@pytest.mark.asyncio
async def test_player_history_cadence_stop_keeps_unprocessed_cursor():
    snapshots = {
        ("First Player", "points"): SimpleNamespace(
            player_name="First Player", sport="NBA", stat_type="points"
        ),
        ("Second Player", "points"): SimpleNamespace(
            player_name="Second Player", sport="NBA", stat_type="points"
        ),
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[])
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("player_history_job._player_history_cadence_budget", return_value=0.0),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=provider,
        ),
    ):
        await player_history_collector_job(context)

    provider.fetch_results.assert_not_awaited()
    assert "_player_history_next_target" not in context.bot_data
    assert "_player_history_cursor" not in context.bot_data
