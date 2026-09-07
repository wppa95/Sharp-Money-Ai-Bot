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


def _endpoint(payload, *, succeeded=True, complete=True):
    return SimpleNamespace(
        payload=payload,
        succeeded=succeeded,
        complete=complete,
    )


def _event_stats_client(*, players, score_status="final", stats_status="final",
                        stats_event_id="event-1", stats_time=None):
    score = {
        "id": "event-1",
        "sport_key": "basketball_nba",
        "commence_time": "2026-08-29T00:00:00Z",
        "status": score_status,
        "home_team": "Boston Celtics",
        "away_team": "New York Knicks",
    }
    stats = {
        "id": stats_event_id,
        "sport_key": "basketball_nba",
        "commence_time": stats_time or "2026-08-29T00:00:00Z",
        "status": stats_status,
        "players": players,
    }
    return SimpleNamespace(
        is_configured=True,
        official_sport_for=lambda sport: "basketball_nba",
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
async def test_event_stats_never_fetches_football():
    client = _event_stats_client(
        players=[{"name": "Alex Example", "stats": {"points": 31}}]
    )
    client.official_sport_for = lambda sport: "football_nfl"
    provider = PlayerHistoryProvider(propline_client=client)

    assert await provider._fetch_propline_event_stats(
        "NFL", "NFL", "passing yards"
    ) == []
    client.fetch_scores.assert_not_awaited()


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

    snapshot = await provider.get_snapshot(
        db,
        "Alex Example",
        "NBA",
        "Points",
        25.5,
        source_provider=source_provider,
        ensure_real_results=False,
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
    "stat,market",
    [
        ("Fantasy Points in Games 1+2", "player_fantasy_points"),
        ("Kills in Games 1+2", "player_kills"),
        ("Regular Season Points", "player_points"),
        ("Headshots on Maps 1+2", "player_headshots"),
        ("1H Goals", "player_goals"),
    ],
)
def test_documented_esports_markets_have_propline_history_keys(stat, market):
    assert official_player_market_for(stat) == market


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sport,stat,official_sport,official_market",
    [
        ("NBA", "points", "basketball_nba", "player_points"),
        ("TENNIS", "sets won", "tennis", "player_sets_won"),
        ("CS", "kills", "cs2", "player_kills"),
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

    snapshot = await provider.get_snapshot(
        db,
        "Alex Example",
        "NBA",
        "Points",
        25.5,
        source_provider="PrizePicks",
        ensure_real_results=False,
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
async def test_raw_propline_games_are_normalized_only_after_final_and_never_for_football():
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
    assert rows[0].source == "propline_games"
    assert football_rows == []


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