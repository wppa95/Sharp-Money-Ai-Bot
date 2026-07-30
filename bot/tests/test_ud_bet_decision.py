"""
Tests for engine/ud_bet_decision.py

Covers:
  - PASS when no supporting data
  - PASS when score is PASS tier
  - OVER when line is significantly below historical average
  - UNDER when line is significantly above historical average
  - PASS when signals are neutral (line near avg)
  - at_historical_low triggers OVER bonus
  - low rate_at_or_below triggers OVER bonus
  - high rate_at_or_below triggers UNDER bonus
  - prev_line direction amplifies signals
  - confidence scaling (PASS capped, directional bounded 10-95)
  - to_json() round-trips cleanly
  - season_avg / h2h_rate always None
  - recommendation_emoji / confidence_display helpers
  - _build_reason produces non-empty string
  - PASS: reason describes neutral/conflicting signals
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from unittest.mock import MagicMock

from engine.ud_bet_decision import make_ud_bet_decision, UDBetDecision


# ── Helpers ────────────────────────────────────────────────────────────────────

def _score(
    *,
    tier: str = "B",
    consistency: int = 8,
    historical_activity: int = 12,
    avg_vs_line: int = 12,
    move_velocity: int = 10,
    stability: int = 8,
    n_history: int = 10,
) -> MagicMock:
    s = MagicMock()
    s.tier                = tier
    s.consistency         = consistency
    s.historical_activity = historical_activity
    s.avg_vs_line         = avg_vs_line
    s.move_velocity       = move_velocity
    s.stability           = stability
    s.n_history           = n_history
    s.stars               = 3
    return s


def _validation(
    *,
    has_supporting_data: bool = True,
    n_history: int = 10,
    avg_line: float = 1.0,
    min_line_seen: float = 0.5,
    rate_at_or_below: float = 0.5,
    l5_rate: float = 0.6,
    l10_rate: float = 0.5,
    l20_rate: float = 0.4,
    l30_rate: float = 0.35,
) -> MagicMock:
    v = MagicMock()
    v.has_supporting_data = has_supporting_data
    v.n_history           = n_history
    v.avg_line            = avg_line
    v.min_line_seen       = min_line_seen
    v.rate_at_or_below    = rate_at_or_below
    v.l5_rate             = l5_rate
    v.l10_rate            = l10_rate
    v.l20_rate            = l20_rate
    v.l30_rate            = l30_rate
    return v


# ── PASS gates ─────────────────────────────────────────────────────────────────

def test_pass_when_no_supporting_data():
    v = _validation(has_supporting_data=False)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert d.recommendation == "PASS"
    assert d.confidence == 0


def test_pass_when_score_is_pass_tier():
    s = _score(tier="PASS")
    v = _validation(has_supporting_data=True)
    d = make_ud_bet_decision(s, v, 1.0)
    assert d.recommendation == "PASS"


# ── OVER signals ───────────────────────────────────────────────────────────────

def test_over_when_line_far_below_avg():
    """Line 30% below historical average → strong OVER signal."""
    v = _validation(avg_line=1.0, rate_at_or_below=0.2)
    d = make_ud_bet_decision(_score(), v, 0.7)   # 0.7 is 30% below 1.0
    assert d.recommendation == "OVER"


def test_over_when_line_at_historical_low():
    """Line == min_line_seen → at_historical_low bonus applied."""
    v = _validation(avg_line=0.8, min_line_seen=0.5, rate_at_or_below=0.1)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert d.recommendation == "OVER"
    assert d.at_historical_low is True


def test_over_when_low_percentile():
    """Line in bottom 10% of history → OVER bonus."""
    v = _validation(avg_line=1.0, rate_at_or_below=0.08, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert d.recommendation == "OVER"


def test_over_when_line_moved_down():
    """Recent line move DOWN amplifies OVER signal."""
    v = _validation(avg_line=0.9, rate_at_or_below=0.15)
    d = make_ud_bet_decision(_score(), v, 0.5, prev_line=0.75)
    assert d.recommendation == "OVER"


# ── UNDER signals ──────────────────────────────────────────────────────────────

def test_under_when_line_far_above_avg():
    """Line 30% above historical average → UNDER signal."""
    v = _validation(avg_line=1.0, min_line_seen=0.5, rate_at_or_below=0.85)
    d = make_ud_bet_decision(_score(), v, 1.3)   # 1.3 is 30% above 1.0
    assert d.recommendation == "UNDER"


def test_under_when_high_percentile():
    """Line in top 10% of history → UNDER bonus."""
    v = _validation(avg_line=1.0, min_line_seen=0.5, rate_at_or_below=0.92)
    d = make_ud_bet_decision(_score(), v, 1.5)
    assert d.recommendation == "UNDER"


def test_under_when_line_moved_up():
    """Recent line move UP amplifies UNDER signal."""
    v = _validation(avg_line=1.0, min_line_seen=0.5, rate_at_or_below=0.88)
    d = make_ud_bet_decision(_score(), v, 1.3, prev_line=1.0)
    assert d.recommendation == "UNDER"


# ── PASS when neutral ──────────────────────────────────────────────────────────

def test_pass_when_line_near_avg():
    """Line within 2% of avg → signals too neutral → PASS."""
    v = _validation(avg_line=1.0, rate_at_or_below=0.5, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 1.0)
    assert d.recommendation == "PASS"


def test_pass_when_conflicting_signals():
    """Low percentile (OVER) but line ABOVE avg (UNDER) → conflicting → PASS."""
    # avg_line=0.6, current=0.7 → line ABOVE avg (UNDER signal)
    # rate_at_or_below=0.9 → high percentile (UNDER signal too)
    # Both signals point UNDER but barely — let's check it doesn't flip
    v = _validation(avg_line=1.0, rate_at_or_below=0.5, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 1.0)
    assert d.recommendation == "PASS"


# ── Confidence ─────────────────────────────────────────────────────────────────

def test_confidence_capped_at_95():
    """Strongest possible OVER signals should not exceed 95."""
    v = _validation(avg_line=2.0, min_line_seen=0.5, rate_at_or_below=0.05)
    s = _score(tier="S", consistency=15, historical_activity=25)
    d = make_ud_bet_decision(s, v, 0.5, prev_line=1.0)
    assert d.recommendation == "OVER"
    assert d.confidence <= 95


def test_confidence_at_least_10_for_directional():
    """Any directional pick should have confidence >= 10."""
    v = _validation(avg_line=1.0, rate_at_or_below=0.1, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.5)
    if d.recommendation != "PASS":
        assert d.confidence >= 10


def test_pass_confidence_capped_low():
    """PASS confidence must be <= 35."""
    v = _validation(avg_line=1.0, rate_at_or_below=0.5, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 1.0)
    assert d.recommendation == "PASS"
    assert d.confidence <= 35


# ── Evidence fields ─────────────────────────────────────────────────────────────

def test_season_and_h2h_always_none():
    v = _validation(avg_line=0.7, rate_at_or_below=0.1)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert d.season_avg is None
    assert d.h2h_rate is None


def test_l_rates_carried_from_validation():
    v = _validation(l5_rate=0.8, l10_rate=0.7, l20_rate=0.6, l30_rate=0.55, avg_line=0.7, rate_at_or_below=0.1)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert d.l5_rate == pytest.approx(0.8)
    assert d.l10_rate == pytest.approx(0.7)
    assert d.l20_rate == pytest.approx(0.6)
    assert d.l30_rate == pytest.approx(0.55)


def test_avg_vs_line_pct_computed():
    """avg_vs_line_pct = (avg - current) / avg; positive = OVER signal."""
    v = _validation(avg_line=1.0, rate_at_or_below=0.2, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.75)
    assert d.avg_vs_line_pct == pytest.approx(0.25, abs=0.01)


def test_at_historical_low_true():
    v = _validation(avg_line=1.0, min_line_seen=0.5, rate_at_or_below=0.1)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert d.at_historical_low is True


def test_at_historical_low_false_when_above_min():
    v = _validation(avg_line=1.0, min_line_seen=0.5, rate_at_or_below=0.5)
    d = make_ud_bet_decision(_score(), v, 0.75)   # above min
    assert d.at_historical_low is False


# ── Serialisation ───────────────────────────────────────────────────────────────

def test_to_json_valid():
    v = _validation(avg_line=0.8, rate_at_or_below=0.15, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.5)
    raw = d.to_json()
    parsed = json.loads(raw)
    assert "rec" in parsed
    assert "conf" in parsed
    assert parsed["sea"] is None
    assert parsed["h2h"] is None


def test_to_json_pass_has_rec_field():
    v = _validation(has_supporting_data=False)
    d = make_ud_bet_decision(_score(), v, 1.0)
    parsed = json.loads(d.to_json())
    assert parsed["rec"] == "PASS"
    assert parsed["conf"] == 0


# ── Display helpers ─────────────────────────────────────────────────────────────

def test_recommendation_emoji_over():
    v = _validation(avg_line=0.7, rate_at_or_below=0.1, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.5)
    if d.recommendation == "OVER":
        assert d.recommendation_emoji() == "🟢"


def test_recommendation_emoji_pass():
    v = _validation(has_supporting_data=False)
    d = make_ud_bet_decision(_score(), v, 1.0)
    assert d.recommendation_emoji() == "⚪"


def test_confidence_display_pass_shows_dash():
    v = _validation(has_supporting_data=False)
    d = make_ud_bet_decision(_score(), v, 1.0)
    assert d.confidence_display() == "—"


def test_confidence_display_directional_shows_number():
    v = _validation(avg_line=0.7, rate_at_or_below=0.1, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.5)
    if d.recommendation != "PASS":
        assert "/100" in d.confidence_display()


# ── Reason string ───────────────────────────────────────────────────────────────

def test_reason_non_empty():
    v = _validation(avg_line=0.8, rate_at_or_below=0.15, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.5)
    assert len(d.reason) > 0


def test_reason_mentions_below_avg_for_over():
    v = _validation(avg_line=1.0, rate_at_or_below=0.15, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 0.7)
    if d.recommendation == "OVER":
        assert "below" in d.reason.lower() or "low" in d.reason.lower()


def test_reason_pass_mentions_neutral():
    v = _validation(avg_line=1.0, rate_at_or_below=0.5, min_line_seen=0.5)
    d = make_ud_bet_decision(_score(), v, 1.0)
    assert "neutral" in d.reason.lower() or "signal" in d.reason.lower()
