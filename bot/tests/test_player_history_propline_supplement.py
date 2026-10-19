from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers.player_history import (
    PROPLINE_COVERAGE_MALFORMED,
    PROPLINE_COVERAGE_REDACTED,
    PROPLINE_COVERAGE_UNAVAILABLE,
    PROPLINE_COVERAGE_USABLE,
    PlayerHistoryProvider,
)
from providers.propline import PropLinePlayerHistoryFetchResult
from providers.propline import (
    PropLineClient,
    _canonical_discovered_sport,
    official_player_market_for,
)


class _DB:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.saved = []

    async def get_player_results(self, player_name, sport, stat_type, limit=40):
        return list(self.results[:limit])

    async def upsert_player_result(self, result):
        self.saved.append(result)


def _primary_result(day: str, value: float = 20.0, game_id: str = ""):
    return SimpleNamespace(
        player_name="Alex Example",
        sport="NBA",
        stat_type="points",
        game_date=day,
        actual_value=value,
        opponent="TEST",
        source="espn_gamelog",
        game_id=game_id,
    )


def _propline_entry(day: str, value: float, event_id: str):
    return SimpleNamespace(
        event_id=event_id,
        commence_time=datetime.fromisoformat(day),
        actual_value=value,
        over_result="won",
        under_result="lost",
    )


def _propline_entry_with_teams(
    day: str,
    value: float,
    event_id: str,
    *,
    player_team: str = "Boston Celtics",
    home_team: str = "Boston Celtics",
    away_team: str = "New York Knicks",
):
    return SimpleNamespace(
        event_id=event_id,
        commence_time=datetime.fromisoformat(day),
        actual_value=value,
        over_result="won",
        under_result="lost",
        player_team=player_team,
        home_team=home_team,
        away_team=away_team,
    )


def _client(entries):
    client = SimpleNamespace(
        is_configured=True,
        official_sport_for=lambda sport: "basketball_nba",
        fetch_player_history=AsyncMock(
            return_value=PropLinePlayerHistoryFetchResult(
                entries=tuple(entries),
                fetched_at=datetime(2026, 8, 30),
                player_name="Alex Example",
                sport_key="basketball_nba",
                market="player_points",
                succeeded=True,
                complete=True,
            )
        ),
    )
    return client


@pytest.mark.asyncio
async def test_existing_sufficient_history_is_reused_without_propline_calls():
    existing_rows = [
        _primary_result(f"2026-08-{day:02d}T00:00:00", value=18.0 + day)
        for day in range(1, 6)
    ]
    client = _client([])
    provider = PlayerHistoryProvider(propline_client=client)

    coverage = await provider.fetch_scheduled_results(
        existing_rows,
        "Alex Example",
        "NBA",
        "points",
        min_samples=5,
    )

    assert coverage.rows == tuple(existing_rows)
    assert coverage.status == "not_needed"
    client.fetch_player_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_settlement_fetch_uses_provider_identity_and_explicit_rows():
    evidence = SimpleNamespace(
        event_id="event-1",
        player_name="Alex Example",
        sport="NBA",
        stat_type="points",
        actual_value=24.0,
        commence_time=datetime(2026, 8, 29),
        resolved_at=None,
        observed_at=None,
        is_settled=True,
    )
    client = SimpleNamespace(
        is_configured=True,
        official_sport_for=lambda sport: "basketball_nba",
        fetch_event_results=AsyncMock(
            return_value=SimpleNamespace(
                results=(evidence,),
                succeeded=True,
                complete=True,
                redacted=False,
            )
        ),
    )
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider.fetch_resolved_event_results(
        "event-1",
        "NBA",
        source_provider="Underdog",
    )

    client.fetch_event_results.assert_awaited_once_with(
        "basketball_nba",
        "event-1",
        bookmaker="underdog",
    )
    assert len(rows) == 1
    assert rows[0].player_name == "Alex Example"
    assert rows[0].actual_value == 24.0
    assert rows[0].game_id == "event-1"


def _endpoint(payload, *, succeeded=True, complete=True):
    return SimpleNamespace(
        payload=payload,
        succeeded=succeeded,
        complete=complete,
    )


def _event_stats_client(*, players, score_status="final", stats_status="final",
                        stats_event_id="event-1", stats_time=None,
                        sport_key="basketball_nba"):
    score = {
        "id": "event-1",
        "sport_key": sport_key,
        "commence_time": "2026-08-29T00:00:00Z",
        "status": score_status,
        "home_team": "Boston Celtics",
        "away_team": "New York Knicks",
    }
    stats = {
        "id": stats_event_id,
        "sport_key": sport_key,
        "commence_time": stats_time or "2026-08-29T00:00:00Z",
        "status": stats_status,
        "players": players,
    }
    return SimpleNamespace(
        is_configured=True,
        official_sport_for=lambda sport: sport_key,
        fetch_scores=AsyncMock(
            return_value=_endpoint({"events": [score]})
        ),
        fetch_event_stats=AsyncMock(return_value=_endpoint(stats)),
    )


@pytest.mark.asyncio
async def test_event_stats_backfill_persists_only_one_verified_final_join():
    db = _DB()
    client = _event_stats_client(
        players=[
            {
                "name": "Alex Example",
                "team": "Boston Celtics",
                "stats": {"points": 31},
            },
            {
                "name": "Other Player",
                "team": "New York Knicks",
                "stats": {"points": 18},
            },
        ]
    )
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider.supplement_missing_results(
        db,
        " alex   example ",
        "NBA",
        "Points",
        min_samples=1,
        source_provider="Underdog",
    )

    assert len(rows) == 1
    assert rows[0].game_id == "event-1"
    assert rows[0].game_date == "2026-08-29"
    assert rows[0].actual_value == 31.0
    assert rows[0].opponent == "New York Knicks"
    assert rows[0].source == "propline_event_stats"
    assert len(db.saved) == 1
    assert db.saved[0].game_id == "event-1"
    client.fetch_scores.assert_awaited_once_with("basketball_nba", days_from=3)
    client.fetch_event_stats.assert_awaited_once_with(
        "basketball_nba", "event-1", stat_type="points"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "players,stats_event_id,stats_time",
    [
        (
            [
                {"name": "Alex Example", "stats": {"points": 31}},
                {"name": "alex example", "stats": {"points": 32}},
            ],
            "event-1",
            None,
        ),
        (
            [{"name": "Alex Example", "stats": {"points": 31}}],
            "event-1",
            "2026-08-30T00:00:00Z",
        ),
        (
            [{"name": "Alex Example", "stats": {"points": 31}}],
            "different-event",
            None,
        ),
    ],
)
async def test_event_stats_reject_ambiguous_or_mismatched_identity_joins(
    players, stats_event_id, stats_time
):
    client = _event_stats_client(
        players=players,
        stats_event_id=stats_event_id,
        stats_time=stats_time,
    )
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider._fetch_propline_event_stats(
        "Alex Example", "NBA", "points"
    )

    assert rows == []


@pytest.mark.asyncio
async def test_event_stats_rejects_in_progress_and_provider_failure_without_writes():
    live_client = _event_stats_client(
        players=[{"name": "Alex Example", "stats": {"points": 31}}],
        score_status="in_progress",
    )
    live_provider = PlayerHistoryProvider(propline_client=live_client)
    assert await live_provider._fetch_propline_event_stats(
        "Alex Example", "NBA", "points"
    ) == []
    live_client.fetch_event_stats.assert_not_awaited()

    failed_client = _event_stats_client(
        players=[{"name": "Alex Example", "stats": {"points": 31}}]
    )
    failed_client.fetch_scores = AsyncMock(
        return_value=_endpoint(None, succeeded=False, complete=False)
    )
    failed_provider = PlayerHistoryProvider(propline_client=failed_client)
    assert await failed_provider._fetch_propline_event_stats(
        "Alex Example", "NBA", "points"
    ) == []
    failed_client.fetch_event_stats.assert_not_awaited()

    incomplete_client = _event_stats_client(
        players=[{"name": "Alex Example", "stats": {"points": 31}}]
    )
    incomplete_client.fetch_event_stats = AsyncMock(
        return_value=_endpoint(
            {"id": "event-1", "status": "final", "players": []},
            succeeded=True,
            complete=False,
        )
    )
    incomplete_provider = PlayerHistoryProvider(
        propline_client=incomplete_client
    )
    assert await incomplete_provider._fetch_propline_event_stats(
        "Alex Example", "NBA", "points"
    ) == []


@pytest.mark.asyncio
async def test_event_stats_supports_verified_football_player_stat():
    client = _event_stats_client(
        players=[{"name": "Alex Example", "stats": {"passing yards": 275}}],
        sport_key="football_nfl",
    )
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider._fetch_propline_event_stats(
        "Alex Example", "NFL", "passing yards"
    )
    assert len(rows) == 1, (
        rows,
        client.fetch_scores.await_args,
        client.fetch_event_stats.await_args_list,
    )
    assert rows[0].sport == "NFL"
    assert rows[0].stat_type == "passing yards"
    assert rows[0].actual_value == 275.0
    client.fetch_scores.assert_awaited_once_with("football_nfl", days_from=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_provider", ["PrizePicks", "Underdog"])
async def test_snapshot_uses_propline_gap_for_both_primary_providers(source_provider):
    db = _DB()
    client = _client(
        [
            _propline_entry("2026-08-29", 30.0, "game-1"),
            _propline_entry("2026-08-28", 28.0, "game-2"),
            _propline_entry("2026-08-27", 26.0, "game-3"),
            _propline_entry("2026-08-26", 24.0, "game-4"),
            _propline_entry("2026-08-25", 22.0, "game-5"),
        ]
    )
    provider = PlayerHistoryProvider(propline_client=client)
    provider._ensure_real_results = AsyncMock()

    snapshot = await provider.get_snapshot(
        db,
        "Alex Example",
        "NBA",
        "Points",
        25.5,
        source_provider=source_provider,
        ensure_real_results=True,
    )

    assert snapshot.has_real_data is False
    assert snapshot.has_supplemental_data is True
    assert snapshot.has_supporting_data(5) is True
    assert snapshot.total_supplemental_games == 5
    assert snapshot.l5 is not None
    assert snapshot.l5.over_count == 3
    assert snapshot.supplemental_sources == ("propline_history",)
    assert db.saved == []
    client.fetch_player_history.assert_awaited_once()
    expected_bookmaker = (
        "prizepicks" if source_provider == "PrizePicks" else "underdog"
    )
    assert (
        client.fetch_player_history.await_args.kwargs["bookmaker"]
        == expected_bookmaker
    )


@pytest.mark.asyncio
async def test_snapshot_computes_h2h_from_opponent_tagged_results():
    db = _DB(
        [
            _primary_result("2026-08-29", 30.0, "game-1"),
            _primary_result("2026-08-22", 28.0, "game-2"),
            _primary_result("2026-08-15", 26.0, "game-3"),
        ]
    )
    provider = PlayerHistoryProvider()

    snapshot = await provider.get_snapshot(
        db,
        "Alex Example",
        "NBA",
        "Points",
        25.5,
        opponent="TEST",
        ensure_real_results=False,
    )

    assert snapshot.h2h is not None
    assert snapshot.h2h.games == 3
    assert snapshot.h2h.over_count == 3
    assert snapshot.h2h.hit_rate == 1.0


@pytest.mark.asyncio
async def test_propline_history_preserves_opponent_when_player_team_is_known():
    client = _client(
        [
            _propline_entry_with_teams(
                "2026-08-29T00:00:00",
                30.0,
                "game-1",
            )
        ]
    )
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider._fetch_propline_history(
        "Alex Example",
        "NBA",
        "Points",
        source_provider="PrizePicks",
    )

    assert len(rows) == 1
    assert rows[0].opponent == "New York Knicks"


@pytest.mark.asyncio
async def test_history_uses_documented_composite_market_key():
    db = _DB()
    client = _client(
        [_propline_entry("2026-08-29", 42.0, "game-1")]
    )
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider._fetch_propline_history(
        "Alex Example",
        "NBA",
        "points_rebounds_assists",
        source_provider="Underdog",
    )

    assert len(rows) == 1
    assert client.fetch_player_history.await_args.args[2] == (
        "player_points_rebounds_assists"
    )
    assert client.fetch_player_history.await_args.kwargs["bookmaker"] == "underdog"


@pytest.mark.parametrize(
    "stat,market,sport",
    [
        ("Kills in Games 1+2", "player_kills_maps_1_2", "CS"),
        ("Regular Season Points", "player_points", "NBA"),
        ("Headshots on Maps 1+2", "player_headshots_maps_1_2", "CS"),
        ("Goals", "anytime_goal_scorer", "SOCCER"),
    ],
)
def test_documented_markets_have_propline_history_keys(stat, market, sport):
    assert official_player_market_for(stat, sport=sport) == market


@pytest.mark.parametrize(
    "sport,stat,market",
    [
        ("NBA", "turnovers", "player_turnovers"),
        ("NBA", "double double", "player_double_double"),
        ("NBA", "blocks + steals", "player_blocks_steals"),
        ("NBA", "three pointers made", "player_threes"),
        ("MLB", "2+ hits", "player_hits_2plus"),
        ("MLB", "2+ home runs", "player_home_runs_2plus"),
        ("MLB", "earned runs allowed", "player_earned_runs"),
        ("MLB", "pitching outs", "player_pitching_outs"),
        ("NHL", "saves", "goalie_saves"),
        ("NHL", "blocked shots", "player_blocked_shots"),
        ("NHL", "first period scorer", "player_first_period_goal"),
        ("TENNIS", "double faults", "player_double_faults"),
        ("TENNIS", "breakpoints won", "player_break_points_won"),
        ("TENNIS", "sets won", "player_sets_won"),
        ("TENNIS", "total games", "total_games"),
        ("UFC", "significant strikes", "player_significant_strikes"),
        ("MMA", "takedowns", "player_takedowns"),
        ("UFC", "fight time (mins)", "fight_time_minutes"),
        ("SOCCER", "shots", "player_shots"),
        ("SOCCER", "shots on target", "player_shots_on_target"),
        ("SOCCER", "goals + assists", "goal_or_assist"),
        ("SOCCER", "goal or assist", "goal_or_assist"),
        ("UCL", "shots", "player_shots"),
    ],
)
def test_propline_documented_non_mlb_market_routes(sport, stat, market):
    assert official_player_market_for(stat, sport=sport) == market


@pytest.mark.parametrize(
    "sport,stat",
    [
        ("KBO", "strikeouts"),
        ("NPB", "strikeouts"),
        ("GOLF", "points"),
        ("PGA Tour", "points"),
        ("UFC", "points"),
        ("MMA", "points"),
        ("SOCCER", "points"),
        ("TENNIS", "points"),
        ("LOL", "kills"),
        ("VALORANT", "kills"),
        ("DOTA 2", "fantasy points"),
        ("BOXING", "significant strikes"),
        ("TABLE TENNIS", "points"),
    ],
)
def test_unsupported_sport_stat_pairs_do_not_use_generic_market_fallback(
    sport, stat
):
    assert official_player_market_for(stat, sport=sport) is None


@pytest.mark.parametrize(
    "sport,market",
    [
        ("KBO", "player_strikeouts"),
        ("NPB", "player_strikeouts"),
        ("GOLF", "player_points"),
        ("UFC", "player_points"),
        ("UCL", "player_assists"),
        ("DOTA 2", "player_kills"),
        ("LOL", "player_kills"),
        ("VALORANT", "player_kills"),
        ("CS2", "player_kills"),
    ],
)
def test_raw_official_market_keys_are_still_sport_scoped(sport, market):
    assert official_player_market_for(market, sport=sport) is None


@pytest.mark.parametrize("sport", ["CS", "DOTA", "LOL", "VAL"])
@pytest.mark.parametrize("stat", ["kills", "fantasy points"])
def test_propline_does_not_treat_unsupported_esports_props_as_graded(
    sport, stat
):
    expected = (
        "player_kills_maps_1_2"
        if sport == "CS" and stat == "kills"
        else None
    )
    assert official_player_market_for(stat, sport=sport) == expected


def test_cs_three_map_props_are_not_routed_to_two_map_markets():
    assert (
        official_player_market_for("Kills on Maps 1+2+3", sport="CS")
        is None
    )
    assert (
        official_player_market_for("Headshots on Maps 1+2+3", sport="CS")
        is None
    )


@pytest.mark.parametrize(
    "sport,official",
    [
        ("NHL", "hockey_nhl"),
        ("FIFA", "soccer_fifa_world_cup"),
        ("CS", "esports"),
        ("DOTA", "esports"),
        ("LOL", "esports"),
        ("VAL", "esports"),
    ],
)
def test_supported_sports_resolve_to_propline_catalog_routes(sport, official):
    client = PropLineClient(api_key="test-key")
    assert client.official_sport_for(sport) == official


def test_kbo_npb_remain_active_without_propline_history_routes():
    from config import config
    from player_history_job import _SUPPORTED_HISTORY_SPORTS

    active_sports = {
        str(sport or "").strip().upper()
        for sport in config.active_sports
        if str(sport or "").strip()
    }
    client = PropLineClient(api_key="test-key")
    missing_from_history_universe = sorted(
        active_sports - _SUPPORTED_HISTORY_SPORTS
    )
    missing_provider_route = sorted(
        sport for sport in active_sports if not client.official_sport_for(sport)
    )

    assert {"KBO", "NPB"} <= active_sports
    assert {"KBO", "NPB"} <= config.ud_alert_sports
    assert missing_from_history_universe == ["KBO", "NPB"]
    assert missing_provider_route == ["KBO", "NPB"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sport,stat,official_sport,official_market",
    [
        ("NBA", "points", "basketball_nba", "player_points"),
        ("NHL", "goals", "hockey_nhl", "player_goals"),
        ("NHL", "shots on goal", "hockey_nhl", "player_shots_on_goal"),
        ("NHL", "blocked shots", "hockey_nhl", "player_blocked_shots"),
        ("NHL", "saves", "hockey_nhl", "goalie_saves"),
        ("TENNIS", "aces", "tennis", "player_aces"),
        ("TENNIS", "double faults", "tennis", "player_double_faults"),
        ("TENNIS", "break points won", "tennis", "player_break_points_won"),
        ("TENNIS", "games won", "tennis", "player_games_won"),
        ("TENNIS", "total games", "tennis", "total_games"),
        ("TENNIS", "total tiebreaks", "tennis", "total_tiebreaks"),
        ("TENNIS", "total sets", "tennis", "total_sets"),
        ("UFC", "significant strikes", "mma_ufc", "player_significant_strikes"),
        ("UFC", "takedowns", "mma_ufc", "player_takedowns"),
        ("UFC", "fight time (mins)", "mma_ufc", "fight_time_minutes"),
        ("SOCCER", "goals", "soccer_epl", "anytime_goal_scorer"),
        ("SOCCER", "goal or assist", "soccer_epl", "goal_or_assist"),
        ("CS", "kills", "cs2", "player_kills_maps_1_2"),
        ("CS", "headshots", "cs2", "player_headshots_maps_1_2"),
    ],
)
async def test_scheduled_history_uses_propline_for_non_mlb_sports(
    sport, stat, official_sport, official_market
):
    entry = _propline_entry("2026-08-29", 31.0, f"{sport}-game")
    client = _client([entry])
    client.official_sport_for = lambda requested: {
        sport: official_sport,
    }.get(requested)
    provider = PlayerHistoryProvider(propline_client=client)

    coverage = await provider.fetch_scheduled_results(
        [],
        "Alex Example",
        sport,
        stat,
        source_provider="Underdog",
    )

    assert coverage.status == PROPLINE_COVERAGE_USABLE
    assert len(coverage.rows) == 1
    assert client.fetch_player_history.await_args.args[:3] == (
        official_sport,
        "Alex Example",
        official_market,
    )


@pytest.mark.asyncio
async def test_complete_but_unresolved_history_is_not_completed_game_evidence():
    client = _client(
        [
            SimpleNamespace(
                event_id="unresolved-game",
                commence_time=datetime(2026, 8, 29),
                actual_value=None,
                over_result=None,
                under_result=None,
            )
        ]
    )
    provider = PlayerHistoryProvider(propline_client=client)

    coverage = await provider.fetch_scheduled_results(
        [],
        "Alex Example",
        "NHL",
        "goals",
    )

    assert coverage.status == PROPLINE_COVERAGE_UNAVAILABLE
    assert coverage.failure_kind == "no_settled_entries"
    assert coverage.rows == ()
    assert coverage.market == "player_goals"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,expected_status",
    [
        (
            PropLinePlayerHistoryFetchResult(
                entries=(),
                fetched_at=datetime(2026, 8, 30),
                player_name="Alex Example",
                sport_key="basketball_nba",
                market="player_points",
                succeeded=True,
                complete=False,
                redacted=True,
                failure_kind="incomplete",
            ),
            PROPLINE_COVERAGE_REDACTED,
        ),
        (
            PropLinePlayerHistoryFetchResult(
                entries=(),
                fetched_at=datetime(2026, 8, 30),
                player_name="Alex Example",
                sport_key="basketball_nba",
                market="player_points",
                succeeded=True,
                complete=False,
                failure_kind="incomplete",
            ),
            PROPLINE_COVERAGE_MALFORMED,
        ),
    ],
)
async def test_scheduled_history_reports_redacted_and_malformed_responses(
    result, expected_status
):
    client = _client([])
    client.fetch_player_history = AsyncMock(return_value=result)
    provider = PlayerHistoryProvider(propline_client=client)

    coverage = await provider.fetch_scheduled_results(
        [],
        "Alex Example",
        "NBA",
        "points",
    )

    assert coverage.status == expected_status


@pytest.mark.asyncio
async def test_scheduled_history_reports_unavailable_response():
    client = SimpleNamespace(
        is_configured=False,
        official_sport_for=lambda sport: "basketball_nba",
    )
    provider = PlayerHistoryProvider(propline_client=client)

    coverage = await provider.fetch_scheduled_results(
        [],
        "Alex Example",
        "NBA",
        "points",
    )

    assert coverage.status == PROPLINE_COVERAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_scheduled_history_reports_http_failure_as_unavailable():
    client = _client([])
    client.fetch_player_history = AsyncMock(
        return_value=PropLinePlayerHistoryFetchResult(
            entries=(),
            fetched_at=datetime(2026, 8, 30),
            player_name="Alex Example",
            sport_key="basketball_nba",
            market="player_points",
            succeeded=False,
            complete=False,
            failure_kind="http",
            error="provider unavailable",
        )
    )
    provider = PlayerHistoryProvider(propline_client=client)

    coverage = await provider.fetch_scheduled_results(
        [],
        "Alex Example",
        "NBA",
        "points",
    )

    assert coverage.status == PROPLINE_COVERAGE_UNAVAILABLE
    assert coverage.failure_kind == "http"


@pytest.mark.parametrize(
    "official_key,title,canonical",
    [
        ("esports_lol", "League of Legends", "LOL"),
        ("valorant", "Valorant", "VALORANT"),
        ("table_tennis", "Table Tennis", "TT"),
        ("dota2", "Dota 2", "DOTA"),
        ("cs2", "Counter-Strike 2", "CS"),
    ],
)
def test_discovered_esports_sports_keep_supported_bot_names(
    official_key, title, canonical
):
    assert _canonical_discovered_sport(official_key, title) == canonical


@pytest.mark.asyncio
async def test_redacted_history_is_logged_as_unusable(caplog):
    db = _DB()
    client = _client([])
    client.fetch_player_history = AsyncMock(
        return_value=PropLinePlayerHistoryFetchResult(
            entries=(),
            fetched_at=datetime(2026, 8, 30),
            player_name="Alex Example",
            sport_key="basketball_nba",
            market="player_points",
            succeeded=True,
            complete=False,
            redacted=True,
            upgrade_url="https://example.test/upgrade",
            failure_kind="incomplete",
            error="PropLine player history response is incomplete",
        )
    )
    provider = PlayerHistoryProvider(propline_client=client)

    with caplog.at_level("WARNING"):
        rows = await provider._fetch_propline_history(
            "Alex Example", "NBA", "points", source_provider="PrizePicks"
        )

    assert rows == []
    assert "redacted=True" in caplog.text
    assert "raw_entries=0" in caplog.text
    assert "upgrade_url=https://example.test/upgrade" in caplog.text


@pytest.mark.asyncio
async def test_snapshot_keeps_primary_results_and_adds_only_distinct_supplemental_games():
    # The primary provider has no event ID here; the shared date is the
    # fallback identity for matching PropLine's event-bearing row.
    db = _DB([_primary_result("2026-08-29", 30.0)])
    client = _client(
        [
            _propline_entry("2026-08-29", 30.0, "game-1"),
            _propline_entry("2026-08-28", 28.0, "game-2"),
            _propline_entry("2026-08-27", 26.0, "game-3"),
            _propline_entry("2026-08-26", 24.0, "game-4"),
        ]
    )
    provider = PlayerHistoryProvider(propline_client=client)
    provider._ensure_real_results = AsyncMock()

    snapshot = await provider.get_snapshot(
        db,
        "Alex Example",
        "NBA",
        "Points",
        25.5,
        source_provider="PrizePicks",
        ensure_real_results=True,
    )

    assert snapshot.total_real_games == 1
    assert snapshot.total_supplemental_games == 3
    assert [record.game_id for record in snapshot.supplemental_records] == [
        "game-2",
        "game-3",
        "game-4",
    ]
    assert db.saved == []


@pytest.mark.asyncio
async def test_raw_propline_games_are_normalized_only_after_final_for_all_sports():
    class _RawGamesClient:
        is_configured = True

        def official_sport_for(self, sport):
            return {"NBA": "basketball_nba", "NFL": "football_nfl"}.get(sport)

        async def fetch_player_games(self, sport, player, **kwargs):
            return SimpleNamespace(
                succeeded=True,
                complete=True,
                payload={
                    "games": [
                        {
                            "event_id": "final-1",
                            "commence_time": "2026-08-29T00:00:00Z",
                            "status": "final",
                            "opponent": "BOS",
                            "stats": {"points": 31},
                        },
                        {
                            "event_id": "live-1",
                            "commence_time": "2026-08-30T00:00:00Z",
                            "status": "in_progress",
                            "stats": {"points": 40},
                        },
                    ]
                },
            )

    provider = PlayerHistoryProvider(propline_client=_RawGamesClient())

    rows = await provider._fetch_propline_games("Alex Example", "NBA", "points")
    football_rows = await provider._fetch_propline_games("Alex Example", "NFL", "points")

    assert len(rows) == 1
    assert rows[0].game_id == "final-1"
    assert rows[0].actual_value == 31.0
    assert rows[0].source == "propline_player_games"
    assert len(football_rows) == 1
    assert football_rows[0].game_id == "final-1"
    assert football_rows[0].actual_value == 31.0


@pytest.mark.asyncio
async def test_raw_propline_games_match_documented_cs2_fields_without_filtering():
    class _CSRawGamesClient:
        is_configured = True

        def __init__(self):
            self.calls = []

        def official_sport_for(self, sport):
            return {"CS": "esports"}.get(sport)

        async def fetch_player_games(self, sport, player, **kwargs):
            self.calls.append((sport, player, kwargs))
            return SimpleNamespace(
                succeeded=True,
                complete=True,
                payload={
                    "games": [
                        {
                            "event_id": "cs-final",
                            "commence_time": "2026-08-29T00:00:00Z",
                            "status": "final",
                            "stats": {"kills_maps_1_2": 24},
                        }
                    ]
                },
            )

    client = _CSRawGamesClient()
    provider = PlayerHistoryProvider(propline_client=client)

    rows = await provider._fetch_propline_games(
        "Counter Player", "CS", "kills"
    )

    assert [(row.game_id, row.actual_value) for row in rows] == [
        ("cs-final", 24.0)
    ]
    assert client.calls == [
        ("esports", "Counter Player", {"limit": 100, "stat_type": "kills"})
    ]


@pytest.mark.asyncio
async def test_supplementation_is_not_used_when_primary_evidence_is_sufficient():
    db = _DB(
        [
            _primary_result(f"2026-08-{day:02d}", 20.0, f"primary-{day}")
            for day in range(25, 30)
        ]
    )
    client = _client([])
    provider = PlayerHistoryProvider(propline_client=client)

    snapshot = await provider.get_snapshot(
        db,
        "Alex Example",
        "NBA",
        "Points",
        25.5,
        source_provider="Underdog",
        ensure_real_results=False,
    )

    assert snapshot.total_real_games == 5
    assert snapshot.supplemental_records == []
    client.fetch_player_history.assert_not_awaited()