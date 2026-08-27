"""Regressions for restored Tier 1/Tier 2 delivery semantics."""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


def test_tier2_classification_is_not_a_suppression_flag():
    assert me._is_tier2_sport("NBA")
    assert me._is_tier2_sport("MLB")
    assert me._is_tier2_sport("NFL")
    assert not me._is_tier2_sport("NHL")


def test_tier2_gate_does_not_use_sabc_labels():
    # A C-labelled decision can still be delivered when the independent
    # quality/confidence/evidence requirements are met.
    assert me._tier_delivery_gate(
        "NBA", "OVER", 85, 85, True, "C", confidence=90
    )


def test_tier1_gate_does_not_require_sabc_labels_or_quality_floor():
    assert me._tier_delivery_gate(
        "TENNIS", "UNDER", 0, 0, True, "PASS", confidence=0
    )


def test_stable_refresh_is_not_a_second_full_pool_rotation():
    source = Path(__file__).parents[1].joinpath("market_engine.py").read_text()
    stable = source[source.index("async def _stable_refresh_job"):source.index(
        "# FULL-POOL RESCAN ROTATION"
    )]
    fpr = source[source.index("async def _full_pool_rescan_job"):]
    assert "get_stable_refresh_cursor" not in stable
    assert "set_stable_refresh_cursor" not in stable
    assert "get_fpr_cursor" in fpr
    assert "set_fpr_cursor" in fpr


def test_alert_boundary_uses_the_same_shared_gate():
    alerts_source = Path(__file__).parents[1].joinpath("alerts.py").read_text()
    assert "_canonical_tier_delivery_gate" in alerts_source
    assert "_has_strong_underdog_evidence" in alerts_source


def test_temporary_suppression_blocks_are_gone_from_prop_paths():
    source = inspect.getsource(me)
    assert "Tier 2 Telegram block (temporary)" not in source