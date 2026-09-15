"""Regression coverage for non-MLB feeds with incomplete event metadata."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from alerts import AlertDelivery
from providers.propline import official_player_market_for


def _db():
    db = MagicMock()
    db.count_today_underdog_alerts = AsyncMock(return_value=0)
    db.record_telegram_delivery_audit = AsyncMock()
    db.mark_ud_snapshot_alert_sent = AsyncMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("sport", ["CS", "TENNIS", "FIFA"])
async def test_supported_feed_without_schedule_cannot_deliver_pick(sport):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    delivery = AlertDelivery(_db(), bot, [123])
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=10,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(recommendation="OVER", confidence=80)

    result = await delivery.deliver_underdog(
        player_name="Supported Player",
        team="TEAM",
        sport=sport,
        stat_type="Points",
        old_line=10.5,
        new_line=10.5,
        game_time=None,
        validation=validation,
        decision=decision,
        strict_pregame=True,
        fetched_at=None,
    )

    assert result.sent is False
    assert result.filtered is True
    assert "unknown" in result.filtered_reason.lower()
    bot.send_message.assert_not_awaited()


def test_snake_case_tennis_labels_use_official_markets():
    assert official_player_market_for("double_faults", sport="TENNIS") == (
        "player_double_faults"
    )
    assert official_player_market_for("games_won", sport="TENNIS") == (
        "player_games_won"
    )


def test_snake_case_fifa_composite_uses_official_market():
    assert official_player_market_for("goals_assists", sport="FIFA") == (
        "goal_or_assist"
    )