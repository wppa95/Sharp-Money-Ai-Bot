"""Regression tests for cache-only evidence in live monitoring."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_engine
from engine.evidence_memory import (
    clear,
    get_line_history,
    get_player_results,
    put_line_observation,
    put_player_results,
)
from providers.player_history import PlayerHistoryProvider


@pytest.fixture(autouse=True)
def clean_evidence_memory():
    clear()
    yield
    clear()


def test_evidence_memory_uses_keyed_bounded_copies():
    rows = [
        SimpleNamespace(game_id="g1", game_date="2026-09-03", source="official"),
        SimpleNamespace(game_id="g2", game_date="2026-09-02", source="official"),
    ]
    put_player_results("Player One", "nba", "Points", rows)
    put_line_observation("Player One", "Points", 20.5)
    put_line_observation("Player One", "Points", 21.5)

    result_copy = get_player_results("player one", "NBA", "points")
    history_copy = get_line_history("PLAYER ONE", "points")

    assert [row.game_id for row in result_copy] == ["g1", "g2"]
    assert [row.line_value for row in history_copy] == [21.5, 20.5]
    assert history_copy[0].line_moved is True
    result_copy.clear()
    history_copy.clear()
    assert len(get_player_results("Player One", "NBA", "Points")) == 2
    assert len(get_line_history("Player One", "Points")) == 2


@pytest.mark.asyncio
async def test_production_candidate_history_never_falls_back_to_database():
    database = market_engine.Database.__new__(market_engine.Database)
    database.get_ud_prop_history = AsyncMock(
        side_effect=AssertionError("live scan queried SQLite")
    )

    assert await market_engine._candidate_line_history(
        database, "Player One", "Points"
    ) == []
    database.get_ud_prop_history.assert_not_awaited()

    put_line_observation("Player One", "Points", 20.5)
    history = await market_engine._candidate_line_history(
        database, "Player One", "Points"
    )
    assert [row.line_value for row in history] == [20.5]
    database.get_ud_prop_history.assert_not_awaited()


def test_production_candidate_results_distinguish_cache_miss_from_warm_cache():
    database = market_engine.Database.__new__(market_engine.Database)

    assert market_engine._candidate_player_results(
        database, "Player One", "NBA", "Points"
    ) is None

    rows = [
        SimpleNamespace(
            game_id="g1",
            game_date="2026-09-03",
            source="official",
        )
    ]
    put_player_results("Player One", "NBA", "Points", rows)

    cached = market_engine._candidate_player_results(
        database, "Player One", "NBA", "Points"
    )
    assert cached is not None
    assert [row.game_id for row in cached] == ["g1"]


@pytest.mark.asyncio
async def test_preloaded_snapshot_never_reads_or_supplements_database():
    provider = PlayerHistoryProvider()
    database = SimpleNamespace(
        get_player_results=AsyncMock(
            side_effect=AssertionError("snapshot queried SQLite")
        )
    )
    provider.supplement_missing_results = AsyncMock(
        side_effect=AssertionError("snapshot fetched supplemental history")
    )
    rows = [
        SimpleNamespace(
            player_name="Player One",
            sport="NBA",
            stat_type="points",
            game_date="2026-09-03",
            opponent="CHI",
            actual_value=24.0,
            source="official",
        )
    ]

    snapshot = await provider.get_snapshot(
        database,
        "Player One",
        "NBA",
        "Points",
        20.5,
        ensure_real_results=False,
        preloaded_results=rows,
    )

    assert snapshot.has_real_data is True
    database.get_player_results.assert_not_awaited()
    provider.supplement_missing_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_production_confirmation_uses_keyed_memory_not_database_pool():
    database = market_engine.Database.__new__(market_engine.Database)
    database.get_all_active_prop_line_history_by_line = AsyncMock(
        side_effect=AssertionError("confirmation scanned SQLite")
    )
    prop = SimpleNamespace(
        player_name="Player One",
        sport="NBA",
        stat_type="Points",
        event_id="event-1",
        line_value=20.5,
        removed=False,
        fetched_at=None,
    )
    market_engine._cache_propline_evidence([prop])

    result = await market_engine._get_odds_api_confirmation(
        "NBA",
        "Player One",
        "Points",
        "OVER",
        20.5,
        db=database,
    )

    assert result is not None
    assert result["num_books"] == 1
    database.get_all_active_prop_line_history_by_line.assert_not_awaited()
