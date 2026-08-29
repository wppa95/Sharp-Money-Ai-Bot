"""Regression tests for the direction-only Telegram delivery gate."""

from __future__ import annotations

import pytest

from market_engine import _apply_delivery_diversification, _cand_priority
from market_engine import _tier_delivery_gate as tier_delivery_gate


@pytest.mark.parametrize("sport", ["NBA", "MLB", "NFL", "NHL", "SOCCER", "DOTA", "TENNIS", "CFL"])
def test_all_sports_require_evidence(sport):
    assert not tier_delivery_gate(sport, "OVER", 0, 0, False, "PASS", confidence=0)
    assert tier_delivery_gate(sport, "UNDER", 1, 1, True, "C", confidence=1)


@pytest.mark.parametrize("direction", ["", "PASS", "WATCHLIST", "NEAR-MISS", None])
def test_non_directional_values_are_rejected(direction):
    assert not tier_delivery_gate("NBA", direction, 100, 100, True, "S", confidence=100)


def test_priority_helpers_remain_callable_but_are_not_qualification_rules():
    values = {
        tier: _cand_priority({
            "tier": tier, "conf": 80, "bq": 80, "mq": 85,
            "is_meaningful_change": False,
        })
        for tier in ("S", "A", "B", "C")
    }
    assert set(values) == {"S", "A", "B", "C"}


def test_delivery_diversification_helper_remains_available():
    assert callable(_apply_delivery_diversification)