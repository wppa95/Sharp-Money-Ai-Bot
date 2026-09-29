"""Focused tests for the bounded, restart-safe hourly opportunity grader."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import main


class _Health:
    def __init__(self, cursor: int = 0):
        self.cursor = cursor
        self.propline_event_lookup_cursor = 0
        self.propline_identity_lookup_cursor = 0
        self.started = 0
        self.runs = 0
        self.failures = []

    def get_opportunity_grader_cursor(self):
        return self.cursor

    def set_opportunity_grader_cursor(self, cursor):
        self.cursor = cursor

    def get_propline_event_lookup_cursor(self):
        return self.propline_event_lookup_cursor

    def set_propline_event_lookup_cursor(self, cursor):
        self.propline_event_lookup_cursor = cursor

    def get_propline_identity_lookup_cursor(self):
        return self.propline_identity_lookup_cursor

    def set_propline_identity_lookup_cursor(self, cursor):
        self.propline_identity_lookup_cursor = cursor

    def record_job_started(self, name):
        self.started += 1

    def record_job_run(self, name):
        self.runs += 1

    def record_job_fail(self, name, reason):
        self.failures.append((name, reason))


def _opp(
    opp_id: int,
    player: str,
    stat: str,
    *,
    recommendation="OVER",
    provider="PrizePicks",
):
    return SimpleNamespace(
        id=opp_id,
        player_name=player,
        sport="NBA",
        stat_type=stat,
        game_time=datetime.utcnow() - timedelta(hours=6),
        line_value=10.0,
        recommendation=recommendation,
        result="PENDING",
        decision_tier="B",
        confidence=60,
        provider=provider,
    )


def _context(db):
    return SimpleNamespace(bot_data={"db": db})


@pytest.mark.asyncio
async def test_grader_deduplicates_provider_fetches_and_grades_directions():
    first = _opp(11, "Player One", "Points")
    duplicate = _opp(12, "Player One", "Points")
    under = _opp(13, "Player Two", "Rebounds", recommendation="UNDER")

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[first, duplicate, under])
    db.upsert_player_result = AsyncMock()
    async def get_result(player, sport, stat, game_date, **kwargs):
        return SimpleNamespace(
            actual_value=11.0 if player == "Player One" else 9.0,
            source="player_stats",
        )

    db.get_game_result_for_grading = AsyncMock(side_effect=get_result)
    db.grade_opportunity = AsyncMock()
    health = _Health()
    fetch_calls = []

    async def fetch_results(player, sport, stat):
        fetch_calls.append((player, sport, stat))
        return []

    provider = MagicMock()
    provider.fetch_results = fetch_results
    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
    ):
        await main._grade_opportunities_job(_context(db))

    assert fetch_calls == [
        ("Player One", "NBA", "Points"),
        ("Player Two", "NBA", "Rebounds"),
    ]
    assert [call.args[:3] for call in db.grade_opportunity.await_args_list] == [
        (11, "HIT", 11.0),
        (12, "HIT", 11.0),
        (13, "HIT", 9.0),
    ]
    assert health.cursor == 13
    assert health.runs == 1


@pytest.mark.asyncio
async def test_grader_reconciles_utc_event_to_provider_previous_calendar_date():
    """Late-night UTC events must settle against the provider-local date."""
    from datetime import datetime

    row = _opp(21, "Late Night Player", "Points")
    row.game_time = datetime(2026, 9, 5, 1, 40)

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[row])
    db.upsert_player_result = AsyncMock()
    db.grade_opportunity = AsyncMock()
    health = _Health()
    lookup_dates = []

    async def get_result(player, sport, stat, game_date, **kwargs):
        lookup_dates.append(game_date)
        if game_date == "2026-09-04":
            return SimpleNamespace(actual_value=11.0, source="player_stats")
        return None

    db.get_game_result_for_grading = AsyncMock(side_effect=get_result)
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[])

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
    ):
        await main._grade_opportunities_job(_context(db))

    assert lookup_dates == ["2026-09-05", "2026-09-04"]
    db.grade_opportunity.assert_awaited_once()
    assert db.grade_opportunity.await_args.args[:3] == (21, "HIT", 11.0)
    assert health.runs == 1


@pytest.mark.asyncio
async def test_grader_reconciles_old_event_identity_without_game_time():
    """A PropLine event ID is sufficient when kickoff time was omitted."""
    row = _opp(22, "Identity Player", "Points")
    row.game_time = None
    row.game_id = "propline-event-22"

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[row])
    db.upsert_player_result = AsyncMock()
    db.grade_opportunity = AsyncMock()
    health = _Health()

    async def get_result(player, sport, stat, game_date, **kwargs):
        assert game_date is None
        assert kwargs["game_id"] == "propline-event-22"
        return SimpleNamespace(actual_value=11.0, source="player_stats")

    db.get_game_result_for_grading = AsyncMock(side_effect=get_result)
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[])

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
    ):
        await main._grade_opportunities_job(_context(db))

    db.grade_opportunity.assert_awaited_once_with(
        22,
        "HIT",
        11.0,
        error_type=None,
        grade_result_source="player_stats",
    )
    assert health.cursor == 22
    assert health.failures == []


@pytest.mark.asyncio
async def test_grader_uses_database_observation_date_when_event_time_is_missing():
    """Stored official evidence must not require a mismatched source event ID."""
    row = _opp(23, "Stored Player", "Points")
    row.game_time = None
    row.detected_at = datetime(2026, 9, 5, 18, 0)
    row.game_id = "underdog-event-23"

    stored = SimpleNamespace(
        game_date="2026-09-05",
        game_id="official-23",
        source="player_stats",
    )
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[row])
    db.get_player_results_bulk = AsyncMock(
        return_value={
            ("Stored Player", "NBA", "points"): [stored],
        }
    )
    db.get_game_result_for_grading = AsyncMock(
        side_effect=lambda player, sport, stat, game_date, **kwargs: (
            SimpleNamespace(actual_value=11.0, source="player_stats")
            if game_date == "2026-09-05"
            else None
        )
    )
    db.grade_opportunity = AsyncMock()
    health = _Health()
    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[])

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
    ):
        await main._grade_opportunities_job(_context(db))

    assert db.get_game_result_for_grading.await_args.args[3] == "2026-09-05"
    db.grade_opportunity.assert_awaited_once_with(
        23,
        "HIT",
        11.0,
        error_type=None,
        grade_result_source="player_stats",
    )
    provider.fetch_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_grader_fetches_concurrently_with_a_bounded_batch():
    opportunities = [_opp(i, f"Player {i}", "Points") for i in range(1, 31)]
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=opportunities)
    db.upsert_player_result = AsyncMock()
    db.get_game_result_for_grading = AsyncMock(return_value=None)
    health = _Health()
    active = 0
    max_active = 0

    async def fetch_results(player, sport, stat):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.001)
        active -= 1
        return []

    provider = MagicMock()
    provider.fetch_results = fetch_results
    propline_provider = SimpleNamespace(
        fetch_resolved_results=AsyncMock(return_value=[])
    )
    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))

    assert 1 < max_active <= main._GRADE_FETCH_CONCURRENCY
    db.get_pending_opportunities.assert_awaited_once_with(
        cutoff_hours=0.0, after_id=0, limit=main._GRADE_BATCH_SIZE
    )
    assert health.cursor == 30
    assert health.runs == 1
    assert (
        propline_provider.fetch_resolved_results.await_count
        == min(main._GRADE_PROPLINE_FETCH_BATCH_SIZE, len(opportunities))
    )


@pytest.mark.asyncio
async def test_grader_uses_explicit_propline_settlement_before_writing_grade():
    row = _opp(31, "PropLine Player", "Points")
    row.game_time = datetime(2026, 9, 5, 18, 0)
    row.game_id = "propline-event-31"
    row.provider = "Underdog"
    propline_result = SimpleNamespace(
        player_name="PropLine Player",
        sport="NBA",
        stat_type="points",
        game_date="2026-09-05",
        actual_value=8.0,
        source="propline_event_results",
        game_id="propline-event-31",
    )
    settlement = SimpleNamespace(
        actual_value=8.0,
        game_date="2026-09-05",
        source="propline_event_results",
        game_id="propline-event-31",
    )

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[row])
    db.get_player_results_bulk = AsyncMock(return_value={})
    db.upsert_player_results_bulk = AsyncMock()
    db.get_game_results_for_grading_bulk = AsyncMock(
        return_value={31: (settlement, "2026-09-05")}
    )
    db.grade_opportunities_bulk = AsyncMock()
    health = _Health()
    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[])
    propline_provider = SimpleNamespace(
        fetch_resolved_event_results=AsyncMock(return_value=[propline_result]),
        fetch_resolved_results=AsyncMock(return_value=[]),
    )

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))

    propline_provider.fetch_resolved_event_results.assert_awaited_once_with(
        "propline-event-31",
        "NBA",
        source_provider="Underdog",
    )
    propline_provider.fetch_resolved_results.assert_not_awaited()
    db.upsert_player_results_bulk.assert_awaited_once_with([propline_result])
    db.grade_opportunities_bulk.assert_awaited_once_with(
        [{
            "id": 31,
            "result": "MISS",
            "actual_value": 8.0,
            "error_type": "Market",
            "grade_result_source": "propline_event_results",
        }]
    )
    stats_provider.fetch_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_grader_rotates_propline_lookups_across_older_pending_targets():
    opportunities = []
    for opp_id in range(1, 121):
        row = _opp(
            opp_id,
            f"Player {opp_id}",
            "Points",
            provider="Underdog",
        )
        row.game_id = f"event-{opp_id}"
        opportunities.append(row)

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=opportunities)
    db.get_pending_tracked_props = AsyncMock(return_value=[])
    db.get_player_results_bulk = AsyncMock(return_value={})
    db.upsert_player_results_bulk = AsyncMock()
    db.get_game_results_for_grading_bulk = AsyncMock(return_value={})
    db.get_game_result_for_grading = AsyncMock(return_value=None)
    db.grade_opportunities_bulk = AsyncMock()
    health = _Health()
    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[])
    propline_provider = SimpleNamespace(
        fetch_resolved_event_results=AsyncMock(return_value=[]),
        fetch_resolved_results=AsyncMock(return_value=[]),
    )

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))
        await main._grade_opportunities_job(_context(db))

    event_calls = propline_provider.fetch_resolved_event_results.await_args_list
    first_event_ids = [call.args[0] for call in event_calls[:45]]
    second_event_ids = [call.args[0] for call in event_calls[45:90]]
    assert first_event_ids == [f"event-{i}" for i in range(1, 46)]
    assert second_event_ids == [f"event-{i}" for i in range(46, 91)]

    identity_calls = propline_provider.fetch_resolved_results.await_args_list
    assert [call.args[0] for call in identity_calls[:5]] == [
        f"Player {i}" for i in range(1, 6)
    ]
    assert [call.args[0] for call in identity_calls[5:10]] == [
        f"Player {i}" for i in range(6, 11)
    ]


@pytest.mark.asyncio
async def test_grader_fetches_event_settlement_once_for_all_event_props():
    first = _opp(34, "First Player", "Hits", provider="Underdog")
    second = _opp(
        35,
        "Second Player",
        "Runs",
        recommendation="UNDER",
        provider="Underdog",
    )
    for row in (first, second):
        row.sport = "MLB"
        row.game_time = datetime(2026, 9, 5, 18, 0)
        row.game_id = "shared-event-34"

    event_results = [
        SimpleNamespace(
            player_name="First Player",
            sport="MLB",
            stat_type="hits",
            game_date="2026-09-05",
            actual_value=12.0,
            source="propline_history",
            game_id="shared-event-34",
        ),
        SimpleNamespace(
            player_name="Second Player",
            sport="MLB",
            stat_type="runs",
            game_date="2026-09-05",
            actual_value=8.0,
            source="propline_history",
            game_id="shared-event-34",
        ),
    ]
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[first, second])
    db.get_player_results_bulk = AsyncMock(return_value={})
    db.upsert_player_results_bulk = AsyncMock()
    db.get_game_results_for_grading_bulk = AsyncMock(
        return_value={
            34: (event_results[0], "2026-09-05"),
            35: (event_results[1], "2026-09-05"),
        }
    )
    db.grade_opportunities_bulk = AsyncMock()
    health = _Health()
    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[])
    propline_provider = SimpleNamespace(
        fetch_resolved_event_results=AsyncMock(return_value=event_results),
        fetch_resolved_results=AsyncMock(return_value=[]),
    )

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))

    propline_provider.fetch_resolved_event_results.assert_awaited_once_with(
        "shared-event-34",
        "MLB",
        source_provider="Underdog",
    )
    propline_provider.fetch_resolved_results.assert_not_awaited()
    db.upsert_player_results_bulk.assert_awaited_once_with(event_results)
    db.grade_opportunities_bulk.assert_awaited_once_with(
        [
            {
                "id": 34,
                "result": "HIT",
                "actual_value": 12.0,
                "error_type": None,
                "grade_result_source": "propline_history",
            },
            {
                "id": 35,
                "result": "HIT",
                "actual_value": 8.0,
                "error_type": None,
                "grade_result_source": "propline_history",
            },
        ]
    )


@pytest.mark.asyncio
async def test_grader_deduplicates_player_history_fallback_across_events():
    first = _opp(36, "Same Player", "Points", provider="Underdog")
    second = _opp(37, "Same Player", "Points", provider="Underdog")
    first.game_id = "event-36"
    second.game_id = "event-37"

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[first, second])
    db.get_player_results_bulk = AsyncMock(return_value={})
    db.get_game_results_for_grading_bulk = AsyncMock(return_value={})
    db.grade_opportunities_bulk = AsyncMock()
    health = _Health()
    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[])
    propline_provider = SimpleNamespace(
        fetch_resolved_event_results=AsyncMock(return_value=[]),
        fetch_resolved_results=AsyncMock(return_value=[]),
    )

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))

    assert propline_provider.fetch_resolved_event_results.await_count == 2
    propline_provider.fetch_resolved_results.assert_awaited_once_with(
        "Same Player",
        "NBA",
        "Points",
        source_provider="Underdog",
    )
    db.get_game_results_for_grading_bulk.assert_awaited_once()
    db.grade_opportunities_bulk.assert_not_awaited()


@pytest.mark.asyncio
async def test_grader_does_not_use_player_stats_for_underdog_settlement():
    row = _opp(
        33,
        "Third Party Player",
        "Points",
        provider="Underdog",
    )
    row.game_time = datetime(2026, 9, 5, 18, 0)
    row.game_id = "underdog-event-33"
    player_stats_result = SimpleNamespace(
        player_name="Third Party Player",
        sport="NBA",
        stat_type="points",
        game_date="2026-09-05",
        actual_value=30.0,
        source="espn_gamelog",
        game_id="underdog-event-33",
    )

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[row])
    db.get_player_results_bulk = AsyncMock(
        return_value={
            ("Third Party Player", "NBA", "points"): [player_stats_result],
        }
    )
    db.upsert_player_results_bulk = AsyncMock()
    db.get_game_results_for_grading_bulk = AsyncMock(
        return_value={33: (player_stats_result, "2026-09-05")}
    )
    db.grade_opportunities_bulk = AsyncMock()
    health = _Health()
    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[player_stats_result])
    propline_provider = SimpleNamespace(
        fetch_resolved_event_results=AsyncMock(return_value=[]),
        fetch_resolved_results=AsyncMock(return_value=[]),
    )

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))

    stats_provider.fetch_results.assert_not_awaited()
    propline_provider.fetch_resolved_event_results.assert_awaited_once_with(
        "underdog-event-33",
        "NBA",
        source_provider="Underdog",
    )
    db.grade_opportunities_bulk.assert_not_awaited()
    assert health.runs == 1


@pytest.mark.asyncio
async def test_grader_closes_explicit_propline_void_without_numeric_history():
    row = _opp(32, "Void Tennis Player", "Aces")
    row.sport = "TENNIS"
    row.game_time = datetime(2026, 9, 22, 5, 0)
    row.game_id = "tennis-event-32"
    row.provider = "PrizePicks"
    void_result = SimpleNamespace(
        player_name="Void Tennis Player",
        sport="TENNIS",
        stat_type="aces",
        game_date="2026-09-22",
        actual_value=0.0,
        source="propline_event_results_void",
        game_id="tennis-event-32",
    )

    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[row])
    db.get_player_results_bulk = AsyncMock(return_value={})
    db.upsert_player_results_bulk = AsyncMock()
    db.get_game_results_for_grading_bulk = AsyncMock(
        return_value={
            32: (
                SimpleNamespace(
                    actual_value=2.0,
                    game_date="2026-09-22",
                    source="player_stats",
                    game_id="tennis-event-32",
                ),
                "2026-09-22",
            )
        }
    )
    db.grade_opportunities_bulk = AsyncMock()
    health = _Health()
    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[])
    propline_provider = SimpleNamespace(
        fetch_resolved_event_results=AsyncMock(return_value=[void_result]),
        fetch_resolved_results=AsyncMock(return_value=[]),
    )

    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await main._grade_opportunities_job(_context(db))

    db.upsert_player_results_bulk.assert_not_awaited()
    db.grade_opportunities_bulk.assert_awaited_once_with(
        [
            {
                "id": 32,
                "result": "VOID",
                "actual_value": 0.0,
                "error_type": None,
                "void_reason": "PropLine VOID",
                    "grade_result_source": "propline_event_results_void",
            }
        ]
    )


@pytest.mark.asyncio
async def test_grader_wraps_cursor_and_records_no_data_completion():
    row = _opp(7, "Wrap Player", "Points")
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(side_effect=[[], [row]])
    db.upsert_player_result = AsyncMock()
    db.get_game_result_for_grading = AsyncMock(return_value=None)
    db.grade_opportunity = AsyncMock()
    health = _Health(cursor=100)

    provider = MagicMock()
    provider.fetch_results = AsyncMock(return_value=[])
    with (
        patch.object(main, "get_health_tracker", return_value=health),
        patch("providers.player_stats.PlayerStatsProvider", return_value=provider),
    ):
        await main._grade_opportunities_job(_context(db))

    assert db.get_pending_opportunities.await_args_list[0].kwargs == {
        "cutoff_hours": 0.0, "after_id": 100, "limit": main._GRADE_BATCH_SIZE
    }
    assert db.get_pending_opportunities.await_args_list[1].kwargs == {
        "cutoff_hours": 0.0, "after_id": None, "limit": main._GRADE_BATCH_SIZE
    }
    assert health.cursor == 7
    assert health.runs == 1
    db.grade_opportunity.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_pending_work_still_records_a_successful_health_run():
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[])
    health = _Health()

    with patch.object(main, "get_health_tracker", return_value=health):
        await main._grade_opportunities_job(_context(db))

    assert health.started == 1
    assert health.runs == 1
    assert health.failures == []


@pytest.mark.asyncio
async def test_monitor_cycle_does_not_couple_grading_to_monitor():
    health = _Health()
    scan = AsyncMock()

    with (
        patch.object(main, "underdog_job", scan),
        patch.object(main, "get_health_tracker", return_value=health),
    ):
        await asyncio.wait_for(
            main._underdog_monitor_cycle(_context(MagicMock())),
            timeout=0.25,
        )

    scan.assert_awaited_once()
    assert health.runs == 0


@pytest.mark.asyncio
async def test_grader_settles_neutral_tracking_rows_without_directional_attributes():
    tracked = SimpleNamespace(
        id=41,
        player_name="Tracked Player",
        sport="NBA",
        stat_type="Points",
        game_time=datetime.utcnow() - timedelta(hours=6),
        line_value=10.0,
        result="PENDING",
        provider="PrizePicks",
    )
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[])
    db.get_pending_tracked_props = AsyncMock(return_value=[tracked])
    db.get_game_result_for_grading = AsyncMock(
        return_value=SimpleNamespace(actual_value=11.0, source="player_stats")
    )
    db.grade_tracked_prop = AsyncMock()
    health = _Health()

    with patch.object(main, "get_health_tracker", return_value=health):
        await main._grade_opportunities_job(_context(db))

    db.grade_tracked_prop.assert_awaited_once_with(41, "HIT", 11.0)
    assert health.failures == []


def test_grader_source_keeps_pending_selection_and_idempotent_update_contract():
    source = __import__("inspect").getsource(main._grade_opportunities_job)
    assert 'result == "PENDING"' not in source  # selection belongs in Database
    assert "get_game_result_for_grading" in source
    assert "grade_opportunity" in source
    assert "get_opportunity_grader_cursor" in source
    assert "set_opportunity_grader_cursor" in source