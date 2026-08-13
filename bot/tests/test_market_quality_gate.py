import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts import AlertDelivery, _actionable_mq_allows_delivery
from alerts_multiplatform import format_underdog_change_alert
from engine.ud_scoring import MarketQuality, MarketQualityLabel
from market_engine import _mq_allows_action


def _mk_decision(
    tier,
    rec,
    l5=(None, None),
    l10=(None, None),
    l20=(None, None),
    l30=(None, None),
    season=(None, None),
):
    d = types.SimpleNamespace()
    d.decision_tier = tier
    d.recommendation = rec
    d.l5_games, d.l5_hit_rate = l5
    d.l10_games, d.l10_hit_rate = l10
    d.l20_games, d.l20_hit_rate = l20
    d.l30_games, d.l30_hit_rate = l30
    d.season_games, d.season_hit_rate = season
    return d


def _mk_mq(label_str, score=50):
    label = MarketQualityLabel(label_str)
    return MarketQuality(label=label, score=score, reasons=("test",))


# HIGH / ELITE always allow
def test_high_elite_allow():
    d = _mk_decision("A", "OVER", l5=(5, 0.6), l10=(10, 0.6))
    for label in ("HIGH", "ELITE"):
        mq = _mk_mq(label)
        allow, reason = _mq_allows_action(d, mq)
        assert allow is True
        assert reason is None


# MEDIUM A-tier requires >=2 supporting windows at 0.55/0.45
def test_medium_a_requires_two_supports_allowed():
    d = _mk_decision("A", "OVER", l5=(5, 0.56), l10=(8, 0.57), l20=(12, 0.52))
    mq = _mk_mq("MEDIUM")
    allowed, reason = _mq_allows_action(d, mq)
    assert allowed is True


def test_medium_a_insufficient_support_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.56), l10=(4, 0.60))  # l10 <5 games ignored
    mq = _mk_mq("MEDIUM")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "MEDIUM_MQ_A_needs_2_supporting_windows"


# LOW MQ behavior
def test_low_s_tier_allowed():
    d = _mk_decision("S", "UNDER", l5=(5, 0.8))
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert allowed


def test_low_b_tier_preserved():
    d = _mk_decision("B", "OVER", l5=(5, 0.7))
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert allowed


def test_low_a_two_strong_supports_allowed_over():
    d = _mk_decision("A", "OVER", l5=(5, 0.62), l10=(10, 0.61))
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert allowed


def test_low_a_two_strong_supports_allowed_under():
    d = _mk_decision("A", "UNDER", l5=(5, 0.38), l10=(10, 0.39))
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert allowed


def test_low_a_one_support_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.62), l10=(10, 0.54))
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "LOW_MQ_A_needs_2_strong_supporting_windows"


def test_low_a_zero_support_blocked():
    d = _mk_decision("A", "UNDER", l5=(5, 0.48), l10=(10, 0.52))
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert not allowed


def test_mq_none_preserves_behavior():
    d = _mk_decision("A", "OVER", l5=(5, 0.66), l10=(6, 0.64))
    allowed, _ = _mq_allows_action(d, None)
    assert allowed


# sanity: contradictory check in LOW A-tier
def test_low_a_contradicted_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.62), l10=(10, 0.38))  # l10 strongly contradicts
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    # this may be "LOW_MQ_A_contradicted" or the insufficient-support reason depending on ordering
    assert reason in (
        "LOW_MQ_A_contradicted",
        "LOW_MQ_A_needs_2_strong_supporting_windows",
    )


@pytest.mark.parametrize("label", ["ELITE", "HIGH"])
def test_actionable_delivery_mq_gate_allows_high_and_elite(label):
    allowed, reason = _actionable_mq_allows_delivery(
        removed=False,
        new_prop=False,
        market_move_only=False,
        decision=_mk_decision("A", "OVER"),
        market_quality=_mk_mq(label),
    )
    assert allowed is True
    assert reason is None


@pytest.mark.parametrize("label", ["MEDIUM", "LOW"])
def test_actionable_delivery_mq_gate_blocks_medium_and_low(label):
    allowed, reason = _actionable_mq_allows_delivery(
        removed=False,
        new_prop=False,
        market_move_only=False,
        decision=_mk_decision("A", "OVER"),
        market_quality=_mk_mq(label),
    )
    assert allowed is False
    assert reason == f"mq_not_actionable:{label}"


@pytest.mark.asyncio
async def test_deliver_underdog_blocks_medium_mq_actionable_delivery():
    db = MagicMock()
    db.count_today_underdog_alerts = AsyncMock(return_value=0)
    bot = MagicMock()
    delivery = AlertDelivery(db, bot, [123])

    with patch("alerts.broadcast_alert", new_callable=AsyncMock) as mock_broadcast:
        result = await delivery.deliver_underdog(
            player_name="Test Player",
            team="Test Team",
            sport="NBA",
            stat_type="Points",
            old_line=24.5,
            new_line=25.5,
            score=types.SimpleNamespace(tier="A", stars=4, total=78),
            decision=_mk_decision("A", "OVER"),
            market_quality=_mk_mq("MEDIUM"),
        )

    assert result.sent is False
    assert result.filtered is True
    assert result.filtered_reason == "mq_not_actionable:MEDIUM"
    mock_broadcast.assert_not_called()


def test_watchlist_only_label_never_shown_under_actionable_header():
    decision = _mk_decision("A", "OVER")
    decision.confidence = 72
    msg = format_underdog_change_alert(
        player_name="Test Player",
        team="Test Team",
        sport="NBA",
        stat_type="Points",
        old_line=24.5,
        new_line=25.5,
        score=types.SimpleNamespace(
            tier="A",
            total=72,
            stars=3,
            stars_display="★★★☆☆",
            n_history=12,
            move_velocity=10,
        ),
        decision=decision,
        market_quality=_mk_mq("HIGH", score=72),
    )

    assert "ACTIONABLE BET PICK" in msg
    assert "WATCHLIST ONLY" not in msg