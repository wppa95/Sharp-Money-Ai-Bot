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
    _completed_propline_game_rows,
    _fair_cycle_targets,
    _filter_completed_propline_game_payload,
    queue_priority_history_targets,
    _uses_propline_history,
    player_history_collector_job,
)
from providers.propline import PropLineReadOnlyTarget


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
    fake_provider.fetch_results = AsyncMock(
        return_value=[
            SimpleNamespace(
                player_name="Player One",
                sport="NBA",
                stat_type="points",
                game_date="2026-09-20",
                actual_value=12,
                opponent=None,
                source="test",
            )
        ]
    )
    with patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
            "Player One", "NBA", "points"
    )
    db.upsert_player_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_player_history_does_not_persist_unusable_single_player_rows():
    snapshot = SimpleNamespace(
        player_name="Player One",
        sport="NBA",
        stat_type="points",
    )
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Player One", "NBA", "points"): snapshot}
    )
    db.upsert_player_results_bulk = AsyncMock()
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[SimpleNamespace()])
    context = SimpleNamespace(bot_data={"db": db})

    with patch("providers.player_stats.PlayerStatsProvider", return_value=provider):
        await player_history_collector_job(context)

    provider.fetch_results.assert_awaited_once_with(
        "Player One", "NBA", "points"
    )
    db.upsert_player_results_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_collector_leaves_propline_calls_to_bounded_rotation():
    snapshot = SimpleNamespace(
        player_name="NBA Player",
        sport="NBA",
        stat_type="points",
    )
    db = MagicMock()
    db.get_all_active_prop_line_history_by_line = AsyncMock(
        return_value={("Underdog", "NBA Player", "NBA", "points"): snapshot}
    )
    db.upsert_player_results_bulk = AsyncMock()
    primary_provider = MagicMock()
    primary_provider.fetch_results = AsyncMock(return_value=[])
    scheduled_provider = MagicMock()
    scheduled_provider.supplement_missing_results = AsyncMock(
        side_effect=AssertionError("unbounded PropLine supplement")
    )
    context = SimpleNamespace(
        bot_data={
            "db": db,
            "player_history_provider": scheduled_provider,
        }
    )

    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=primary_provider,
    ):
        await player_history_collector_job(context)

    scheduled_provider.supplement_missing_results.assert_not_awaited()
    primary_provider.fetch_results.assert_awaited_once_with(
        "NBA Player", "NBA", "points"
    )
    db.upsert_player_results_bulk.assert_not_awaited()


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
async def test_dota_without_propline_market_falls_back_to_player_stats():
    from providers.player_stats import RawGameResult

    dota_row = RawGameResult(
        player_name="DOTA Alias",
        sport="DOTA",
        stat_type="kills",
        game_date="2026-09-15",
        actual_value=24.0,
        opponent=None,
        source="opendota_series",
        game_id="dota-event-1",
    )
    snapshot = SimpleNamespace(
        player_name="DOTA Alias",
        sport="DOTA",
        stat_type="kills",
    )
    db = MagicMock()
    db.get_all_active_prop_line_history_by_line = AsyncMock(
        return_value={
            ("PrizePicks", "DOTA Alias", "DOTA", "kills"): snapshot,
        }
    )
    db.upsert_player_results_bulk = AsyncMock()
    primary_provider = MagicMock()
    primary_provider.fetch_results = AsyncMock(return_value=[dota_row])
    scheduled_provider = MagicMock()
    scheduled_provider._propline_client = SimpleNamespace(is_configured=True)
    scheduled_provider.supplement_missing_results = AsyncMock(return_value=[])
    context = SimpleNamespace(
        bot_data={
            "db": db,
            "player_history_provider": scheduled_provider,
        }
    )

    with patch(
        "providers.player_stats.PlayerStatsProvider",
        return_value=primary_provider,
    ):
        await player_history_collector_job(context)

    # DOTA has no documented PropLine player-market mapping. Empty history
    # must fall through to OpenDota instead of ending the lookup.
    scheduled_provider.supplement_missing_results.assert_not_awaited()
    primary_provider.fetch_results.assert_awaited_once_with(
        "DOTA Alias", "DOTA", "kills"
    )
    db.upsert_player_results_bulk.assert_awaited_once_with([dota_row])


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
        ("NFL", True),
        ("NCAAF", True),
        ("CFB", True),
        ("CFL", False),
        ("FCS", True),
    ],
)
def test_propline_history_scope_uses_supported_sport_universe(sport, expected):
    assert _uses_propline_history(sport) is expected


def test_player_games_persist_only_final_identified_rows_and_preserve_zero():
    target = PropLineReadOnlyTarget(
        family="player_games",
        official_sport="basketball_nba",
        canonical_sport="NBA",
        player_name="History Player",
        market="points",
    )
    result = SimpleNamespace(
        payload={
            "games": [
                {
                    "game_id": "game-final-zero",
                    "status": "Final",
                    "game_date": "2026-09-20T20:00:00Z",
                    "stats": {"player_points": 0},
                },
                {
                    "game_id": "game-live",
                    "status": "In Progress",
                    "game_date": "2026-09-21T20:00:00Z",
                    "stats": {"points": 42},
                },
                {
                    "game_id": "game-complete",
                    "status": "Completed",
                    "date": "2026-09-22",
                    "stats": [
                        {"stat_type": "points", "value": "14"},
                    ],
                },
            ]
        }
    )

    persisted_payload = _filter_completed_propline_game_payload(result.payload)
    rows = _completed_propline_game_rows(target, result)

    assert [game["game_id"] for game in persisted_payload["games"]] == [
        "game-final-zero",
        "game-complete",
    ]
    assert [(row.game_id, row.actual_value) for row in rows] == [
        ("game-final-zero", 0.0),
        ("game-complete", 14.0),
    ]
    assert {row.source for row in rows} == {"propline_player_games"}


def test_player_games_map_cs2_market_to_documented_maps_1_2_field():
    target = PropLineReadOnlyTarget(
        family="player_games",
        official_sport="esports",
        canonical_sport="CS",
        player_name="Counter Player",
        market="kills",
    )
    result = SimpleNamespace(
        payload={
            "games": [
                {
                    "game_id": "cs-final",
                    "status": "Final",
                    "game_date": "2026-09-20T20:00:00Z",
                    "stats": {
                        "kills_map_1": 12,
                        "kills_maps_1_2": 27,
                    },
                }
            ]
        }
    )

    rows = _completed_propline_game_rows(target, result)

    assert [(row.game_id, row.stat_type, row.actual_value) for row in rows] == [
        ("cs-final", "kills", 27.0),
    ]


def test_player_games_require_matching_player_sport_stat_and_final_value():
    target = PropLineReadOnlyTarget(
        family="player_games",
        official_sport="basketball_nba",
        canonical_sport="NBA",
        player_name="History Player",
        market="points",
    )
    result = SimpleNamespace(
        payload={
            "games": [
                {
                    "game_id": "no-stat",
                    "status": "Final",
                    "game_date": "2026-09-20",
                    "actual_value": 28,
                },
                {
                    "game_id": "wrong-stat",
                    "status": "Final",
                    "game_date": "2026-09-20",
                    "stat_type": "assists",
                    "actual_value": 8,
                },
                {
                    "game_id": "wrong-player",
                    "status": "Final",
                    "game_date": "2026-09-20",
                    "player_name": "Different Player",
                    "stats": {"points": 31},
                },
                {
                    "game_id": "wrong-sport",
                    "status": "Final",
                    "game_date": "2026-09-20",
                    "sport": "NFL",
                    "stats": {"points": 31},
                },
                {
                    "game_id": "valid",
                    "status": "Final",
                    "game_date": "2026-09-20",
                    "player_name": "History Player",
                    "sport": "NBA",
                    "stats": {"player_points": 21},
                },
            ]
        }
    )

    rows = _completed_propline_game_rows(target, result)

    assert [(row.game_id, row.stat_type, row.actual_value) for row in rows] == [
        ("valid", "points", 21.0),
    ]


@pytest.mark.parametrize(
    ("sport", "official_sport"),
    [("NFL", "football_nfl"), ("CFB", "football_ncaaf")],
)
def test_player_games_persist_verified_football_rows_to_shared_history(
    sport, official_sport
):
    target = PropLineReadOnlyTarget(
        family="player_games",
        official_sport=official_sport,
        canonical_sport=sport,
        player_name="Football Player",
        market="passing_yards",
    )
    result = SimpleNamespace(
        payload=[
            {
                "game_id": f"{sport.lower()}-final",
                "status": "Final",
                "game_date": "2026-09-20",
                "stat_type": "passing yards",
                "actual_value": 250,
            }
        ]
    )

    persisted_payload = _filter_completed_propline_game_payload(result.payload)
    rows = _completed_propline_game_rows(target, result)

    assert persisted_payload == result.payload
    assert [(row.game_id, row.actual_value) for row in rows] == [
        (f"{sport.lower()}-final", 250.0),
    ]


@pytest.mark.parametrize(
    ("sport", "market", "stats", "expected"),
    [
        (
            "NBA",
            "pts+reb+ast",
            {"points": 20, "rebounds": 7, "assists": 5},
            32.0,
        ),
        (
            "MLB",
            "hits+runs+rbis",
            [
                {"stat_type": "hits", "value": 2},
                {"stat_type": "runs", "value": 1},
                {"stat_type": "rbis", "value": 3},
            ],
            6.0,
        ),
        (
            "NFL",
            "pass+rush yards",
            [
                {"stat_type": "passing yards", "value": 220},
                {"stat_type": "rushing yards", "value": 18},
            ],
            238.0,
        ),
    ],
)
def test_player_games_aggregate_documented_multi_sport_composites(
    sport, market, stats, expected
):
    target = PropLineReadOnlyTarget(
        family="player_games",
        official_sport={"NBA": "basketball_nba", "MLB": "baseball_mlb", "NFL": "football_nfl"}[sport],
        canonical_sport=sport,
        player_name="History Player",
        market=market,
    )
    result = SimpleNamespace(
        payload={
            "games": [{
                "event_id": f"{sport.lower()}-composite",
                "status": "Final",
                "game_date": "2026-09-20",
                "player_name": "History Player",
                "sport": sport,
                "stats": stats,
            }]
        }
    )

    rows = _completed_propline_game_rows(target, result)

    assert [(row.stat_type, row.actual_value) for row in rows] == [
        (market, expected),
    ]


def test_player_games_do_not_aggregate_composites_with_missing_or_duplicate_parts():
    target = PropLineReadOnlyTarget(
        family="player_games",
        official_sport="basketball_nba",
        canonical_sport="NBA",
        player_name="History Player",
        market="pts+reb+ast",
    )
    result = SimpleNamespace(
        payload={
            "games": [
                {
                    "game_id": "missing-component",
                    "status": "Final",
                    "game_date": "2026-09-20",
                    "stats": {"points": 20, "rebounds": 7},
                },
                {
                    "game_id": "duplicate-component",
                    "status": "Final",
                    "game_date": "2026-09-21",
                    "stats": {
                        "points": 20,
                        "rebounds": 7,
                        "assists": 5,
                        "assists ": 6,
                    },
                },
            ]
        }
    )

    assert _completed_propline_game_rows(target, result) == []


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

    async def fetch_one(player, sport, stat):
        return [
            SimpleNamespace(
                player_name=player,
                sport=sport,
                stat_type=stat,
                game_date="2026-09-20",
                actual_value=1,
                opponent=None,
                source="test",
            )
        ]

    provider.fetch_results = AsyncMock(side_effect=fetch_one)
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
async def test_player_history_persists_over_legacy_20k_setting_across_sports(caplog):
    caplog.set_level("INFO")
    sports = (
        "NBA",
        "NFL",
        "MLB",
        "WNBA",
        "NHL",
        "TENNIS",
        "DOTA",
        "MMA",
    )
    rows_per_sport = 2_501
    total_rows = len(sports) * rows_per_sport
    snapshots = {
        (f"{sport} Player", "points"): SimpleNamespace(
            player_name=f"{sport} Player",
            sport=sport,
            stat_type="points",
        )
        for sport in sports
    }
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=snapshots)
    db.upsert_player_results_bulk = AsyncMock()
    db.upsert_player_result = AsyncMock()
    provider = MagicMock()

    async def fetch_rows(player, sport, stat):
        return [
            SimpleNamespace(
                player_name=player,
                sport=sport,
                stat_type=stat,
                game_date="2026-09-28",
                actual_value=float(index),
                opponent=None,
                source="test",
                game_id=f"{sport}-{index}",
            )
            for index in range(rows_per_sport)
        ]

    provider.fetch_results = AsyncMock(side_effect=fetch_rows)
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("player_history_job._API_CALL_TARGET", 20_000),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
        patch("engine.evidence_memory.put_player_results"),
    ):
        await player_history_collector_job(context)

    persisted_rows = sum(
        len(call.args[0]) for call in db.upsert_player_results_bulk.await_args_list
    )
    assert provider.fetch_results.await_count == len(sports)
    assert persisted_rows == total_rows
    assert persisted_rows > 20_000
    assert not db.upsert_player_result.await_args_list
    for sport in sports:
        assert f"'{sport}': {rows_per_sport}" in caplog.text
    assert "result_row_cap=none" in caplog.text


@pytest.mark.asyncio
async def test_player_history_fetches_targets_concurrently_then_commits_batches():
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

    events: list[str] = []

    async def fetch(player, sport, stat):
        events.append("fetch")
        return [
            SimpleNamespace(
                player_name=player,
                sport=sport,
                stat_type=stat,
                game_date="2026-09-20",
                actual_value=10,
                opponent=None,
                source="test",
            )
        ]

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
        patch("player_history_job._player_history_cadence_budget", return_value=0.2),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
        caplog.at_level("INFO"),
    ):
        await asyncio.wait_for(
            player_history_collector_job(context),
            timeout=0.5,
        )

    assert "budget_exhausted=True" in caplog.text
    assert "cycle_deadline_targets=1" in caplog.text
    assert "provider_timeout_targets=0" in caplog.text


@pytest.mark.asyncio
async def test_player_history_provider_timeouts_do_not_claim_cycle_budget(caplog):
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
        await asyncio.sleep(0.1)
        return []

    provider.fetch_results = slow_fetch
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("player_history_job._player_history_cadence_budget", return_value=1.0),
        patch(
            "player_history_job._PLAYER_HISTORY_PROVIDER_TIMEOUT_SECONDS",
            0.02,
        ),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
        caplog.at_level("INFO"),
    ):
        await player_history_collector_job(context)

    assert "deadline_targets=1" in caplog.text
    assert "provider_timeout_targets=1" in caplog.text
    assert "provider_timeout_by_sport={'NBA': 1}" in caplog.text
    assert "cycle_deadline_targets=0" in caplog.text
    assert "budget_exhausted=False" in caplog.text


@pytest.mark.asyncio
async def test_player_history_waits_for_blocked_writer_and_retains_results(caplog):
    snapshot = SimpleNamespace(
        player_name="Blocked Writer",
        sport="NBA",
        stat_type="points",
    )
    result = SimpleNamespace(
        player_name="Blocked Writer",
        sport="NBA",
        stat_type="points",
        game_date="2026-09-20",
        actual_value=10,
        opponent=None,
        source="test",
    )
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
