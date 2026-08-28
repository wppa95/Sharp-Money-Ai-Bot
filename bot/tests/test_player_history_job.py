from __future__ import annotations

import sys
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from player_history_job import (
    _SUPPORTED_HISTORY_SPORTS,
    _fair_cycle_targets,
    player_history_collector_job,
)


def test_fair_cycle_targets_round_robins_all_sports():
    targets = [
        ("nfl-1", "NFL", "passing yards"),
        ("nfl-2", "NFL", "passing yards"),
        ("nba-1", "NBA", "points"),
        ("mlb-1", "MLB", "hits"),
    ]
    selected = _fair_cycle_targets(targets, 3)
    assert [target[1] for target in selected] == ["MLB", "NBA", "NFL"]


def test_fair_cycle_targets_preserves_all_when_under_limit():
    targets = [("nba-1", "NBA", "points"), ("wnba-1", "WNBA", "assists")]
    assert _fair_cycle_targets(targets, 100) == [
        ("nba-1", "NBA", "points"),
        ("wnba-1", "WNBA", "assists"),
    ]


def test_fifa_uses_existing_soccer_history_path():
    assert "FIFA" in _SUPPORTED_HISTORY_SPORTS


@pytest.mark.asyncio
async def test_player_history_yields_when_underdog_claims_slot_after_active_read():
    """The fallback query/provider work must not start after a live scan claims."""
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value={})
    db.get_latest_props_for_provider = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    with (
        patch("player_history_job._underdog_scan_running", side_effect=[False, True]),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await player_history_collector_job(context)

    db.get_latest_props_for_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_player_history_stops_before_db_write_when_underdog_claims_after_fetch():
    """A completed stats request must not trigger writes into the hot scan."""
    snapshot = SimpleNamespace(player="Player One", sport="NFL", selection="Passing Yards 10.5")
    db = MagicMock()
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value={("Player One", "Passing Yards"): snapshot}
    )
    db.upsert_player_result = AsyncMock()
    context = SimpleNamespace(bot_data={"db": db})

    fake_provider = MagicMock()
    fake_provider.fetch_results = AsyncMock(return_value=[SimpleNamespace()])
    with (
        patch("player_history_job._underdog_scan_running", side_effect=[False, False, False, True]),
        patch("providers.player_stats.PlayerStatsProvider", return_value=fake_provider),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await player_history_collector_job(context)

    fake_provider.fetch_results.assert_awaited_once_with(
        "Player One", "NFL", "passing yards"
    )
    db.upsert_player_result.assert_not_awaited()
