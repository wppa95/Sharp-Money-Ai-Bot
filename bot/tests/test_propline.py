"""Focused tests for the authorized PropLine → PrizePicks adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import providers.propline as propline_module
from providers.propline import (
    PROPLINE_DAILY_REQUEST_LIMIT,
    PROPLINE_MAX_REQUESTS_PER_HOUR,
    PropLineRequestBudget,
    PropLineClient,
    PropLinePrizePicksProvider,
    PropLineRequestLimiter,
    propline_event_identity,
    propline_market_identity,
    propline_player_identity,
    normalize_propline_prizepicks_record,
)


@pytest.fixture(autouse=True)
def isolated_propline_limiter(monkeypatch):
    """Keep HTTP-fixture tests fast while exercising the real limiter."""
    virtual_time = [0.0]

    async def advance(seconds: float):
        virtual_time[0] += seconds

    limiter = PropLineRequestLimiter(
        clock=lambda: virtual_time[0],
        sleep=advance,
    )
    monkeypatch.setattr(
        propline_module,
        "_DEFAULT_PROPLINE_REQUEST_LIMITER",
        limiter,
    )
    return limiter
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
    assert prop.supplemental_source == "PropLine"


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

    assert client.min_request_interval == pytest.approx(3600 / 208)
    assert client.request_limiter.effective_limit == 208


def test_shared_limiter_caps_rate_and_continuously_spaces_requests():
    clock = [0.0]
    sleeps = []

    async def advance(seconds: float):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = PropLineRequestLimiter(limit=999, clock=lambda: clock[0], sleep=advance)

    async def exercise():
        for _ in range(208):
            await limiter.acquire()
        before = list(limiter._request_times)
        await limiter.acquire()
        return before

    before = asyncio.run(exercise())

    assert limiter.configured_limit == 208
    assert len(before) == 208
    assert all(delay == pytest.approx(3600 / 208) for delay in sleeps)
    assert clock[0] >= 3600 / 208 * 208


def test_provider_lower_limit_is_adopted_by_shared_limiter():
    limiter = PropLineRequestLimiter()
    limiter.observe_provider_limit({"X-RateLimit-Limit": "100"})
    limiter.observe_provider_limit({"X-RateLimit-Limit": "999"})

    assert limiter.effective_limit == 100
    assert limiter.min_interval == pytest.approx(36.0)


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


def test_confirmation_requires_matching_event_for_id_bearing_propline_rows():
    now = datetime(2026, 8, 27, 18, 0)
    target = ConfirmationTarget(
        sport="NBA",
        market_type="player_prop",
        player="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
        event_id="underdog-game-1",
        game_time=now,
    )
    observation = ConfirmationObservation(
        provider="PrizePicks",
        sport="NBA",
        market_type="player_prop",
        player="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event_id="propline-game-2",
        observed_at=now,
        supplemental_source="PropLine",
        game_time=now,
    )

    assert confirm_market(
        target,
        [observation],
        allowed_providers={"PrizePicks"},
        max_age_seconds=600,
        now=now,
    ) is None


def test_confirmation_returns_propline_provenance():
    now = datetime(2026, 8, 27, 18, 0)
    target = ConfirmationTarget(
        sport="NBA",
        market_type="player_prop",
        player="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
        event_id="game-1",
        game_time=now,
    )
    observation = ConfirmationObservation(
        provider="PrizePicks",
        sport="NBA",
        market_type="player_prop",
        player="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
        event_id="game-1",
        observed_at=now,
        supplemental_source="PropLine",
        game_time=now,
    )

    result = confirm_market(
        target,
        [observation],
        allowed_providers={"PrizePicks"},
        max_age_seconds=600,
        now=now,
    )

    assert result is not None
    assert result["source"] == "PrizePicks"
    assert result["provenance"]["provider"] == "PrizePicks"
    assert result["provenance"]["supplemental_source"] == ["PropLine"]
    assert result["provenance"]["event_ids"] == ["game-1"]


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
    assert prop.supplemental_source == ""


def test_propline_default_budget_matches_plan_without_changing_hourly_limit():
    budget = PropLineRequestBudget()

    assert PROPLINE_DAILY_REQUEST_LIMIT == 5_000
    assert budget.limit == 5_000
    assert PROPLINE_MAX_REQUESTS_PER_HOUR == 208


def test_sports_pagination_follows_all_cursors_without_a_page_ceiling():
    session = _QueueSession(
        [
            _Response(
                200,
                {
                    "data": [{"key": "basketball_nba", "active": True}],
                    "pagination": {"next_cursor": "cursor-1"},
                },
            ),
            _Response(
                200,
                {
                    "data": [
                        {"key": "hockey_nhl", "active": True},
                        {"key": "tennis", "active": True},
                    ],
                    "pagination": {"next_cursor": "cursor-2"},
                },
            ),
            _Response(200, {"data": [{"key": "golf", "active": True}]}),
        ]
    )
    budget = PropLineRequestBudget(10)
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=budget,
    )

    sports = asyncio.run(client.discover_sports())

    assert [sport.canonical_key for sport in sports] == ["NBA", "NHL", "TENNIS", "GOLF"]
    assert [call[1]["params"] for call in session.calls] == [
        {},
        {"cursor": "cursor-1"},
        {"cursor": "cursor-2"},
    ]
    assert budget.count == 3


def test_sports_pagination_stops_on_repeated_page_and_marks_no_partial_catalog():
    session = _QueueSession(
        [
            _Response(
                200,
                {
                    "data": [{"key": "basketball_nba", "active": True}],
                    "pagination": {"next_cursor": "cursor-1"},
                },
            ),
            _Response(
                200,
                {
                    "data": [{"key": "basketball_nba", "active": True}],
                    "pagination": {"next_cursor": "cursor-2"},
                },
            ),
        ]
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
        request_budget=PropLineRequestBudget(10),
    )

    assert asyncio.run(client.discover_sports()) == []
    assert len(session.calls) == 2


def _event(event_id, home, away, start="2026-08-28T00:00:00Z"):
    return {
        "id": event_id,
        "sport_key": "basketball_nba",
        "home_team": home,
        "away_team": away,
        "commence_time": start,
    }


def _event_odds(event_id, player="Alex Example", direction="Over"):
    return [
        {
            "id": event_id,
            "sport_key": "basketball_nba",
            "bookmakers": [
                {
                    "key": "prizepicks",
                    "markets": [
                        {
                            "key": "player_points",
                            "outcomes": [
                                {
                                    "description": player,
                                    "player_id": "player-1",
                                    "name": direction,
                                    "point": 24.5,
                                    "dfs_odds_type": "standard",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]


def test_event_discovery_and_event_scoped_odds_build_complete_snapshot():
    session = _QueueSession(
        [
            _Response(
                200,
                {
                    "events": [_event("game-1", "Test Town", "Example City")],
                    "pagination": {"has_more": True},
                },
            ),
            _Response(200, {"events": [_event("game-2", "Other Town", "Another City")]}),
            _Response(200, _event_odds("game-1")),
            _Response(200, _event_odds("game-2", "Second Player", "Under")),
        ]
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )

    result = asyncio.run(
        client.fetch_event_scoped_props(sport_key="basketball_nba")
    )

    assert result.succeeded is True
    assert result.complete is True
    assert result.discovered_event_count == 2
    assert result.fetched_event_count == 2
    assert {prop.player_name for prop in result.props} == {"Alex Example", "Second Player"}
    assert [call[0] for call in session.calls] == [
        "https://api.prop-line.com/v1/sports/basketball_nba/events",
        "https://api.prop-line.com/v1/sports/basketball_nba/events",
        "https://api.prop-line.com/v1/sports/basketball_nba/events/game-1/odds",
        "https://api.prop-line.com/v1/sports/basketball_nba/events/game-2/odds",
    ]


def test_incomplete_event_snapshot_retains_last_complete_snapshot():
    session = _QueueSession(
        [
            _Response(200, {"events": [_event("game-1", "Test Town", "Example City")]}),
            _Response(200, _event_odds("game-1")),
            _Response(200, {"events": [_event("game-1", "Test Town", "Example City")]}),
            _Response(503, {}),
        ]
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
        request_budget=PropLineRequestBudget(10),
    )

    first = asyncio.run(client.fetch_event_scoped_props(sport_key="basketball_nba"))
    second = asyncio.run(client.fetch_event_scoped_props(sport_key="basketball_nba"))

    assert first.complete is True
    assert second.succeeded is False
    assert second.complete is False
    assert second.retained_last_success is True
    assert [prop.player_name for prop in second.props] == ["Alex Example"]
    assert second.failed_event_ids == ("game-1",)


def test_event_404_rediscovery_matches_home_away_start_before_retrying():
    session = _QueueSession(
        [
            _Response(404, {}),
            _Response(
                200,
                {"events": [_event("rotated-game", "Test Town", "Example City")]},
            ),
            _Response(200, _event_odds("rotated-game")),
        ]
    )
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )
    stale_event = _event("old-game", "Test Town", "Example City")

    result = asyncio.run(
        client.fetch_event_props("basketball_nba", stale_event)
    )

    assert result.succeeded is True
    assert result.complete is True
    assert [call[0] for call in session.calls] == [
        "https://api.prop-line.com/v1/sports/basketball_nba/events/old-game/odds",
        "https://api.prop-line.com/v1/sports/basketball_nba/events",
        "https://api.prop-line.com/v1/sports/basketball_nba/events/rotated-game/odds",
    ]


def test_stable_identities_prefer_ids_and_normalize_fallback_components():
    assert propline_event_identity({"id": "GAME-1"}) == "event:game-1"
    assert propline_player_identity({"id": "P-1", "name": "Alex Example"}) == "player:p-1"
    assert propline_market_identity("player_points", direction="Over") == "market:points:over"
    first = propline_event_identity(
        {
            "home_team": "Test Town",
            "away_team": "Example City",
            "start_time": "2026-08-28T00:00:00Z",
        },
        sport_key="basketball_nba",
    )
    second = propline_event_identity(
        {
            "home_team": " Test   Town ",
            "away_team": "Example City",
            "start_time": "2026-08-28T00:00:00+00:00",
        },
        sport_key="basketball_nba",
    )
    assert first == second


def test_request_budget_is_centralized_across_clients_and_counts_every_request():
    budget = PropLineRequestBudget(1)
    first_session = _Session(_Response(200, []))
    second_session = _Session(_Response(200, []))
    first = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=first_session,
        min_request_interval=0,
        request_budget=budget,
    )
    second = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=second_session,
        min_request_interval=0,
        request_budget=budget,
    )

    assert asyncio.run(first.fetch_prizepicks_props(sport_key="basketball_nba")).succeeded
    blocked = asyncio.run(second.fetch_prizepicks_props(sport_key="basketball_nba"))

    assert budget.count == 1
    assert blocked.succeeded is False
    assert blocked.failure_kind == "quota"
    assert second_session.calls == []


@pytest.mark.asyncio
async def test_propline_observation_reaches_existing_confirmation_boundary(monkeypatch):
    import market_engine

    class _Provider:
        async def fetch_props(self):
            raise AssertionError("confirmation must not fetch PropLine")

    history_db = SimpleNamespace(
        get_prop_line_history=AsyncMock(
            return_value=[
                SimpleNamespace(
                    provider="PrizePicks",
                    sport="NBA",
                    player_name="Alex Example",
                    team="Example City",
                    stat_type="points",
                    line_value=24.5,
                    event="Example City at Test Town",
                    game_id="game-1",
                    external_id="projection-1",
                    fetched_at=datetime.utcnow(),
                    removed=False,
                )
            ]
        )
    )
    monkeypatch.setattr(market_engine, "_propline_provider", _Provider())
    monkeypatch.setattr(market_engine, "_confirmation_db", history_db)
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
    history_db.get_prop_line_history.assert_awaited_once()
    assert result["confirmed"] is True


@pytest.mark.asyncio
async def test_same_cycle_propline_cache_supplements_underdog_without_new_request(monkeypatch):
    import market_engine

    observed = datetime.utcnow()
    market_engine._propline_current_observations.clear()
    market_engine._propline_current_observations[(
        "Alex Example", "NBA", "points", "game-1"
    )] = PlayerProp(
        provider="PrizePicks",
        sport="NBA",
        player_name="Alex Example",
        team="Example City",
        stat_type="points",
        line_value=24.5,
        game_time=observed,
        external_id="projection-1",
        game_id="game-1",
        event_id="game-1",
        event="Example City at Test Town",
        fetched_at=observed,
        supplemental_source="PropLine",
    )

    history_db = SimpleNamespace(
        get_prop_line_history=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(market_engine, "_confirmation_db", history_db)
    monkeypatch.setattr(market_engine.config, "ODDS_API_ENABLED", False)

    result = await market_engine._get_odds_api_confirmation(
        "NBA",
        player_name="Alex Example",
        stat_type="points",
        direction="OVER",
        line=24.5,
        event="Example City at Test Town",
        game_time=observed,
        event_id="game-1",
        db=history_db,
    )

    assert result is not None
    assert result["source"] == "PrizePicks"
    assert result["provenance"]["supplemental_source"] == ["PropLine"]
    assert result["confirmed"] is True
    history_db.get_prop_line_history.assert_awaited_once()
    market_engine._propline_current_observations.clear()