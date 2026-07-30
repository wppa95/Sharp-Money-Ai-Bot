"""
Tests for engine/pp_scoring.py — PPAnalysisScore five-dimension scoring.

Covers:
  - Each dimension individually (market_edge, hit_rate, matchup, role, variance)
  - Tier thresholds (S / A / B / PASS)
  - Star thresholds (1–5)
  - total = sum of five dimensions
  - score_pp_edge integration (produces valid PPAnalysisScore from live objects)
  - normalize_pp wires score → AlertTier correctly
  - Hit-rate neutral default and small-sample blending
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from prizepicks import PrizePicksLine, compare_pp_to_sportsbook
from engine.pp_scoring import (
    PPAnalysisScore,
    PPScoreTier,
    score_pp_edge,
    _score_market_edge,
    _score_hit_rate,
    _score_matchup,
    _score_role,
    _score_variance,
    _HIT_RATE_NEUTRAL,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_pp_line(
    *,
    stat_type: str = "Points",
    line_value: float = 25.5,
    start_time=None,
    sport: str = "NBA",
) -> PrizePicksLine:
    return PrizePicksLine(
        external_id="test-001",
        player_name="Test Player",
        team="TM",
        sport=sport,
        league=sport,
        stat_type=stat_type,
        line_value=line_value,
        start_time=start_time,
        game_description="vs OPP",
    )


def make_opp(
    *,
    stat_type: str = "Points",
    pp_line: float = 25.5,
    sb_line: float = 27.5,
    over_odds: int = -110,
    under_odds: int = -110,
    start_time=None,
    sport: str = "NBA",
):
    line = make_pp_line(
        stat_type=stat_type, line_value=pp_line,
        start_time=start_time, sport=sport,
    )
    return compare_pp_to_sportsbook(
        line, sportsbook="DraftKings",
        sb_line=sb_line,
        sb_over_odds=over_odds,
        sb_under_odds=under_odds,
    )


def make_mock_record(result: str) -> MagicMock:
    r = MagicMock()
    r.result = result
    return r


# ── PPAnalysisScore dataclass ─────────────────────────────────────────────────

class TestPPAnalysisScore:
    def test_total_is_sum_of_dimensions(self):
        s = PPAnalysisScore(market_edge=20, hit_rate=18, matchup=15, role=12, variance=10)
        assert s.total == 75

    def test_all_zeros_total(self):
        s = PPAnalysisScore(market_edge=0, hit_rate=0, matchup=0, role=0, variance=0)
        assert s.total == 0

    def test_tier_s_at_80(self):
        s = PPAnalysisScore(market_edge=20, hit_rate=20, matchup=16, role=14, variance=10)
        assert s.total == 80
        assert s.tier == "S"

    def test_tier_s_above_80(self):
        s = PPAnalysisScore(market_edge=25, hit_rate=25, matchup=20, role=15, variance=15)
        assert s.tier == "S"

    def test_tier_a_at_65(self):
        s = PPAnalysisScore(market_edge=14, hit_rate=15, matchup=14, role=12, variance=10)
        assert s.total == 65
        assert s.tier == "A"

    def test_tier_a_below_80(self):
        s = PPAnalysisScore(market_edge=18, hit_rate=18, matchup=14, role=12, variance=10)
        assert s.total == 72
        assert s.tier == "A"

    def test_tier_b_at_50(self):
        s = PPAnalysisScore(market_edge=10, hit_rate=12, matchup=12, role=10, variance=6)
        assert s.total == 50
        assert s.tier == "B"

    def test_tier_pass_below_50(self):
        s = PPAnalysisScore(market_edge=5, hit_rate=8, matchup=8, role=6, variance=4)
        assert s.total == 31
        assert s.tier == "PASS"

    def test_tier_pass_at_49(self):
        s = PPAnalysisScore(market_edge=10, hit_rate=12, matchup=12, role=10, variance=5)
        assert s.total == 49
        assert s.tier == "PASS"

    def test_stars_5_at_85(self):
        s = PPAnalysisScore(market_edge=20, hit_rate=20, matchup=18, role=15, variance=12)
        assert s.total == 85
        assert s.stars == 5

    def test_stars_4_at_70(self):
        s = PPAnalysisScore(market_edge=14, hit_rate=15, matchup=15, role=14, variance=12)
        assert s.total == 70
        assert s.stars == 4

    def test_stars_3_at_55(self):
        s = PPAnalysisScore(market_edge=11, hit_rate=12, matchup=12, role=10, variance=10)
        assert s.total == 55
        assert s.stars == 3

    def test_stars_2_at_40(self):
        s = PPAnalysisScore(market_edge=8, hit_rate=8, matchup=8, role=8, variance=8)
        assert s.total == 40
        assert s.stars == 2

    def test_stars_1_below_40(self):
        s = PPAnalysisScore(market_edge=5, hit_rate=5, matchup=5, role=5, variance=5)
        assert s.total == 25
        assert s.stars == 1

    def test_tier_is_ppscoreier_value(self):
        for tier in PPScoreTier:
            assert isinstance(tier.value, str)

    def test_frozen_immutable(self):
        s = PPAnalysisScore(market_edge=10, hit_rate=10, matchup=10, role=10, variance=10)
        with pytest.raises((AttributeError, TypeError)):
            s.market_edge = 99  # type: ignore


# ── Market Edge dimension ─────────────────────────────────────────────────────

class TestMarketEdge:
    def test_zero_edge_gives_zero(self):
        opp = make_opp(pp_line=25.5, sb_line=25.5)
        assert _score_market_edge(opp) == 0

    def test_small_edge_3pct_gives_5(self):
        # Points: ppu=2.5, 1 unit diff → 2.5% edge
        # 2 unit diff → 5% edge, 1.2 unit → 3%
        # Use a 1.2 unit diff on Points (2.5 ppu): 1.2 × 2.5 = 3%
        opp = make_opp(pp_line=25.5, sb_line=26.7, stat_type="Points")
        assert opp.best_edge >= 3.0
        score = _score_market_edge(opp)
        assert score >= 5

    def test_edge_5pct_gives_at_least_8(self):
        # 2 unit diff on Points: 2 × 2.5 = 5%
        opp = make_opp(pp_line=25.5, sb_line=27.5, stat_type="Points")
        assert opp.best_edge >= 5.0
        assert _score_market_edge(opp) >= 8

    def test_high_edge_gives_bonus_points(self):
        # Large line diff produces high edge and hence bonus points
        opp_small = make_opp(pp_line=25.5, sb_line=27.5, stat_type="Points")
        opp_large = make_opp(pp_line=20.0, sb_line=30.0, stat_type="Points")
        assert _score_market_edge(opp_large) > _score_market_edge(opp_small)

    def test_max_capped_at_25(self):
        # Extreme edge should not exceed 25
        opp = make_opp(pp_line=5.0, sb_line=50.0, stat_type="Points")
        assert _score_market_edge(opp) <= 25

    def test_returns_int(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        assert isinstance(_score_market_edge(opp), int)


# ── Hit Rate dimension ────────────────────────────────────────────────────────

class TestHitRate:
    def test_no_history_returns_neutral(self):
        assert _score_hit_rate([]) == _HIT_RATE_NEUTRAL

    def test_pending_only_returns_neutral(self):
        records = [make_mock_record("PENDING")] * 5
        assert _score_hit_rate(records) == _HIT_RATE_NEUTRAL

    def test_all_wins_gives_high_score(self):
        records = [make_mock_record("WIN")] * 10
        assert _score_hit_rate(records) == 25

    def test_all_losses_gives_low_score(self):
        records = [make_mock_record("LOSS")] * 10
        assert _score_hit_rate(records) <= 6

    def test_50pct_win_rate_returns_12(self):
        records = [make_mock_record("WIN")] * 5 + [make_mock_record("LOSS")] * 5
        score = _score_hit_rate(records)
        assert score == 12

    def test_push_counts_half_win(self):
        # 2 WIN + 2 PUSH + 2 LOSS = win_rate (2+1)/6 ≈ 0.50
        records = (
            [make_mock_record("WIN")] * 2
            + [make_mock_record("PUSH")] * 2
            + [make_mock_record("LOSS")] * 2
        )
        score = _score_hit_rate(records)
        assert score == 12   # ~50% win rate

    def test_small_sample_blends_toward_neutral(self):
        # 1 WIN → raw=25, blended toward 12 (40% weight)
        single_win = [make_mock_record("WIN")]
        full_wins  = [make_mock_record("WIN")] * 10
        score_1    = _score_hit_rate(single_win)
        score_10   = _score_hit_rate(full_wins)
        assert score_1 < score_10, "Single record should score lower than large sample"
        assert score_1 > _HIT_RATE_NEUTRAL - 1, "Should not fall below neutral for 1 WIN"

    def test_score_bounded_0_to_25(self):
        for results in [
            [],
            [make_mock_record("WIN")] * 10,
            [make_mock_record("LOSS")] * 10,
        ]:
            s = _score_hit_rate(results)
            assert 0 <= s <= 25, f"Out of range: {s}"

    def test_pending_mixed_with_resolved(self):
        # PENDING records should be ignored
        records = [make_mock_record("WIN")] * 5 + [make_mock_record("PENDING")] * 3
        assert _score_hit_rate(records) == 25   # 5/5 resolved = 100% win rate


# ── Matchup dimension ─────────────────────────────────────────────────────────

class TestMatchup:
    def test_zero_line_diff_gives_line_agreement_bonus(self):
        opp = make_opp(pp_line=25.5, sb_line=25.5)
        # |line_diff| == 0 → +8 for agreement
        score = _score_matchup(opp, now=datetime(2025, 1, 15, 19, 0))
        assert score >= 8

    def test_large_line_diff_reduces_agreement_pts(self):
        opp_agree = make_opp(pp_line=25.5, sb_line=25.5)
        opp_diff  = make_opp(pp_line=25.5, sb_line=30.0)
        s_agree = _score_matchup(opp_agree, now=datetime(2025, 1, 15, 19, 0))
        s_diff  = _score_matchup(opp_diff,  now=datetime(2025, 1, 15, 19, 0))
        assert s_agree >= s_diff

    def test_game_within_4h_gives_max_proximity_bonus(self):
        now = datetime(2025, 1, 15, 18, 0)
        start = datetime(2025, 1, 15, 20, 0)   # 2 h away
        opp = make_opp(pp_line=25.5, sb_line=25.5, start_time=start)
        score = _score_matchup(opp, now=now)
        # Should include +8 for proximity
        assert score >= 8 + 8   # agreement + proximity

    def test_game_within_48h_gives_proximity_pts(self):
        now = datetime(2025, 1, 15, 18, 0)
        start = datetime(2025, 1, 17, 0, 0)   # ~30 h away
        opp = make_opp(pp_line=25.5, sb_line=25.5, start_time=start)
        score = _score_matchup(opp, now=now)
        assert score >= 8 + 2   # agreement + 2 for 48h band

    def test_no_start_time_gives_zero_proximity(self):
        opp_no_time  = make_opp(pp_line=25.5, sb_line=25.5, start_time=None)
        now = datetime(2025, 1, 15, 18, 0)
        start = datetime(2025, 1, 15, 20, 0)
        opp_with_time = make_opp(pp_line=25.5, sb_line=25.5, start_time=start)
        s_no   = _score_matchup(opp_no_time,   now=now)
        s_with = _score_matchup(opp_with_time, now=now)
        assert s_with > s_no

    def test_capped_at_20(self):
        now = datetime(2025, 1, 15, 18, 0)
        start = datetime(2025, 1, 15, 20, 0)
        opp = make_opp(pp_line=5.0, sb_line=50.0, start_time=start)
        assert _score_matchup(opp, now=now) <= 20

    def test_returns_int(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        assert isinstance(_score_matchup(opp), int)


# ── Role dimension ────────────────────────────────────────────────────────────

class TestRole:
    def test_primary_stat_scores_highest(self):
        # "Points" is primary
        s_primary   = _score_role(make_opp(stat_type="Points"))
        s_secondary = _score_role(make_opp(stat_type="Rebounds"))
        s_specialty = _score_role(make_opp(stat_type="Steals"))
        assert s_primary > s_secondary > s_specialty

    def test_passing_yards_is_primary(self):
        opp = make_opp(stat_type="Passing Yards")
        assert _score_role(opp) >= 10

    def test_rebounds_is_secondary(self):
        opp = make_opp(stat_type="Rebounds")
        score = _score_role(opp)
        assert 6 <= score <= 15

    def test_steals_is_specialty(self):
        opp = make_opp(stat_type="Steals")
        # Specialty base = 3; plus possible market balance bonus
        assert _score_role(opp) >= 3

    def test_balanced_market_gives_balance_bonus(self):
        # -110/-110 is perfectly balanced
        opp_balanced = make_opp(over_odds=-110, under_odds=-110)
        # -150/+130 is skewed
        opp_skewed   = make_opp(over_odds=-150, under_odds=130)
        assert _score_role(opp_balanced) > _score_role(opp_skewed)

    def test_capped_at_15(self):
        opp = make_opp(stat_type="Points", over_odds=-110, under_odds=-110)
        assert _score_role(opp) <= 15

    def test_returns_int(self):
        opp = make_opp(stat_type="Points")
        assert isinstance(_score_role(opp), int)


# ── Variance dimension ────────────────────────────────────────────────────────

class TestVariance:
    def test_stable_stat_scores_higher(self):
        # Passing Yards (ppu=0.4) vs Steals (ppu=10.0)
        opp_stable   = make_opp(stat_type="Passing Yards")
        opp_volatile = make_opp(stat_type="Steals")
        assert _score_variance(opp_stable) > _score_variance(opp_volatile)

    def test_low_vig_gives_bonus(self):
        # -105/-105 has lower vig than -120/-120
        opp_low_vig  = make_opp(over_odds=-105, under_odds=-105)
        opp_high_vig = make_opp(over_odds=-120, under_odds=-120)
        assert _score_variance(opp_low_vig) > _score_variance(opp_high_vig)

    def test_stable_opening_line_gives_bonus(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        # opening_line == current pp_line → no movement → +3
        s_stable = _score_variance(opp, opening_line=25.5)
        s_moved  = _score_variance(opp, opening_line=24.0)   # 1.5 unit move
        assert s_stable > s_moved

    def test_no_opening_line_gives_zero_stability(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        s_no_history = _score_variance(opp, opening_line=None)
        s_stable     = _score_variance(opp, opening_line=25.5)
        assert s_stable > s_no_history

    def test_large_line_move_gives_no_stability_bonus(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        s = _score_variance(opp, opening_line=22.5)   # 3 unit move
        # stability component should be 0; vig + stat pts only
        opp_ppu = opp.prob_per_unit
        max_without_stability = (
            (8 if opp_ppu <= 1.0 else 6 if opp_ppu <= 3.0 else 4 if opp_ppu <= 6.0 else 2)
            + 4   # max vig bonus (at -110/-110 ≈ 4.8% vig → 3 pts actually)
        )
        assert s <= max_without_stability + 1   # +1 tolerance

    def test_capped_at_15(self):
        opp = make_opp(stat_type="Passing Yards", over_odds=-105, under_odds=-105)
        assert _score_variance(opp, opening_line=opp.pp_line.line_value) <= 15

    def test_returns_int(self):
        opp = make_opp(stat_type="Points")
        assert isinstance(_score_variance(opp), int)


# ── score_pp_edge integration ─────────────────────────────────────────────────

class TestScorePPEdge:
    def test_returns_ppanalysisscore(self):
        opp = make_opp()
        result = score_pp_edge(opp)
        assert isinstance(result, PPAnalysisScore)

    def test_all_fields_in_range(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5, start_time=datetime(2025, 1, 15, 20, 0))
        s = score_pp_edge(opp, now=datetime(2025, 1, 15, 18, 0))
        assert 0 <= s.market_edge <= 25
        assert 0 <= s.hit_rate    <= 25
        assert 0 <= s.matchup     <= 20
        assert 0 <= s.role        <= 15
        assert 0 <= s.variance    <= 15
        assert 0 <= s.total       <= 100

    def test_tier_is_valid(self):
        opp = make_opp()
        s = score_pp_edge(opp)
        assert s.tier in {"S", "A", "B", "PASS"}

    def test_stars_is_1_to_5(self):
        opp = make_opp()
        s = score_pp_edge(opp)
        assert 1 <= s.stars <= 5

    def test_history_none_uses_neutral_hit_rate(self):
        opp = make_opp()
        s = score_pp_edge(opp, history=None)
        assert s.hit_rate == _HIT_RATE_NEUTRAL

    def test_history_empty_uses_neutral_hit_rate(self):
        opp = make_opp()
        s = score_pp_edge(opp, history=[])
        assert s.hit_rate == _HIT_RATE_NEUTRAL

    def test_winning_history_raises_score(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        wins = [make_mock_record("WIN")] * 10
        s_wins = score_pp_edge(opp, history=wins)
        s_none = score_pp_edge(opp, history=[])
        assert s_wins.total > s_none.total

    def test_opening_line_affects_variance(self):
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        s_stable = score_pp_edge(opp, opening_line=25.5)
        s_moved  = score_pp_edge(opp, opening_line=22.0)   # 3.5 unit move
        assert s_stable.variance >= s_moved.variance

    def test_now_affects_matchup_via_proximity(self):
        start = datetime(2025, 1, 15, 20, 0)
        opp   = make_opp(start_time=start)
        # 2 h before game vs 5 days before
        s_soon = score_pp_edge(opp, now=datetime(2025, 1, 15, 18, 0))
        s_far  = score_pp_edge(opp, now=datetime(2025, 1, 10, 18, 0))
        assert s_soon.matchup > s_far.matchup

    def test_strong_edge_reaches_b_or_higher(self):
        # 10% edge on a stable stat should at minimum reach B tier
        # Passing Yards with a 6 unit line diff = 6 × 0.4 = 2.4% ... that's tiny
        # Use Points: 6 unit diff = 6 × 2.5 = 15% edge
        opp = make_opp(stat_type="Points", pp_line=20.0, sb_line=26.0)
        s = score_pp_edge(opp)
        assert s.tier in {"S", "A", "B"}, f"Expected B or higher, got {s.tier} (total={s.total})"


# ── normalize_pp wiring ───────────────────────────────────────────────────────

class TestNormalizePPWiring:
    def test_normalize_pp_without_score_uses_legacy_tier(self):
        from alert_normalizer import normalize_pp
        from models import AlertTier
        # best_edge >= 10% → CRITICAL in legacy path
        opp = make_opp(pp_line=20.0, sb_line=26.0, stat_type="Points")   # 15% edge
        obj = normalize_pp(opp)
        assert obj.tier == AlertTier.CRITICAL

    def test_normalize_pp_with_score_s_maps_to_critical(self):
        from alert_normalizer import normalize_pp
        from models import AlertTier
        opp   = make_opp(pp_line=25.5, sb_line=27.5)
        score = PPAnalysisScore(market_edge=25, hit_rate=25, matchup=20, role=15, variance=15)
        assert score.tier == "S"
        obj = normalize_pp(opp, score=score)
        assert obj.tier == AlertTier.CRITICAL

    def test_normalize_pp_with_score_a_maps_to_high(self):
        from alert_normalizer import normalize_pp
        from models import AlertTier
        opp   = make_opp()
        score = PPAnalysisScore(market_edge=14, hit_rate=15, matchup=14, role=12, variance=10)
        assert score.tier == "A"
        obj = normalize_pp(opp, score=score)
        assert obj.tier == AlertTier.HIGH

    def test_normalize_pp_with_score_b_maps_to_medium(self):
        from alert_normalizer import normalize_pp
        from models import AlertTier
        opp   = make_opp()
        score = PPAnalysisScore(market_edge=10, hit_rate=12, matchup=12, role=10, variance=6)
        assert score.tier == "B"
        obj = normalize_pp(opp, score=score)
        assert obj.tier == AlertTier.MEDIUM

    def test_normalize_pp_with_score_pass_maps_to_low(self):
        from alert_normalizer import normalize_pp
        from models import AlertTier
        opp   = make_opp()
        score = PPAnalysisScore(market_edge=5, hit_rate=8, matchup=8, role=6, variance=4)
        assert score.tier == "PASS"
        obj = normalize_pp(opp, score=score)
        assert obj.tier == AlertTier.LOW

    def test_normalize_pp_with_score_uses_total_as_confidence(self):
        from alert_normalizer import normalize_pp
        opp   = make_opp()
        score = PPAnalysisScore(market_edge=20, hit_rate=18, matchup=15, role=12, variance=10)
        obj = normalize_pp(opp, score=score)
        assert obj.confidence == float(score.total)

    def test_normalize_pp_without_score_uses_best_edge_as_confidence(self):
        from alert_normalizer import normalize_pp
        opp = make_opp(pp_line=25.5, sb_line=27.5)
        obj = normalize_pp(opp)
        assert obj.confidence == round(float(opp.best_edge), 2)
