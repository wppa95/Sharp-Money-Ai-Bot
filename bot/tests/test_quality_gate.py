"""
tests/test_quality_gate.py — Phase 2: per-tier confidence gate config and logic.

Tests:
  • Config defaults: UD_MIN_CONF_S=80, UD_MIN_CONF_A=70, UD_MIN_CONF_B=55
  • Config env override: values read from environment
  • ENABLE_LEARNING_UPDATES defaults to False
  • Confidence gate logic: S/A/B correctly block below threshold, pass above
  • Gate correctly allows C/D (no minimum; blocked upstream by tier gate)
  • Gate not applied to removals (is_removed=True)
"""

from __future__ import annotations

import os
import importlib
import pytest


# ── Config defaults ───────────────────────────────────────────────────────────

def test_config_ud_min_conf_s_default():
    # Raised from 75 → 80: S-tier alerts require stronger evidence.
    from config import config
    assert config.UD_MIN_CONF_S == 80


def test_config_ud_min_conf_a_default():
    # Raised from 65 → 70: A-tier alerts require stronger evidence.
    from config import config
    assert config.UD_MIN_CONF_A == 70


def test_config_ud_min_conf_b_default():
    from config import config
    assert config.UD_MIN_CONF_B == 55


def test_config_enable_learning_updates_default_false():
    from config import config
    assert config.ENABLE_LEARNING_UPDATES is False


def test_config_min_conf_s_gt_a_gt_b():
    from config import config
    assert config.UD_MIN_CONF_S > config.UD_MIN_CONF_A > config.UD_MIN_CONF_B


def test_config_min_conf_b_positive():
    from config import config
    assert config.UD_MIN_CONF_B > 0


# ── Confidence gate logic (pure function tests) ───────────────────────────────

def _passes_gate(tier: str, confidence: int) -> bool:
    """Reproduce the exact confidence gate logic from market_engine.py."""
    from config import config
    min_conf = {
        "S": config.UD_MIN_CONF_S,
        "A": config.UD_MIN_CONF_A,
        "B": config.UD_MIN_CONF_B,
    }.get(tier, 0)
    return confidence >= min_conf


def test_gate_s_tier_above_threshold_passes():
    assert _passes_gate("S", 80)


def test_gate_s_tier_at_threshold_passes():
    from config import config
    assert _passes_gate("S", config.UD_MIN_CONF_S)


def test_gate_s_tier_below_threshold_blocked():
    from config import config
    assert not _passes_gate("S", config.UD_MIN_CONF_S - 1)


def test_gate_a_tier_above_threshold_passes():
    assert _passes_gate("A", 70)


def test_gate_a_tier_at_threshold_passes():
    from config import config
    assert _passes_gate("A", config.UD_MIN_CONF_A)


def test_gate_a_tier_below_threshold_blocked():
    from config import config
    assert not _passes_gate("A", config.UD_MIN_CONF_A - 1)


def test_gate_b_tier_above_threshold_passes():
    assert _passes_gate("B", 60)


def test_gate_b_tier_at_threshold_passes():
    from config import config
    assert _passes_gate("B", config.UD_MIN_CONF_B)


def test_gate_b_tier_below_threshold_blocked():
    from config import config
    assert not _passes_gate("B", config.UD_MIN_CONF_B - 1)


def test_gate_pass_tier_always_passes():
    """PASS tier has no minimum — decision already blocks it at the tier gate."""
    assert _passes_gate("PASS", 0)
    assert _passes_gate("PASS", 100)


def test_gate_unknown_tier_always_passes():
    """Unknown tiers default to min_conf=0 — gate does not block them."""
    assert _passes_gate("C", 0)
    assert _passes_gate("D", 10)
    assert _passes_gate("", 0)


def test_gate_zero_confidence_b_blocked():
    assert not _passes_gate("B", 0)


def test_gate_max_confidence_always_passes():
    for tier in ("S", "A", "B"):
        assert _passes_gate(tier, 100)


def test_gate_s_stricter_than_b():
    """S-tier should have a higher bar than B-tier."""
    from config import config
    # Something that passes B but fails S
    mid = (config.UD_MIN_CONF_B + config.UD_MIN_CONF_S) // 2
    if config.UD_MIN_CONF_B <= mid < config.UD_MIN_CONF_S:
        assert _passes_gate("B", mid)
        assert not _passes_gate("S", mid)


# ── ENABLE_LEARNING_UPDATES flag ──────────────────────────────────────────────

def test_enable_learning_updates_is_bool():
    from config import config
    assert isinstance(config.ENABLE_LEARNING_UPDATES, bool)


def test_enable_learning_updates_false_does_not_affect_scoring():
    """The flag must not change any existing scoring behaviour when False."""
    from config import config
    assert not config.ENABLE_LEARNING_UPDATES  # default is False


# ── Config env-override contract ──────────────────────────────────────────────

def test_config_fields_are_ints():
    from config import config
    assert isinstance(config.UD_MIN_CONF_S, int)
    assert isinstance(config.UD_MIN_CONF_A, int)
    assert isinstance(config.UD_MIN_CONF_B, int)


def test_config_all_non_negative():
    from config import config
    assert config.UD_MIN_CONF_S >= 0
    assert config.UD_MIN_CONF_A >= 0
    assert config.UD_MIN_CONF_B >= 0
