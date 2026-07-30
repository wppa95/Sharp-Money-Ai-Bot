"""
Tests for engine/ud_bet_decision.py

Covers:
  PASS gates:
    - no hit_rates (None)
    - has_real_data=False
    - insufficient sample (< 5 games in any window)
    - hit rate in inconclusive zone (40–60%)
    - contradicting window

  Directional picks:
    - OVER when hit rate >= 60%
    - UNDER when hit rate <= 40%
    - PASS just below B-tier threshold

  Tier classification:
    - S-tier (all windows aligned, at least one ≥8 games)
    - A-tier (primary 62–65%)
    - B-tier (primary 60–62%)

  Confidence:
    - always 0 for PASS
    - within 10–95 for picks
    - higher rate → higher confidence

  Window evidence:
    - fields populated from hit_rates
    - all None when hit_rates is None

  Display helpers:
    - recommendation_emoji
    - tier_display
    - confidence_display
    - window_display
    - avg_vs_line_display

  Serialisation:
    - to_json() produces valid JSON with all required keys
    - PASS serialises cleanly
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from unittest.mock import MagicMock

from engine.player_results import PlayerHitRates, WindowStats
from engine.ud_bet_decision import (
    UDBetDecision,
    make_ud_bet_decision,
    _MIN_GAMES_PRIMARY,
    _MIN_GAMES_S_TIER,
    _B_RATE,
    _A_RATE,
    _S_RATE,
)


# ── Shared fixtures ───────────────────────────────────────────────────────────

def _score(**kw) -> MagicMock:
    s = MagicMock()
    s.tier                = kw.get("tier",                "B")
    s.consistency         = kw.get("consistency",         8)
    s.historical_activity = kw.get("historical_activity", 12)
    s.n_history           = kw.get("n_history",           10)
    s.stars               = kw.get("stars",               3)
    return s


def _validation(**kw) -> MagicMock:
    v = MagicMock()
    v.has_supporting_data = kw.get("has_supporting_data", True)
    v.avg_line            = kw.get("avg_line",            26.0)
    v.min_line_seen       = kw.get("min_line_seen",       24.0)
    v.l5_rate             = kw.get("l5_rate",             0.6)
    v.l10_rate            = kw.get("l10_rate",            0.5)
    v.l20_rate            = kw.get("l20_rate",            0.4)
    v.l30_rate            = kw.get("l30_rate",            0.3)
    v.rate_at_or_below    = kw.get("rate_at_or_below",    0.2)
    return v


def _make_window(games: int, hit_rate: float, avg: float = 3.0) -> WindowStats:
    oc = round(games * hit_rate)
    uc = games - oc
    return WindowStats(games=games, over_count=oc, under_count=uc,
                       hit_rate=hit_rate, average=avg)


def _hit_rates(
    *,
    has_real_data: bool = True,
    total_games: int = 20,
    l5_n: int = 5,          l5_r: float | None = None,
    l10_n: int = 10,        l10_r: float | None = None,
    l20_n: int | None = None, l20_r: float | None = None,
    l30_n: int | None = None, l30_r: float | None = None,
    season_n: int | None = None, season_r: float | None = None,
    h2h_n: int | None = None,   h2h_r: float | None = None,
    current_line: float = 25.5,
) -> PlayerHitRates:
    def _w(n, r):
        return _make_window(n, r) if n is not None and r is not None else None

    return PlayerHitRates(
        player_name   = "Test Player",
        stat_type     = "points",
        current_line  = current_line,
        l5            = _w(l5_n, l5_r),
        l10           = _w(l10_n, l10_r),
        l20           = _w(l20_n, l20_r),
        l30           = _w(l30_n, l30_r),
        season        = _w(season_n, season_r),
        h2h           = _w(h2h_n, h2h_r),
        has_real_data = has_real_data,
        total_games   = total_games,
    )


# ── PASS gate tests ───────────────────────────────────────────────────────────

class TestPassGates:
    def test_pass_when_hit_rates_none(self):
        d = make_ud_bet_decision(_score(), _validation(), 25.5)
        assert d.recommendation == "PASS"
        assert d.decision_tier  == "PASS"
        assert d.confidence     == 0

    def test_pass_reason_mentions_no_history(self):
        d = make_ud_bet_decision(_score(), _validation(), 25.5)
        assert d.reason  # non-empty
        low = d.reason.lower()
        assert "no" in low or "market" in low

    def test_pass_when_no_real_data(self):
        hr = _hit_rates(has_real_data=False, l5_r=None, l10_r=None)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"

    def test_pass_when_only_4_games(self):
        """Window with 4 games < _MIN_GAMES_PRIMARY — should always PASS."""
        hr = PlayerHitRates(
            player_name="P", stat_type="pts", current_line=25.5,
            l5=WindowStats(4, 4, 0, 1.0, 30.0),
            l10=None, l20=None, l30=None, season=None, h2h=None,
            has_real_data=True, total_games=4,
        )
        d = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"
        low = d.reason.lower()
        assert "insufficient" in low or "need" in low or "sample" in low

    def test_pass_when_hit_rate_exactly_50_pct(self):
        hr = _hit_rates(l5_r=0.50, l10_r=0.50)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"

    def test_pass_when_hit_rate_55_pct(self):
        """55% is in the inconclusive zone (below _B_RATE 60%)."""
        hr = _hit_rates(l5_r=0.55, l10_r=0.55)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"

    def test_pass_when_l5_contradicts_l10(self):
        """L5=80% but L10=35% → contradicting windows → PASS."""
        hr = _hit_rates(l5_r=0.80, l10_r=0.35)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"
        assert "conflict" in d.reason.lower()

    def test_pass_when_under_pick_has_high_contradicting_window(self):
        """L5=20% but L10=65% → contradicting windows for UNDER → PASS."""
        hr = _hit_rates(l5_r=0.20, l10_r=0.65)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"


# ── OVER pick tests ───────────────────────────────────────────────────────────

class TestOverPicks:
    def test_over_when_l5_at_80_pct(self):
        hr = _hit_rates(l5_r=0.80, l10_r=0.70)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"

    def test_over_at_exactly_b_tier_threshold(self):
        # 3/5 = 60.0% = _B_RATE exactly
        hr = _hit_rates(l5_r=0.60, l10_r=0.60)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "B"

    def test_over_l10_primary_when_l5_unavailable(self):
        """If L5 has no data, L10 becomes primary."""
        hr = _hit_rates(l5_r=None, l10_r=0.70)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"

    def test_just_below_b_tier_is_pass(self):
        """59% < 60% → PASS."""
        hr = _hit_rates(l5_r=0.59, l10_r=0.59)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"


# ── UNDER pick tests ──────────────────────────────────────────────────────────

class TestUnderPicks:
    def test_under_when_l5_at_20_pct(self):
        hr = _hit_rates(l5_r=0.20, l10_r=0.30)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "UNDER"

    def test_under_at_exactly_b_tier_threshold(self):
        # 2/5 = 40.0% → 1 - 0.40 = _B_RATE exactly
        hr = _hit_rates(l5_r=0.40, l10_r=0.40)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "UNDER"
        assert d.decision_tier  == "B"

    def test_under_confidence_positive(self):
        hr = _hit_rates(l5_r=0.20, l10_r=0.25)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "UNDER"
        assert d.confidence > 0

    def test_just_above_under_threshold_is_pass(self):
        """41% > 40% → PASS."""
        hr = _hit_rates(l5_r=0.41, l10_r=0.41)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "PASS"


# ── Tier classification ───────────────────────────────────────────────────────

class TestTierClassification:
    def test_s_tier_over_all_windows_aligned(self):
        """
        S-tier requires:
          - primary rate >= 0.65
          - at least one window with >= 8 games AND rate >= 0.65  (L10 satisfies this)
          - all windows >= 0.55
          - season >= 0.60
        """
        hr = _hit_rates(
            l5_r     = 0.80,
            l10_r    = 0.70,
            l20_n    = 20, l20_r    = 0.68,
            season_n = 50, season_r = 0.65,
        )
        d = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "S"

    def test_s_tier_under_all_windows_aligned(self):
        hr = _hit_rates(
            l5_r     = 0.20,
            l10_r    = 0.30,
            l20_n    = 20, l20_r    = 0.32,
            season_n = 50, season_r = 0.35,
        )
        d = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "UNDER"
        assert d.decision_tier  == "S"

    def test_not_s_tier_when_no_large_window(self):
        """S requires at least one window with ≥8 games.  Only L5 (5 games) → A."""
        hr = _hit_rates(l5_r=0.80, l10_r=None)   # L10 absent
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "A"

    def test_not_s_tier_when_season_too_low(self):
        """Season at 55% < 60% requirement → A-tier."""
        hr = _hit_rates(
            l5_r=0.80, l10_r=0.70, l20_n=20, l20_r=0.70,
            season_n=50, season_r=0.55,       # below 0.60 threshold
        )
        d = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "A"

    def test_a_tier_primary_in_a_range(self):
        """L5 at 63% (A-range 62–65%)."""
        hr = _hit_rates(l5_r=0.63, l10_r=0.63)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "A"

    def test_b_tier_primary_in_b_range(self):
        """L5 at 61% (B-range 60–62%)."""
        hr = _hit_rates(l5_r=0.61, l10_r=0.61)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "B"

    def test_pass_tier_when_no_data(self):
        d = make_ud_bet_decision(_score(), _validation(), 25.5)
        assert d.decision_tier == "PASS"


# ── Confidence ────────────────────────────────────────────────────────────────

class TestConfidence:
    def test_confidence_zero_for_pass(self):
        d = make_ud_bet_decision(_score(), _validation(), 25.5)
        assert d.confidence == 0

    def test_confidence_within_bounds(self):
        hr = _hit_rates(l5_r=0.80, l10_r=0.70)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        if d.recommendation != "PASS":
            assert 10 <= d.confidence <= 95

    def test_higher_hit_rate_higher_confidence(self):
        hr_high = _hit_rates(l5_r=0.90, l10_r=0.85)
        hr_low  = _hit_rates(l5_r=0.61, l10_r=0.60)
        d_high  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr_high)
        d_low   = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr_low)
        if d_high.recommendation != "PASS" and d_low.recommendation != "PASS":
            assert d_high.confidence > d_low.confidence

    def test_s_tier_higher_confidence_than_b_tier(self):
        hr_s = _hit_rates(l5_r=0.80, l10_r=0.70, l20_n=20, l20_r=0.68, season_n=50, season_r=0.65)
        hr_b = _hit_rates(l5_r=0.61, l10_r=0.61)
        d_s  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr_s)
        d_b  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr_b)
        if d_s.decision_tier == "S" and d_b.decision_tier == "B":
            assert d_s.confidence > d_b.confidence

    def test_pass_via_make_pass_has_zero_confidence(self):
        d = UDBetDecision.make_pass("test reason")
        assert d.confidence == 0
        assert d.recommendation == "PASS"


# ── Window evidence fields ────────────────────────────────────────────────────

class TestWindowFields:
    def test_window_fields_from_hit_rates(self):
        hr = _hit_rates(l5_r=0.80, l10_r=0.70)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.l5_games    == 5
        assert d.l5_over     == round(5 * 0.80)
        assert d.l10_games   == 10
        assert d.l10_over    == round(10 * 0.70)

    def test_season_fields_populated(self):
        hr = _hit_rates(l5_r=0.70, l10_r=0.70, season_n=50, season_r=0.68)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.season_games == 50
        assert d.season_hit_rate is not None

    def test_h2h_fields_none_when_no_h2h(self):
        hr = _hit_rates(l5_r=0.70, l10_r=0.70)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.h2h_games    is None
        assert d.h2h_hit_rate is None

    def test_h2h_fields_populated_when_available(self):
        hr = _hit_rates(l5_r=0.70, l10_r=0.70, h2h_n=5, h2h_r=0.80)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d.h2h_games == 5
        assert d.h2h_over  == round(5 * 0.80)

    def test_all_window_fields_none_when_no_hit_rates(self):
        d = UDBetDecision.make_pass("no data")
        assert d.l5_games   is None
        assert d.l10_games  is None
        assert d.l20_games  is None
        assert d.season_games is None
        assert d.h2h_games  is None

    def test_market_supplement_fields_in_pass(self):
        d = UDBetDecision.make_pass("test", avg_vs_line_pct=0.15, at_historical_low=True)
        assert d.avg_vs_line_pct   == 0.15
        assert d.at_historical_low is True


# ── Display helpers ───────────────────────────────────────────────────────────

class TestDisplayHelpers:
    def test_emoji_over(self):
        hr = _hit_rates(l5_r=0.80)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        if d.recommendation == "OVER":
            assert d.recommendation_emoji() == "🟢"

    def test_emoji_under(self):
        hr = _hit_rates(l5_r=0.20)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        if d.recommendation == "UNDER":
            assert d.recommendation_emoji() == "🔴"

    def test_emoji_pass(self):
        d = UDBetDecision.make_pass("test")
        assert d.recommendation_emoji() == "⚪"

    def test_tier_display_s(self):
        d = UDBetDecision.make_pick(
            "OVER", "S", 85, "test",
            _hit_rates(l5_r=0.80, l10_r=0.70),
        )
        assert "S" in d.tier_display()

    def test_tier_display_a(self):
        d = UDBetDecision.make_pick(
            "OVER", "A", 70, "test",
            _hit_rates(l5_r=0.63, l10_r=0.63),
        )
        assert "A" in d.tier_display()

    def test_tier_display_pass(self):
        d = UDBetDecision.make_pass("test")
        assert d.tier_display() == "—"

    def test_confidence_display_pass(self):
        d = UDBetDecision.make_pass("test")
        assert d.confidence_display() == "—"

    def test_confidence_display_pick_format(self):
        hr = _hit_rates(l5_r=0.80)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        if d.recommendation != "PASS":
            assert "/100" in d.confidence_display()

    def test_window_display_with_data(self):
        d = UDBetDecision.make_pass("test")
        result = d.window_display(5, 4, 1, 0.8, 3.0)
        assert "4/5" in result
        assert "80%" in result
        assert "3.0" in result

    def test_window_display_no_games(self):
        d = UDBetDecision.make_pass("test")
        assert d.window_display(None, None, None, None, None) == "N/A"

    def test_avg_vs_line_display_positive(self):
        d = UDBetDecision.make_pass("test", avg_vs_line_pct=0.15)
        result = d.avg_vs_line_display()
        assert "+" in result
        assert "15" in result  # "+15.0%" or "+15%" both acceptable

    def test_avg_vs_line_display_negative(self):
        d = UDBetDecision.make_pass("test", avg_vs_line_pct=-0.10)
        result = d.avg_vs_line_display()
        assert "-" in result or "10" in result

    def test_avg_vs_line_display_none(self):
        d = UDBetDecision.make_pass("test", avg_vs_line_pct=None)
        assert d.avg_vs_line_display() == "N/A"

    def test_avg_vs_line_display_historical_low(self):
        d = UDBetDecision.make_pass("test", avg_vs_line_pct=0.10, at_historical_low=True)
        assert "low" in d.avg_vs_line_display().lower()


# ── Reason strings ────────────────────────────────────────────────────────────

class TestReasonStrings:
    def test_reason_non_empty_for_all_decisions(self):
        # PASS
        d = make_ud_bet_decision(_score(), _validation(), 25.5)
        assert d.reason

        # OVER
        hr = _hit_rates(l5_r=0.80, l10_r=0.70)
        d2 = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        assert d2.reason

    def test_reason_includes_hit_rate_info(self):
        hr = _hit_rates(l5_r=0.80, l10_r=0.70)
        d  = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        if d.recommendation == "OVER":
            assert "%" in d.reason or "/" in d.reason


# ── Serialisation ─────────────────────────────────────────────────────────────

class TestSerialization:
    _REQUIRED_KEYS = {"rec", "tier", "conf", "l5", "l10", "l20", "l30", "sea", "h2h", "mkt"}

    def test_to_json_valid_json(self):
        d    = UDBetDecision.make_pass("test")
        blob = json.loads(d.to_json())
        assert isinstance(blob, dict)

    def test_to_json_has_all_required_keys(self):
        d    = UDBetDecision.make_pass("test")
        blob = json.loads(d.to_json())
        for key in self._REQUIRED_KEYS:
            assert key in blob, f"Missing key: {key}"

    def test_to_json_pass_values(self):
        d    = UDBetDecision.make_pass("no data")
        blob = json.loads(d.to_json())
        assert blob["rec"]  == "PASS"
        assert blob["tier"] == "PASS"
        assert blob["conf"] == 0

    def test_to_json_pick_values(self):
        hr   = _hit_rates(l5_r=0.80, l10_r=0.70)
        d    = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        blob = json.loads(d.to_json())
        assert blob["rec"] in ("OVER", "UNDER", "PASS")
        assert isinstance(blob["conf"], int)

    def test_to_json_l5_sub_dict(self):
        hr   = _hit_rates(l5_r=0.80, l10_r=0.70)
        d    = make_ud_bet_decision(_score(), _validation(), 25.5, hit_rates=hr)
        blob = json.loads(d.to_json())
        l5   = blob["l5"]
        assert "g" in l5
        assert "o" in l5
        assert "r" in l5

    def test_to_json_roundtrip_stable(self):
        d     = UDBetDecision.make_pass("test", avg_vs_line_pct=0.12)
        blob1 = json.loads(d.to_json())
        blob2 = json.loads(d.to_json())
        assert blob1 == blob2


# ── Constructors ──────────────────────────────────────────────────────────────

class TestConstructors:
    def test_make_pass_sets_recommendation(self):
        d = UDBetDecision.make_pass("reason")
        assert d.recommendation == "PASS"
        assert d.decision_tier  == "PASS"
        assert d.confidence     == 0
        assert d.reason         == "reason"

    def test_make_pass_with_no_hit_rates(self):
        d = UDBetDecision.make_pass("test", hit_rates=None)
        assert d.l5_games is None

    def test_make_pass_inherits_window_data(self):
        hr = _hit_rates(l5_r=0.80, l10_r=0.70)
        d  = UDBetDecision.make_pass("test", hit_rates=hr)
        # Window data still populated even though it's a PASS
        assert d.l5_games    == 5
        assert d.l10_games   == 10

    def test_make_pick_over(self):
        hr = _hit_rates(l5_r=0.80)
        d  = UDBetDecision.make_pick("OVER", "A", 75, "test reason", hr)
        assert d.recommendation == "OVER"
        assert d.decision_tier  == "A"
        assert d.confidence     == 75
        assert d.reason         == "test reason"
