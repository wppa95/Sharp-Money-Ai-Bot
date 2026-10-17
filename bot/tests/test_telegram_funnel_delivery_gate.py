"""Tests for the tracking/grading boundary at Telegram delivery."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts import AlertDelivery
from database import Database


@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _future_game() -> datetime:
    return datetime.utcnow() + timedelta(hours=1)


def _directional_decision(
    direction: str,
    *,
    over_rate: float = 0.75,
    l30_over_rate: float | None = None,
) -> SimpleNamespace:
    windows = ("l5", "l10", "l15", "l20", "l30", "season")
    values = {"recommendation": direction, "confidence": 75}
    for window in windows:
        rate = l30_over_rate if window == "l30" and l30_over_rate is not None else over_rate
        over_count = round(rate * 100)
        values[f"{window}_games"] = 100
        values[f"{window}_over"] = over_count
        values[f"{window}_under"] = 100 - over_count
    return SimpleNamespace(**values)


async def _track_and_log(db: Database, *, external_id: str = "funnel-prop") -> None:
    await db.track_prop_records(
        [{
            "provider": "Underdog",
            "external_id": external_id,
            "player_name": "Funnel Player",
            "team": "TST",
            "sport": "MLB",
            "stat_type": "Hits",
            "line_value": 0.5,
            "game_time": _future_game(),
            "game_id": "funnel-game",
        }]
    )
    await db.log_prop_opportunity(
        external_id=external_id,
        player_name="Funnel Player",
        team="TST",
        sport="MLB",
        stat_type="Hits",
        line_value=0.5,
        recommendation="OVER",
        decision_tier="S",
        confidence=90,
        game_time=_future_game(),
        game_id="funnel-game",
        provider="Underdog",
    )


async def _confirm_pick_delivery(db, external_id: str = "funnel-prop") -> None:
    await db.record_telegram_play_delivery(
        provider="Underdog",
        delivery_path="standing",
        player_name="Funnel Player",
        sport="MLB",
        stat_type="Hits",
        external_id=external_id,
        event_id="funnel-game",
        line_value=0.5,
        recipients_sent=1,
        recipients_failed=0,
        evidence_snapshot={"direction": "OVER"},
        direction="OVER",
    )
    await db.mark_opportunity_alert_sent(
        external_id, "Hits", provider="Underdog"
    )


@pytest.mark.asyncio
async def test_delivery_state_requires_tracking_and_directional_opportunity(db):
    state = await db.get_telegram_delivery_state(
        provider="Underdog",
        external_id="funnel-prop",
        stat_type="Hits",
    )
    assert state == {
        "tracked": False,
        "directional": False,
        "already_delivered": False,
    }

    await _track_and_log(db)
    state = await db.get_telegram_delivery_state(
        provider="Underdog",
        external_id="funnel-prop",
        stat_type="Hits",
    )
    assert state["tracked"] is True
    assert state["directional"] is True
    assert state["already_delivered"] is False

    await _confirm_pick_delivery(db)
    state = await db.get_telegram_delivery_state(
        provider="Underdog",
        external_id="funnel-prop",
        stat_type="Hits",
    )
    assert state["already_delivered"] is True


@pytest.mark.asyncio
async def test_alert_delivery_blocks_already_delivered_funnel_row(db):
    await _track_and_log(db)
    await _confirm_pick_delivery(db)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    delivery = AlertDelivery(db, bot, [123])
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=10,
    )

    with (
        patch("alert_scope_filter.check", return_value=SimpleNamespace(allowed=True)),
        patch("reporting.is_current_reporting_day", return_value=True),
    ):
        result = await delivery.deliver_underdog(
            player_name="Funnel Player",
            team="TST",
            sport="MLB",
            stat_type="Hits",
            old_line=0.5,
            new_line=0.5,
            game_time=_future_game(),
            validation=validation,
            decision=_directional_decision("OVER"),
            strict_pregame=True,
            source_id="funnel-prop",
            event_id="funnel-game",
        )

    assert result.sent is False
    assert result.deduped is True
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_delivery_blocks_57_percent_l30_even_for_history_optional_sport(db):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    delivery = AlertDelivery(db, bot, [123])
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=10,
    )

    with (
        patch("alert_scope_filter.check", return_value=SimpleNamespace(allowed=True)),
        patch("reporting.is_current_reporting_day", return_value=True),
    ):
        result = await delivery.deliver_underdog(
            player_name="Tennis Player",
            team="TST",
            sport="TENNIS",
            stat_type="Aces",
            old_line=2.5,
            new_line=2.5,
            game_time=_future_game(),
            validation=validation,
            decision=_directional_decision(
                "OVER",
                over_rate=0.70,
                l30_over_rate=0.57,
            ),
            strict_pregame=True,
            source_id="tennis-prop",
            event_id="tennis-game",
        )

    assert result.sent is False
    assert result.filtered is True
    bot.send_message.assert_not_awaited()