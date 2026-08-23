"""Extended v3.6 coverage for the shared monitoring and delivery gate."""

from __future__ import annotations

import inspect

import pytest

import market_engine
from market_engine import _tier_delivery_gate


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA", "WNBA", "CS2", "TENNIS"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_every_scan_path_accepts_universal_directions(sport, direction):
    assert _tier_delivery_gate(sport, direction, 80, 85, True, "S") is True


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA", "WNBA", "CS2"])
def test_mq_hard_gate_is_consistent_across_sports(sport):
    assert _tier_delivery_gate(sport, "OVER", 80, 84, True, "S") is False
    assert _tier_delivery_gate(sport, "OVER", 80, 85, True, "S") is True


@pytest.mark.parametrize("tier", ["A", "B", "C"])
def test_lower_tiers_are_not_reclassified_as_watchlist(tier):
    """A failed delivery gate must not imply a separate Watchlist class."""
    assert _tier_delivery_gate("WNBA", "UNDER", 80, 85, True, tier) is False


def test_invalid_recommendations_never_deliver():
    assert _tier_delivery_gate("SOCCER", "PASS", 100, 100, True, "S") is False


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


def test_cycle_metrics_record_persistence_writes():
    timing = market_engine._CycleTiming(7)
    timing.database("write", "snapshot", 0)
    timing.database("write", "mq_score_tracking", 0)
    diagnostics = timing.summary(1, 1)["diagnostics"]["counts"]
    assert diagnostics["persistence_snapshot_writes"] == 1
    assert diagnostics["persistence_grading_tracking_writes"] == 1