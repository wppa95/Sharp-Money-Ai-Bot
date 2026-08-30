"""Focused tests for the authorized PropLine → PrizePicks adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from providers.propline import (
    PropLineClient,
    PropLinePrizePicksProvider,
    normalize_propline_prizepicks_record,
)
from providers.prop_provider import PlayerProp
from engine.provider_confirmation import (
    ConfirmationObservation,
    ConfirmationTarget,
    confirm_market,
)


class _Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class _QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def test_normalization_preserves_prizepicks_identity_and_metadata():
    observed = datetime(2026, 8, 27, 18, 0)
    prop = normalize_propline_prizepicks_record(
        {
            "provider": "PrizePicks",
            "market_type": "player_prop",
            "id": "projection-1",
            "player": {"id": "player-1", "name": "Alex Example"},
            "sport": "basketball",
            "stat": "PTS",
            "line": 24.5,
            "direction": "Over",
            "team": "Example City",
            "event": {
                "id": "game-1",
                "name": "Example City at Test Town",
                "start_time": "2026-08-28T00:00:00Z",
            },
            "updated_at": "2026-08-27T17:59:00Z",
            "status": "available",
        },
        fetched_at=observed,
    )

    assert prop is not None
    assert prop.provider == "PrizePicks"
    assert prop.player_name == "Alex Example"
    assert prop.player_id == "player-1"
    assert prop.sport == "NBA"
    assert prop.stat_type == "points"
    assert prop.line_value == 24.5
    assert prop.direction == "OVER"
    assert prop.event_id == "game-1"
    assert prop.external_id == "projection-1"
    assert prop.game_time == datetime(2026, 8, 28, 0, 0)
    assert prop.provider_status == "available"
    assert prop.source_timestamp == datetime(2026, 8, 27, 17, 59)


@pytest.mark.parametrize(
    "record",
    [
        {"provider": "Underdog", "market_type": "player_prop", "player_name": "A", "sport": "NBA", "stat": "Points", "line": 20},
        {"provider": "PrizePicks", "market_type": "moneyline", "player_name": "A", "sport": "NBA", "stat": "Points", "line": 20},
        {"provider": "PrizePicks", "market_type": "game_total", "player_name": "A", "sport": "NBA", "stat": "Points", "line": 20},
        {"provider": "PrizePicks", "market_type": "player_prop", "player_name": "A", "sport": "NBA", "stat": "Points", "line": "not-a-line"},
    ],
)
def test_only_prizepicks_player_props_are_accepted(record):
    assert normalize_propline_prizepicks_record(record) is None


def test_missing_key_disables_provider_without_request():
    client = PropLineClient(api_key="", api_url="https://example.invalid", min_request_interval=0)
    provider = PropLinePrizePicksProvider(client=client)

    assert provider.is_available() is False
    assert asyncio.run(provider.fetch_props()) == []


def test_client_uses_key_without_logging_or_polling_other_sources():
    session = _Session(
        _Response(
            200,
            {"data": [{
                "provider": "PrizePicks",
                "market": "player_props",
                "player_name": "Alex Example",
                "sport": "NBA",
                "stat_type": "Points",
                "line": 24.5,
                "id": "pp-1",
            }]},
        )
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://authorized.example/props",
        session=session,
        min_request_interval=0,
    )

    props = asyncio.run(PropLinePrizePicksProvider(client=client).fetch_props())

    assert len(props) == 1
    assert props[0].provider == "PrizePicks"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "https://authorized.example/props"
    assert session.calls[0][1]["params"] == {
        "provider": "prizepicks",
        "market": "player_props",
    }


def test_official_odds_route_accepts_standard_prizepicks_props_only():
    session = _Session(
        _Response(
            200,
            [
                {
                    "id": "game-1",
                    "sport_key": "basketball_nba",
                    "home_team": "Test Town",
                    "away_team": "Example City",
                    "commence_time": "2026-08-28T00:00:00Z",
                    "bookmakers": [
                        {
                            "key": "prizepicks",
                            "markets": [
                                {
                                    "key": "player_points",
                                    "last_update": "2026-08-27T18:00:00Z",
                                    "outcomes": [
                                        {
                                            "description": "Alex Example",
                                            "player_id": "player-1",
                                            "name": "Over",
                                            "point": 24.5,
                                            "dfs_odds_type": "standard",
                                            "last_change_at": "2026-08-27T17:59:00Z",
                                        },
                                        {
                                            "description": "Alex Example",
                                            "player_id": "player-1",
                                            "name": "Under",
                                            "point": 24.5,
                                            "dfs_odds_type": "standard",
                                            "last_change_at": "2026-08-27T17:59:00Z",
                                        },
                                        {
                                            "description": "Alex Example",
                                            "player_id": "player-1",
                                            "name": "Over",
                                            "point": 26.5,
                                            "dfs_odds_type": "demon",
                                            "last_change_at": "2026-08-27T17:59:00Z",
                                        },
                                    ],
                                },
                                {
                                    "key": "h2h",
                                    "outcomes": [
                                        {"description": "Example City", "name": "Over", "point": 1}
                                    ],
                                },
                            ],
                        },
                        {
                            "key": "draftkings",
                            "markets": [
                                {
                                    "key": "player_points",
                                    "outcomes": [
                                        {"description": "Other Player", "name": "Over", "point": 20}
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        )
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
    )

    props = asyncio.run(
        PropLinePrizePicksProvider(client=client).fetch_props_for_sport("NBA")
    )

    assert len(props) == 2
    assert {prop.direction for prop in props} == {"OVER", "UNDER"}
    assert all(prop.provider == "PrizePicks" for prop in props)
    assert all(prop.sport == "NBA" for prop in props)
    assert session.calls[0][0] == "https://api.prop-line.com/v1/sports/basketball_nba/odds"
    assert session.calls[0][1]["params"]["bookmakers"] == "prizepicks"
    assert "player_points" in session.calls[0][1]["params"]["markets"]


def test_official_sports_discovery_returns_active_sports_only():
    session = _Session(
        _Response(
            200,
            [
                {"key": "basketball_nba", "title": "NBA", "active": True},
                {"key": "football_ncaaf", "title": "NCAAF", "active": True},
                {"key": "boxing", "title": "Boxing", "active": False},
                {"key": "", "title": "Invalid", "active": True},
            ],
        )
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
    )

    sports = asyncio.run(client.discover_sports())

    assert [(sport.official_key, sport.canonical_key) for sport in sports] == [
        ("basketball_nba", "NBA"),
        ("football_ncaaf", "NCAAF"),
    ]
    assert session.calls[0][0] == "https://api.prop-line.com/v1/sports"


def test_bulk_fetch_discovers_and_fetches_multiple_sports_without_nba_only_path():
    discovery = _Response(
        200,
        [
            {"key": "basketball_nba", "title": "NBA", "active": True},
            {"key": "football_nfl", "title": "NFL", "active": True},
            {"key": "boxing", "title": "Boxing", "active": True},
        ],
    )
    nba = _Response(
        200,
        [
            {
                "id": "nba-game",
                "sport_key": "basketball_nba",
                "bookmakers": [{
                    "key": "prizepicks",
                    "markets": [{
                        "key": "player_points",
                        "outcomes": [{
                            "description": "NBA Player",
                            "name": "Over",
                            "point": 20.5,
                            "dfs_odds_type": "standard",
                        }],
                    }],
                }],
            }
        ],
    )
    nfl = _Response(
        200,
        [
            {
                "id": "nfl-game",
                "sport_key": "football_nfl",
                "bookmakers": [{
                    "key": "prizepicks",
                    "markets": [{
                        "key": "player_rushing_yards",
                        "outcomes": [{
                            "description": "NFL Player",
                            "name": "Under",
                            "point": 55.5,
                            "dfs_odds_type": "standard",
                        }],
                    }],
                }],
            }
        ],
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=_QueueSession([discovery, nba, nfl, _Response(200, [])]),
        min_request_interval=0,
    )

    props = asyncio.run(PropLinePrizePicksProvider(client=client).fetch_props())

    assert {(prop.sport, prop.player_name, prop.direction) for prop in props} == {
        ("NBA", "NBA Player", "OVER"),
    }
    assert all(prop.provider == "PrizePicks" for prop in props)


@pytest.mark.parametrize(
    ("official_key", "title", "expected"),
    [
        ("football_ncaaf", "NCAAF", "NCAAF"),
        ("americanfootball_nfl", "NFL", "NFL"),
        ("hockey_nhl", "NHL", "NHL"),
        ("mma_ufc", "UFC", "UFC"),
        ("soccer_fifa_world_cup", "FIFA World Cup", "FIFA"),
        ("aussie_rules_afl", "AFL", "AFL"),
    ],
)
def test_discovered_sport_normalization(official_key, title, expected):
    from providers.propline import _canonical_discovered_sport

    assert _canonical_discovered_sport(official_key, title) == expected


@pytest.mark.parametrize(
    "sport",
    ["MLB", "NBA", "NFL", "CFB", "NCAAF", "FOOTBALL", "NHL", "WNBA", "FIFA", "TENNIS", "PGA", "CS", "CFL"],
)
def test_discovered_sports_are_not_delivery_tier_classified(sport):
    """Sport normalization is independent from actionable delivery policy."""
    from engine import delivery_gates

    assert not hasattr(delivery_gates, "is_tier2_sport")
    assert not hasattr(delivery_gates, "TIER2_SPORTS")


def test_sport_scoped_fetch_preserves_requested_alias_for_confirmation_matching():
    session = _Session(
        _Response(
            200,
            [{
                "id": "cfb-game",
                "sport_key": "football_ncaaf",
                "bookmakers": [{
                    "key": "prizepicks",
                    "markets": [{
                        "key": "player_rushing_yards",
                        "outcomes": [{
                            "description": "College Player",
                            "name": "Over",
                            "point": 40.5,
                            "dfs_odds_type": "standard",
                        }],
                    }],
                }],
            }],
        )
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
    )

    props = asyncio.run(
        PropLinePrizePicksProvider(client=client).fetch_props_for_sport("CFB")
    )

    assert props == []
    assert session.calls == []


def test_client_reuses_fresh_cache_instead_of_repeating_request():
    session = _Session(
        _Response(
            200,
            {"props": [{
                "provider": "PrizePicks",
                "market": "player_props",
                "player_name": "Alex Example",
                "sport": "NBA",
                "stat_type": "Points",
                "line": 24.5,
            }]},
        )
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://authorized.example/props",
        session=session,
        min_request_interval=0,
        cache_ttl=300,
    )

    first = asyncio.run(client.fetch_prizepicks_props())
    second = asyncio.run(client.fetch_prizepicks_props())

    assert first.from_cache is False
    assert second.from_cache is True
    assert first.succeeded is True
    assert second.succeeded is True
    assert len(session.calls) == 1


def test_client_handles_rate_limit_with_backoff():
    session = _Session(_Response(429, headers={"Retry-After": "60"}))
    clock_value = [100.0]
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://authorized.example/props",
        session=session,
        min_request_interval=0,
        clock=lambda: clock_value[0],
    )

    first = asyncio.run(client.fetch_prizepicks_props())
    second = asyncio.run(client.fetch_prizepicks_props())
    assert first.props == []
    assert second.props == []
    assert first.succeeded is False
    assert second.succeeded is False
    assert len(session.calls) == 1


def test_client_default_pacing_supports_one_request_per_120_second_cycle():
    client = PropLineClient(api_key="test-only-key")

    assert client.min_request_interval == 120.0
    # At this rate the companion's one request each 120-second cycle makes
    # 720 daily requests, safely below the provider's 1,000-request limit.
    assert 24 * 60 * 60 / client.min_request_interval < 1000


def test_discovery_reuses_fresh_cache_instead_of_repeating_request():
    session = _Session(_Response(200, [{"key": "basketball_nba", "active": True}]))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        cache_ttl=300,
    )

    first = asyncio.run(client.discover_sports())
    second = asyncio.run(client.discover_sports())

    assert first == second
    assert len(session.calls) == 1


def test_discovery_rate_limit_protects_daily_budget_and_backoff():
    session = _Session(_Response(429, headers={"Retry-After": "60"}))
    clock_value = [100.0]
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        clock=lambda: clock_value[0],
    )

    assert asyncio.run(client.discover_sports()) == []
    assert asyncio.run(client.discover_sports()) == []
    assert len(session.calls) == 1


def test_unknown_sport_is_skipped_without_fabricating_an_official_key():
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        min_request_interval=0,
    )

    props = asyncio.run(
        PropLinePrizePicksProvider(client=client).fetch_props_for_sport("CRICKET")
    )

    assert props == []


def test_confirmation_rejects_stale_or_ambiguous_prizepicks_matches():
    now = datetime(2026, 8, 27, 18, 0)
    target = ConfirmationTarget(
        sport="NBA",
        market_type="player_prop",
        player="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
    )
    stale = ConfirmationObservation(
        provider="PrizePicks",
        sport="NBA",
        market_type="player_prop",
        player="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
        observed_at=now - timedelta(hours=1),
    )
    assert confirm_market(
        target, [stale], allowed_providers={"PrizePicks"},
        max_age_seconds=600, now=now,
    ) is None

    fresh_a = ConfirmationObservation(**{**stale.__dict__, "observed_at": now, "event_id": "g1"})
    fresh_b = ConfirmationObservation(**{**stale.__dict__, "observed_at": now, "event_id": "g2"})
    assert confirm_market(
        target, [fresh_a, fresh_b], allowed_providers={"PrizePicks"},
        max_age_seconds=600, now=now,
    ) is None


def test_player_prop_extra_metadata_is_optional_for_existing_providers():
    prop = PlayerProp(
        provider="Underdog",
        sport="NBA",
        player_name="Alex Example",
        team="",
        stat_type="points",
        line_value=24.5,
    )
    assert prop.direction == ""
    assert prop.source_timestamp is None


@pytest.mark.asyncio
async def test_propline_observation_reaches_existing_confirmation_boundary(monkeypatch):
    import market_engine

    prop = PlayerProp(
        provider="PrizePicks",
        sport="NBA",
        player_name="Alex Example",
        team="Example City",
        stat_type="points",
        line_value=24.5,
        direction="OVER",
        event="Example City at Test Town",
        event_id="game-1",
        external_id="projection-1",
        fetched_at=datetime.utcnow(),
    )

    class _Provider:
        def is_available(self):
            return True

        async def fetch_props(self):
            return [prop]

    monkeypatch.setattr(market_engine, "_propline_provider", _Provider())
    monkeypatch.setattr(market_engine.config, "ODDS_API_ENABLED", False)

    result = await market_engine._get_odds_api_confirmation(
        "NBA",
        player_name="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
    )

    assert result is not None
    assert result["source"] == "PrizePicks"
    assert result["matched_books"] == ["PrizePicks"]
    assert result["confirmed"] is True