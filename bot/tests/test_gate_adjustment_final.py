"""Regression tests for the canonical direction-only delivery gate."""

from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


def test_tier2_sports_use_exact_canonical_aliases():
    assert me._TIER2_SPORTS == frozenset(
        {"MLB", "NFL", "NBA", "FOOTBALL", "CFB", "NCAA", "NCAAF"}
    )
    assert me._is_tier2_sport("mlb")
    assert me._is_tier2_sport("NFL")
    assert me._is_tier2_sport("NBA")
    assert me._is_tier2_sport("football")
    assert me._is_tier2_sport("CFB")
    assert me._is_tier2_sport("NCAA")
    assert me._is_tier2_sport("NCAAF")
    assert not me._is_tier2_sport("WNBA")
    assert not me._is_tier2_sport("SOCCER")
    assert not me._is_tier2_sport("CFL")
    assert not me._is_tier2_sport("future-football-league")


@pytest.mark.parametrize("sport", ["WNBA", "NHL", "SOCCER", "CS", "TENNIS", "MLB", "NFL"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_every_direction_is_eligible_without_evidence_or_quality(sport, direction):
    assert me._tier_delivery_gate(sport, direction, 0, 0, False, "PASS", confidence=0)


def test_direction_is_always_required():
    assert not me._tier_delivery_gate("NHL", "PASS", 100, 100, True, "S", confidence=100)
    assert not me._tier_delivery_gate("NBA", "", 100, 100, True, "S", confidence=100)


def test_all_scan_paths_call_the_canonical_wrapper():
    source = inspect.getsource(me)
    assert source.count("_tier_delivery_gate(") >= 7
    assert "engine.delivery_gates" in source
    assert "_full_pool_rescan_job" in source