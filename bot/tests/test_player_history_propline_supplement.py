from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers.player_history import PlayerHistoryProvider
from providers.propline import PropLinePlayerHistoryFetchResult


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
    assert client.fetch_player_history.await_args.kwargs["bookmaker"] == "prizepicks"


@pytest.mark.asyncio
async def test_snapshot_keeps_primary_results_and_adds_only_distinct_supplemental_games():
    db = _DB([_primary_result("2026-08-29", 30.0, "game-1")])
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