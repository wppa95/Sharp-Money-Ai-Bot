import types
import pytest

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