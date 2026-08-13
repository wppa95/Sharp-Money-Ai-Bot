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
    label = label_str if label_str == "STRONG" else MarketQualityLabel(label_str)
    return MarketQuality(label=label, score=score, reasons=("test",))


# ELITE / HIGH / STRONG always allow
def test_high_elite_allow():
    d = _mk_decision("A", "OVER", l5=(5, 0.6), l10=(10, 0.6))
    d.confidence = 81
    for label in ("HIGH", "ELITE", "STRONG"):
        mq = _mk_mq(label)
        allow, reason = _mq_allows_action(d, mq)
        assert allow is True
        assert reason is None


# MEDIUM is a hard block
def test_medium_mq_hard_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.80), l10=(10, 0.80))
    d.confidence = 95
    mq = _mk_mq("MEDIUM")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "MEDIUM_MQ_hard_block"


def test_low_bq_lte_80_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.80), l10=(10, 0.70))
    d.confidence = 80
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "LOW_MQ_BQ_must_be_gt_80"


def test_low_bq_gt_80_insufficient_directional_evidence_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.62), l10=(10, 0.61))
    d.confidence = 81
    d.l10_hit_rate = 0.55
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "LOW_MQ_needs_2_supporting_windows"


def test_low_two_qualifying_directional_windows_allowed_over():
    d = _mk_decision("A", "OVER", l5=(5, 0.60), l10=(10, 0.61), season=(20, 0.58))
    d.confidence = 81
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert allowed
 

def test_low_two_qualifying_directional_windows_allowed_under():
    d = _mk_decision("A", "UNDER", l5=(5, 0.40), l10=(10, 0.39), season=(20, 0.42))
    d.confidence = 82
    mq = _mk_mq("LOW")
    allowed, _ = _mq_allows_action(d, mq)
    assert allowed


def test_mq_none_preserves_behavior():
    d = _mk_decision("A", "OVER", l5=(5, 0.66), l10=(6, 0.64))
    d.confidence = 81
    allowed, _ = _mq_allows_action(d, None)
    assert allowed


def test_low_requires_recent_support_window():
    d = _mk_decision("A", "OVER", l5=(4, 0.75), l10=(4, 0.75), l20=(20, 0.65), season=(30, 0.62))
    d.confidence = 84
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "LOW_MQ_needs_recent_support"


def test_low_contradicted_blocked():
    d = _mk_decision("A", "OVER", l5=(5, 0.62), l10=(10, 0.61), season=(20, 0.40))
    d.confidence = 85
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert not allowed
    assert reason == "LOW_MQ_contradicted"


def test_low_donovan_style_under_case_remains_eligible():
    d = _mk_decision("A", "UNDER", l5=(5, 0.00), l10=(10, 0.30), season=(24, 0.38))
    d.confidence = 95
    mq = _mk_mq("LOW")
    allowed, reason = _mq_allows_action(d, mq)
    assert allowed is True
    assert reason is None


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

@pytest.mark.parametrize("label", ["ELITE", "HIGH", "STRONG"])
def test_actionable_delivery_mq_gate_allows_aliases(label):
    allowed, reason = _actionable_mq_allows_delivery(
        removed=False,
        new_prop=False,
        market_move_only=False,
        decision=types.SimpleNamespace(
            recommendation="OVER",
            confidence=81,
            l5_games=5,
            l5_hit_rate=0.60,
            l10_games=10,
            l10_hit_rate=0.61,
        ),
        market_quality=_mk_mq(label),
    )
    assert allowed is True
    assert reason is None


def test_actionable_delivery_mq_gate_blocks_medium():
    allowed, reason = _actionable_mq_allows_delivery(
        removed=False,
        new_prop=False,
        market_move_only=False,
        decision=types.SimpleNamespace(recommendation="OVER", confidence=95),
        market_quality=_mk_mq("MEDIUM"),
    )
    assert allowed is False
    assert reason == "MEDIUM_MQ_hard_block"


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
    assert result.filtered_reason == "MEDIUM_MQ_hard_block"
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