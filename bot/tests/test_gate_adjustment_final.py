"""Regression tests for the canonical Tier 1/Tier 2 delivery gate."""

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


@pytest.mark.parametrize("sport", ["WNBA", "NHL", "SOCCER", "CS", "TENNIS"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_tier1_needs_direction_and_evidence_only(sport, direction):
    assert me._tier_delivery_gate(sport, direction, 1, 1, True, "C", confidence=1)
    assert not me._tier_delivery_gate(sport, direction, 1, 1, False, "S", confidence=99)


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA", "Football", "CFB", "NCAA", "NCAAF"])
def test_tier2_requires_strong_evidence_confidence_bq_and_mq(sport):
    kwargs = dict(evidence_available=True, score_tier="C", confidence=90)
    assert me._tier_delivery_gate(sport, "OVER", 85, 85, **kwargs)
    assert me._tier_delivery_gate(sport, "UNDER", 85, 85, **kwargs)
    assert not me._tier_delivery_gate(sport, "OVER", 84, 85, **kwargs)
    assert not me._tier_delivery_gate(sport, "OVER", 85, 84, **kwargs)
    assert not me._tier_delivery_gate(sport, "OVER", 85, 85, True, "S", confidence=89)
    assert not me._tier_delivery_gate(sport, "OVER", 85, 85, False, "S", confidence=95)


def test_direction_is_always_required():
    assert not me._tier_delivery_gate("NHL", "PASS", 100, 100, True, "S", confidence=100)
    assert not me._tier_delivery_gate("NBA", "", 100, 100, True, "S", confidence=100)


def test_all_scan_paths_call_the_canonical_wrapper():
    source = inspect.getsource(me)
    assert source.count("_tier_delivery_gate(") >= 7
    assert "engine.delivery_gates" in source
    assert "_full_pool_rescan_job" in source
