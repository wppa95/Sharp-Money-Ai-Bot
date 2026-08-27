"""Focused regression tests for additive provider confirmation support."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, ".")

from connectors.underdog import UnderdogConnector
from engine.provider_confirmation import (
    ConfirmationObservation,
    ConfirmationTarget,
    confirm_market,
)


def test_underdog_parses_explicit_game_totals_without_player_props():
    payload = {
        "totals": [{
            "id": "total-1",
            "sport": "NFL",
            "event": "Chiefs @ Raiders",
            "outcomes": [
                {"id": "over-1", "name": "Over", "point": 44.5},
                {"id": "under-1", "name": "Under", "point": 44.5},
            ],
        }],
    }

    projections = UnderdogConnector()._parse(payload)

    assert len(projections) == 2
    assert {p.selection for p in projections} == {"Over", "Under"}
    assert all(p.is_game_market for p in projections)
    assert all(p.market_type == "Total (O/U)" for p in projections)


def test_confirmation_supports_moneyline_total_and_player_prop():
    now = datetime.utcnow()
    cases = [
        (
            ConfirmationTarget(
                sport="NFL", market_type="Moneyline",
                selection="Kansas City Chiefs", team="Kansas City Chiefs",
                event="Chiefs @ Raiders",
            ),
            ConfirmationObservation(
                provider="DraftKings", sport="NFL", market_type="h2h",
                selection="Kansas City Chiefs", team="Kansas City Chiefs",
                event="Chiefs @ Raiders", odds=-140, observed_at=now,
            ),
        ),
        (
            ConfirmationTarget(
                sport="NFL", market_type="Total (O/U)", direction="OVER",
                line=44.5, event="Chiefs @ Raiders",
            ),
            ConfirmationObservation(
                provider="FanDuel", sport="NFL", market_type="totals",
                selection="Over", direction="OVER", line=44.5,
                event="Chiefs @ Raiders", odds=-110, observed_at=now,
            ),
        ),
        (
            ConfirmationTarget(
                sport="NBA", market_type="player_prop", player="LeBron James",
                stat_type="points", direction="OVER", line=25.5,
                event="Lakers @ Suns",
            ),
            ConfirmationObservation(
                provider="DraftKings", sport="NBA", market_type="player_points",
                player="LeBron James", stat_type="points", direction="OVER",
                line=25.5, event="Lakers @ Suns", odds=-115,
                observed_at=now,
            ),
        ),
    ]

    for target, observation in cases:
        result = confirm_market(
            target, [observation], now=now, max_age_seconds=60,
        )
        assert result is not None
        assert result["confirmed"] is True
        assert result["num_books"] == 1


def test_confirmation_rejects_wrong_side_ambiguous_name_and_stale_data():
    now = datetime.utcnow()
    target = ConfirmationTarget(
        sport="NBA", market_type="player_prop", player="Jordan Lee",
        stat_type="points", direction="OVER", line=20.5,
    )
    observations = [
        ConfirmationObservation(
            provider="DraftKings", sport="NBA", market_type="player_points",
            player="Jordan Lee", stat_type="points", direction="UNDER",
            line=20.5, observed_at=now,
        ),
        ConfirmationObservation(
            provider="FanDuel", sport="NBA", market_type="player_points",
            player="Lee", stat_type="points", direction="OVER",
            line=20.5, observed_at=now - timedelta(hours=5),
        ),
    ]

    assert confirm_market(
        target, observations, now=now, max_age_seconds=4 * 60 * 60,
    ) is None


@pytest.mark.asyncio
async def test_game_confirmation_uses_persisted_odds_rows_without_polling():
    # Kept async so callers can use the same shape as the market-engine path.
    from market_engine import _get_odds_api_confirmation, init_odds_confirmation

    db = SimpleNamespace(
        get_odds_window=AsyncMock(return_value=[
            SimpleNamespace(
                sportsbook="DraftKings", sport="NFL",
                market_type="Total (O/U)", event="Chiefs @ Raiders",
                selection="Over", line=44.5, american_odds=-110,
                recorded_at=datetime.utcnow(),
            ),
        ])
    )
    init_odds_confirmation(None, db)
    try:
        result = await _get_odds_api_confirmation(
            "NFL", market_type="total", direction="OVER", line=44.5,
            event="Chiefs @ Raiders",
        )
        assert result is not None
        assert result["confirmed"] is True
        db.get_odds_window.assert_awaited_once()
    finally:
        init_odds_confirmation(None)