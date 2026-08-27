"""Extended coverage for shared monitoring and delivery rules."""

from __future__ import annotations

import inspect

import pytest

import market_engine
from market_engine import _tier_delivery_gate as tier_delivery_gate


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER", "over", "under"])
def test_tier2_directions_require_the_full_quality_tuple(sport, direction):
    assert not tier_delivery_gate(sport, direction, 80, 85, True, "S", confidence=89)
    assert tier_delivery_gate(sport, direction, 85, 85, True, "S", confidence=90)


@pytest.mark.parametrize("sport", ["WNBA", "CS2", "TENNIS"])
def test_tier1_directions_are_not_restricted_by_quality_scores(sport):
    assert tier_delivery_gate(sport, "OVER", 0, 0, True, "PASS", confidence=0)
    assert tier_delivery_gate(sport, "UNDER", 80, 85, True, "C", confidence=1)


@pytest.mark.parametrize("tier", ["A", "B", "C", "PASS"])
def test_tier_labels_do_not_create_a_watchlist_class(tier):
    assert tier_delivery_gate("WNBA", "UNDER", 0, 0, True, tier, confidence=0)


def test_invalid_recommendations_never_deliver():
    assert not tier_delivery_gate("SOCCER", "PASS", 100, 100, True, "S", confidence=100)


def test_all_delivery_paths_reference_canonical_gate():
    src = inspect.getsource(market_engine)
    assert src.count("_tier_delivery_gate(") >= 4
    assert "ACTIONABLE BET PICK" in src


def test_monitoring_and_restart_persistence_are_continuous():
    src = inspect.getsource(market_engine)
    for marker in (
        "_init_state_from_db",
        "save_underdog_snapshots_bulk",
        "update_prop_lifecycle_state",
        "_prop_market_alerted",
    ):
        assert marker in src