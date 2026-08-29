"""Regression tests for the canonical direction-and-evidence delivery gate."""

from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


@pytest.mark.parametrize("sport", ["WNBA", "NHL", "SOCCER", "CS", "TENNIS", "MLB", "NFL"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_direction_requires_evidence(sport, direction):
    assert not me._tier_delivery_gate(sport, direction, 0, 0, False, "PASS", confidence=0)
    assert me._tier_delivery_gate(sport, direction, 0, 0, True, "PASS", confidence=0)


def test_direction_is_always_required():
    assert not me._tier_delivery_gate("NHL", "PASS", 100, 100, True, "S", confidence=100)
    assert not me._tier_delivery_gate("NBA", "", 100, 100, True, "S", confidence=100)


def test_all_scan_paths_call_the_canonical_wrapper():
    source = inspect.getsource(me)
    assert source.count("_tier_delivery_gate(") >= 7
    assert "engine.delivery_gates" in source
    assert "_full_pool_rescan_job" in source


def test_legacy_tier_names_do_not_appear_in_delivery_gate_source():
    source = inspect.getsource(me._tier_delivery_gate)
    assert "decision_tier" not in source
    assert "TIER2" not in source