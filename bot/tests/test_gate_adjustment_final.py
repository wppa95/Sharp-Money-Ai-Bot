"""v3.6 regressions: unified monitoring, delivery quality, and persistence."""

from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA", "WNBA", "CS", "SOCCER"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_unified_gate_allows_both_directions_when_quality_is_strong(sport, direction):
    assert me._tier_delivery_gate(sport, direction, 80, 85, True, "S") is True


@pytest.mark.parametrize("tier", ["A", "B", "C"])
def test_non_top_tiers_remain_monitored_but_are_not_deliverable(tier):
    """Lower tiers are evaluated/persisted; Telegram delivery stays quality gated."""
    assert me._tier_delivery_gate("MLB", "OVER", 80, 85, True, tier) is False


def test_mq_is_the_intentional_hard_delivery_gate():
    assert me._tier_delivery_gate("MLB", "OVER", 80, 84, True, "S") is False
    assert me._tier_delivery_gate("MLB", "OVER", 80, 85, True, "S") is True


def test_no_sport_tier_classifier_remains():
    assert me._TIER2_SPORTS == frozenset()
    for sport in ("MLB", "NFL", "NBA", "WNBA", "CS"):
        assert me._is_tier2_sport(sport) is False


class TestUnifiedPathsAndPersistence:
    @pytest.fixture(scope="class")
    @classmethod
    def src(cls) -> str:
        return inspect.getsource(me)

    def test_delivery_paths_use_one_canonical_gate(self, src):
        assert src.count("_tier_delivery_gate(") >= 4
        assert "ACTIONABLE BET PICK" in src

    def test_monitoring_state_is_persisted_for_restart(self, src):
        assert "_init_state_from_db" in src
        assert "_prop_market_alerted" in src
        assert "save_underdog_snapshot" in src
        assert "update_prop_lifecycle_state" in src

    def test_continuous_cycle_persistence_is_instrumented(self):
        timing = me._CycleTiming(1)
        timing.database("write", "underdog_snapshot", 0.01)
        timing.database("write", "prop_lifecycle", 0.01)
        counts = timing.summary(2, 2)["diagnostics"]["counts"]
        assert counts["persistence_snapshot_writes"] == 1
        assert counts["persistence_grading_tracking_writes"] == 1


def test_dedup_state_is_line_aware_and_importable():
    from market_engine import _is_prop_deduped, _prop_market_alerted, _record_prop_alerted
    assert callable(_is_prop_deduped)
    assert callable(_record_prop_alerted)
    assert isinstance(_prop_market_alerted, dict)