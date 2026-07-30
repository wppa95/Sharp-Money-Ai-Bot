"""
Tests for alert daily limits in AlertDelivery.deliver_pp and deliver_underdog.

Uses mocking so no real DB, bot, or network is needed.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_db(
    pp_today: int = 0,
    ud_today: int = 0,
    has_recent_pp: bool = False,
) -> AsyncMock:
    db = AsyncMock()
    db.count_today_pp_alerts = AsyncMock(return_value=pp_today)
    db.count_today_underdog_alerts = AsyncMock(return_value=ud_today)
    db.has_recent_pp_alert = AsyncMock(return_value=has_recent_pp)
    return db


def _make_bot() -> AsyncMock:
    return AsyncMock()


def _make_pp_line(start_time=None) -> MagicMock:
    line = MagicMock()
    line.player_name = "Test Player"
    line.stat_type   = "Points"
    line.start_time  = start_time
    line.sport       = "NBA"
    line.line_value  = 25.5
    return line


def _make_opp(tier: str = "A", edge: float = 6.0, start_time=None) -> MagicMock:
    opp = MagicMock()
    opp.pp_line   = _make_pp_line(start_time)
    opp.best_side  = "OVER"
    opp.best_edge  = edge
    opp.sportsbook = "FanDuel"
    opp.sportsbook_line = 25.5
    opp.sportsbook_over_odds  = -115
    opp.sportsbook_under_odds = -105
    opp.adjusted_fair_prob_over  = 0.52
    opp.adjusted_fair_prob_under = 0.48
    opp.edge_over  = edge
    opp.edge_under = 0.0
    return opp


def _make_score(tier: str = "A") -> MagicMock:
    score = MagicMock()
    score.tier  = tier
    score.total = 75.0
    return score


def _make_delivery(db, chat_ids=None):
    from alerts import AlertDelivery
    bot = _make_bot()
    return AlertDelivery(db, bot, chat_ids or [123456])


# ── Scope filter + normaliser stubs ──────────────────────────────────────────

def _patch_scope_allowed():
    """Patch scope filter to always allow (we're testing limits, not scope)."""
    from unittest.mock import patch
    from alert_scope_filter import FilterResult
    return patch(
        "alerts.alert_scope_filter.check",
        return_value=FilterResult(allowed=True),
    )


# ── deliver_pp: S-tier bypasses cap ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_s_tier_bypasses_daily_cap():
    """S-tier PP alerts always fire regardless of daily count."""
    db = _make_db(pp_today=999)  # cap massively exceeded
    delivery = _make_delivery(db)
    opp   = _make_opp(tier="S", edge=10.0)
    score = _make_score(tier="S")

    # Patch scope + timing + broadcast so only the cap logic is under test
    # Patch at source — these are imported dynamically inside deliver_pp
    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts.format_pp_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(opp, score)

    assert result.sent is True
    assert result.filtered is False


@pytest.mark.asyncio
async def test_a_tier_blocked_when_cap_reached():
    """A-tier PP alert is suppressed when daily cap is hit."""
    from config import config
    db = _make_db(pp_today=config.DAILY_ALERT_LIMIT)
    delivery = _make_delivery(db)
    opp   = _make_opp(tier="A", edge=6.0)
    score = _make_score(tier="A")

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(opp, score)

    assert result.sent is False
    assert result.filtered is True
    assert "cap" in result.filtered_reason.lower() or "limit" in result.filtered_reason.lower()


@pytest.mark.asyncio
async def test_b_tier_blocked_when_cap_reached():
    """B-tier PP alert is suppressed when daily cap is hit."""
    from config import config
    db = _make_db(pp_today=config.DAILY_ALERT_LIMIT)
    delivery = _make_delivery(db)
    opp   = _make_opp(tier="B", edge=5.5)
    score = _make_score(tier="B")

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(opp, score)

    assert result.sent is False
    assert result.filtered is True


@pytest.mark.asyncio
async def test_a_tier_allowed_when_under_cap():
    """A-tier PP alert fires when the daily count is below the cap."""
    db = _make_db(pp_today=0)
    delivery = _make_delivery(db)
    opp   = _make_opp(tier="A", edge=6.0)
    score = _make_score(tier="A")

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts.format_pp_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(opp, score)

    assert result.sent is True


# ── deliver_pp: timing filter ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pp_timing_filter_blocks_in_progress_game():
    """deliver_pp blocks when the game timing filter says no."""
    db = _make_db()
    delivery = _make_delivery(db)
    opp   = _make_opp()
    score = _make_score()

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("engine.timing.is_game_alertable",
              return_value=(False, "🔴 Game already in progress")),
    ):
        result = await delivery.deliver_pp(opp, score)

    assert result.sent is False
    assert result.filtered is True
    assert "IN PROGRESS" in result.filtered_reason or "progress" in result.filtered_reason.lower()


# ── deliver_underdog: daily cap ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_underdog_blocked_when_daily_cap_reached():
    """Underdog alert suppressed when daily Underdog cap is hit."""
    from config import config
    db = _make_db(ud_today=config.DAILY_UNDERDOG_LIMIT)
    delivery = _make_delivery(db)

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_underdog(
            "Test Player", "TeamA", "NBA", "Points",
            25.5, 26.0,
            game_time=None,
        )

    assert result.sent is False
    assert result.filtered is True
    assert "cap" in result.filtered_reason.lower() or "limit" in result.filtered_reason.lower()


@pytest.mark.asyncio
async def test_underdog_allowed_when_under_cap():
    """Underdog alert fires when count is below daily cap."""
    db = _make_db(ud_today=0)
    delivery = _make_delivery(db)

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts_multiplatform.format_underdog_change_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_underdog(
            "Test Player", "TeamA", "NBA", "Points",
            25.5, 26.0,
            game_time=None,
        )

    assert result.sent is True


# ── deliver_underdog: timing filter ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_underdog_timing_filter_blocks():
    """Underdog alert blocked when game already started."""
    db = _make_db()
    delivery = _make_delivery(db)

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("engine.timing.is_game_alertable",
              return_value=(False, "🔴 Game already in progress")),
    ):
        result = await delivery.deliver_underdog(
            "Test Player", "TeamA", "NBA", "Points",
            25.5, 26.0,
            game_time=datetime.utcnow(),
        )

    assert result.sent is False
    assert result.filtered is True


@pytest.mark.asyncio
async def test_underdog_removal_skips_timing_filter():
    """Removal notices skip the timing filter (game may have ended)."""
    db = _make_db()
    delivery = _make_delivery(db)

    from alert_scope_filter import FilterResult
    _timing_called = []

    def _timing_side_effect(*a, **kw):
        _timing_called.append(True)
        return (True, "")

    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("engine.timing.is_game_alertable", side_effect=_timing_side_effect),
        patch("alerts_multiplatform.format_underdog_change_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
    ):
        await delivery.deliver_underdog(
            "Test Player", "TeamA", "NBA", "Points",
            26.0, 0.0,
            game_time=datetime.utcnow(),
            removed=True,
        )

    # timing filter should NOT have been called for removals
    assert len(_timing_called) == 0


# ── S-tier does not consume A/B cap budget ────────────────────────────────────

@pytest.mark.asyncio
async def test_s_tier_does_not_consume_ab_cap():
    """
    When many S-tier alerts have fired today, A-tier alerts must still be
    allowed as long as the A/B-specific count is below the cap.

    This verifies that count_today_pp_alerts is called with in_tiers=["A","B"]
    rather than counting all tiers.
    """
    from config import config

    # DB mock: total alerts would be huge, but A/B count is zero
    db = AsyncMock()
    db.count_today_underdog_alerts = AsyncMock(return_value=0)
    db.has_recent_pp_alert = AsyncMock(return_value=False)

    def _count_by_tiers(*args, **kwargs):
        in_tiers = kwargs.get("in_tiers")
        if in_tiers == ["A", "B"]:
            return 0          # no A/B sent today
        return 50             # lots of S sent — should be irrelevant
    db.count_today_pp_alerts = AsyncMock(side_effect=_count_by_tiers)

    delivery = _make_delivery(db)
    opp   = _make_opp(tier="A", edge=7.0)
    score = _make_score(tier="A")

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts.format_pp_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(opp, score)

    # A-tier should be sent — S-tier volume must not drain the A/B budget
    assert result.sent is True
    assert result.filtered is False
