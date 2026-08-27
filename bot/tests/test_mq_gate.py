"""Regression tests for the canonical Tier 1/Tier 2 Telegram policy."""

from __future__ import annotations

import pytest

from market_engine import _apply_delivery_diversification, _cand_priority
from engine.delivery_gates import TIER2_SPORTS, is_tier2_sport
from market_engine import _tier_delivery_gate as tier_delivery_gate


@pytest.mark.parametrize("sport", ["NBA", "MLB", "NFL", "Football", "CFB", "NCAA", "NCAAF"])
def test_tier2_sports_use_the_strict_delivery_policy(sport):
    assert is_tier2_sport(sport)
    assert not tier_delivery_gate(sport, "OVER", 80, 85, True, "S", confidence=89)
    assert tier_delivery_gate(sport, "OVER", 85, 85, True, "C", confidence=90)
    assert tier_delivery_gate(sport, "UNDER", 85, 85, True, "B", confidence=95)


@pytest.mark.parametrize("sport", ["NHL", "WNBA", "SOCCER", "DOTA", "TENNIS", "CFL", "FUTURE_SPORT"])
def test_tier1_sports_need_direction_and_evidence_but_not_quality_floors(sport):
    assert not is_tier2_sport(sport)
    assert tier_delivery_gate(sport, "OVER", 0, 0, True, "PASS", confidence=0)
    assert tier_delivery_gate(sport, "UNDER", 1, 1, True, "C", confidence=1)
    assert not tier_delivery_gate(sport, "PASS", 100, 100, True, "S", confidence=100)
    assert not tier_delivery_gate(sport, "OVER", 100, 100, False, "S", confidence=100)


def test_tier2_sports_are_the_shared_canonical_set():
    assert TIER2_SPORTS == frozenset(
        {"MLB", "NFL", "NBA", "FOOTBALL", "CFB", "NCAA", "NCAAF"}
    )


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