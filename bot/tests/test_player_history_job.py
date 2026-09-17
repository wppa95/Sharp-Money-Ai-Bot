from __future__ import annotations

import sys
import os
import asyncio
import inspect
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from player_history_job import (
    _API_CALL_TARGET,
    _SUPPORTED_HISTORY_SPORTS,
    _fair_cycle_targets,
    queue_priority_history_targets,
    _uses_propline_history,
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
    assert [target[1] for target in selected] == ["NBA", "NFL", "MLB"]


def test_fair_cycle_targets_preserves_all_when_under_limit():
    targets = [("nba-1", "NBA", "points"), ("wnba-1", "WNBA", "assists")]
    assert _fair_cycle_targets(targets, 100) == [
        ("nba-1", "NBA", "points"),
        ("wnba-1", "WNBA", "assists"),
    ]


def test_history_target_floor_covers_current_active_book():
    assert _API_CALL_TARGET >= 5200


def test_fifa_uses_existing_soccer_history_path():
    assert "FIFA" in _SUPPORTED_HISTORY_SPORTS


def test_priority_history_targets_are_deduplicated_and_normalized():
    bot_data = {}
    assert queue_priority_history_targets(
        bot_data,
        [
            ("  New Player ", "NBA", " Points "),
            ("New Player", "NBA", "points"),
            ("", "NBA", "assists"),
            ("Unknown Sport", "UNKNOWN", "points"),
        ],
    ) == 1
    assert bot_data["_player_history_priority_targets"] == {
        ("New Player", "NBA", "points")
    }


def test_football_aliases_are_history_targets():
    assert {"NFL", "NCAAF", "CFB"} <= _SUPPORTED_HISTORY_SPORTS


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
async def test_player_history_includes_football_before_provider_fetch():
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

    fake_provider.fetch_results.assert_awaited_once()


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
async def test_history_collector_never_calls_scheduled_propline_history():
    snapshot = SimpleNamespace(
        player_name="CS Player",
        sport="CS",
        stat_type="kills",
    )
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("CS Player", "CS", "kills"): snapshot}
    )
    db.upsert_player_results_bulk = AsyncMock()
    primary_provider = MagicMock()
    primary_provider.fetch_results = AsyncMock(return_value=[])
    scheduled_provider = MagicMock()
    scheduled_provider.fetch_scheduled_results = AsyncMock(
        side_effect=AssertionError("singular PropLine history request")
    )
    bot_data = {
        "db": db,
        "player_history_provider": scheduled_provider,
    }
    context = SimpleNamespace(bot_data=bot_data)

    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=primary_provider,
    ):
        await player_history_collector_job(context)

    scheduled_provider.fetch_scheduled_results.assert_not_awaited()
    assert "_player_history_propline_cursor" not in bot_data


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
async def test_priority_propline_target_is_fetched_when_active_read_misses_it():
    db = MagicMock()
    db.get_all_active_prop_line_history_by_line = AsyncMock(return_value={})
    db.get_latest_props_for_provider = AsyncMock(return_value=[])
    db.upsert_player_results_bulk = AsyncMock()
    context = SimpleNamespace(
        bot_data={
            "db": db,
            "_player_history_priority_targets": {
                ("New PP Player", "NBA", "points")
            },
        }
    )
    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=[])

    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=fake_provider,
    ):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
        "New PP Player", "NBA", "points"
    )
    assert "_player_history_priority_targets" not in context.bot_data


@pytest.mark.asyncio
async def test_player_history_skips_scheduled_propline_coverage(caplog):
    snapshots = {
        ("NBA Player", "points"): SimpleNamespace(
            player_name="NBA Player", sport="NBA", stat_type="points"
        ),
        ("Tennis Player", "sets won"): SimpleNamespace(
            player_name="Tennis Player", sport="TENNIS", stat_type="sets won"
        ),
        ("Esports Player", "kills"): SimpleNamespace(
            player_name="Esports Player", sport="CS", stat_type="kills"
        ),
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    db.upsert_player_results_bulk = AsyncMock()
    primary_provider = MagicMock()
    primary_provider.fetch_results = AsyncMock(return_value=[])
    scheduled_provider = MagicMock()

    scheduled_provider.fetch_scheduled_results = AsyncMock(
        side_effect=AssertionError("singular PropLine history request")
    )
    context = SimpleNamespace(
        bot_data={
            "db": db,
            "player_history_provider": scheduled_provider,
        }
    )

    with (
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=primary_provider,
        ),
        caplog.at_level("INFO"),
    ):
        await player_history_collector_job(context)

    assert primary_provider.fetch_results.await_count == 3
    scheduled_provider.fetch_scheduled_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_collector_does_not_schedule_propline_quota_calls(caplog):
    targets = [
        ("NHL Player", "NHL", "goals"),
        ("Tennis Player", "TENNIS", "aces"),
        ("UFC Fighter", "UFC", "takedowns"),
        ("Soccer Player", "SOCCER", "goals"),
        ("CS Player", "CS", "kills"),
    ]
    snapshots = {
        (player, sport, stat): SimpleNamespace(
            player_name=player,
            sport=sport,
            stat_type=stat,
        )
        for player, sport, stat in targets
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    db.upsert_player_results_bulk = AsyncMock()
    primary_provider = MagicMock()
    primary_provider.fetch_results = AsyncMock(return_value=[])
    scheduled_provider = MagicMock()
    scheduled_provider.fetch_scheduled_results = AsyncMock(
        side_effect=AssertionError("singular PropLine history request")
    )
    context = SimpleNamespace(
        bot_data={
            "db": db,
            "player_history_provider": scheduled_provider,
        }
    )

    with (
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=primary_provider,
        ),
        caplog.at_level("INFO"),
    ):
        for _ in targets:
            await player_history_collector_job(context)

    scheduled_provider.fetch_scheduled_results.assert_not_awaited()
    assert "propline_coverage_by_sport_market" in caplog.text


@pytest.mark.parametrize(
    ("sport", "expected"),
    [
        ("NBA", True),
        ("WNBA", True),
        ("NHL", True),
        ("TENNIS", True),
        ("SOCCER", True),
        ("CS", True),
        ("MMA", True),
        ("MLB", True),
        ("BASEBALL", True),
        ("NFL", False),
        ("NCAAF", False),
        ("CFB", False),
        ("CFL", False),
        ("FCS", False),
    ],
)
def test_propline_history_scope_excludes_only_mlb_and_football(sport, expected):
    assert _uses_propline_history(sport) is expected


@pytest.mark.asyncio
async def test_player_history_flushes_results_in_bounded_durable_batches():
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

    assert [len(call.args[0]) for call in db.upsert_player_results_bulk.await_args_list] == [26]
    assert not db.upsert_player_result.await_args_list


@pytest.mark.asyncio
async def test_player_history_attempts_and_persists_full_active_book():
    snapshots = {
        (f"Player {index:03d}", "points"): SimpleNamespace(
            player_name=f"Player {index:03d}",
            sport="NBA",
            stat_type="points",
        )
        for index in range(5200)
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    db.upsert_player_results_bulk = AsyncMock()
    db.upsert_player_result = AsyncMock()
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[SimpleNamespace()])
    context = SimpleNamespace(bot_data={"db": db})

    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=provider,
    ):
        await player_history_collector_job(context)

    assert provider.fetch_results.await_count == 5200
    assert sum(
        len(call.args[0])
        for call in db.upsert_player_results_bulk.await_args_list
    ) == 5200
    assert not db.upsert_player_result.await_args_list


@pytest.mark.asyncio
async def test_player_history_fetches_targets_concurrently_then_commits_batches():
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

    assert events == ["fetch", "fetch", "commit:2"]


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
        side_effect=[[], RuntimeError("provider unavailable"), []]
    )
    with (
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=provider,
        ),
        caplog.at_level("INFO"),
    ):
        await player_history_collector_job(context)

    assert "processed=3" in caplog.text
    assert "remaining=0" in caplog.text
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


@pytest.mark.asyncio
async def test_player_history_waits_for_all_provider_targets_in_one_cycle():
    snapshots = {
        (f"Player {index}", "points"): SimpleNamespace(
            player_name=f"Player {index}",
            sport="NBA",
            stat_type="points",
        )
        for index in range(4)
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value=snapshots
    )
    provider = MagicMock()

    async def provider_fetch(*_args):
        await asyncio.sleep(0.03)
        return []

    provider.fetch_results = provider_fetch
    context = SimpleNamespace(bot_data={"db": db})

    started = time.monotonic()
    with (
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
    ):
        await player_history_collector_job(context)

    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_player_history_provider_timeout_cannot_hold_next_cadence(caplog):
    snapshot = SimpleNamespace(
        player_name="Slow Provider",
        sport="NBA",
        stat_type="points",
    )
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Slow Provider", "points"): snapshot}
    )
    provider = MagicMock()

    async def slow_fetch(*_args):
        await asyncio.sleep(10)
        return []

    provider.fetch_results = slow_fetch
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("player_history_job._player_history_cadence_budget", return_value=0.03),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
        caplog.at_level("INFO"),
    ):
        await asyncio.wait_for(
            player_history_collector_job(context),
            timeout=0.2,
        )

    assert "budget_exhausted=True" in caplog.text


@pytest.mark.asyncio
async def test_player_history_waits_for_blocked_writer_and_retains_results(caplog):
    snapshot = SimpleNamespace(
        player_name="Blocked Writer",
        sport="NBA",
        stat_type="points",
    )
    result = SimpleNamespace()
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Blocked Writer", "points"): snapshot}
    )
    writer_started = asyncio.Event()

    async def blocked_writer(_batch):
        writer_started.set()
        await asyncio.sleep(0.01)

    db.upsert_player_results_bulk = blocked_writer
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[result])
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
        caplog.at_level("INFO"),
    ):
        await player_history_collector_job(context)

    assert writer_started.is_set()
    assert "persisted_rows=1" in caplog.text
    assert "unpersisted_rows" not in caplog.text
    assert "_player_history_pending_results" not in context.bot_data
