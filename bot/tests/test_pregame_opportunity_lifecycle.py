"""Focused regressions for the pregame-only opportunity boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts import AlertDelivery
from commands import _is_upcoming_game
from engine.timing import is_game_alertable
from prizepicks import PPEdgeOpportunity, PrizePicksLine


def _future(minutes: int = 60) -> datetime:
    return datetime.utcnow() + timedelta(minutes=minutes)


def _pp_opportunity(*, start_time=None, status: str = "") -> PPEdgeOpportunity:
    line = PrizePicksLine(
        external_id="pp-pregame-1",
        player_name="PP Player",
        team="TEAM",
        sport="NBA",
        league="NBA",
        stat_type="Points",
        line_value=20.5,
        start_time=start_time,
    )
    line.provider_status = status
    return PPEdgeOpportunity(
        pp_line=line,
        sportsbook="DraftKings",
        sportsbook_line=21.5,
        sportsbook_over_odds=-110,
        sportsbook_under_odds=-110,
        fair_prob_over_at_sb_line=0.5,
        fair_prob_under_at_sb_line=0.5,
        line_diff=1.0,
        adjusted_fair_prob_over=0.6,
        adjusted_fair_prob_under=0.4,
        edge_over=10.0,
        edge_under=0.0,
        best_side="OVER",
        best_edge=10.0,
        prob_per_unit=3.0,
    )


def _delivery() -> AlertDelivery:
    db = MagicMock()
    db.count_today_underdog_alerts = AsyncMock(return_value=0)
    return AlertDelivery(db, MagicMock(), [123])


def test_strict_timing_rejects_unknown_event_state():
    allowed, reason = is_game_alertable(
        None,
        min_minutes=30,
        max_minutes=120,
        urgent_edge=8.0,
        edge=99.0,
        require_known=True,
    )
    assert allowed is False
    assert "unknown" in reason.lower()


@pytest.mark.parametrize("status", ["STARTED", "LIVE", "IN_PROGRESS", "FINAL", "COMPLETED"])
def test_strict_timing_rejects_provider_started_states(status):
    allowed, _ = is_game_alertable(
        _future(),
        min_minutes=30,
        max_minutes=120,
        urgent_edge=8.0,
        edge=99.0,
        game_status=status,
        require_known=True,
    )
    assert allowed is False


def test_explicit_pregame_status_can_supply_missing_start_time():
    allowed, reason = is_game_alertable(
        None,
        min_minutes=30,
        max_minutes=120,
        urgent_edge=8.0,
        edge=5.0,
        game_status="SCHEDULED",
        require_known=True,
    )
    assert allowed is True
    assert reason == ""


def test_command_views_reject_unknown_and_started_props():
    assert _is_upcoming_game(None) is False
    assert _is_upcoming_game(_future(), "OPEN") is True
    assert _is_upcoming_game(_future(), "LIVE") is False


@pytest.mark.asyncio
async def test_pregame_underdog_prop_can_reach_delivery():
    delivery = _delivery()
    with (
        patch("alert_scope_filter.check", return_value=SimpleNamespace(allowed=True)),
        patch(
            "alerts_multiplatform.format_underdog_change_alert",
            return_value="<msg>",
        ),
        patch(
            "alerts.broadcast_alert",
            new_callable=AsyncMock,
            return_value={"sent": 1, "failed": 0},
        ),
    ):
        result = await delivery.deliver_underdog(
            "UD Player",
            "TEAM",
            "NBA",
            "Points",
            20.5,
            21.5,
            game_time=_future(),
            strict_pregame=True,
        )
    assert result.sent is True


@pytest.mark.asyncio
async def test_pregame_prizepicks_prop_can_reach_delivery():
    delivery = _delivery()
    with (
        patch("alert_scope_filter.check", return_value=SimpleNamespace(allowed=True)),
        patch("alerts.format_pp_alert", return_value="<msg>"),
        patch(
            "alerts.broadcast_alert",
            new_callable=AsyncMock,
            return_value={"sent": 1, "failed": 0},
        ),
    ):
        result = await delivery.deliver_pp(
            _pp_opportunity(start_time=_future()),
            strict_pregame=True,
        )
    assert result.sent is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["STARTED", "LIVE", "FINAL"])
async def test_started_underdog_prop_cannot_reach_telegram(status):
    delivery = _delivery()
    with patch("alerts.broadcast_alert", new_callable=AsyncMock) as broadcast:
        result = await delivery.deliver_underdog(
            "UD Player",
            "TEAM",
            "NBA",
            "Points",
            20.5,
            21.5,
            game_time=_future(),
            game_status=status,
            strict_pregame=True,
        )
    assert result.sent is False
    assert result.filtered is True
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["STARTED", "LIVE", "FINAL"])
async def test_started_prizepicks_prop_cannot_reach_telegram(status):
    delivery = _delivery()
    with patch("alerts.broadcast_alert", new_callable=AsyncMock) as broadcast:
        result = await delivery.deliver_pp(
            _pp_opportunity(start_time=_future(), status=status),
            strict_pregame=True,
        )
    assert result.sent is False
    assert result.filtered is True
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_underdog_state_cannot_bypass_pregame_protection():
    delivery = _delivery()
    with patch("alerts.broadcast_alert", new_callable=AsyncMock) as broadcast:
        result = await delivery.deliver_underdog(
            "UD Player",
            "TEAM",
            "NBA",
            "Points",
            20.5,
            21.5,
            strict_pregame=True,
        )
    assert result.sent is False
    assert "unknown" in result.filtered_reason.lower()
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_prizepicks_state_cannot_bypass_pregame_protection():
    delivery = _delivery()
    with patch("alerts.broadcast_alert", new_callable=AsyncMock) as broadcast:
        result = await delivery.deliver_pp(
            _pp_opportunity(),
            strict_pregame=True,
        )
    assert result.sent is False
    assert "unknown" in result.filtered_reason.lower()
    broadcast.assert_not_awaited()