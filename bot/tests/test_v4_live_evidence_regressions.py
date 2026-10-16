"""Offline regressions for defects found during V4 live-data inspection."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import market_engine
import providers.propline as propline
from database import Database, is_gradeable_player_result_for_event


def _results(count):
    return [
        SimpleNamespace(
            player_name="Stored Player", sport="NHL", stat_type="Shots on Goal",
            actual_value=4.0, game_date=(datetime.utcnow() - timedelta(days=i + 1)).date().isoformat(),
            game_id=f"game-{i}", source="nhl_stats_api", opponent="TEST",
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
@pytest.mark.parametrize("count", [3, 10])
async def test_local_history_survives_missing_remote_provider(monkeypatch, provider, count):
    rows = _results(count)
    db = SimpleNamespace(
        get_player_results=AsyncMock(return_value=rows),
        upsert_player_results_bulk=AsyncMock(),
    )
    remote = SimpleNamespace(fetch_results=AsyncMock(side_effect=RuntimeError("provider unavailable")))
    monkeypatch.setattr(market_engine, "_supports_history_enrichment", lambda db: True)
    monkeypatch.setattr(market_engine, "_get_player_stats_provider", lambda: remote)
    monkeypatch.setattr(market_engine, "_player_result_fetch_cache", set())
    result = await market_engine._fetch_and_compute_hit_rates(
        db, "Stored Player", "NHL", "Shots on Goal", 2.5,
        source_provider=provider,
    )
    assert result.has_real_data
    if count < 5:
        assert result.l5 is None
        assert result.l10 is None
        assert result.season.games == count
    else:
        assert result.l5.games == 5
        assert result.l10.games == 10
    if count == 10:
        remote.fetch_results.assert_not_awaited()
    else:
        remote.fetch_results.assert_awaited_once()


def test_default_shared_budget_leaves_reserve_and_cannot_raise_hard_cap():
    first = propline.PropLineClient(api_key="fixture")
    second = propline.PropLineClient(api_key="fixture")
    assert first.request_budget is second.request_budget
    assert first.endpoint_request_budget is first.request_budget
    assert first.request_budget.limit == 960
    assert 1000 - first.request_budget.limit == 40
    assert propline.PropLineRequestBudget(limit=25000).limit == 1000
    budget = propline.PropLineRequestBudget()
    client = propline.PropLineClient(api_key="fixture", request_budget=budget)
    client.daily_request_limit = 25000
    assert client.daily_request_limit == 1000


def test_overlapping_clients_cannot_spend_normal_reserve_after_restart(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    ledger = str(tmp_path / "quota.sqlite3")
    budgets = [
        propline.PropLineRequestBudget(960, ledger_path=ledger)
        for _ in range(4)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(lambda i: budgets[i % 4].reserve(), range(980)))
    assert sum(accepted) == 960
    restarted = propline.PropLineRequestBudget(960, ledger_path=ledger)
    assert restarted.count == 960
    assert restarted.reserve() is False


def test_global_hourly_pace_allows_at_most_daily_target(tmp_path):
    clock = [100000.0]
    async def advance(seconds):
        clock[0] += seconds

    ledger = str(tmp_path / "quota.sqlite3")
    first = propline.PropLineRequestLimiter(
        ledger_path=ledger, ledger_clock=lambda: clock[0], sleep=advance,
    )
    second = propline.PropLineRequestLimiter(
        ledger_path=ledger, ledger_clock=lambda: clock[0], sleep=advance,
    )
    async def run():
        start = clock[0]
        for i in range(961):
            await (first if i % 2 else second).acquire()
        return clock[0] - start

    assert asyncio.run(run()) >= 24 * 3600


@pytest.mark.asyncio
async def test_weekly_audit_totals_do_not_fill_missing_game_windows():
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    try:
        real = _results(8)
        weekly = [
            SimpleNamespace(**{
                **vars(row), "game_id": f"weekly-{i}",
                "source": "sleeper_stats_weekly", "actual_value": 100.0,
            })
            for i, row in enumerate(_results(19))
        ]
        await db.upsert_player_results_bulk(real + weekly)
        assert await db.count_player_results() == 27  # retain audit data
        single = await db.get_player_results("Stored Player", "NHL", "Shots on Goal")
        bulk = await db.get_player_results_bulk([("Stored Player", "NHL", "Shots on Goal")])
        assert len(single) == len(bulk[("Stored Player", "NHL", "shots on goal")]) == 8
        for provider in ("Underdog", "PrizePicks"):
            result = await market_engine._fetch_and_compute_hit_rates(
                db, "Stored Player", "NHL", "Shots on Goal", 2.5,
                allow_remote=False, preloaded_results=real + weekly,
                source_provider=provider,
            )
            assert result.total_games == 8
            assert result.l5.games == 5
            assert result.l10 is None
            assert not is_gradeable_player_result_for_event(
                provider, "sleeper_stats_weekly",
                expected_game_id="weekly-0", result_game_id="weekly-0",
            )
    finally:
        await db.close()
