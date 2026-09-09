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
    PROPLINE_ENDPOINT_DAILY_REQUEST_LIMIT,
    PROPLINE_ENDPOINT_REQUESTS_PER_HOUR,
    PROPLINE_MAX_REQUESTS_PER_HOUR,
    PropLineRequestBudget,
    PropLineClient,
    PropLineClosingLineEvidence,
    PropLinePlayerHistoryEntry,
    PropLinePlayerMarketTrend,
    PropLinePrizePicksProvider,
    PropLineResultEvidence,
    PropLineRequestLimiter,
    PROPLINE_RESULT_INCOMPLETE,
    PROPLINE_RESULT_LOST,
    PROPLINE_RESULT_PUSH,
    PROPLINE_RESULT_UNGRADED,
    PROPLINE_RESULT_UNRESOLVED,
    PROPLINE_RESULT_VOID,
    PROPLINE_RESULT_WON,
    build_propline_line_movement,
    build_propline_closing_line_evidence,
    build_propline_opening_line_evidence,
    build_propline_result_evidence,
    normalize_propline_result_state,
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
    endpoint_limiter = PropLineRequestLimiter(
        limit=PROPLINE_ENDPOINT_REQUESTS_PER_HOUR,
        clock=lambda: virtual_time[0],
        sleep=advance,
    )
    monkeypatch.setattr(
        propline_module,
        "_DEFAULT_PROPLINE_ENDPOINT_REQUEST_LIMITER",
        endpoint_limiter,
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

    def post(self, url, **kwargs):
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


def test_official_odds_route_retains_all_prizepicks_line_variants():
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

    assert len(props) == 3
    assert {prop.direction for prop in props} == {"OVER", "UNDER"}
    assert {prop.line_variant for prop in props} == {"standard", "demon"}
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
        ("NFL", "NFL Player", "UNDER"),
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

    assert [(prop.sport, prop.player_name) for prop in props] == [
        ("CFB", "College Player")
    ]
    assert len(session.calls) == 1


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
    assert first.raw_record_count == 1
    assert first.normalized_prop_count == 1
    assert second.raw_record_count == 1
    assert second.normalized_prop_count == 1
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

    assert client.min_request_interval == pytest.approx(20.0)
    assert client.request_limiter.effective_limit == 180
    assert client.endpoint_request_limiter.effective_limit == 27


def test_shared_limiter_caps_rate_and_continuously_spaces_requests():
    clock = [0.0]
    sleeps = []

    async def advance(seconds: float):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = PropLineRequestLimiter(limit=999, clock=lambda: clock[0], sleep=advance)

    async def exercise():
        for _ in range(190):
            await limiter.acquire()
        before = list(limiter._request_times)
        await limiter.acquire()
        return before

    before = asyncio.run(exercise())

    assert limiter.configured_limit == 190
    assert len(before) == 190
    assert all(delay == pytest.approx(3600 / 190) for delay in sleeps)
    assert clock[0] >= 3600 / 190 * 190


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


def test_documented_read_only_event_capabilities_keep_payload_and_query_params():
    session = _QueueSession(
        [
            _Response(200, {"event_id": "16", "redacted": True, "upgrade_url": "https://upgrade"}),
            _Response(200, {"bookmakers": []}),
            _Response(200, {"steam": []}),
        ]
    )
    client = PropLineClient(
        api_key="test-only-key",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
    )

    context = asyncio.run(client.fetch_event_context("baseball_mlb", "16"))
    history = asyncio.run(
        client.fetch_odds_history(
            "baseball_mlb",
            "16",
            markets=["pitcher_strikeouts", "batter_hits"],
            from_time="2026-08-01T00:00:00Z",
            changes_only=True,
        )
    )
    movement = asyncio.run(
        client.fetch_movement("baseball_mlb", "16", markets="totals", period="q1")
    )

    assert context.succeeded is True
    assert context.redacted is True
    assert context.upgrade_url == "https://upgrade"
    assert history.succeeded is True
    assert movement.payload == {"steam": []}
    assert session.calls[0][0].endswith("/v1/sports/baseball_mlb/events/16/context")
    assert session.calls[1][1]["params"] == {
        "markets": "pitcher_strikeouts,batter_hits",
        "from": "2026-08-01T00:00:00Z",
        "changes_only": "true",
    }
    assert session.calls[2][1]["params"] == {
        "markets": "totals",
        "period": "q1",
    }


def test_hobby_operational_and_bulk_capabilities_use_documented_routes():
    session = _QueueSession(
        [
            _Response(200, [{"key": "baseball_mlb"}]),
            _Response(200, [{"id": "game-1"}]),
            _Response(200, [{"id": "game-1", "bookmakers": []}]),
            _Response(200, {"runs": 8.5}),
            _Response(200, {"goals": 6.5}),
            _Response(200, "sample,csv"),
        ]
    )
    client = PropLineClient(
        api_key="test-only-key",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
    )

    results = [
        asyncio.run(client.fetch_sports()),
        asyncio.run(client.fetch_events("baseball_mlb")),
        asyncio.run(client.fetch_sport_odds(
            "baseball_mlb",
            markets=["h2h", "totals"],
            bookmakers=["pinnacle", "draftkings"],
            period="all",
            include_links=True,
            include_book_ids=True,
        )),
        asyncio.run(client.fetch_grand_salami(date="2026-09-03")),
        asyncio.run(client.fetch_daily_goals_total(date="2026-09-03")),
        asyncio.run(client.fetch_exports_sample()),
    ]

    assert all(result.succeeded for result in results)
    assert session.calls[0][0].endswith("/v1/sports")
    assert session.calls[1][0].endswith("/v1/sports/baseball_mlb/events")
    assert session.calls[2][1]["params"] == {
        "markets": "h2h,totals",
        "bookmakers": "pinnacle,draftkings",
        "period": "all",
        "includeLinks": "true",
        "includeBookIds": "true",
    }
    assert session.calls[3][0].endswith("/v1/sports/baseball_mlb/grand-salami")
    assert session.calls[4][0].endswith("/v1/sports/hockey_nhl/daily-goals-total")
    assert session.calls[5][0].endswith("/v1/exports/sample")


def test_hobby_sgp_and_clv_actions_are_posted_without_touching_polling_models():
    session = _Session(_Response(200, {"quoted": True, "clv_pct": 4.2}))
    client = PropLineClient(
        api_key="test-only-key",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
    )

    sgp = asyncio.run(client.fetch_sgp(
        "basketball_nba",
        "game-1",
        bookmaker="fanduel",
        legs=[
            {"market": "player_points", "name": "Over", "description": "A", "point": 20.5},
            {"market": "player_rebounds", "name": "Under", "description": "B", "point": 8.5},
        ],
    ))
    clv = asyncio.run(client.grade_clv([
        {"event_id": "game-1", "market": "player_points", "name": "Over", "point": 20.5, "price": -110}
    ]))

    assert sgp.succeeded is True
    assert clv.succeeded is True
    assert session.calls[0][0].endswith(
        "/v1/sports/basketball_nba/events/game-1/sgp"
    )
    assert session.calls[0][1]["json"]["bookmaker"] == "fanduel"
    assert len(session.calls[0][1]["json"]["legs"]) == 2
    assert session.calls[1][0].endswith("/v1/clv/grade")
    assert "bets" in session.calls[1][1]["json"]


def test_player_games_are_cached_and_player_name_is_escaped():
    session = _Session(
        _Response(
            200,
            {
                "player_name": "Aaron Judge",
                "sport_key": "baseball_mlb",
                "games": [],
            },
        )
    )
    client = PropLineClient(
        api_key="test-only-key",
        session=session,
        min_request_interval=0,
        cache_ttl=300,
    )

    first = asyncio.run(
        client.fetch_player_games(
            "baseball_mlb", "Aaron Judge", limit=10, opponent="BOS",
            stat_type=["hits", "rbis"],
        )
    )
    second = asyncio.run(
        client.fetch_player_game_log("baseball_mlb", "Aaron Judge", limit=10, opponent="BOS",
                                     stat_type=["hits", "rbis"])
    )

    assert first.succeeded is True
    assert second.from_cache is True
    assert len(session.calls) == 1
    assert "/players/Aaron%20Judge/games" in session.calls[0][0]
    assert session.calls[0][1]["params"] == {
        "limit": "10",
        "opponent": "BOS",
        "stat_type": "hits,rbis",
    }


def test_read_only_capability_validation_does_not_spend_request_budget():
    session = _Session(_Response(200, {}))
    client = PropLineClient(
        api_key="test-only-key",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(limit=10),
    )

    result = asyncio.run(client.fetch_market_hit_rates(days=61))

    assert result.succeeded is False
    assert result.failure_kind == "configuration"
    assert client.total_request_count == 0
    assert session.calls == []


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


def test_propline_line_movement_is_chronological_and_provider_scoped():
    now = datetime(2026, 8, 28, 12, 0, 0)
    rows = [
        SimpleNamespace(
            provider="PrizePicks",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=25.5,
            external_id="projection-1",
            game_id="game-1",
            fetched_at=now + timedelta(minutes=20),
            supplemental_source="PropLine",
        ),
        SimpleNamespace(
            provider="Underdog",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=99.0,
            external_id="ud-1",
            game_id="game-1",
            fetched_at=now + timedelta(minutes=30),
        ),
        SimpleNamespace(
            provider="PrizePicks",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=24.5,
            external_id="projection-1",
            game_id="game-1",
            fetched_at=now,
            supplemental_source="PropLine",
        ),
    ]

    history = build_propline_line_movement(rows)

    assert history is not None
    assert history.provider == "PrizePicks"
    assert history.opening_line == 24.5
    assert history.current_line == 25.5
    assert history.total_delta == 1.0
    assert history.change_count == 1
    assert history.direction == "UP"
    assert [point.line_value for point in history.points] == [24.5, 25.5]
    assert history.points[-1].delta_from_previous == 1.0
    assert history.supplemental_source == "PropLine"
    assert history.opening_evidence is not None
    assert history.opening_evidence.observed_at == now
    assert history.opening_evidence.line_value == 24.5
    assert history.opening_evidence.external_id == "projection-1"
    assert history.opening_evidence.event_id == "game-1"
    assert history.opening_evidence.supplemental_source == "PropLine"


def test_propline_opening_evidence_rejects_non_prizepicks_rows():
    row = SimpleNamespace(
        provider="Underdog",
        player_name="Alex Example",
        sport="NBA",
        stat_type="points",
        line_value=24.5,
        fetched_at=datetime(2026, 8, 28, 12, 0, 0),
    )

    assert build_propline_opening_line_evidence(row) is None


def test_propline_closing_evidence_labels_last_observation_as_terminal():
    now = datetime(2026, 8, 28, 12, 0, 0)
    rows = [
        SimpleNamespace(
            provider="PrizePicks",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=24.5,
            external_id="projection-1",
            game_id="game-1",
            fetched_at=now,
            provider_status="available",
            supplemental_source="PropLine",
        ),
        SimpleNamespace(
            provider="PrizePicks",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=25.5,
            external_id="projection-1",
            game_id="game-1",
            fetched_at=now + timedelta(minutes=20),
            provider_status="available",
            supplemental_source="PropLine",
        ),
    ]

    evidence = build_propline_closing_line_evidence(rows)

    assert isinstance(evidence, PropLineClosingLineEvidence)
    assert evidence.is_verified_closing is False
    assert evidence.label == "TERMINAL_LAST_OBSERVED"
    assert evidence.evidence_basis == "last_persisted_observation"
    assert evidence.line_value == 25.5
    assert evidence.provider == "PrizePicks"
    assert evidence.supplemental_source == "PropLine"


def test_propline_closing_evidence_requires_explicit_closed_state():
    row = SimpleNamespace(
        provider="PrizePicks",
        player_name="Alex Example",
        sport="NBA",
        stat_type="points",
        line_value=25.5,
        external_id="projection-1",
        game_id="game-1",
        fetched_at=datetime(2026, 8, 28, 12, 0, 0),
        provider_status="final",
        supplemental_source="PropLine",
    )

    evidence = build_propline_closing_line_evidence([row])

    assert evidence is not None
    assert evidence.is_verified_closing is True
    assert evidence.label == "VERIFIED_CLOSING_LINE"
    assert evidence.evidence_basis == "provider_closed_state"
    assert evidence.provider_status == "final"


def test_propline_closing_evidence_rejects_missing_or_mixed_identity():
    base = dict(
        provider="PrizePicks",
        player_name="Alex Example",
        sport="NBA",
        stat_type="points",
        line_value=25.5,
        fetched_at=datetime(2026, 8, 28, 12, 0, 0),
        provider_status="final",
    )
    assert build_propline_closing_line_evidence([SimpleNamespace(**base)]) is None
    assert build_propline_closing_line_evidence([
        SimpleNamespace(**base, external_id="projection-1"),
        SimpleNamespace(**base, external_id="projection-2"),
    ]) is None


@pytest.mark.parametrize(
    ("source_value", "expected"),
    [
        ("won", PROPLINE_RESULT_WON),
        ("lost", PROPLINE_RESULT_LOST),
        ("push", PROPLINE_RESULT_PUSH),
        ("void", PROPLINE_RESULT_VOID),
        ("ungraded", PROPLINE_RESULT_UNGRADED),
        (None, PROPLINE_RESULT_UNRESOLVED),
    ],
)
def test_propline_result_state_preserves_explicit_resolution_states(
    source_value, expected
):
    assert normalize_propline_result_state(source_value) == expected


def test_propline_result_state_marks_incomplete_payload_without_settling_it():
    assert normalize_propline_result_state("won", response_complete=False) == (
        PROPLINE_RESULT_INCOMPLETE
    )


def test_propline_result_evidence_preserves_prizepicks_provenance_and_timestamps():
    observed_at = datetime(2026, 8, 29, 2, 0)
    evidence = build_propline_result_evidence(
        {
            "provider": "PrizePicks",
            "event_id": "game-1",
            "sport_key": "basketball_nba",
            "event_status": "final",
            "bookmaker_key": "prizepicks",
            "bookmaker_title": "PrizePicks",
            "market_key": "player_points",
            "market_description": "Player Points",
            "player_name": "Alex Example",
            "player_id": "player-1",
            "direction": "Over",
            "line": 24.5,
            "resolution": "won",
            "actual_value": 30,
            "resolved_at": "2026-08-29T01:55:00Z",
            "redacted": False,
            "dfs_odds_type": "standard",
            "supplemental_source": "PropLine",
        },
        observed_at=observed_at,
    )

    assert isinstance(evidence, PropLineResultEvidence)
    assert evidence.provider == "PrizePicks"
    assert evidence.supplemental_source == "PropLine"
    assert evidence.event_id == "game-1"
    assert evidence.player_id == "player-1"
    assert evidence.player_name == "Alex Example"
    assert evidence.stat_type == "points"
    assert evidence.direction == "OVER"
    assert evidence.line_value == 24.5
    assert evidence.actual_value == 30.0
    assert evidence.resolution_state == PROPLINE_RESULT_WON
    assert evidence.source_resolution == "won"
    assert evidence.resolved_at == datetime(2026, 8, 29, 1, 55)
    assert evidence.observed_at == observed_at
    assert evidence.is_settled is True


def test_propline_result_evidence_does_not_infer_from_final_status_or_actual_value():
    evidence = build_propline_result_evidence(
        {
            "provider": "PrizePicks",
            "event_id": "game-1",
            "sport_key": "basketball_nba",
            "event_status": "final",
            "bookmaker_key": "prizepicks",
            "bookmaker_title": "PrizePicks",
            "market_key": "player_points",
            "market_description": "Player Points",
            "player_name": "Alex Example",
            "line": 24.5,
            "resolution": None,
            "actual_value": 30,
            "resolved_at": None,
        },
        observed_at=datetime(2026, 8, 29, 2, 0),
    )

    assert evidence is not None
    assert evidence.provider_status == "final"
    assert evidence.resolution_state == PROPLINE_RESULT_UNRESOLVED
    assert evidence.is_settled is False


def test_propline_result_evidence_rejects_underdog_or_missing_event_identity():
    base = {
        "event_id": "game-1",
        "sport_key": "basketball_nba",
        "bookmaker_key": "prizepicks",
        "market_key": "player_points",
        "player_name": "Alex Example",
        "line": 24.5,
        "resolution": "won",
        "actual_value": 30,
        "resolved_at": None,
    }
    assert build_propline_result_evidence(
        {**base, "provider": "Underdog"},
        observed_at=datetime(2026, 8, 29, 2, 0),
    ) is None
    assert build_propline_result_evidence(
        {**base, "provider": "PrizePicks", "event_id": ""},
        observed_at=datetime(2026, 8, 29, 2, 0),
    ) is None


def test_propline_line_movement_rejects_mixed_source_identities():
    now = datetime(2026, 8, 28, 12, 0, 0)
    rows = [
        SimpleNamespace(
            provider="PrizePicks",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=24.5,
            external_id="projection-1",
            fetched_at=now,
        ),
        SimpleNamespace(
            provider="PrizePicks",
            player_name="Alex Example",
            sport="NBA",
            stat_type="points",
            line_value=25.5,
            external_id="projection-2",
            fetched_at=now + timedelta(minutes=5),
        ),
    ]

    assert build_propline_line_movement(rows) is None


def test_propline_default_budget_matches_plan_without_exceeding_hourly_limit():
    budget = PropLineRequestBudget()

    assert PROPLINE_DAILY_REQUEST_LIMIT == 5_000
    assert budget.limit == 5_000
    assert PROPLINE_ENDPOINT_DAILY_REQUEST_LIMIT == 650
    assert PROPLINE_ENDPOINT_REQUESTS_PER_HOUR == 27
    assert PROPLINE_MAX_REQUESTS_PER_HOUR == 190


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


def _event_results(event_id, resolution="won"):
    return {
        "id": event_id,
        "sport_key": "basketball_nba",
        "home_team": "Test Town",
        "away_team": "Example City",
        "commence_time": "2026-08-28T00:00:00Z",
        "status": "final",
        "home_score": 100,
        "away_score": 95,
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [],
            },
            {
                "key": "prizepicks",
                "title": "PrizePicks",
                "markets": [
                    {
                        "key": "player_points",
                        "description": "Player Points",
                        "outcomes": [
                            {
                                "name": "Over",
                                "description": "Alex Example",
                                "price": None,
                                "point": 24.5,
                                "resolution": resolution,
                                "actual_value": 30,
                                "resolved_at": "2026-08-28T03:00:00Z",
                                "redacted": False,
                                "dfs_odds_type": "standard",
                                "player_id": "player-1",
                            }
                        ],
                    }
                ],
            },
        ],
    }


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


def test_official_event_results_uses_documented_route_and_prizepicks_only():
    session = _Session(_Response(200, _event_results("game-1")))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )

    result = asyncio.run(
        client.fetch_event_results(
            "basketball_nba",
            {"id": "game-1"},
            markets="player_points",
        )
    )

    assert result.succeeded is True
    assert result.complete is True
    assert result.event_id == "game-1"
    assert len(result.results) == 1
    assert result.results[0].provider == "PrizePicks"
    assert result.results[0].resolution_state == PROPLINE_RESULT_WON
    assert session.calls[0][0] == (
        "https://api.prop-line.com/v1/sports/basketball_nba/events/game-1/results"
    )
    assert session.calls[0][1]["params"] == {"markets": "player_points"}


def test_official_event_results_keeps_unresolved_distinct_from_incomplete_payload():
    unresolved_session = _Session(_Response(200, _event_results("game-1", None)))
    unresolved_client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=unresolved_session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )
    unresolved = asyncio.run(
        unresolved_client.fetch_event_results("basketball_nba", "game-1")
    )
    assert unresolved.succeeded is True
    assert unresolved.complete is True
    assert unresolved.results[0].resolution_state == PROPLINE_RESULT_UNRESOLVED
    assert unresolved.results[0].is_settled is False

    incomplete_session = _Session(
        _Response(
            200,
            {
                "id": "game-1",
                "sport_key": "basketball_nba",
                "commence_time": "2026-08-28T00:00:00Z",
                "status": "final",
                "home_score": 100,
                "away_score": 95,
            },
        )
    )
    incomplete_client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=incomplete_session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )
    incomplete = asyncio.run(
        incomplete_client.fetch_event_results("basketball_nba", "game-1")
    )
    assert incomplete.succeeded is False
    assert incomplete.complete is False
    assert incomplete.failure_kind == "incomplete"
    assert incomplete.results == ()


def test_event_results_are_not_requested_from_custom_endpoint():
    session = _Session(_Response(200, _event_results("game-1")))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://authorized.example/props",
        session=session,
        min_request_interval=0,
    )

    result = asyncio.run(
        client.fetch_event_results("basketball_nba", "game-1")
    )

    assert result.complete is False
    assert result.failure_kind == "configuration"
    assert session.calls == []


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


def _player_history_payload(
    *,
    player_name="Alex Example",
    sport_key="basketball_nba",
    market="player_points",
    **entry_overrides,
):
    entry = {
        "event_id": "game-1",
        "commence_time": "2026-08-28T00:00:00Z",
        "home_team": "Test Town",
        "away_team": "Example City",
        "bookmaker": "prizepicks",
        "bookmaker_title": "PrizePicks",
        "line": 24.5,
        "over_price": None,
        "under_price": None,
        "actual_value": 30,
        "over_result": "won",
        "under_result": "lost",
        "resolved_at": "2026-08-28T03:00:00Z",
    }
    entry.update(entry_overrides)
    return {
        "player_name": player_name,
        "sport_key": sport_key,
        "market": market,
        "entries": [entry],
        "upgrade_url": None,
    }


def _player_trends_payload(
    *,
    player_name="Alex Example",
    sport_key="basketball_nba",
    market="player_points",
    **market_overrides,
):
    market_record = {
        "market": market,
        "games_graded": 12,
        "reference_bookmaker": "draftkings",
        "reference_bookmaker_title": "DraftKings",
        "recent_line": 24.5,
        "avg_actual": 27.25,
        "last_5": {
            "window": 5,
            "games": 5,
            "over": 3,
            "under": 1,
            "push": 1,
            "over_pct": 0.75,
        },
        "last_10": None,
        "last_20": None,
        "last_50": None,
        "current_streak": {"result": "won", "length": 2},
        "last_game": {
            "event_id": "game-1",
            "commence_time": "2026-08-28T00:00:00Z",
            "line": 24.5,
            "actual_value": 30,
            "result": "won",
        },
    }
    market_record.update(market_overrides)
    return {
        "player_name": player_name,
        "sport_key": sport_key,
        "dfs_odds_type": None,
        "markets": [market_record],
        "upgrade_url": None,
    }


def test_official_player_history_preserves_identity_context_and_provenance():
    session = _Session(_Response(200, _player_history_payload()))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )

    result = asyncio.run(
        client.fetch_player_history(
            "basketball_nba",
            "Alex Example",
            market="player_points",
            bookmaker="prizepicks",
            limit=10,
        )
    )

    assert result.succeeded is True
    assert result.complete is True
    assert result.player_name == "Alex Example"
    assert result.sport_key == "basketball_nba"
    assert result.market == "player_points"
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert isinstance(entry, PropLinePlayerHistoryEntry)
    assert entry.provider == "PrizePicks"
    assert entry.supplemental_source == "PropLine"
    assert entry.event_id == "game-1"
    assert entry.home_team == "Test Town"
    assert entry.away_team == "Example City"
    assert entry.commence_time == datetime(2026, 8, 28)
    assert entry.resolved_at == datetime(2026, 8, 28, 3)
    assert entry.over_result == "won"
    assert entry.over_resolution_state == PROPLINE_RESULT_WON
    assert entry.over_is_settled is True
    assert session.calls[0][0] == (
        "https://api.prop-line.com/v1/sports/basketball_nba/"
        "players/Alex%20Example/history"
    )
    assert session.calls[0][1]["params"] == {
        "market": "player_points",
        "limit": "10",
        "bookmaker": "prizepicks",
    }


def test_current_propline_prop_preserves_opponent_for_h2h_evidence():
    prop = normalize_propline_prizepicks_record(
        {
            "provider": "PrizePicks",
            "sport": "NBA",
            "player_name": "Alex Example",
            "team": "Test Town",
            "stat_type": "Points",
            "line": 24.5,
            "event": {
                "id": "game-1",
                "name": "Example City at Test Town",
                "home_team": "Test Town",
                "away_team": "Example City",
                "start_time": "2026-08-28T00:00:00Z",
            },
        }
    )

    assert prop is not None
    assert prop.opponent == "Example City"


def test_player_history_keeps_missing_and_ungraded_results_non_settled():
    payload = _player_history_payload(
        actual_value=30,
        over_result=None,
        under_result="ungraded",
        resolved_at=None,
    )
    session = _Session(_Response(200, payload))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )

    entry = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    ).entries[0]

    assert entry.over_result is None
    assert entry.over_resolution_state == PROPLINE_RESULT_UNRESOLVED
    assert entry.over_is_settled is False
    assert entry.under_resolution_state == PROPLINE_RESULT_UNGRADED
    assert entry.under_is_settled is False


def test_player_history_accepts_redacted_nullable_values_and_rejects_incomplete_rows():
    redacted_payload = _player_history_payload(
        line=None,
        actual_value=None,
        over_result=None,
        under_result=None,
        resolved_at=None,
    )
    redacted_payload["redacted"] = True
    session = _QueueSession([
        _Response(200, redacted_payload),
        _Response(200, {
            "player_name": "Alex Example",
            "sport_key": "basketball_nba",
            "market": "player_points",
            "entries": [{}],
        }),
    ])
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
        request_budget=PropLineRequestBudget(10),
    )

    redacted = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )
    incomplete = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )

    assert redacted.succeeded is True
    assert redacted.complete is True
    assert redacted.redacted is True
    assert redacted.entries[0].line is None
    assert redacted.entries[0].over_resolution_state == PROPLINE_RESULT_UNRESOLVED
    assert incomplete.succeeded is False
    assert incomplete.complete is False
    assert incomplete.failure_kind == "incomplete"
    assert incomplete.entries == ()


def test_player_history_cache_reuses_complete_evidence_without_a_second_request():
    session = _Session(_Response(200, _player_history_payload()))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        cache_ttl=300,
        request_budget=PropLineRequestBudget(10),
    )

    first = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )
    second = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )

    assert first.from_cache is False
    assert second.from_cache is True
    assert second.entries == first.entries
    assert len(session.calls) == 1


def test_official_player_trends_preserves_documented_splits_and_last_game():
    session = _Session(_Response(200, _player_trends_payload()))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )

    result = asyncio.run(
        client.fetch_player_trends(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )

    assert result.succeeded is True
    assert result.complete is True
    assert len(result.markets) == 1
    trend = result.markets[0]
    assert isinstance(trend, PropLinePlayerMarketTrend)
    assert trend.provider == "DraftKings"
    assert trend.supplemental_source == "PropLine"
    assert trend.games_graded == 12
    assert trend.last_5.over == 3
    assert trend.last_5.over_pct == pytest.approx(0.75)
    assert trend.last_10 is None
    assert trend.current_streak.result == "won"
    assert trend.last_game.event_id == "game-1"
    assert trend.last_game.resolution_state == PROPLINE_RESULT_WON
    assert session.calls[0][0] == (
        "https://api.prop-line.com/v1/sports/basketball_nba/"
        "players/Alex%20Example/trends"
    )
    assert session.calls[0][1]["params"] == {"market": "player_points"}


def test_player_trends_flavor_filter_is_documented_and_invalid_values_do_not_request():
    session = _Session(_Response(200, {
        **_player_trends_payload(),
        "dfs_odds_type": "goblin",
    }))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        request_budget=PropLineRequestBudget(10),
    )

    valid = asyncio.run(
        client.fetch_player_trends(
            "basketball_nba",
            "Alex Example",
            dfs_odds_type="goblin",
        )
    )
    invalid = asyncio.run(
        client.fetch_player_trends(
            "basketball_nba",
            "Alex Example",
            dfs_odds_type="other",
        )
    )

    assert valid.dfs_odds_type == "goblin"
    assert session.calls[0][1]["params"] == {"dfs_odds_type": "goblin"}
    assert invalid.succeeded is False
    assert invalid.failure_kind == "configuration"
    assert len(session.calls) == 1


def test_history_and_trends_share_the_existing_request_budget():
    session = _QueueSession([
        _Response(200, _player_history_payload()),
        _Response(200, _player_trends_payload()),
    ])
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://api.prop-line.com",
        session=session,
        min_request_interval=0,
        cache_ttl=0,
        request_budget=PropLineRequestBudget(1),
    )

    history = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )
    trends = asyncio.run(
        client.fetch_player_trends("basketball_nba", "Alex Example")
    )

    assert history.succeeded is True
    assert trends.succeeded is False
    assert trends.failure_kind == "quota"
    assert client.request_budget.count == 1
    assert len(session.calls) == 1


def test_player_history_and_trends_reject_nonofficial_endpoints_before_request():
    session = _Session(_Response(200, _player_history_payload()))
    client = PropLineClient(
        api_key="test-only-key",
        api_url="https://authorized.example/props",
        session=session,
        min_request_interval=0,
    )

    history = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Alex Example", market="player_points"
        )
    )
    trends = asyncio.run(
        client.fetch_player_trends("basketball_nba", "Alex Example")
    )

    assert history.failure_kind == "configuration"
    assert trends.failure_kind == "configuration"
    assert session.calls == []