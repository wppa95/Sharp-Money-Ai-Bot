"""Regression coverage for the temporary DK/FanDuel confirmation shutdown."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

@pytest.fixture
def disabled_confirmation(monkeypatch):
    import config

    monkeypatch.setattr(config.config, "SPORTSBOOK_CONFIRMATION_ENABLED", False)
    monkeypatch.setattr(config.config, "DRAFTKINGS_ENABLED", True)
    monkeypatch.setattr(config.config, "FANDUEL_ENABLED", True)


@pytest.mark.asyncio
async def test_dk_fd_player_prop_confirmation_is_not_called(disabled_confirmation):
    import market_engine as me

    engine = MagicMock()
    engine.fetch_player_prop_lines = AsyncMock(
        return_value=[
            SimpleNamespace(
                sportsbook="DraftKings",
                sport="NBA",
                market_key="player_points",
                player_name="LeBron James",
                description="Over",
                line=25.5,
                american_odds=-110,
            ),
        ]
    )
    with patch.object(me, "_analysis_engine", engine), patch.object(
        me, "_propline_provider", None
    ):
        result = await me._get_odds_api_confirmation(
            "NBA", "LeBron James", "points", "OVER", 25.5
        )

    assert result is None
    engine.fetch_player_prop_lines.assert_not_awaited()


@pytest.mark.asyncio
async def test_moneyline_and_total_confirmation_do_not_query_dk_fd_rows(
    disabled_confirmation,
):
    import market_engine as me

    db = SimpleNamespace(get_odds_window=AsyncMock(return_value=[]))
    with patch.object(me, "_confirmation_db", db):
        moneyline = await me._get_odds_api_confirmation(
            "NFL",
            market_type="moneyline",
            selection="Kansas City Chiefs",
            team="Kansas City Chiefs",
            event="Chiefs @ Raiders",
        )
        total = await me._get_odds_api_confirmation(
            "NFL",
            market_type="total",
            direction="OVER",
            line=44.5,
            event="Chiefs @ Raiders",
        )

    assert moneyline is None
    assert total is None
    db.get_odds_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_propline_prizepicks_confirmation_remains_available(
    disabled_confirmation,
):
    import market_engine as me

    history = [
        SimpleNamespace(
            provider="PrizePicks",
            sport="NBA",
            player_name="LeBron James",
            team="LAL",
            stat_type="points",
            line_value=25.5,
            external_id="projection-1",
            game_id="game-1",
            fetched_at=datetime.utcnow(),
            removed=False,
        )
    ]
    db = SimpleNamespace(get_prop_line_history=AsyncMock(return_value=history))
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.fetch_props_for_sport = AsyncMock(
        side_effect=AssertionError("confirmation must not fetch PropLine")
    )
    with patch.object(me, "_propline_provider", provider), patch.object(
        me, "_analysis_engine", None
    ), patch.object(me, "_confirmation_db", db):
        result = await me._get_odds_api_confirmation(
            "NBA", "LeBron James", "points", "OVER", 25.5
        )

    assert result is not None
    assert result["confirmed"] is True
    assert result["num_books"] == 1
    db.get_prop_line_history.assert_awaited_once()
    provider.fetch_props_for_sport.assert_not_called()


@pytest.mark.asyncio
async def test_future_dk_fd_path_is_preserved_as_explicit_opt_in(
    monkeypatch,
):
    import config
    import market_engine as me

    monkeypatch.setattr(config.config, "SPORTSBOOK_CONFIRMATION_ENABLED", True)
    monkeypatch.setattr(config.config, "DRAFTKINGS_ENABLED", True)
    monkeypatch.setattr(config.config, "FANDUEL_ENABLED", True)
    monkeypatch.setattr(me, "_propline_provider", None)

    lines = [
        SimpleNamespace(
            sportsbook="DraftKings",
            sport="NBA",
            market_key="player_points",
            player_name="LeBron James",
            description="Over",
            line=25.5,
            american_odds=-110,
        ),
        SimpleNamespace(
            sportsbook="FanDuel",
            sport="NBA",
            market_key="player_points",
            player_name="LeBron James",
            description="Over",
            line=25.5,
            american_odds=-105,
        ),
    ]
    engine = MagicMock()
    engine.fetch_player_prop_lines = AsyncMock(return_value=lines)
    with patch.object(me, "_analysis_engine", engine):
        result = await me._get_odds_api_confirmation(
            "NBA", "LeBron James", "points", "OVER", 25.5
        )

    assert result is not None
    assert result["num_books"] == 2
    engine.fetch_player_prop_lines.assert_awaited_once()