from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from providers.odds_api import (
    OddsApiClient,
    OddsApiDeferred,
    OddsApiError,
    OddsApiEvent,
    OddsApiHttpResponse,
    OddsApiMarket,
    OddsApiQuote,
    OddsApiScheduler,
    OddsApiSport,
    match_event_exactly,
)
from providers.odds_api_budget import OddsApiCreditBudget

UTC = timezone.utc


class FakeTransport:
    def __init__(self, *responses: OddsApiHttpResponse):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, path: str, params: dict):
        self.calls.append((path, dict(params)))
        if not self.responses:
            raise AssertionError("unexpected provider call")
        return self.responses.pop(0)


def _budget(tmp_path, *, used: int = 0, due: datetime | None = None):
    path = tmp_path / "odds-api.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "period_start": start.isoformat(),
                "credits_used": used,
                "next_paid_request_at": (
                    due or (now - timedelta(seconds=1))
                ).isoformat(),
                "rotation_cursor": 0,
                "reservations": {},
                "caches": {},
            }
        ),
        encoding="utf-8",
    )
    return OddsApiCreditBudget(str(path))


def _client(tmp_path, transport):
    return OddsApiClient(
        api_key="unit-test-key",
        budget=_budget(tmp_path),
        transport=transport,
    )


def test_sports_and_events_are_quota_free_and_all_is_explicit(tmp_path):
    sports_transport = FakeTransport(
        OddsApiHttpResponse(
            200, {}, [{"key": "basketball_nba", "title": "NBA", "active": True}]
        )
    )
    client = _client(tmp_path, sports_transport)
    sports = asyncio.run(client.fetch_sports())
    assert sports[0].key == "basketball_nba"
    assert sports_transport.calls[0][0] == "/sports"
    assert "all" not in sports_transport.calls[0][1]
    assert client.budget.diagnostics()["credits_used"] == 0

    event_transport = FakeTransport(
        OddsApiHttpResponse(
            200,
            {},
            [{
                "id": "event-1",
                "sport_key": "basketball_nba",
                "commence_time": "2026-10-18T01:00:00Z",
                "home_team": "Home",
                "away_team": "Away",
            }],
        )
    )
    events_client = _client(tmp_path / "events", event_transport)
    events = asyncio.run(events_client.fetch_events("basketball_nba"))
    assert events[0].id == "event-1"
    assert event_transport.calls[0][0] == "/sports/basketball_nba/events"
    assert "apiKey" in event_transport.calls[0][1]
    assert events_client.budget.diagnostics()["credits_used"] == 0

    all_transport = FakeTransport(OddsApiHttpResponse(200, {}, []))
    all_client = _client(tmp_path / "all", all_transport)
    asyncio.run(all_client.fetch_sports(all_sports=True))
    assert all_transport.calls[0][1]["all"] == "true"
    assert "apiKey" in all_transport.calls[0][1]


def test_market_availability_is_one_credit_and_cached(tmp_path):
    transport = FakeTransport(
        OddsApiHttpResponse(
            200,
            {"x-requests-last": "1"},
            {"bookmakers": [{
                "key": "book-a",
                "markets": [{"key": "player_points", "description": "Player Points"}],
            }]},
        )
    )
    client = _client(tmp_path, transport)
    result = asyncio.run(client.fetch_event_markets("basketball_nba", "event-1"))
    again = asyncio.run(client.fetch_event_markets("basketball_nba", "event-1"))
    assert [market.key for market in result] == ["player_points"]
    assert again == result
    assert transport.calls[0][0] == "/sports/basketball_nba/events/event-1/markets"
    assert client.budget.diagnostics()["credits_used"] == 1
    assert len(transport.calls) == 1


def test_event_odds_reserves_maximum_but_charges_unique_returned_market_keys(tmp_path):
    transport = FakeTransport(
        OddsApiHttpResponse(
            200,
            {"x-requests-last": "1"},
            {
                "bookmakers": [{
                    "key": "book-a",
                    "title": "Book A",
                    "markets": [{
                        "key": "player_points",
                        "outcomes": [{
                            "description": "Example Player",
                            "name": "Over",
                            "price": -110,
                            "point": 21.5,
                        }],
                    }],
                }]
            },
        )
    )
    client = _client(tmp_path, transport)
    quotes = asyncio.run(
        client.fetch_event_odds(
            "basketball_nba",
            "event-1",
            ("player_points", "player_rebounds"),
        )
    )
    assert len(quotes) == 1
    assert quotes[0].point == 21.5
    assert client.budget.diagnostics()["credits_used"] == 1
    assert transport.calls[0][1]["markets"] == "player_points,player_rebounds"
    assert "apiKey" in transport.calls[0][1]
    state = json.loads((tmp_path / "odds-api.json").read_text(encoding="utf-8"))
    assert "unit-test-key" not in json.dumps(state)


def test_scores_cost_two_with_days_from_and_one_without(tmp_path):
    body = [{
        "id": "event-1",
        "sport_key": "basketball_nba",
        "commence_time": "2026-10-17T23:00:00Z",
        "home_team": "Home",
        "away_team": "Away",
        "completed": True,
        "scores": [{"name": "Home", "score": "101"}, {"name": "Away", "score": "98"}],
    }]
    with_days = FakeTransport(
        OddsApiHttpResponse(200, {"x-requests-last": "2"}, body)
    )
    client = _client(tmp_path, with_days)
    scores = asyncio.run(client.fetch_scores("basketball_nba", days_from=1))
    assert scores[0].completed
    assert scores[0].scores[0] == ("Home", 101.0)
    assert client.budget.diagnostics()["credits_used"] == 2

    no_days = FakeTransport(
        OddsApiHttpResponse(200, {"x-requests-last": "1"}, [])
    )
    no_days_client = _client(tmp_path / "no-days", no_days)
    asyncio.run(no_days_client.fetch_scores("basketball_nba", days_from=None))
    assert no_days_client.budget.diagnostics()["credits_used"] == 1


def test_retry_after_is_global_and_transport_is_not_retried(tmp_path):
    transport = FakeTransport(
        OddsApiHttpResponse(429, {"retry-after": "120"}, {"message": "slow down"})
    )
    client = _client(tmp_path, transport)
    with pytest.raises(OddsApiError):
        asyncio.run(client.fetch_event_markets("basketball_nba", "event-1"))
    assert len(transport.calls) == 1
    assert client.budget.diagnostics()["cooldown_until"]
    with pytest.raises(OddsApiDeferred, match="cooldown"):
        asyncio.run(client.fetch_scores("basketball_nba"))
    assert len(transport.calls) == 1


def test_unknown_paid_response_charges_reserved_cost_and_redacts_errors(tmp_path):
    transport = FakeTransport(OddsApiHttpResponse(200, {}, None))
    client = _client(tmp_path, transport)
    with pytest.raises(OddsApiError) as error:
        asyncio.run(client.fetch_event_markets("basketball_nba", "event-1"))
    assert str(error.value) == "malformed JSON response"
    assert "unit-test-key" not in str(error.value)
    assert client.budget.diagnostics()["credits_used"] == 1


def test_exact_event_matching_rejects_mismatched_or_ambiguous_events():
    start = datetime(2026, 10, 18, 1, tzinfo=UTC)
    source = {
        "sport_key": "basketball_nba",
        "home_team": "Home",
        "away_team": "Away",
        "commence_time": start,
    }
    event = OddsApiEvent(
        "odds-event",
        "basketball_nba",
        start,
        "Away",
        "Home",
    )
    assert match_event_exactly(source, [event]) == event
    assert match_event_exactly(
        {**source, "sport_key": "americanfootball_nfl"}, [event]
    ) is None
    assert match_event_exactly(source, [event, event]) is None


def test_pick_quote_context_is_exact_player_only_and_keeps_source_line(tmp_path):
    client = _client(tmp_path, FakeTransport())
    start = datetime(2026, 10, 18, 1, tzinfo=UTC)
    event = OddsApiEvent(
        "odds-event", "basketball_nba", start, "Home", "Away"
    )

    async def fetch_events(_sport):
        return [event]

    async def fetch_quotes(_sport, _event, _markets):
        return [
            OddsApiQuote(
                "player_points", "Book A", "Example Player", "Over", -110, 22.5
            ),
            OddsApiQuote(
                "player_points", "Book B", "Different Player", "Over", -105, 18.5
            ),
        ]

    client.fetch_events = fetch_events
    client.fetch_event_odds = fetch_quotes
    pick = {
        "qualified": True,
        "provider": "Underdog",
        "source_id": "ud-source-1",
        "event_id": "ud-event-1",
        "player_name": "Example Player",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 21.5,
        "direction": "OVER",
        "line_variant": "standard",
    }
    source_event = {
        "sport_key": "basketball_nba",
        "home_team": "Home",
        "away_team": "Away",
        "commence_time": start,
    }
    context = asyncio.run(
        client.market_context_for_qualified_pick(
            pick, source_event, market_key="player_points"
        )
    )
    assert context["source_pick"]["line_value"] == 21.5
    assert context["source_pick"]["source_id"] == "ud-source-1"
    assert context["quotes"][0]["point"] == 22.5
    assert len(context["quotes"]) == 1
    assert context["is_player_result"] is False


def test_pending_context_requires_exact_qualified_source_and_prizepicks_variant(tmp_path):
    scheduler = OddsApiScheduler(_client(tmp_path, FakeTransport()))
    queue_item = {
        "pick_key": "distinct-source-line",
        "provider": "PrizePicks",
        "source_id": "pp-source-1",
        "event_id": "pp-event-1",
        "player_name": "Example Player",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 21.5,
        "direction": "OVER",
        "line_variant": "standard",
        "game_time": datetime(2026, 10, 18, 1, tzinfo=UTC),
        "qualification_json": json.dumps({"qualified": True}),
        "evidence_snapshot_json": json.dumps({
            "source_id": "pp-source-1",
            "source_event_id": "pp-event-1",
            "direction": "OVER",
            "line": 21.5,
            "line_variant": "standard",
            "source_event_teams": [["Home", "Away"]],
        }),
    }
    assert scheduler.queue_qualified_pick_context(queue_item)
    assert not scheduler.queue_qualified_pick_context({
        **queue_item, "line_variant": ""
    })
    assert not scheduler.queue_qualified_pick_context({
        **queue_item,
        "evidence_snapshot_json": json.dumps({
            "source_id": "pp-source-1",
            "source_event_id": "pp-event-1",
            "direction": "OVER",
            "line": 22.5,
            "line_variant": "standard",
            "source_event_teams": [["Home", "Away"]],
        }),
    })


def test_paid_scheduler_rotates_all_live_sports_and_exposes_scoreless_sport():
    class RotationBudget:
        def __init__(self):
            self.cursor = 0

        def diagnostics(self, now=None):
            return {
                "next_paid_request_at": (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat(),
                "cooldown_until": None,
            }

        def advance_rotation_cursor(self, count, now=None):
            selected = self.cursor % count
            self.cursor = (self.cursor + 1) % count
            return selected

    class RotationClient:
        is_configured = True

        def __init__(self):
            self.budget = RotationBudget()
            self.score_calls = []
            self.market_calls = []

        async def fetch_sports(self):
            return [
                OddsApiSport("basketball_nba", "NBA"),
                OddsApiSport("americanfootball_nfl", "NFL"),
                OddsApiSport("mma_mixed_martial_arts", "MMA"),
            ]

        async def fetch_events(self, sport_key):
            self.market_calls.append(("events", sport_key))
            return []

        async def fetch_scores(self, sport_key, days_from=None):
            self.score_calls.append((sport_key, days_from))
            return []

        def diagnostics(self, now=None):
            return {}

    client = RotationClient()
    scheduler = OddsApiScheduler(client)
    results = [
        asyncio.run(scheduler.paid_rotation_tick())
        for _ in range(3)
    ]
    assert client.score_calls == [
        ("basketball_nba", 1),
        ("americanfootball_nfl", 1),
    ]
    assert results[2]["status"] == "market_unavailable"
    assert results[2]["sport_key"] == "mma_mixed_martial_arts"


def test_qualified_context_waits_for_exact_market_availability_then_fetches_quotes():
    future = datetime.now(UTC) + timedelta(hours=3)

    class RotationBudget:
        def diagnostics(self):
            return {
                "next_paid_request_at": (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat(),
                "cooldown_until": None,
            }

        def advance_rotation_cursor(self, _count, now=None):
            return 0

    class ContextClient:
        is_configured = True
        budget = RotationBudget()

        def __init__(self):
            self.market_calls = 0
            self.quote_calls = 0

        async def fetch_sports(self):
            return [OddsApiSport("basketball_nba", "NBA")]

        async def fetch_events(self, _sport_key):
            return [
                OddsApiEvent(
                    "odds-event",
                    "basketball_nba",
                    future,
                    "Home",
                    "Away",
                )
            ]

        async def fetch_event_markets(self, _sport, _event):
            self.market_calls += 1
            return [OddsApiMarket("player_points")]

        async def fetch_scores(self, *_args, **_kwargs):
            raise AssertionError("qualified quote work should be selected first")

        async def market_context_for_qualified_pick(self, pick, _event, *, market_key):
            self.quote_calls += 1
            return {
                "source_pick": pick,
                "quotes": [{"market_key": market_key}],
                "is_player_result": False,
            }

    client = ContextClient()
    scheduler = OddsApiScheduler(client)
    queue_item = {
        "pick_key": "ud-pick-identity",
        "provider": "Underdog",
        "source_id": "ud-source",
        "event_id": "ud-event",
        "player_name": "Example Player",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 22.5,
        "direction": "OVER",
        "line_variant": "standard",
        "game_time": future,
        "qualification_json": json.dumps({"qualified": True}),
        "evidence_snapshot_json": json.dumps({
            "source_id": "ud-source",
            "source_event_id": "ud-event",
            "direction": "OVER",
            "line": 22.5,
            "line_variant": "standard",
            "source_event_teams": [["Home", "Away"]],
        }),
    }
    assert scheduler.queue_qualified_pick_context(queue_item)
    first = asyncio.run(scheduler.paid_rotation_tick())
    assert first["status"] == "pick_market_confirmed"
    assert client.market_calls == 1
    assert client.quote_calls == 0

    second = asyncio.run(scheduler.paid_rotation_tick())
    assert second["status"] == "pick_context"
    assert second["identity_preserved"] is True
    assert second["is_player_result"] is False
    assert client.quote_calls == 1
    assert scheduler._last_pick_contexts["ud-pick-identity"]["source_pick"][
        "line_value"
    ] == 22.5
