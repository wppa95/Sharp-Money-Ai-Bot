"""Extended coverage for direction-and-evidence delivery rules."""

from __future__ import annotations

import inspect

import pytest

import market_engine
from market_engine import _tier_delivery_gate as tier_delivery_gate


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA", "WNBA", "CS2", "TENNIS"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER", "over", "under"])
def test_directional_candidates_ignore_quality_and_tier_metadata(sport, direction):
    assert not tier_delivery_gate(sport, direction, 0, 0, False, "PASS", confidence=0)
    assert tier_delivery_gate(sport, direction, 0, 0, True, "PASS", confidence=0)


@pytest.mark.parametrize("tier", ["A", "B", "C", "PASS"])
def test_tier_labels_do_not_create_a_delivery_class(tier):
    assert not tier_delivery_gate("WNBA", "UNDER", 0, 0, False, tier, confidence=0)
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