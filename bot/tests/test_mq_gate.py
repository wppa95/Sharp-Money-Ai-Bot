"""Regression tests for the v3.6 unified monitoring delivery gate.

The gate is a delivery-quality gate, not a sport/tier classifier.  Evaluation
and persistence continue for every sport and decision tier; only a candidate
with a valid direction, strong BQ/MQ, and evidence is deliverable.
"""

from __future__ import annotations

import pytest

from market_engine import (
    _apply_delivery_diversification,
    _cand_priority,
    _is_tier2_sport,
    _tier_delivery_gate,
)


@pytest.mark.parametrize("sport", ["NBA", "MLB", "NFL", "NCAA", "WNBA", "CS2", "UNKNOWN"])
def test_all_sports_share_one_delivery_policy(sport):
    """Sport never changes eligibility in the unified monitoring pool."""
    assert _is_tier2_sport(sport) is False
    assert _tier_delivery_gate(sport, "OVER", 80, 85, True, "S") is True
    assert _tier_delivery_gate(sport, "UNDER", 80, 85, True, "S") is True


@pytest.mark.parametrize("direction", ["OVER", "UNDER", "over", "under"])
def test_both_directions_are_eligible(direction):
    assert _tier_delivery_gate("MLB", direction, 80, 85, True, "S") is True


@pytest.mark.parametrize("direction", ["PASS", "", "PUSH", "sideways"])
def test_invalid_direction_is_rejected(direction):
    assert _tier_delivery_gate("NBA", direction, 100, 100, True, "S") is False


@pytest.mark.parametrize(
    "bq,mq,evidence",
    [(79, 100, True), (80, 84, True), (80, 85, False), (0, 0, True)],
)
def test_hard_quality_gate_blocks_weak_candidates(bq, mq, evidence):
    """The intentional v3.6 hard gate requires BQ, MQ, and evidence."""
    assert _tier_delivery_gate("SOCCER", "OVER", bq, mq, evidence, "S") is False


def test_score_tier_remains_a_delivery_quality_signal():
    assert _tier_delivery_gate("TENNIS", "UNDER", 80, 85, True, "A") is False
    assert _tier_delivery_gate("TENNIS", "UNDER", 80, 85, True, "S") is True


def test_gate_is_deterministic():
    args = ("WNBA", "OVER", 80, 85, True, "S")
    assert _tier_delivery_gate(*args) is True
    assert _tier_delivery_gate(*args) is True


def test_priority_helpers_cover_all_decision_tiers():
    values = {
        tier: _cand_priority({
            "tier": tier, "conf": 80, "bq": 80, "mq": 85,
            "is_tier1": False, "is_meaningful_change": False,
        })
        for tier in ("S", "A", "B", "C")
    }
    assert values["S"] > values["A"] > values["B"] > values["C"]


def test_delivery_diversification_helper_remains_available():
    assert callable(_apply_delivery_diversification)