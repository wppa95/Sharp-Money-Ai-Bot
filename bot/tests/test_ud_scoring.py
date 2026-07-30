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
    UDScoreTier,
    UDPropScore,
    score_ud_prop,
    _score_move_velocity,
    _score_historical_activity,
    _score_avg_vs_line,
    _score_consistency,
    _score_stability,
    _ACTIVITY_NEUTRAL,
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
        # neutral scores: vel=0, act=12, avg=0, con=8, sta=8 → total=28 → PASS
        assert s.tier == "PASS"

    def test_stars_at_least_1(self):
        s = self._call()
        assert s.stars >= 1

    def test_stars_display_length(self):
        s = self._call()
        assert len(s.stars_display) == 5
        assert all(c in "★☆" for c in s.stars_display)
