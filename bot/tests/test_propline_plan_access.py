"""Free-plan safety for PropLine while preserving subscriber endpoints."""

from __future__ import annotations

import asyncio

from providers.propline import (
    PropLineClient,
    PropLineRequestBudget,
    PropLineRequestLimiter,
    PropLineReadOnlyRotation,
    PropLineReadOnlyTarget,
    propline_family_allowed_for_plan,
)


class _Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.response = _Response(payload)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


async def _no_sleep(_seconds):
    return None


def _client(session, *, plan="free", budget=None):
    request_budget = budget or PropLineRequestBudget(20)
    endpoint_budget = PropLineRequestBudget(20)
    limiter = PropLineRequestLimiter(
        limit=60, clock=lambda: 0.0, sleep=_no_sleep
    )
    endpoint_limiter = PropLineRequestLimiter(
        limit=60, clock=lambda: 0.0, sleep=_no_sleep
    )
    return PropLineClient(
        api_key="test-only-value",
        session=session,
        min_request_interval=0,
        subscription_plan=plan,
        request_budget=request_budget,
        endpoint_request_budget=endpoint_budget,
        request_limiter=limiter,
        endpoint_request_limiter=endpoint_limiter,
    )


def test_free_plan_blocks_paid_history_and_settlement_before_budget_reservation():
    session = _Session({})
    budget = PropLineRequestBudget(20)
    client = _client(session, budget=budget)

    history = asyncio.run(
        client.fetch_player_history(
            "basketball_nba", "Example Player", "player_points"
        )
    )
    settlement = asyncio.run(
        client.fetch_event_results("basketball_nba", "verified-event")
    )

    assert history.failure_kind == "plan_restricted"
    assert settlement.failure_kind == "plan_restricted"
    assert session.calls == []
    assert budget.count == 0


def test_free_plan_keeps_raw_player_game_collection_active():
    session = _Session({
        "player_name": "Example Player",
        "sport_key": "basketball_nba",
        "games": [],
    })
    budget = PropLineRequestBudget(20)
    client = _client(session, budget=budget)

    result = asyncio.run(
        client.fetch_player_games(
            "basketball_nba", "Example Player", limit=10, stat_type="points"
        )
    )

    assert result.succeeded is True
    assert len(session.calls) == 1
    assert "/players/Example%20Player/games" in session.calls[0][0]
    assert budget.count == 1


def test_free_plan_allows_event_player_props_without_misclassifying_events_as_ev():
    session = _Session({"id": "verified-event", "bookmakers": []})
    budget = PropLineRequestBudget(20)
    client = _client(session, budget=budget)

    result = asyncio.run(client._request_json(
        "https://api.prop-line.com/v1/sports/basketball_nba/"
        "events/verified-event/odds",
        {"bookmakers": "prizepicks", "markets": "player_points"},
    ))

    assert result == {"id": "verified-event", "bookmakers": []}
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith(
        "/v1/sports/basketball_nba/events/verified-event/odds"
    )
    assert budget.count == 1


def test_free_plan_still_blocks_actual_event_ev_endpoint_before_budget_reservation():
    session = _Session({})
    budget = PropLineRequestBudget(20)
    client = _client(session, budget=budget)

    try:
        asyncio.run(client._request_json(
            "https://api.prop-line.com/v1/sports/basketball_nba/"
            "events/verified-event/ev",
            {},
        ))
    except Exception as exc:
        assert "event_ev endpoint unavailable" in str(exc)
    else:
        raise AssertionError("The paid EV endpoint must be blocked on Free")

    assert session.calls == []
    assert budget.count == 0


def test_paid_endpoints_remain_available_when_hobby_plan_is_configured():
    for family in (
        "player_history",
        "player_trends",
        "event_results",
        "event_odds_history",
        "event_movement",
        "event_best_line",
        "event_ev",
        "resolved_props_export",
        "sgp",
        "clv_grade",
    ):
        assert propline_family_allowed_for_plan(family, "free") is False
        assert propline_family_allowed_for_plan(family, "monthly hobby") is True
    for free_family in (
        "feed",
        "discovery_events",
        "scores",
        "event_stats",
        "event_markets",
        "player_games",
    ):
        assert propline_family_allowed_for_plan(free_family, "free") is True


def test_free_read_only_rotation_skips_paid_families_but_runs_raw_stats(tmp_path):
    class Client:
        subscription_plan = "free"

        async def fetch_event_stats(self, *_args, **_kwargs):
            return _ResponseResult()

        async def fetch_player_games(self, *_args, **_kwargs):
            return _ResponseResult()

        async def fetch_event_results(self, *_args, **_kwargs):
            raise AssertionError("Free-plan rotation must not call settlement")

        async def fetch_player_history(self, *_args, **_kwargs):
            raise AssertionError("Free-plan rotation must not call resolved history")

    rotation = PropLineReadOnlyRotation(
        Client(), state_path=str(tmp_path / "rotation.sqlite"), max_requests_per_cycle=8
    )
    targets = [
        PropLineReadOnlyTarget(
            "event_results", "basketball_nba", event_id="event-1"
        ),
        PropLineReadOnlyTarget(
            "event_stats", "basketball_nba", event_id="event-1"
        ),
        PropLineReadOnlyTarget(
            "player_history",
            "basketball_nba",
            player_name="Example Player",
            market="points",
        ),
        PropLineReadOnlyTarget(
            "player_games",
            "basketball_nba",
            player_name="Example Player",
            market="points",
        ),
    ]

    report = asyncio.run(rotation.run(targets, max_requests=8))

    assert report.skipped == 2
    assert report.attempted == 2
    assert report.completed == 2
    assert {item["family"] for item in report.provenance if item["status"] == "skipped"} == {
        "event_results",
        "player_history",
    }


class _ResponseResult:
    succeeded = True
    complete = True
    redacted = False
