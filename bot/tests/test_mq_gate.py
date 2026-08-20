"""Regression tests for canonical Tier 1/Tier 2 delivery policy."""

from __future__ import annotations

import pytest

from market_engine import (
    _TIER2_SPORTS,
    _apply_delivery_diversification,
    _cand_priority,
    _is_tier2_sport,
    _tier_delivery_gate,
)


@pytest.mark.parametrize("sport", ["NBA", "MLB", "NFL", "NCAA", "NCAAF", "CFB"])
def test_existing_core_sports_are_tier2(sport):
    assert _is_tier2_sport(sport)


@pytest.mark.parametrize(
    "sport",
    [
        "WNBA", "CS", "CS2", "LOL", "VAL", "VALORANT", "DOTA", "DOTA2",
        "TENNIS", "ATP", "WTA", "NPB", "KBO", "NHL", "SOCCER", "FIFA",
        "EPL", "PGA", "GOLF", "MMA", "BOXING", "AFL", "AFLW", "TT",
        "BADMINTON", "MATCH", "MOTORCYCLE", "UNKNOWN",
    ],
)
def test_every_other_sport_is_tier1(sport):
    assert not _is_tier2_sport(sport)


def test_tier2_alias_set_is_preserved():
    assert _TIER2_SPORTS == frozenset({"NBA", "MLB", "NFL", "NCAA", "NCAAF", "CFB"})


@pytest.mark.parametrize("direction", ["OVER", "UNDER", "over", "under"])
def test_tier1_requires_strong_scores_and_evidence(direction):
    assert _tier_delivery_gate("WNBA", direction, 70, 70, True)
    assert _tier_delivery_gate("CS2", direction, 84, 84, True)
    assert _tier_delivery_gate("NHL", direction, 85, 85, True)


@pytest.mark.parametrize(
    "bq,mq,evidence",
    [(69, 90, True), (90, 69, True), (90, 90, False), (0, 0, True)],
)
def test_tier1_blocks_below_threshold_or_without_evidence(bq, mq, evidence):
    assert not _tier_delivery_gate("WNBA", "OVER", bq, mq, evidence)


def test_tier1_does_not_require_score_tier():
    # The gate has no S/A/B/C argument or requirement.
    assert _tier_delivery_gate("TENNIS", "UNDER", 70, 70, True)


def test_tier1_low_scores_remain_evaluable_but_not_deliverable():
    # This is a delivery assertion only; evaluation persistence is handled
    # underneath the gate by the existing scan paths.
    assert not _tier_delivery_gate("DOTA2", "OVER", 40, 69, True)


@pytest.mark.parametrize("sport", ["NBA", "MLB", "NFL", "NCAA", "NCAAF", "CFB"])
def test_tier2_behavior_is_unchanged(sport):
    assert _tier_delivery_gate(sport, "OVER", 85, 85, False)
    assert not _tier_delivery_gate(sport, "OVER", 84, 85, False)
    assert not _tier_delivery_gate(sport, "OVER", 85, 84, False)
    assert not _tier_delivery_gate(sport, "PASS", 100, 100, False)


def test_gate_is_deterministic():
    args = ("WNBA", "OVER", 75, 75, True)
    assert _tier_delivery_gate(*args) is True
    assert _tier_delivery_gate(*args) is True


def test_priority_and_diversification_helpers_remain_available():
    base = {
        "tier": "B", "conf": 60, "bq": 60, "mq": 60,
        "is_tier1": False, "is_meaningful_change": False,
    }
    assert _cand_priority(dict(base, is_tier1=True)) - _cand_priority(base) == pytest.approx(500.0)
    assert callable(_apply_delivery_diversification)