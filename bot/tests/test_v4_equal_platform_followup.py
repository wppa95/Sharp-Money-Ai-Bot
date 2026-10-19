"""Focused regressions for equal-platform startup, ingestion, and history."""
import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import market_engine
import providers.propline as propline
from database import Database
from engine.player_results import compute_hit_rates, directional_window_counts


def test_startup_binds_the_adapter_used_by_the_monitoring_worker(monkeypatch):
    monkeypatch.setattr(market_engine, "_propline_provider", None)
    provider = market_engine.initialize_propline_provider()
    assert market_engine._propline_provider is provider
    assert market_engine.initialize_propline_provider() is provider
    assert provider.client.request_budget is propline._DEFAULT_PROPLINE_REQUEST_BUDGET
    import main
    source = inspect.getsource(main.post_init)
    assert "initialize_propline_provider().is_available()" in source
    assert source.index("reset_monitoring_runtime_state()") < source.index(
        "initialize_propline_provider().is_available()"
    )


def _isolate_worker(monkeypatch, provider, bookmakers):
    monkeypatch.setattr(market_engine, "_propline_provider", provider)
    monkeypatch.setattr(market_engine, "_propline_request_lock", None)
    monkeypatch.setattr(market_engine, "_propline_request_cursor", 0)
    monkeypatch.setattr(market_engine, "_propline_bookmaker_cursor", 0)
    monkeypatch.setattr(market_engine, "_propline_pending_props", {})
    monkeypatch.setattr(market_engine, "_propline_empty_sport_until", {})
    monkeypatch.setattr(market_engine, "_propline_empty_feed_streak", {})
    monkeypatch.setattr(market_engine, "config", NS(propline_bookmakers=bookmakers))
    monkeypatch.setattr(market_engine, "_get_prizepicks_scan_sports",
                        AsyncMock(return_value=("NBA",)))
    monkeypatch.setattr(market_engine, "get_health_monitor", lambda: None)
    monkeypatch.setattr(market_engine, "get_health_tracker", lambda: None)
    monkeypatch.setattr(market_engine, "_queue_propline_history_targets",
                        lambda *args, **kwargs: 0)


@pytest.mark.asyncio
async def test_shared_requests_alternate_first_platform_without_changing_identity(monkeypatch):
    calls = []

    async def fetch(sport, bookmaker):
        calls.append(bookmaker)
        prop = NS(provider=propline.provider_name_for_bookmaker(bookmaker),
                  external_id=bookmaker, game_id="event", sport=sport,
                  player_name="Fixture Player", stat_type="Points",
                  line_value=12.5, line_variant="standard")
        return propline.PropLineFetchResult(
            [prop], datetime.utcnow(), succeeded=True, complete=True
        )

    provider = NS(is_available=lambda: True, fetch_props_for_sport_result=fetch)
    _isolate_worker(monkeypatch, provider, ["prizepicks", "underdog"])
    persisted = []

    async def save(db, props, provider):
        assert all(p.provider == provider for p in props)
        persisted.append(provider)
        return len(props)

    monkeypatch.setattr(market_engine, "_persist_propline_observations", save)
    context = NS(bot_data={"db": object()})
    assert await market_engine.propline_request_scan_job(context)
    assert await market_engine.propline_request_scan_job(context)
    assert calls == ["prizepicks", "underdog", "underdog", "prizepicks"]
    assert persisted == ["PrizePicks", "Underdog", "Underdog", "PrizePicks"]


class _Response:
    status = 200
    headers = {}

    def __init__(self, kind, request_number):
        self.kind = kind
        self.request_number = request_number

    async def __aenter__(self):
        if self.kind == "timeout" and self.request_number == 2:
            raise asyncio.TimeoutError()
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        if self.request_number == 1:
            return [{
                "id": "fixture-event", "sport_key": "basketball_nba",
                "home_team": "Fixture Home", "away_team": "Fixture Away",
                "commence_time": (
                    datetime.utcnow() + timedelta(days=1)
                ).isoformat() + "Z",
            }]
        if self.kind == "malformed":
            raise ValueError("Invalid JSON fixture")
        if self.kind == "empty":
            return {"id": "fixture-event", "bookmakers": []}
        return [{
            "id": "fixture-event", "sport_key": "basketball_nba",
            "home_team": "Fixture Home", "away_team": "Fixture Away",
            "commence_time": (datetime.utcnow() + timedelta(days=1)).isoformat() + "Z",
            "bookmakers": [{"key": "prizepicks", "markets": [{
                "key": "player_points", "outcomes": [{
                    "description": "Fixture Player", "player_id": "fixture-player",
                    "name": "Over", "point": 12.5, "dfs_odds_type": "standard",
                }],
            }]}],
        }]


class _Session:
    def __init__(self, kind):
        self.kind = kind
        self.requests = 0

    def get(self, *args, **kwargs):
        self.requests += 1
        return _Response(self.kind, self.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,success,expected_rows", [
    ("timeout", False, 0), ("malformed", False, 0),
    ("empty", True, 0), ("success", True, 1),
])
async def test_real_parser_to_worker_persistence_handles_response_states(
    monkeypatch, kind, success, expected_rows
):
    session = _Session(kind)
    budget = propline.PropLineRequestBudget(10)
    now = [0.0]

    def clock():
        return now[0]

    async def advance_clock(seconds):
        now[0] += seconds

    limiter = propline.PropLineRequestLimiter(
        clock=clock, sleep=advance_clock
    )
    client = propline.PropLineClient(
        api_key="fixture-not-a-secret", api_url=propline.DEFAULT_PROPLINE_API_URL,
        session=session, request_budget=budget, endpoint_request_budget=budget,
        request_limiter=limiter, endpoint_request_limiter=limiter,
    )
    provider = propline.PropLinePrizePicksProvider(client=client, bookmaker="prizepicks")
    _isolate_worker(monkeypatch, provider, ["prizepicks"])
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    try:
        result = await market_engine.propline_request_scan_job(NS(bot_data={"db": db}))
        assert result is success
        rows = await db.get_prop_line_history(
            "PrizePicks", "Fixture Player", "NBA", "points"
        )
        assert len(rows) == expected_rows
        assert session.requests == 2
        assert len(market_engine._propline_pending_props) == expected_rows
        if rows:
            assert rows[0].provider == "PrizePicks"
            assert rows[0].line_value == 12.5
            assert rows[0].game_id == "fixture-event"
    finally:
        await db.close()


@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_all_directional_windows_pushes_and_h2h_use_known_game_results(direction):
    now = datetime.utcnow().date()
    # Newest first: UNDER, PUSH, OVER repeated ten times.
    rows = [NS(actual_value=[1, 2, 3][i % 3],
               game_date=(now - timedelta(days=i)).isoformat(), opponent="OPP")
            for i in range(30)]
    rates = compute_hit_rates(rows, 2, opponent="OPP")
    for name, n in [("l5", 5), ("l10", 10), ("l20", 20), ("l30", 30),
                    ("h2h", 30), ("h2h_l5", 5), ("h2h_l10", 10),
                    ("h2h_l20", 20)]:
        values = [r.actual_value for r in rows[:n]]
        expected = sum(v < 2 if direction == "UNDER" else v > 2 for v in values)
        assert directional_window_counts(getattr(rates, name), direction) == (
            expected, n, values.count(2)
        )
    short = compute_hit_rates(rows[:4], 2, opponent="UNAVAILABLE")
    assert all(getattr(short, name) is None
               for name in ("l5", "l10", "l20", "l30", "h2h"))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_dota_evaluation_refreshes_from_completed_local_history(provider):
    from providers.player_history import get_player_history_provider
    from engine.player_validator import validate_player_prop
    from engine.ud_scoring import score_ud_prop
    from engine.ud_bet_decision import make_ud_bet_decision

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    stat = "Fantasy Points in Games 1+2"
    try:
        await db.upsert_player_results_bulk([
            NS(player_name="Dota Fixture", sport="DOTA", stat_type=stat,
               game_date=(datetime.utcnow() - timedelta(days=i+1)).date().isoformat(),
               opponent="Opponent", actual_value=120+i, source="opendota_series",
               game_id=f"completed-{i}")
            for i in range(13)
        ])
        keyed = await db.get_player_results_bulk([("Dota Fixture", "DOTA", stat.lower())])
        rows = keyed[("Dota Fixture", "DOTA", stat.lower())]
        hits = await market_engine._fetch_and_compute_hit_rates(
            db, "Dota Fixture", "DOTA", stat, 105.6, allow_remote=False,
            source_provider=provider, preloaded_results=rows
        )
        snapshot = await get_player_history_provider().get_snapshot(
            db, "Dota Fixture", "DOTA", stat, 105.6, ud_history=[],
            source_provider=provider, ensure_real_results=False,
            preloaded_results=rows
        )
        validation = validate_player_prop(
            "Dota Fixture", stat, 105.6, [], history_snap=snapshot
        )
        decision = make_ud_bet_decision(
            score_ud_prop("Dota Fixture", stat, "DOTA", 105.6, None, []),
            validation, 105.6, hit_rates=hits, sport="DOTA"
        )
        assert validation.n_real_games == 13
        assert validation.has_supporting_data
        assert decision.recommendation == "OVER"
        assert hits.l5.over_count == 5 and hits.l10.over_count == 10
    finally:
        await db.close()
