"""
Tests for alert daily limits in AlertDelivery.deliver_pp and deliver_underdog.

Uses mocking so no real DB, bot, or network is needed.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
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


def _directional_history(direction: str = "OVER") -> SimpleNamespace:
    over = 75 if direction == "OVER" else 25
    windows = ("l5", "l10", "l15", "l20", "l30", "season")
    return SimpleNamespace(
        recommendation=direction,
        confidence=75,
        **{f"{window}_games": 100 for window in windows},
        **{f"{window}_over": over for window in windows},
        **{f"{window}_under": 100 - over for window in windows},
    )


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
    opp.hit_rates = _directional_history("OVER")
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


# ── deliver_pp: no tier-based daily cap ─────────────────────────────────────

@pytest.mark.asyncio
async def test_s_tier_bypasses_daily_cap():
    """A directional PP alert fires regardless of the legacy tier label."""
    db = _make_db(pp_today=999)  # cap massively exceeded
    delivery = _make_delivery(db)
    opp   = _make_opp(tier="S", edge=10.0)
    score = _make_score(tier="S")

    # Patch scope + timing + broadcast so only the cap logic is under test
    # Patch at source — these are imported dynamically inside deliver_pp
    # Also patch _TIER2_SPORTS_BLOCK so NBA sport doesn't hit the Tier-2 delivery block.
    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts.format_pp_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(
            opp, score, evidence_available=True
        )

    assert result.sent is True
    assert result.filtered is False


@pytest.mark.asyncio
async def test_a_tier_is_not_blocked_when_daily_count_is_high():
    """The old PrizePicks tier-based daily cap is no longer a gate."""
    from config import config
    db = _make_db(pp_today=config.DAILY_ALERT_LIMIT)
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
        result = await delivery.deliver_pp(
            opp, score, evidence_available=True
        )

    assert result.sent is True
    assert result.filtered is False


@pytest.mark.asyncio
async def test_b_tier_is_not_blocked_when_daily_count_is_high():
    """Low-tier metadata does not block a directional PP alert."""
    from config import config
    db = _make_db(pp_today=config.DAILY_ALERT_LIMIT)
    delivery = _make_delivery(db)
    opp   = _make_opp(tier="B", edge=5.5)
    score = _make_score(tier="B")

    from alert_scope_filter import FilterResult
    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts.format_pp_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", return_value=(True, "")),
    ):
        result = await delivery.deliver_pp(
            opp, score, evidence_available=True
        )

    assert result.sent is True
    assert result.filtered is False


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
        result = await delivery.deliver_pp(
            opp, score, evidence_available=True
        )

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
        result = await delivery.deliver_pp(
            opp, score, evidence_available=True
        )

    assert result.sent is False
    assert result.filtered is True
    assert "IN PROGRESS" in result.filtered_reason or "progress" in result.filtered_reason.lower()


# ── deliver_underdog: daily cap ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_underdog_cap_is_not_a_delivery_gate():
    """A legacy book-specific limit cannot favor the other primary platform."""
    from config import config
    with patch.object(config, "DAILY_UNDERDOG_LIMIT", 10):
        db = _make_db(ud_today=10)
        delivery = _make_delivery(db)

        from alert_scope_filter import FilterResult
        import alerts as alerts_mod
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
    assert "cap" not in result.filtered_reason.lower()
    assert "limit" not in result.filtered_reason.lower()
    db.count_today_underdog_alerts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["Underdog", "PrizePicks"])
@pytest.mark.parametrize("legacy_limit", [0, 10])
async def test_primary_platforms_have_no_legacy_daily_cap(source, legacy_limit):
    """Every eligible individual pick is attempted for either platform."""
    from config import config
    with patch.object(config, "DAILY_UNDERDOG_LIMIT", legacy_limit):
        db = _make_db(ud_today=9999)  # enormous count — must not block
        delivery = _make_delivery(db)

        import alerts as alerts_mod
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
                source=source,
                game_time=None,
                score=MagicMock(total=75),
                validation=SimpleNamespace(
                    has_supporting_data=True,
                        data_source="propline",
                        n_real_games=0,
                        n_supplemental_games=10,
                ),
                 decision=SimpleNamespace(
                     **{**vars(_directional_history("OVER")), "confidence": 80},
                 ),
            )

    assert result.sent is True
    assert result.filtered is False
    db.count_today_underdog_alerts.assert_not_awaited()


@pytest.mark.asyncio
async def test_underdog_allowed_when_under_cap():
    """Underdog alert fires when count is below a configured daily cap."""
    import alerts as alerts_mod
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
            score=MagicMock(total=75),
            validation=SimpleNamespace(
                has_supporting_data=True,
                    data_source="propline",
                    n_real_games=0,
                    n_supplemental_games=10,
            ),
             decision=SimpleNamespace(
                 **{**vars(_directional_history("OVER")), "confidence": 80},
             ),
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
async def test_future_event_still_requires_evidence_without_calendar_wait():
    """Removing the calendar wait must not waive main-league evidence."""
    db = _make_db()
    delivery = _make_delivery(db)

    from alert_scope_filter import FilterResult
    with patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)):
        result = await delivery.deliver_underdog(
            "Tomorrow Player", "TeamA", "NBA", "Points",
            25.5, 26.0,
            game_time=datetime.utcnow() + timedelta(days=1),
         decision=SimpleNamespace(**vars(_directional_history("OVER"))),
            strict_pregame=True,
        )

    assert result.sent is False
    assert result.filtered is True
    assert "Canonical direction/evidence gate" in result.filtered_reason
    delivery._bot.send_message.assert_not_awaited()


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sport",
    ["CFL", "RUGBY_UNION", "AFL", "college_football"],
)
async def test_disallowed_football_family_is_not_delivered(sport):
    """Only NFL/CFB labels and their active aliases may reach delivery."""
    db = _make_db()
    delivery = _make_delivery(db)

    result = await delivery.deliver_underdog(
        "Football Player",
        "TeamA",
        sport,
        "Points",
        25.5,
        26.0,
        validation=SimpleNamespace(
            has_supporting_data=True,
            data_source="primary",
            n_real_games=5,
        ),
        decision=SimpleNamespace(**vars(_directional_history("OVER"))),
    )

    assert result.sent is False
    assert result.filtered is True
    assert "football-related" in result.filtered_reason.lower()
    delivery._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ncaaf_alias_is_delivered_as_active_cfb_scope():
    db = _make_db()
    delivery = _make_delivery(db)

    result = await delivery.deliver_underdog(
        "Football Player",
        "TeamA",
        "NCAAF",
        "Points",
        25.5,
        26.0,
        validation=SimpleNamespace(
            has_supporting_data=True,
            data_source="primary",
            n_real_games=5,
        ),
        decision=SimpleNamespace(**vars(_directional_history("OVER"))),
    )

    assert result.sent is True
    delivery._bot.send_message.assert_awaited()


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
        result = await delivery.deliver_pp(
            opp, score, evidence_available=True
        )

    # A-tier should be sent — S-tier volume must not drain the A/B budget
    assert result.sent is True
    assert result.filtered is False
