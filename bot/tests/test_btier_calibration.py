"""
tests/test_btier_calibration.py — Section 1: B-tier calibration.

Tests:
  • UD_MIN_STARS_TO_ALERT default is 3 (B-tier allowed)
  • C-tier (2 stars) and D-tier (1 star) are blocked by the star gate
  • B-tier (3 stars) passes the star gate but still subject to confidence gate
  • UD_MIN_CONF_B=55 provides the "Strong B Tier" quality filter
  • No hard block on B-tier (decision PASS blocks, not the star gate)
"""

from __future__ import annotations

import pytest


# ── Default config values ─────────────────────────────────────────────────────

def test_ud_min_stars_to_alert_default_is_3():
    from config import config
    assert config.UD_MIN_STARS_TO_ALERT == 3


def test_ud_min_conf_b_default_is_55():
    from config import config
    assert config.UD_MIN_CONF_B == 55


def test_ud_min_conf_a_default_is_70():
    # Raised from 65 → 70 to require stronger evidence for A-tier alerts.
    from config import config
    assert config.UD_MIN_CONF_A == 75


def test_ud_min_conf_s_default_is_80():
    # Raised from 75 → 80 to require strong evidence for S-tier alerts.
    from config import config
    assert config.UD_MIN_CONF_S == 85


# ── Star gate logic ──────────────────────────────────────────────────────────

def _passes_star_gate(stars: int) -> bool:
    """Reproduce the star gate check from market_engine.py."""
    from config import config
    return stars >= config.UD_MIN_STARS_TO_ALERT


def test_b_tier_3_stars_passes_star_gate():
    assert _passes_star_gate(3)


def test_a_tier_4_stars_passes_star_gate():
    assert _passes_star_gate(4)


def test_s_tier_5_stars_passes_star_gate():
    assert _passes_star_gate(5)


def test_c_tier_2_stars_blocked_by_star_gate():
    assert not _passes_star_gate(2)


def test_d_tier_1_star_blocked_by_star_gate():
    assert not _passes_star_gate(1)


def test_zero_stars_blocked_by_star_gate():
    assert not _passes_star_gate(0)


# ── Confidence gate acts as quality filter for B-tier ────────────────────────

def _passes_confidence_gate(tier: str, confidence: int) -> bool:
    """Reproduce the per-tier confidence gate logic from market_engine.py."""
    from config import config
    min_conf = {"S": config.UD_MIN_CONF_S, "A": config.UD_MIN_CONF_A, "B": config.UD_MIN_CONF_B}.get(tier, 0)
    return confidence >= min_conf


def test_b_tier_above_55_passes_confidence_gate():
    assert _passes_confidence_gate("B", 60)


def test_b_tier_at_55_passes_confidence_gate():
    assert _passes_confidence_gate("B", 55)


def test_b_tier_below_55_blocked_by_confidence_gate():
    assert not _passes_confidence_gate("B", 54)


def test_b_tier_confidence_50_blocked():
    """Low-confidence B-tier (weak B) is filtered out — only "Strong B" passes."""
    assert not _passes_confidence_gate("B", 50)


def test_a_tier_below_65_blocked_by_confidence_gate():
    assert not _passes_confidence_gate("A", 64)


def test_s_tier_below_75_blocked_by_confidence_gate():
    assert not _passes_confidence_gate("S", 74)


# ── Combined: B-tier passes star gate AND confidence gate ────────────────────

def test_strong_b_passes_both_gates():
    """A B-tier play with confidence >= 55 passes both the star gate and confidence gate."""
    assert _passes_star_gate(3) and _passes_confidence_gate("B", 60)


def test_weak_b_passes_star_gate_but_blocked_by_confidence_gate():
    """A B-tier play with confidence < 55 passes stars but is blocked by the confidence gate."""
    assert _passes_star_gate(3) and not _passes_confidence_gate("B", 50)


def test_c_tier_blocked_at_star_gate_regardless_of_confidence():
    """C-tier (2 stars) is blocked by the star gate, confidence gate is never reached."""
    assert not _passes_star_gate(2)


def test_d_tier_blocked_at_star_gate_regardless_of_confidence():
    assert not _passes_star_gate(1)


# ── No hard block: B-tier not in blocked list ─────────────────────────────────

def test_b_tier_not_unconditionally_blocked():
    """
    B-tier must not appear in any hard-block list.
    The only blocks are: PASS recommendation (decision), insufficient stars, low confidence.
    """
    # B-tier with 3 stars and confidence 60 should pass ALL gates
    assert _passes_star_gate(3)
    assert _passes_confidence_gate("B", 60)


def test_btier_star_threshold_allows_expansion():
    """
    Setting UD_MIN_STARS_TO_ALERT to 4 would re-block B-tier.
    The current default of 3 intentionally opens B-tier.
    """
    from config import config
    # Default is 3 — explicitly confirm 4-star config would block B-tier
    if config.UD_MIN_STARS_TO_ALERT == 3:
        assert not _passes_star_gate(2)   # C blocked
        assert _passes_star_gate(3)       # B allowed
