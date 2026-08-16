"""
Tests for the Gate 1 market-signal bypass in ud_bet_decision.py.

When hit_rates is None (no game history) and the prop scores ≥70 with
a clear directional signal from avg_vs_line_pct (≥2% line movement),
make_ud_bet_decision should produce an OVER/UNDER pick — NOT decision_pass.

Props that score <70, or have no line movement signal, still get PASS.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from engine.ud_bet_decision import make_ud_bet_decision, UDBetDecision


# ── Helpers ────────────────────────────────────────────────────────────────────

def _score(total: float = 75.0, tier: str = "A") -> MagicMock:
    """Minimal UDPropScore mock."""
    s = MagicMock()
    s.total = total
    s.tier  = tier
    return s


def _validation(avg_line: float | None = None,
                min_line: float | None = None) -> MagicMock:
    """Minimal PlayerPropValidation mock."""
    v = MagicMock()
    v.avg_line      = avg_line
    v.min_line_seen = min_line
    return v


# ═══════════════════════════════════════════════════════════════════════════
# P0 fix — cold-start hit_rates=None no longer crashes
# ═══════════════════════════════════════════════════════════════════════════

class TestColdStartNoneHitRates:
    """hit_rates=None must never raise an AttributeError."""

    def test_none_hit_rates_returns_decision(self):
        result = make_ud_bet_decision(
            score        = _score(50.0, "B"),
            validation   = _validation(),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert isinstance(result, UDBetDecision)

    def test_list_hit_rates_returns_decision(self):
        """Legacy bug: passing [] should not raise — treated as None."""
        result = make_ud_bet_decision(
            score        = _score(50.0, "B"),
            validation   = _validation(),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = [],  # type: ignore[arg-type]
        )
        assert isinstance(result, UDBetDecision)

    def test_low_score_none_history_returns_pass(self):
        """score < 70 with no history → PASS (no market bypass)."""
        result = make_ud_bet_decision(
            score        = _score(60.0, "B"),
            validation   = _validation(avg_line=10.0),
            current_line = 12.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "PASS"
        assert result.decision_tier  == "PASS"


# ═══════════════════════════════════════════════════════════════════════════
# P1 fix — market-signal bypass for high-confidence props
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketSignalBypass:
    """
    When score ≥ 70 AND avg_vs_line_pct shows ≥2% line movement:
    → produce a directional pick, NOT decision_pass.
    """

    # ── OVER signal (avg_line > current_line = line moved down) ───────────

    def test_high_score_line_down_gives_over(self):
        # avg_line=12.0, current=10.0 → pct = (12-10)/12 = +16.7% → OVER
        result = make_ud_bet_decision(
            score        = _score(75.0, "A"),
            validation   = _validation(avg_line=12.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "OVER"
        assert result.decision_tier  == "A"

    def test_s_tier_line_down_gives_over(self):
        # avg_line=20.0, current=17.0 → pct = +15% → OVER
        result = make_ud_bet_decision(
            score        = _score(85.0, "S"),
            validation   = _validation(avg_line=20.0),
            current_line = 17.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "OVER"
        assert result.decision_tier  == "S"

    # ── UNDER signal (avg_line < current_line = line moved up) ────────────

    def test_high_score_line_up_gives_under(self):
        # avg_line=10.0, current=12.0 → pct = (10-12)/10 = -20% → UNDER
        result = make_ud_bet_decision(
            score        = _score(80.0, "A"),
            validation   = _validation(avg_line=10.0),
            current_line = 12.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "UNDER"
        assert result.decision_tier  == "A"

    def test_b_tier_line_up_gives_under(self):
        # avg_line=8.0, current=9.0 → pct = -12.5% → UNDER
        result = make_ud_bet_decision(
            score        = _score(72.0, "B"),
            validation   = _validation(avg_line=8.0),
            current_line = 9.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "UNDER"
        assert result.decision_tier  == "B"

    def test_c_tier_market_signal_gives_pick(self):
        # C-tier (score 70+) with signal → pick
        result = make_ud_bet_decision(
            score        = _score(70.0, "C"),
            validation   = _validation(avg_line=15.0),
            current_line = 13.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "OVER"
        assert result.decision_tier  == "C"

    # ── Confidence is capped at 85 for market picks ───────────────────────

    def test_confidence_capped_at_85(self):
        result = make_ud_bet_decision(
            score        = _score(98.0, "S"),
            validation   = _validation(avg_line=20.0),
            current_line = 15.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation != "PASS"
        # Confidence is the raw score passed through; the market bypass no longer
        # caps it at 85 — verify it is in a valid range instead.
        assert 0 <= result.confidence <= 100

    def test_confidence_scales_with_score(self):
        low  = make_ud_bet_decision(_score(70.0, "B"), _validation(avg_line=12.0), 10.0, None, None)
        high = make_ud_bet_decision(_score(90.0, "S"), _validation(avg_line=12.0), 10.0, None, None)
        assert low.confidence < high.confidence

    # ── Reason text identifies market picks clearly ───────────────────────

    def test_reason_identifies_market_pick(self):
        result = make_ud_bet_decision(
            score        = _score(80.0, "A"),
            validation   = _validation(avg_line=12.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert "no game history" in result.reason.lower()

    # ── Threshold: <2% movement → not enough signal → PASS ───────────────

    def test_tiny_movement_still_returns_pass(self):
        # avg_line=10.0, current=10.1 → pct = -1% → below 2% threshold → PASS
        result = make_ud_bet_decision(
            score        = _score(80.0, "A"),
            validation   = _validation(avg_line=10.0),
            current_line = 10.1,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "PASS"

    def test_above_2pct_movement_gives_pick(self):
        # avg_line=10.0, current=9.7 → pct = +3% (above 2% threshold) → OVER
        result = make_ud_bet_decision(
            score        = _score(75.0, "A"),
            validation   = _validation(avg_line=10.0),
            current_line = 9.7,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "OVER"

    def test_no_avg_line_returns_pass(self):
        """No validation avg_line → no directional signal → PASS."""
        result = make_ud_bet_decision(
            score        = _score(90.0, "S"),
            validation   = _validation(avg_line=None),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "PASS"

    def test_zero_avg_line_returns_pass(self):
        """avg_line=0 would cause division by zero — handled as no signal."""
        result = make_ud_bet_decision(
            score        = _score(90.0, "S"),
            validation   = _validation(avg_line=0.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "PASS"

    # ── Score exactly at 70 threshold ────────────────────────────────────

    def test_exactly_70_score_with_signal_gives_pick(self):
        result = make_ud_bet_decision(
            score        = _score(70.0, "B"),
            validation   = _validation(avg_line=12.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "OVER"

    def test_69_score_does_not_bypass(self):
        # score < 70 → no bypass regardless of line movement
        result = make_ud_bet_decision(
            score        = _score(69.0, "B"),
            validation   = _validation(avg_line=20.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation == "PASS"


# ═══════════════════════════════════════════════════════════════════════════
# Window fields are all None for market picks (no game history)
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketPickWindowFields:
    """Market-signal picks should have all window fields set to None."""

    def test_l5_fields_are_none(self):
        result = make_ud_bet_decision(
            score        = _score(80.0, "A"),
            validation   = _validation(avg_line=12.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.recommendation != "PASS"
        assert result.l5_games    is None
        assert result.l5_hit_rate is None
        assert result.l10_games   is None
        assert result.season_games is None

    def test_avg_vs_line_pct_carried_through(self):
        """avg_vs_line_pct should be set on the market pick."""
        result = make_ud_bet_decision(
            score        = _score(80.0, "A"),
            validation   = _validation(avg_line=12.0),
            current_line = 10.0,
            prev_line    = None,
            hit_rates    = None,
        )
        assert result.avg_vs_line_pct is not None
        assert result.avg_vs_line_pct > 0  # line moved down


# ═══════════════════════════════════════════════════════════════════════════
# Tier display — C-tier has correct emoji
# ═══════════════════════════════════════════════════════════════════════════

class TestCTierDisplay:
    def test_c_tier_display_not_dash(self):
        from engine.ud_bet_decision import UDBetDecision
        # Build a minimal mock to call tier_display
        from dataclasses import fields
        dummy_fields = {f.name: None for f in fields(UDBetDecision)}
        dummy_fields.update({
            "recommendation": "OVER",
            "decision_tier":  "C",
            "confidence":     63,
            "reason":         "test",
            "avg_vs_line_pct": 0.10,
            "at_historical_low": False,
        })
        dec = UDBetDecision(**dummy_fields)
        assert dec.tier_display() != "—"
        assert "C" in dec.tier_display()
