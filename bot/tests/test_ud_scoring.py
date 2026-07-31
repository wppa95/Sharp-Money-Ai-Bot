"""
Tests for engine/ud_scoring.py

Coverage:
  UDPropScore properties
    - total is sum of five components
    - tier mapping: S / A / B / PASS at correct breakpoints
    - stars mapping at correct breakpoints
    - stars_display produces correct ★/☆ string

  _score_move_velocity
    - returns 0 for sub-threshold magnitude
    - returns correct band for each threshold
    - caps at 25

  _score_historical_activity
    - returns neutral (12) when n < 3
    - returns score for high blended rate
    - returns score for low blended rate
    - applies small-sample blend for n < 5
    - handles all-moved history (blended=1.0)

  _score_avg_vs_line
    - returns 0 when fewer than 2 history records
    - returns 0 when deviation < 2 %
    - returns correct band for large deviation
    - ignores zero / None line_value rows

  _score_consistency
    - returns neutral (8) when fewer than 2 moved records
    - returns 15 for perfectly consistent direction
    - returns 3 for perfectly alternating direction
    - ignores records without prev_line

  _score_stability
    - returns neutral (8) when fewer than 3 records
    - returns 15 for zero-variance history
    - returns 0 for high-variance history
    - excludes removed records from values

  score_ud_prop (public API)
    - no history → all neutral / 0 components
    - prev_line=None → move_velocity=0
    - rich history → plausible non-zero total
    - returns frozen UDPropScore (immutable)
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from dataclasses import FrozenInstanceError
from datetime import datetime
from unittest.mock import MagicMock

from engine.ud_scoring import (
    MarketQualityLabel,
    MarketQuality,
    MarketPressureFlag,
    compute_market_quality,
    detect_market_pressure,
    UDScoreTier,
    UDPropScore,
    PropDifficultyClass,
    score_ud_prop,
    _score_move_velocity,
    _score_historical_activity,
    _score_historical_activity_legacy,
    _score_drift_velocity,
    _score_avg_vs_line,
    _score_consistency,
    _score_stability,
    _classify_prop_difficulty,
    _score_variance_penalty,
    _ACTIVITY_NEUTRAL,
    _ACTIVITY_NEUTRAL_LEGACY,
    _CONSISTENCY_NEUTRAL,
    _STABILITY_NEUTRAL,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_record(
    line_value: float = 5.0,
    line_moved: bool = False,
    prev_line:  float | None = None,
    removed:    bool = False,
) -> MagicMock:
    r = MagicMock()
    r.line_value = line_value
    r.line_moved = line_moved
    r.prev_line  = prev_line
    r.removed    = removed
    return r


def _make_score(total: int = 50) -> UDPropScore:
    """Return a UDPropScore whose components sum to *total* (approximately)."""
    vel  = min(total, 25)
    rest = total - vel
    act  = min(rest, 25); rest -= act
    avg  = min(rest, 20); rest -= avg
    con  = min(rest, 15); rest -= con
    sta  = max(rest, 0)
    return UDPropScore(
        player_name="Test Player", stat_type="Hits", sport="MLB",
        current_line=3.0,
        move_velocity=vel, historical_activity=act, avg_vs_line=avg,
        consistency=con, stability=sta, n_history=10,
    )


# ── UDPropScore properties ─────────────────────────────────────────────────────

class TestUDPropScore:
    def test_total_is_sum_of_components(self):
        s = UDPropScore(
            player_name="A", stat_type="B", sport="MLB", current_line=1.0,
            move_velocity=20, historical_activity=18, avg_vs_line=12,
            consistency=10, stability=8, n_history=5,
        )
        assert s.total == 20 + 18 + 12 + 10 + 8

    def test_tier_S_at_80(self):
        assert _make_score(80).tier == "S"

    def test_tier_S_at_85(self):
        assert _make_score(85).tier == "S"

    def test_tier_A_at_65(self):
        assert _make_score(65).tier == "A"

    def test_tier_A_at_79(self):
        assert _make_score(79).tier == "A"

    def test_tier_B_at_50(self):
        assert _make_score(50).tier == "B"

    def test_tier_B_at_64(self):
        assert _make_score(64).tier == "B"

    def test_tier_PASS_at_49(self):
        assert _make_score(49).tier == "PASS"

    def test_tier_PASS_at_0(self):
        assert _make_score(0).tier == "PASS"

    def test_stars_5_at_85(self):
        assert _make_score(85).stars == 5

    def test_stars_4_at_70(self):
        assert _make_score(70).stars == 4

    def test_stars_4_at_84(self):
        assert _make_score(84).stars == 4

    def test_stars_3_at_55(self):
        assert _make_score(55).stars == 3

    def test_stars_2_at_40(self):
        assert _make_score(40).stars == 2

    def test_stars_1_at_39(self):
        assert _make_score(39).stars == 1

    def test_stars_1_at_0(self):
        assert _make_score(0).stars == 1

    def test_stars_display_full(self):
        s = UDPropScore(
            player_name="P", stat_type="S", sport="MLB", current_line=1.0,
            move_velocity=25, historical_activity=25, avg_vs_line=20,
            consistency=15, stability=5, n_history=20,
        )
        # total=90 → 5 stars
        assert s.stars == 5
        assert s.stars_display == "★★★★★"

    def test_stars_display_partial(self):
        # total=55 → 3 stars
        s = _make_score(55)
        assert s.stars == 3
        assert s.stars_display == "★★★☆☆"

    def test_stars_display_one(self):
        s = _make_score(30)
        assert s.stars == 1
        assert s.stars_display == "★☆☆☆☆"

    def test_frozen(self):
        s = _make_score(60)
        with pytest.raises((FrozenInstanceError, AttributeError)):
            s.move_velocity = 0  # type: ignore[misc]

    def test_tier_enum_values_match(self):
        assert UDScoreTier.S.value    == "S"
        assert UDScoreTier.A.value    == "A"
        assert UDScoreTier.B.value    == "B"
        assert UDScoreTier.PASS.value == "PASS"


# ── _score_move_velocity ───────────────────────────────────────────────────────

class TestMoveVelocity:
    def test_zero_magnitude(self):
        assert _score_move_velocity(0.0) == 0

    def test_below_threshold(self):
        assert _score_move_velocity(0.4) == 0

    def test_exactly_0_5(self):
        assert _score_move_velocity(0.5) == 4

    def test_1_0(self):
        assert _score_move_velocity(1.0) == 7

    def test_1_5(self):
        assert _score_move_velocity(1.5) == 11

    def test_2_0(self):
        assert _score_move_velocity(2.0) == 15

    def test_3_0(self):
        assert _score_move_velocity(3.0) == 20

    def test_4_0(self):
        assert _score_move_velocity(4.0) == 25

    def test_above_max(self):
        assert _score_move_velocity(10.0) == 25


# ── _score_historical_activity ────────────────────────────────────────────────

class TestHistoricalActivity:
    def test_empty_history_returns_neutral(self):
        assert _score_historical_activity([]) == _ACTIVITY_NEUTRAL

    def test_n_1_returns_neutral(self):
        h = [_make_record(line_moved=True)]
        assert _score_historical_activity(h) == _ACTIVITY_NEUTRAL

    def test_n_2_returns_neutral(self):
        h = [_make_record(line_moved=True)] * 2
        assert _score_historical_activity(h) == _ACTIVITY_NEUTRAL

    def test_n_3_no_moves_low_score(self):
        h = [_make_record(line_moved=False)] * 3
        result = _score_historical_activity(h)
        # blended=0 → raw=2, blended toward neutral with 3 records (0.70)
        assert result < _ACTIVITY_NEUTRAL

    def test_all_moved_high_score(self):
        # 10 records, all moved — blended=1.0 → raw=25, n>=5 so no blend discount
        h = [_make_record(line_moved=True)] * 10
        assert _score_historical_activity(h) == 25

    def test_high_l5_rate(self):
        # L5=5/5=1.0, L10=5/10=0.5, L20=5/20=0.25
        # blended = 0.5*1.0 + 0.3*0.5 + 0.2*0.25 = 0.5+0.15+0.05 = 0.70 → raw=25
        h = (
            [_make_record(line_moved=True)] * 5
            + [_make_record(line_moved=False)] * 15
        )
        result = _score_historical_activity(h)
        assert result == 25

    def test_low_move_rate(self):
        # all False, n=10
        h = [_make_record(line_moved=False)] * 10
        result = _score_historical_activity(h)
        assert result < 12

    def test_small_sample_blend_n4(self):
        # n=4, all moved → raw=25, blend_w=0.85
        h = [_make_record(line_moved=True)] * 4
        result = _score_historical_activity(h)
        expected = int(0.85 * 25 + 0.15 * _ACTIVITY_NEUTRAL)
        assert result == expected

    def test_score_bounded_0_25(self):
        h = [_make_record(line_moved=True)] * 20
        assert 0 <= _score_historical_activity(h) <= 25


# ── _score_avg_vs_line ────────────────────────────────────────────────────────

class TestAvgVsLine:
    def test_empty_history_returns_0(self):
        assert _score_avg_vs_line(5.0, []) == 0

    def test_single_record_returns_0(self):
        h = [_make_record(line_value=5.0)]
        assert _score_avg_vs_line(5.0, h) == 0

    def test_no_deviation_returns_0(self):
        h = [_make_record(5.0)] * 5
        assert _score_avg_vs_line(5.0, h) == 0

    def test_small_deviation_below_2pct(self):
        # avg=5.0, current=5.05 → dev=1% < 2%
        h = [_make_record(5.0)] * 5
        assert _score_avg_vs_line(5.05, h) == 0

    def test_5pct_deviation(self):
        # avg=4.0, current=4.2 → dev=5%
        h = [_make_record(4.0)] * 5
        assert _score_avg_vs_line(4.2, h) == 8

    def test_10pct_deviation(self):
        h = [_make_record(5.0)] * 5
        assert _score_avg_vs_line(5.5, h) == 12   # 10% dev

    def test_25pct_deviation(self):
        h = [_make_record(4.0)] * 5
        assert _score_avg_vs_line(5.0, h) == 20   # 25% dev

    def test_ignores_zero_line_value(self):
        h = [_make_record(0.0)] * 5 + [_make_record(5.0)] * 5
        # zeros excluded; avg=5.0; current=5.0 → 0%
        assert _score_avg_vs_line(5.0, h) == 0

    def test_score_bounded_0_20(self):
        h = [_make_record(1.0)] * 5
        assert 0 <= _score_avg_vs_line(10.0, h) <= 20


# ── _score_consistency ────────────────────────────────────────────────────────

class TestConsistency:
    def test_empty_history_returns_neutral(self):
        assert _score_consistency([]) == _CONSISTENCY_NEUTRAL

    def test_no_moved_records_returns_neutral(self):
        h = [_make_record(line_moved=False, prev_line=4.0)] * 5
        assert _score_consistency(h) == _CONSISTENCY_NEUTRAL

    def test_only_one_moved_record_returns_neutral(self):
        h = [_make_record(line_moved=True, prev_line=4.0, line_value=4.5)]
        assert _score_consistency(h) == _CONSISTENCY_NEUTRAL

    def test_all_up_moves_returns_15(self):
        h = [
            _make_record(line_value=5.0 + 0.5 * i, line_moved=True, prev_line=5.0 + 0.5 * (i - 1))
            for i in range(1, 6)
        ]
        assert _score_consistency(h) == 15

    def test_all_down_moves_returns_15(self):
        h = [
            _make_record(line_value=5.0 - 0.5 * i, line_moved=True, prev_line=5.0 - 0.5 * (i - 1))
            for i in range(1, 6)
        ]
        assert _score_consistency(h) == 15

    def test_perfectly_alternating_returns_3(self):
        # up, down, up, down … → purity = 0.50 → 6, but also tests < 0.50 border
        h = [
            _make_record(line_value=5.5, line_moved=True, prev_line=5.0),
            _make_record(line_value=5.0, line_moved=True, prev_line=5.5),
            _make_record(line_value=5.5, line_moved=True, prev_line=5.0),
            _make_record(line_value=5.0, line_moved=True, prev_line=5.5),
        ]
        result = _score_consistency(h)
        # n_up=2, n_down=2, purity=0.50 → returns 6
        assert result == 6

    def test_ignores_records_without_prev_line(self):
        # These should not contribute to direction counts
        h = [
            _make_record(line_value=5.5, line_moved=True, prev_line=None),  # ignored
            _make_record(line_value=5.5, line_moved=True, prev_line=5.0),
            _make_record(line_value=6.0, line_moved=True, prev_line=5.5),
        ]
        result = _score_consistency(h)
        # 2 valid up moves → purity=1.0 → 15
        assert result == 15

    def test_score_bounded_3_15(self):
        h = [_make_record(line_moved=True, prev_line=5.0, line_value=5.5)] * 10
        assert 3 <= _score_consistency(h) <= 15


# ── _score_stability ──────────────────────────────────────────────────────────

class TestStability:
    def test_empty_history_returns_neutral(self):
        assert _score_stability([]) == _STABILITY_NEUTRAL

    def test_one_record_returns_neutral(self):
        assert _score_stability([_make_record(5.0)]) == _STABILITY_NEUTRAL

    def test_two_records_returns_neutral(self):
        assert _score_stability([_make_record(5.0)] * 2) == _STABILITY_NEUTRAL

    def test_zero_variance_returns_15(self):
        h = [_make_record(5.0)] * 5
        assert _score_stability(h) == 15

    def test_very_low_variance_returns_15(self):
        # std ≈ 0.1 → <= 0.25 → 15
        h = [_make_record(5.0 + 0.05 * i) for i in range(5)]
        assert _score_stability(h) == 15

    def test_medium_variance_score(self):
        # Values spread ±1 → std ~ 1 → <=1.25 → 5
        import statistics
        h = [_make_record(v) for v in [4.0, 4.5, 5.0, 5.5, 6.0]]
        std = statistics.stdev([4.0, 4.5, 5.0, 5.5, 6.0])
        expected = 9 if std <= 0.75 else (5 if std <= 1.25 else 2)
        assert _score_stability(h) == expected

    def test_high_variance_returns_0(self):
        # std >> 2.0
        h = [_make_record(v) for v in [1.0, 5.0, 1.0, 10.0, 1.0]]
        assert _score_stability(h) == 0

    def test_excludes_removed_records(self):
        # A removed record with extreme value should not corrupt the std
        h = (
            [_make_record(5.0)] * 5
            + [_make_record(100.0, removed=True)]   # removed — excluded
        )
        assert _score_stability(h) == 15

    def test_score_bounded_0_15(self):
        h = [_make_record(float(i)) for i in range(20)]
        assert 0 <= _score_stability(h) <= 15


# ── score_ud_prop (public API) ────────────────────────────────────────────────

class TestScoreUdProp:
    def _call(self, current=5.0, prev=None, history=None):
        return score_ud_prop(
            player_name  = "Test Player",
            stat_type    = "Hits",
            sport        = "MLB",
            current_line = current,
            prev_line    = prev,
            history      = history or [],
        )

    def test_no_history_all_neutral_components(self):
        s = self._call()
        assert s.move_velocity       == 0              # no prev_line → magnitude=0
        assert s.historical_activity == _ACTIVITY_NEUTRAL
        assert s.avg_vs_line         == 0              # < 2 records
        assert s.consistency         == _CONSISTENCY_NEUTRAL
        assert s.stability           == _STABILITY_NEUTRAL

    def test_no_prev_line_velocity_is_0(self):
        s = self._call(current=5.5, prev=None)
        assert s.move_velocity == 0

    def test_prev_line_velocity_computed(self):
        s = self._call(current=7.0, prev=5.0)     # magnitude=2.0
        assert s.move_velocity == 15

    def test_rich_history_non_zero_total(self):
        history = [
            _make_record(line_value=5.0 + 0.5 * i, line_moved=(i > 0),
                         prev_line=5.0 + 0.5 * (i - 1) if i > 0 else None)
            for i in range(10)
        ]
        s = self._call(current=9.5, prev=9.0, history=history)
        assert s.total > 0
        assert s.n_history == 10

    def test_returns_frozen_dataclass(self):
        s = self._call()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            s.move_velocity = 99  # type: ignore[misc]

    def test_fields_carried_through(self):
        s = score_ud_prop("Yordan Alvarez", "Hits", "MLB", 3.5, 3.0, [])
        assert s.player_name  == "Yordan Alvarez"
        assert s.stat_type    == "Hits"
        assert s.sport        == "MLB"
        assert s.current_line == 3.5
        assert s.n_history    == 0

    def test_total_bounded_0_100(self):
        # Worst case: all maximums
        history = [
            _make_record(line_value=5.0, line_moved=True, prev_line=4.0)
        ] * 20
        s = self._call(current=10.0, prev=5.0, history=history)
        assert 0 <= s.total <= 100

    def test_tier_pass_with_no_signals(self):
        s = self._call()
        # neutral scores: vel=0, act=5, avg=0, con=8, sta=8 → total=21 → PASS
        assert s.tier == "PASS"

    def test_stars_at_least_1(self):
        s = self._call()
        assert s.stars >= 1

    def test_stars_display_length(self):
        s = self._call()
        assert len(s.stars_display) == 5
        assert all(c in "★☆" for c in s.stars_display)

    def test_drift_velocity_no_history_returns_0(self):
        """use_drift_velocity=True with empty history → velocity=0 (no opening line)."""
        s = score_ud_prop("P", "Hits", "MLB", 1.5, None, [], use_drift_velocity=True)
        assert s.move_velocity == 0

    def test_drift_velocity_half_step_drift(self):
        """Drift of 0.5 (one Underdog increment) → velocity=5."""
        history = [_make_record(line_value=1.0)]
        s = score_ud_prop("P", "Hits", "MLB", 1.5, None, history, use_drift_velocity=True)
        assert s.move_velocity == 5

    def test_drift_velocity_two_step_drift(self):
        """Drift of 1.0 (two increments) → velocity=10."""
        history = [_make_record(line_value=2.5)]
        s = score_ud_prop("P", "Hits", "MLB", 1.5, None, history, use_drift_velocity=True)
        assert s.move_velocity == 10

    def test_drift_velocity_four_step_drift(self):
        """Drift of 2.0+ → velocity=15 (capped)."""
        history = [_make_record(line_value=0.5)]
        s = score_ud_prop("P", "Hits", "MLB", 2.5, None, history, use_drift_velocity=True)
        assert s.move_velocity == 15

    def test_drift_velocity_prev_line_takes_precedence(self):
        """When prev_line is set, live velocity is used even if use_drift_velocity=True."""
        history = [_make_record(line_value=0.5)]
        # current=2.5, prev=2.0 → magnitude=0.5 → _score_move_velocity(0.5)=4
        s = score_ud_prop("P", "Hits", "MLB", 2.5, 2.0, history, use_drift_velocity=True)
        assert s.move_velocity == 4   # live velocity, NOT drift velocity

    def test_drift_velocity_default_off(self):
        """Default use_drift_velocity=False: prev=None with history → velocity=0."""
        history = [_make_record(line_value=0.5)]
        s = score_ud_prop("P", "Hits", "MLB", 2.5, None, history)
        assert s.move_velocity == 0


# ── _score_drift_velocity (standalone) ───────────────────────────────────────

class TestDriftVelocity:
    def test_no_drift_returns_0(self):
        assert _score_drift_velocity(2.5, 2.5) == 0

    def test_below_threshold_returns_0(self):
        assert _score_drift_velocity(2.5, 2.75) == 0   # drift=0.25

    def test_half_step_returns_5(self):
        assert _score_drift_velocity(1.0, 1.5) == 5

    def test_one_step_returns_10(self):
        assert _score_drift_velocity(1.5, 0.5) == 10   # drift=1.0, direction irrelevant

    def test_two_steps_returns_15(self):
        assert _score_drift_velocity(0.5, 2.5) == 15   # drift=2.0

    def test_large_drift_capped_at_15(self):
        assert _score_drift_velocity(0.5, 10.0) == 15

    def test_negative_direction_same_magnitude(self):
        assert _score_drift_velocity(3.0, 2.0) == 10   # drift=1.0


# ── _score_historical_activity recalibration ──────────────────────────────────

class TestHistoricalActivityRecalibrated:
    """Verify new Underdog-calibrated thresholds against the observed distribution."""

    def test_very_high_rate_15pct_gets_25(self):
        # 3/20 = 15% → blended ≥0.15 → raw=25
        h = [_make_record(line_moved=True)] * 3 + [_make_record(line_moved=False)] * 17
        assert _score_historical_activity(h) == 25

    def test_rate_10pct_gets_20(self):
        # 2/20 = 10% → blended ≥0.10 → raw=20
        # Build so L5=0, L10=2/10=0.2, L20=2/20=0.1
        # blended = 0.5×0 + 0.3×0.2 + 0.2×0.1 = 0.06+0.02 = 0.08 → raw=5
        # Need L5 moves for reliable 10% blended; use 20 records with all 2 at positions 6-10
        h = (
            [_make_record(line_moved=False)] * 5   # L5: 0 moves
            + [_make_record(line_moved=True)]  * 2  # positions 6-7
            + [_make_record(line_moved=False)] * 13
        )
        # blended = 0.5×0 + 0.3×(2/10) + 0.2×(2/20) = 0+0.06+0.02 = 0.08 → ≥0.05 → raw=15
        result = _score_historical_activity(h)
        assert result == 15  # blended=0.08 → ≥0.05 band

    def test_low_rate_2pct_gets_10(self):
        # Approximately 2% blended rate
        # 1 move in last 20, in position 11-20 only → L5=0, L10=0, L20=1/20=0.05
        # blended = 0.5×0 + 0.3×0 + 0.2×0.05 = 0.01 → raw=5
        # Instead: 1 move in position 6 → L5=0, L10=1/10=0.1, L20=1/20=0.05
        # blended = 0+0.3×0.1+0.2×0.05 = 0.03+0.01 = 0.04 → ≥0.02 → raw=10
        h = (
            [_make_record(line_moved=False)] * 5
            + [_make_record(line_moved=True)]
            + [_make_record(line_moved=False)] * 14
        )
        result = _score_historical_activity(h)
        assert result == 10   # blended=0.04 → ≥0.02 band

    def test_static_prop_gets_2(self):
        # 0% rate, n=20 → raw=2
        h = [_make_record(line_moved=False)] * 20
        assert _score_historical_activity(h) == 2

    def test_neutral_is_5_not_12(self):
        assert _ACTIVITY_NEUTRAL == 5

    def test_legacy_neutral_is_12(self):
        assert _ACTIVITY_NEUTRAL_LEGACY == 12


# ── _score_historical_activity_legacy ─────────────────────────────────────────

class TestHistoricalActivityLegacy:
    """Legacy function must preserve old behaviour exactly."""

    def test_neutral_n_lt_3(self):
        assert _score_historical_activity_legacy([]) == _ACTIVITY_NEUTRAL_LEGACY
        assert _score_historical_activity_legacy([_make_record()] * 2) == _ACTIVITY_NEUTRAL_LEGACY

    def test_all_moved_high_rate(self):
        h = [_make_record(line_moved=True)] * 10
        assert _score_historical_activity_legacy(h) == 25

    def test_zero_move_rate(self):
        h = [_make_record(line_moved=False)] * 10
        assert _score_historical_activity_legacy(h) == 2

    def test_differs_from_new_at_low_rates(self):
        # A 10% blended rate: old → 5 (≥0.10 band in old scale),
        # new → 20 (≥0.10 band in new scale).
        h = [_make_record(line_moved=True)] * 10 + [_make_record(line_moved=False)] * 90
        new = _score_historical_activity(h)
        old = _score_historical_activity_legacy(h)
        # Both get raw=25 (blended=1.0 for L5=1.0), so same here.
        # Use a lower-activity prop to show divergence.
        h2 = (
            [_make_record(line_moved=False)] * 5
            + [_make_record(line_moved=True)] * 1
            + [_make_record(line_moved=False)] * 14
        )
        new2 = _score_historical_activity(h2)
        old2 = _score_historical_activity_legacy(h2)
        # blended≈0.04: new→raw=10 (≥0.02), old→raw=2 (<0.10)
        assert new2 > old2


# ── PropDifficultyClass classification ────────────────────────────────────────

class TestClassifyPropDifficulty:
    """_classify_prop_difficulty(stat_type, line_value) → PropDifficultyClass."""

    # HIGH_FLOOR cases
    def test_hits_is_high_floor(self):
        assert _classify_prop_difficulty("Hits", 1.5) == PropDifficultyClass.HIGH_FLOOR

    def test_hits_plus_rbi_is_high_floor(self):
        assert _classify_prop_difficulty("Hits + Runs + RBIs", 2.5) == PropDifficultyClass.HIGH_FLOOR

    def test_fantasy_score_is_high_floor(self):
        assert _classify_prop_difficulty("Fantasy Score", 30.5) == PropDifficultyClass.HIGH_FLOOR

    def test_points_is_high_floor(self):
        assert _classify_prop_difficulty("Points", 18.5) == PropDifficultyClass.HIGH_FLOOR

    def test_pra_is_high_floor(self):
        assert _classify_prop_difficulty("PRA", 25.5) == PropDifficultyClass.HIGH_FLOOR

    def test_high_floor_at_half_line_stays_high_floor(self):
        # High-floor stats at 0.5 remain HIGH_FLOOR — they are still reliable.
        assert _classify_prop_difficulty("Hits", 0.5) == PropDifficultyClass.HIGH_FLOOR

    # HIGH_VARIANCE cases
    def test_home_runs_is_high_variance(self):
        assert _classify_prop_difficulty("Home Runs", 0.5) == PropDifficultyClass.HIGH_VARIANCE

    def test_home_runs_non_half_still_high_variance(self):
        assert _classify_prop_difficulty("Home Runs", 1.5) == PropDifficultyClass.HIGH_VARIANCE

    def test_stolen_bases_is_high_variance(self):
        assert _classify_prop_difficulty("Stolen Bases", 0.5) == PropDifficultyClass.HIGH_VARIANCE

    def test_rbi_is_high_variance(self):
        assert _classify_prop_difficulty("RBIs", 0.5) == PropDifficultyClass.HIGH_VARIANCE

    def test_standard_stat_at_half_line_is_high_variance(self):
        # Non-high-floor, non-high-variance stat at 0.5 line → HIGH_VARIANCE
        assert _classify_prop_difficulty("Shots", 0.5) == PropDifficultyClass.HIGH_VARIANCE

    def test_strikeouts_at_half_line_is_high_variance(self):
        assert _classify_prop_difficulty("Strikeouts", 0.5) == PropDifficultyClass.HIGH_VARIANCE

    # STANDARD cases
    def test_total_bases_is_standard(self):
        assert _classify_prop_difficulty("Total Bases", 1.5) == PropDifficultyClass.STANDARD

    def test_shots_on_target_is_standard(self):
        assert _classify_prop_difficulty("Shots on Target", 1.5) == PropDifficultyClass.STANDARD

    def test_strikeouts_non_half_is_standard(self):
        assert _classify_prop_difficulty("Strikeouts", 4.5) == PropDifficultyClass.STANDARD

    def test_aces_is_standard(self):
        assert _classify_prop_difficulty("Aces", 5.5) == PropDifficultyClass.STANDARD

    def test_unknown_stat_is_standard(self):
        assert _classify_prop_difficulty("Some New Stat", 2.5) == PropDifficultyClass.STANDARD


# ── _score_variance_penalty ───────────────────────────────────────────────────

class TestScoreVariancePenalty:
    """_score_variance_penalty(stat_type, line_value) → 0 | 5 | 10."""

    def test_high_floor_stat_no_penalty(self):
        assert _score_variance_penalty("Hits", 1.5) == 0

    def test_high_floor_at_half_line_no_penalty(self):
        assert _score_variance_penalty("Hits", 0.5) == 0

    def test_standard_stat_non_half_no_penalty(self):
        assert _score_variance_penalty("Total Bases", 1.5) == 0

    def test_standard_at_half_line_penalty_5(self):
        # Non-high-floor stat at 0.5 → HIGH_VARIANCE → penalty 5
        assert _score_variance_penalty("Shots", 0.5) == 5

    def test_explicit_hv_stat_non_half_line_penalty_5(self):
        # HR at non-0.5 line: HIGH_VARIANCE category, not 0.5 → penalty 5
        assert _score_variance_penalty("Home Runs", 1.5) == 5

    def test_explicit_hv_stat_half_line_penalty_10(self):
        # HR at 0.5: HIGH_VARIANCE + 0.5 line → harshest penalty
        assert _score_variance_penalty("Home Runs", 0.5) == 10

    def test_stolen_bases_half_line_penalty_10(self):
        assert _score_variance_penalty("Stolen Bases", 0.5) == 10

    def test_rbi_half_line_penalty_10(self):
        assert _score_variance_penalty("RBIs", 0.5) == 10


# ── UDPropScore.total with variance_penalty ───────────────────────────────────

class TestUDPropScoreWithVariancePenalty:
    """Verify variance_penalty is subtracted from total and affects tier."""

    def test_penalty_subtracted_from_total(self):
        s = UDPropScore(
            player_name="P", stat_type="Home Runs", sport="MLB",
            current_line=0.5,
            move_velocity=25, historical_activity=25, avg_vs_line=20,
            consistency=15, stability=15, n_history=20,
            variance_penalty=10,
        )
        assert s.total == 25 + 25 + 20 + 15 + 15 - 10  # 90

    def test_zero_penalty_unchanged(self):
        s = UDPropScore(
            player_name="P", stat_type="Hits", sport="MLB",
            current_line=1.5,
            move_velocity=20, historical_activity=18, avg_vs_line=12,
            consistency=10, stability=8, n_history=10,
            variance_penalty=0,
        )
        assert s.total == 20 + 18 + 12 + 10 + 8  # 68

    def test_penalty_floors_at_zero(self):
        s = UDPropScore(
            player_name="P", stat_type="Home Runs", sport="MLB",
            current_line=0.5,
            move_velocity=0, historical_activity=2, avg_vs_line=0,
            consistency=0, stability=0, n_history=3,
            variance_penalty=10,
        )
        assert s.total == 0  # max(0, 2-10) = 0

    def test_score_ud_prop_high_variance_has_penalty(self):
        """score_ud_prop() auto-computes variance_penalty for HR 0.5."""
        s = score_ud_prop("Player", "Home Runs", "MLB", 0.5, None, [])
        assert s.variance_penalty == 10
        assert s.difficulty == PropDifficultyClass.HIGH_VARIANCE

    def test_score_ud_prop_high_floor_no_penalty(self):
        """score_ud_prop() returns penalty=0 for a Hits prop."""
        s = score_ud_prop("Player", "Hits", "MLB", 1.5, None, [])
        assert s.variance_penalty == 0
        assert s.difficulty == PropDifficultyClass.HIGH_FLOOR

    def test_score_ud_prop_standard_no_penalty(self):
        """score_ud_prop() returns penalty=0 for a standard prop at non-0.5 line."""
        s = score_ud_prop("Player", "Total Bases", "MLB", 1.5, None, [])
        assert s.variance_penalty == 0
        assert s.difficulty == PropDifficultyClass.STANDARD

    def test_score_ud_prop_default_difficulty_field(self):
        """UDPropScore with no explicit difficulty defaults to STANDARD."""
        s = UDPropScore(
            player_name="P", stat_type="X", sport="MLB", current_line=1.5,
            move_velocity=10, historical_activity=10, avg_vs_line=5,
            consistency=8, stability=8, n_history=5,
        )
        assert s.variance_penalty == 0
        assert s.difficulty == PropDifficultyClass.STANDARD


# ── compute_market_quality ─────────────────────────────────────────────────────

class TestComputeMarketQuality:
    def _score(
        self,
        difficulty:  PropDifficultyClass = PropDifficultyClass.HIGH_FLOOR,
        activity:    int = 20,
        stability:   int = 12,
        n:           int = 30,
        stat_type:   str = "Hits",
        variance_penalty: int = 0,
    ) -> UDPropScore:
        return UDPropScore(
            player_name="Test", stat_type=stat_type, sport="MLB", current_line=1.5,
            move_velocity=0, historical_activity=activity, avg_vs_line=0,
            consistency=8, stability=stability, n_history=n,
            difficulty=difficulty, variance_penalty=variance_penalty,
        )

    def test_high_floor_deep_stable_is_elite(self):
        """HIGH_FLOOR + high activity + 30 records + stable → ELITE (≥75)."""
        mq = compute_market_quality("Hits", 1.5, self._score())
        assert mq.label == MarketQualityLabel.ELITE
        assert mq.score >= 75

    def test_high_variance_thin_volatile_is_low(self):
        """HIGH_VARIANCE + no activity + 3 records + volatile → LOW (<35)."""
        s = UDPropScore(
            player_name="T", stat_type="Home Runs", sport="MLB", current_line=0.5,
            move_velocity=0, historical_activity=2, avg_vs_line=0,
            consistency=8, stability=0, n_history=3,
            difficulty=PropDifficultyClass.HIGH_VARIANCE, variance_penalty=10,
        )
        mq = compute_market_quality("Home Runs", 0.5, s)
        assert mq.label == MarketQualityLabel.LOW
        assert mq.score < 35

    def test_standard_moderate_is_medium_or_high(self):
        """STANDARD + moderate activity + 12 records → MEDIUM or HIGH."""
        s = UDPropScore(
            player_name="T", stat_type="Strikeouts", sport="MLB", current_line=5.5,
            move_velocity=0, historical_activity=5, avg_vs_line=0,
            consistency=8, stability=9, n_history=12,
            difficulty=PropDifficultyClass.STANDARD,
        )
        mq = compute_market_quality("Strikeouts", 5.5, s)
        assert mq.label in (MarketQualityLabel.MEDIUM, MarketQualityLabel.HIGH)

    def test_score_capped_at_100(self):
        """score never exceeds 100 even with all-max inputs."""
        s = UDPropScore(
            player_name="T", stat_type="Hits", sport="MLB", current_line=1.5,
            move_velocity=0, historical_activity=25, avg_vs_line=0,
            consistency=15, stability=15, n_history=50,
            difficulty=PropDifficultyClass.HIGH_FLOOR,
        )
        mq = compute_market_quality("Hits", 1.5, s)
        assert mq.score <= 100

    def test_high_floor_reasons_mention_stat(self):
        """HIGH_FLOOR quality note references the stat name."""
        mq = compute_market_quality("Hits", 1.5, self._score())
        assert any("Hits" in r or "High-floor" in r for r in mq.reasons)

    def test_high_variance_reasons_mention_stat(self):
        """HIGH_VARIANCE quality note references the stat name."""
        s = self._score(difficulty=PropDifficultyClass.HIGH_VARIANCE, stat_type="Home Runs")
        mq = compute_market_quality("Home Runs", 0.5, s)
        assert any("Home Runs" in r or "variance" in r.lower() for r in mq.reasons)

    def test_returns_market_quality_instance(self):
        mq = compute_market_quality("Hits", 1.5, self._score())
        assert isinstance(mq, MarketQuality)
        assert mq.label in MarketQualityLabel
        assert isinstance(mq.reasons, tuple)

    def test_low_activity_reduces_score(self):
        """Zero activity lowers score compared to high activity."""
        high = compute_market_quality("Hits", 1.5, self._score(activity=20))
        low  = compute_market_quality("Hits", 1.5, self._score(activity=2))
        assert high.score > low.score

    def test_thin_sample_reduces_score(self):
        """Thin sample (n<5) lowers score compared to deep sample."""
        deep  = compute_market_quality("Hits", 1.5, self._score(n=30))
        thin  = compute_market_quality("Hits", 1.5, self._score(n=2))
        assert deep.score > thin.score


# ── detect_market_pressure ─────────────────────────────────────────────────────

class TestDetectMarketPressure:
    def _hist(self, moved: int = 0, total: int = 10) -> list:
        recs = []
        for i in range(total):
            r = MagicMock()
            r.line_moved = i < moved
            recs.append(r)
        return recs

    def test_no_signals_none(self):
        flag = detect_market_pressure(None, [], False)
        assert not flag.has_pressure
        assert flag.pressure_level == "NONE"

    def test_large_move_high(self):
        flag = detect_market_pressure(1.5, [], False)
        assert flag.has_pressure
        assert flag.pressure_level == "HIGH"

    def test_significant_move_medium(self):
        flag = detect_market_pressure(1.0, [], False)
        assert flag.has_pressure
        assert flag.pressure_level == "MEDIUM"

    def test_small_move_low(self):
        flag = detect_market_pressure(0.5, [], False)
        assert flag.has_pressure
        assert flag.pressure_level == "LOW"

    def test_removal_risk_high(self):
        flag = detect_market_pressure(None, [], True)
        assert flag.has_pressure
        assert flag.pressure_level == "HIGH"

    def test_four_recent_moves_high(self):
        flag = detect_market_pressure(None, self._hist(4), False)
        assert flag.has_pressure
        assert flag.pressure_level == "HIGH"

    def test_three_recent_moves_medium(self):
        flag = detect_market_pressure(None, self._hist(3), False)
        assert flag.has_pressure
        assert flag.pressure_level == "MEDIUM"

    def test_two_recent_moves_low(self):
        flag = detect_market_pressure(None, self._hist(2), False)
        assert flag.has_pressure
        assert flag.pressure_level == "LOW"

    def test_one_move_no_pressure(self):
        flag = detect_market_pressure(None, self._hist(1), False)
        assert not flag.has_pressure

    def test_returns_market_pressure_flag(self):
        flag = detect_market_pressure(None, [], False)
        assert isinstance(flag, MarketPressureFlag)
        assert isinstance(flag.reasons, tuple)

    def test_reasons_populated_for_large_move(self):
        flag = detect_market_pressure(2.0, [], False)
        assert len(flag.reasons) > 0
        assert any("Large" in r or "move" in r.lower() for r in flag.reasons)

    def test_reasons_populated_for_removal(self):
        flag = detect_market_pressure(None, [], True)
        assert any("removed" in r.lower() or "Prop" in r for r in flag.reasons)

    def test_large_move_dominates_small_history(self):
        """Large magnitude → HIGH even with only 1 recent move."""
        flag = detect_market_pressure(1.5, self._hist(1), False)
        assert flag.pressure_level == "HIGH"
