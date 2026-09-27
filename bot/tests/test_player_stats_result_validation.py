from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from providers.player_stats import (
    PlayerStatsProvider,
    RawGameResult,
)
import providers.player_stats as player_stats_module


@pytest.mark.asyncio
async def test_single_player_fetch_returns_only_complete_matching_rows(monkeypatch):
    valid = RawGameResult(
        player_name="s4",
        sport="DOTA",
        stat_type="kills on maps 1+2",
        game_date="2026-09-20",
        actual_value=16.0,
        opponent=None,
        source="opendota_series",
        game_id="series-1",
    )
    invalid_rows = [
        SimpleNamespace(),
        valid._replace(player_name="another player"),
        valid._replace(sport="NBA"),
        valid._replace(stat_type="assists"),
        valid._replace(game_date="not-a-date"),
        valid._replace(actual_value=float("nan")),
        valid._replace(source=""),
    ]
    provider = PlayerStatsProvider()
    provider._fetch_results_unchecked = AsyncMock(
        return_value=[valid, *invalid_rows]
    )

    rows = await provider.fetch_results("s4", "DOTA", "kills on maps 1+2")

    assert rows == [valid]
    assert isinstance(rows[0], RawGameResult)


@pytest.mark.asyncio
async def test_single_player_dota_multi_map_route_returns_usable_rows(monkeypatch):
    expected = RawGameResult(
        player_name="s4",
        sport="DOTA",
        stat_type="kills on maps 1+2",
        game_date="2026-09-20",
        actual_value=16.0,
        opponent=None,
        source="opendota_series",
        game_id="series-1",
    )
    esports_provider = SimpleNamespace(
        fetch_results=AsyncMock(return_value=[expected])
    )
    monkeypatch.setattr(
        player_stats_module,
        "_get_esports_provider",
        lambda: esports_provider,
    )

    rows = await PlayerStatsProvider().fetch_results(
        "s4", "DOTA", "kills on maps 1+2"
    )

    assert rows == [expected]