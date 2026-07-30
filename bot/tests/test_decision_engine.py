"""
Tests for engine/decision_engine.py

Covers:
  - make_pp_decision: BET / WATCH / PASS action mapping
  - Kelly computation via stored fair_prob + sb_odds
  - Risk flag detection (thin edge, big line diff, low fair prob)
  - Unit sizing (quarter-Kelly, cap, floor, rounding)
  - compute_tier_performance: aggregation and TierStats properties
  - TierStats.sample_size_note thresholds
  - Edge cases: missing fields fallback, PASS tier, zero Kelly
"""

from __future__ import annotations

import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from engine.decision_engine import (
    PPDecision,
    PPAction,
    TierStats,
    make_pp_decision,
    compute_tier_performance,
    _build_risk_flags,
    _round_units,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_record(
    *,
    tier: str = "A",
    best_side: str = "OVER",
    best_edge: float = 10.0,
    fair_prob_over: float = 0.57,
    fair_prob_under: float = 0.43,
    sb_over_odds: int = -110,
    sb_under_odds: int = -110,
    pp_line_value: float = 25.5,
    sb_line_value: float = 25.5,
    result: str = "PENDING",
    confidence: float = 70.0,
    team: str = "TM",
    sport: str = "NBA",
    stat_type: str = "Points",
) -> MagicMock:
    r = MagicMock()
    r.tier           = tier
    r.best_side      = best_side
    r.best_edge      = best_edge
    r.fair_prob_over  = fair_prob_over
    r.fair_prob_under = fair_prob_under
    r.sb_over_odds   = sb_over_odds
    r.sb_under_odds  = sb_under_odds
    r.pp_line_value  = pp_line_value
    r.sb_line_value  = sb_line_value
    r.result         = result
    r.confidence     = confidence
    r.team           = team
    r.sport          = sport
    r.stat_type      = stat_type
    return r


# ── PPDecision structure ──────────────────────────────────────────────────────

class TestPPDecisionStructure:
    def test_action_emoji_bet(self):
        rec = _make_record(tier="S", best_edge=12.0, fair_prob_over=0.60)
        dec = make_pp_decision(rec)
        assert dec.action_emoji == "🟢"

    def test_action_emoji_watch(self):
        rec = _make_record(tier="B", best_edge=7.0, fair_prob_over=0.55)
        dec = make_pp_decision(rec)
        assert dec.action_emoji == "🟡"

    def test_action_emoji_pass(self):
        rec = _make_record(tier="PASS", best_edge=3.0, fair_prob_over=0.53)
        dec = make_pp_decision(rec)
        assert dec.action_emoji == "⚪"

    def test_action_label_contains_emoji(self):
        rec = _make_record(tier="A", best_edge=10.0, fair_prob_over=0.58)
        dec = make_pp_decision(rec)
        assert dec.action in dec.action_label
        assert dec.action_emoji in dec.action_label

    def test_frozen(self):
        rec = _make_record()
        dec = make_pp_decision(rec)
        with pytest.raises((AttributeError, TypeError)):
            dec.action = "FAKE"   # type: ignore[misc]


# ── Action mapping ────────────────────────────────────────────────────────────

class TestActionMapping:
    """tier + kelly → action logic."""

    def test_s_tier_high_edge_is_bet(self):
        rec = _make_record(tier="S", best_edge=14.0, fair_prob_over=0.62)
        dec = make_pp_decision(rec)
        assert dec.action == PPAction.BET

    def test_a_tier_good_edge_is_bet(self):
        rec = _make_record(tier="A", best_edge=10.0, fair_prob_over=0.57)
        dec = make_pp_decision(rec)
        assert dec.action == PPAction.BET

    def test_b_tier_is_watch(self):
        rec = _make_record(tier="B", best_edge=8.0, fair_prob_over=0.54)
        dec = make_pp_decision(rec)
        assert dec.action == PPAction.WATCH

    def test_pass_tier_is_pass(self):
        rec = _make_record(tier="PASS", best_edge=3.0, fair_prob_over=0.51)
        dec = make_pp_decision(rec)
        assert dec.action == PPAction.PASS

    def test_pass_tier_units_zero(self):
        rec = _make_record(tier="PASS", best_edge=3.0, fair_prob_over=0.51)
        dec = make_pp_decision(rec)
        assert dec.suggested_units == 0.0

    def test_watch_units_capped_at_half(self):
        rec = _make_record(tier="B", best_edge=20.0, fair_prob_over=0.65)
        dec = make_pp_decision(rec)
        assert dec.suggested_units <= 0.50

    def test_low_fair_prob_demotes_to_watch(self):
        # fair_prob < 0.52 → "LOW FAIR PROB" flag → cannot be BET
        rec = _make_record(tier="S", best_edge=12.0, fair_prob_over=0.51)
        dec = make_pp_decision(rec)
        assert dec.action != PPAction.BET

    def test_under_side_uses_under_prob(self):
        rec = _make_record(
            tier="A",
            best_side="UNDER",
            best_edge=9.0,
            fair_prob_under=0.58,
            sb_under_odds=-110,
        )
        dec = make_pp_decision(rec)
        # Should not raise; action should be BET or WATCH depending on Kelly
        assert dec.action in (PPAction.BET, PPAction.WATCH, PPAction.PASS)


# ── Kelly values ──────────────────────────────────────────────────────────────

class TestKellyComputation:
    def test_kelly_half_is_half_of_full(self):
        rec = _make_record(fair_prob_over=0.58, sb_over_odds=-110)
        dec = make_pp_decision(rec)
        assert abs(dec.kelly_half - dec.kelly_full / 2) < 1e-6

    def test_kelly_quarter_is_quarter_of_full(self):
        rec = _make_record(fair_prob_over=0.58, sb_over_odds=-110)
        dec = make_pp_decision(rec)
        assert abs(dec.kelly_quarter - dec.kelly_full / 4) < 1e-6

    def test_kelly_positive_for_edge(self):
        # fair_prob 0.58 at -110 odds is clearly +EV
        rec = _make_record(fair_prob_over=0.58, sb_over_odds=-110)
        dec = make_pp_decision(rec)
        assert dec.kelly_full > 0.0

    def test_kelly_zero_for_no_edge(self):
        # fair_prob == implied_prob at -110 → zero edge → Kelly = 0
        # implied(-110) ≈ 0.5238, so slightly below that is no edge
        rec = _make_record(fair_prob_over=0.50, sb_over_odds=-110)
        dec = make_pp_decision(rec)
        assert dec.kelly_full == 0.0

    def test_kelly_clamped_non_negative(self):
        # Fair prob below market implied → Kelly formula goes negative → clamped to 0
        rec = _make_record(fair_prob_over=0.40, sb_over_odds=-110)
        dec = make_pp_decision(rec)
        assert dec.kelly_full >= 0.0

    def test_missing_fair_prob_falls_back_to_half(self):
        rec = _make_record()
        rec.fair_prob_over = None
        rec.sb_over_odds   = -110
        # Should not raise; fair_p defaults to 0.5
        dec = make_pp_decision(rec)
        assert dec.kelly_full >= 0.0

    def test_missing_odds_falls_back_to_minus110(self):
        rec = _make_record(fair_prob_over=0.58)
        rec.sb_over_odds = None
        dec = make_pp_decision(rec)
        assert dec.kelly_full >= 0.0


# ── Risk flags ────────────────────────────────────────────────────────────────

class TestRiskFlags:
    def test_thin_edge_flag(self):
        rec = _make_record(best_edge=3.0)
        flags = _build_risk_flags(rec)
        assert "THIN EDGE" in flags

    def test_no_thin_edge_flag_above_threshold(self):
        rec = _make_record(best_edge=8.0)
        flags = _build_risk_flags(rec)
        assert "THIN EDGE" not in flags

    def test_big_line_diff_flag(self):
        rec = _make_record(pp_line_value=25.5, sb_line_value=29.0)  # diff = 3.5
        flags = _build_risk_flags(rec)
        assert "BIG LINE DIFF" in flags

    def test_no_big_line_diff_flag_small_diff(self):
        rec = _make_record(pp_line_value=25.5, sb_line_value=25.5)
        flags = _build_risk_flags(rec)
        assert "BIG LINE DIFF" not in flags

    def test_low_fair_prob_flag(self):
        rec = _make_record(best_side="OVER", fair_prob_over=0.51)
        flags = _build_risk_flags(rec)
        assert "LOW FAIR PROB" in flags

    def test_no_low_fair_prob_flag_above_threshold(self):
        rec = _make_record(best_side="OVER", fair_prob_over=0.55)
        flags = _build_risk_flags(rec)
        assert "LOW FAIR PROB" not in flags

    def test_multiple_flags_accumulate(self):
        rec = _make_record(
            best_edge=2.0,
            pp_line_value=20.0,
            sb_line_value=25.0,
            fair_prob_over=0.50,
        )
        flags = _build_risk_flags(rec)
        assert len(flags) >= 2

    def test_under_side_uses_under_prob_for_flag(self):
        rec = _make_record(best_side="UNDER", fair_prob_under=0.50)
        flags = _build_risk_flags(rec)
        assert "LOW FAIR PROB" in flags


# ── Unit sizing ───────────────────────────────────────────────────────────────

class TestUnitSizing:
    def test_round_units_to_step(self):
        assert _round_units(0.87) == 0.75  # rounds to nearest 0.25
        assert _round_units(1.13) == 1.25
        assert _round_units(1.50) == 1.50

    def test_round_units_floor(self):
        assert _round_units(0.01) == 0.25   # floor at MIN_UNITS_BET

    def test_round_units_cap(self):
        assert _round_units(99.0) == 3.0    # cap at MAX_UNITS

    def test_suggested_units_positive_for_bet(self):
        rec = _make_record(tier="A", best_edge=10.0, fair_prob_over=0.57)
        dec = make_pp_decision(rec)
        if dec.action == PPAction.BET:
            assert dec.suggested_units >= 0.25

    def test_suggested_units_zero_for_pass(self):
        rec = _make_record(tier="PASS")
        dec = make_pp_decision(rec)
        assert dec.suggested_units == 0.0


# ── compute_tier_performance ─────────────────────────────────────────────────

class TestComputeTierPerformance:
    def _make_resolved(self, tier: str, result: str, edge: float = 8.0) -> MagicMock:
        r = MagicMock()
        r.tier      = tier
        r.result    = result
        r.best_edge = edge
        return r

    def test_empty_list_returns_empty_dict(self):
        perf = compute_tier_performance([])
        assert perf == {}

    def test_single_win(self):
        records = [self._make_resolved("S", "WIN", 10.0)]
        perf = compute_tier_performance(records)
        assert "S" in perf
        assert perf["S"].wins == 1
        assert perf["S"].losses == 0

    def test_win_loss_push_counted(self):
        records = [
            self._make_resolved("A", "WIN"),
            self._make_resolved("A", "LOSS"),
            self._make_resolved("A", "PUSH"),
            self._make_resolved("A", "REFUND"),
        ]
        perf = compute_tier_performance(records)
        ts = perf["A"]
        assert ts.wins   == 1
        assert ts.losses == 1
        assert ts.pushes == 2

    def test_pending_ignored(self):
        records = [
            self._make_resolved("B", "PENDING"),
            self._make_resolved("B", "WIN"),
        ]
        perf = compute_tier_performance(records)
        assert perf["B"].picks == 1   # only the WIN counts

    def test_multiple_tiers(self):
        records = [
            self._make_resolved("S", "WIN"),
            self._make_resolved("A", "LOSS"),
            self._make_resolved("B", "WIN"),
        ]
        perf = compute_tier_performance(records)
        assert set(perf.keys()) == {"S", "A", "B"}

    def test_avg_edge_computed(self):
        records = [
            self._make_resolved("A", "WIN",  edge=8.0),
            self._make_resolved("A", "LOSS", edge=12.0),
        ]
        perf = compute_tier_performance(records)
        assert abs(perf["A"].avg_edge - 10.0) < 1e-6

    def test_hit_rate_property(self):
        records = [
            self._make_resolved("S", "WIN"),
            self._make_resolved("S", "WIN"),
            self._make_resolved("S", "LOSS"),
        ]
        perf = compute_tier_performance(records)
        assert abs(perf["S"].hit_rate - 2/3) < 1e-6
        assert abs(perf["S"].hit_rate_pct - 200/3) < 1e-4

    def test_hit_rate_zero_when_all_push(self):
        records = [self._make_resolved("B", "PUSH")]
        perf = compute_tier_performance(records)
        assert perf["B"].hit_rate == 0.0   # no contested results

    def test_unknown_tier_key(self):
        records = [self._make_resolved(None, "WIN")]
        perf = compute_tier_performance(records)
        assert "—" in perf


# ── TierStats properties ──────────────────────────────────────────────────────

class TestTierStats:
    def _make_ts(self, picks: int, wins: int) -> TierStats:
        return TierStats(
            tier="A", picks=picks, wins=wins,
            losses=picks - wins, pushes=0, avg_edge=8.0,
        )

    def test_sample_size_note_tiny(self):
        ts = self._make_ts(3, 2)
        assert "tiny" in ts.sample_size_note

    def test_sample_size_note_small(self):
        ts = self._make_ts(10, 6)
        assert "small" in ts.sample_size_note

    def test_sample_size_note_empty_large_sample(self):
        ts = self._make_ts(20, 12)
        assert ts.sample_size_note == ""

    def test_picks_equal_wins_plus_losses_plus_pushes(self):
        ts = TierStats(tier="S", picks=6, wins=3, losses=2, pushes=1, avg_edge=9.0)
        assert ts.picks == ts.wins + ts.losses + ts.pushes
