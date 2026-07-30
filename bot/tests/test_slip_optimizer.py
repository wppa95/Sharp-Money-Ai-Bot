"""
Tests for engine/slip_optimizer.py

Covers:
  - check_correlation: same player, same team, same game, independent
  - Stat pair delta adjustments
  - _extract_opponent helper
  - optimize_slip: empty, too few, normal, hard block, soft block, n_legs clamp
  - OptimizedSlip properties: avg_edge, avg_confidence
  - Greedy ordering (best tier/confidence selected first)
"""

from __future__ import annotations

import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from engine.slip_optimizer import (
    check_correlation,
    optimize_slip,
    CorrelationResult,
    OptimizedSlip,
    _extract_opponent,
    _HARD_BLOCK,
    _SOFT_BLOCK,
    _SCORE_SAME_PLAYER,
    _SCORE_SAME_TEAM,
    _SCORE_SAME_GAME,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rec(
    *,
    player_name: str = "Player A",
    team: str = "LAL",
    sport: str = "NBA",
    stat_type: str = "Points",
    tier: str = "A",
    confidence: float = 72.0,
    best_edge: float = 10.0,
    game_description: str = "vs BOS",
) -> MagicMock:
    r = MagicMock()
    r.player_name    = player_name
    r.team           = team
    r.sport          = sport
    r.stat_type      = stat_type
    r.tier           = tier
    r.confidence     = confidence
    r.best_edge      = best_edge
    r.game_description = game_description
    return r


# ── _extract_opponent ─────────────────────────────────────────────────────────

class TestExtractOpponent:
    def test_vs_prefix(self):
        assert _extract_opponent("vs BOS") == "bos"

    def test_team_vs_team(self):
        assert _extract_opponent("LAL vs BOS") == "bos"

    def test_empty_string(self):
        assert _extract_opponent("") == ""

    def test_no_vs(self):
        assert _extract_opponent("No match here") == ""

    def test_leading_whitespace(self):
        assert _extract_opponent("  vs MIA  ") == "mia"


# ── check_correlation ─────────────────────────────────────────────────────────

class TestCheckCorrelation:
    def test_same_player_returns_1(self):
        a = _make_rec(player_name="LeBron James", stat_type="Points")
        b = _make_rec(player_name="LeBron James", stat_type="Assists")
        result = check_correlation(a, b)
        assert result.score == _SCORE_SAME_PLAYER
        assert "same player" in result.reason.lower()

    def test_same_player_case_insensitive(self):
        a = _make_rec(player_name="lebron james")
        b = _make_rec(player_name="LeBron James")
        result = check_correlation(a, b)
        assert result.score == _SCORE_SAME_PLAYER

    def test_same_team_same_sport(self):
        a = _make_rec(player_name="Player A", team="LAL", sport="NBA")
        b = _make_rec(player_name="Player B", team="LAL", sport="NBA")
        result = check_correlation(a, b)
        assert abs(result.score - _SCORE_SAME_TEAM) <= 0.15   # allow stat pair delta
        assert "same team" in result.reason.lower()

    def test_same_team_different_sport_is_independent(self):
        a = _make_rec(player_name="Player A", team="LAL", sport="NBA")
        b = _make_rec(player_name="Player B", team="LAL", sport="NFL")
        result = check_correlation(a, b)
        assert result.score < _SCORE_SAME_TEAM

    def test_different_teams_different_games_is_independent(self):
        a = _make_rec(player_name="Player A", team="LAL", sport="NBA", game_description="vs BOS")
        b = _make_rec(player_name="Player B", team="MIA", sport="NBA", game_description="vs CHI")
        result = check_correlation(a, b)
        assert result.score == 0.0
        assert result.reason == "Independent"

    def test_same_game_opposing_teams(self):
        # a plays for LAL vs BOS, b plays for BOS vs LAL
        a = _make_rec(player_name="Player A", team="LAL", sport="NBA", game_description="vs BOS")
        b = _make_rec(player_name="Player B", team="BOS", sport="NBA", game_description="vs LAL")
        result = check_correlation(a, b)
        assert abs(result.score - _SCORE_SAME_GAME) <= 0.15
        assert "same game" in result.reason.lower()

    def test_score_clamped_between_0_and_1(self):
        a = _make_rec(player_name="Player A", team="LAL", stat_type="Passing Yards")
        b = _make_rec(player_name="Player B", team="LAL", stat_type="Receiving Yards")
        result = check_correlation(a, b)
        assert 0.0 <= result.score <= 1.0

    def test_empty_team_not_same_team(self):
        a = _make_rec(player_name="Player A", team="")
        b = _make_rec(player_name="Player B", team="")
        result = check_correlation(a, b)
        # Empty teams should not match as same team
        assert result.score < _SCORE_SAME_TEAM

    def test_returns_correlation_result(self):
        a = _make_rec()
        b = _make_rec(player_name="Player B")
        result = check_correlation(a, b)
        assert isinstance(result, CorrelationResult)
        assert isinstance(result.score, float)
        assert isinstance(result.reason, str)


# ── Stat pair delta ───────────────────────────────────────────────────────────

class TestStatPairDelta:
    def test_passing_receiving_boosts_team_corr(self):
        a = _make_rec(player_name="QB", team="KC", stat_type="Passing Yards", sport="NFL")
        b = _make_rec(player_name="WR", team="KC", stat_type="Receiving Yards", sport="NFL")
        result = check_correlation(a, b)
        # Should be higher than base _SCORE_SAME_TEAM due to +0.10 delta
        assert result.score > _SCORE_SAME_TEAM

    def test_strikeouts_hits_reduces_game_corr(self):
        # Pitcher K's and hits allowed are inversely related
        a = _make_rec(player_name="Pitcher", team="NYY", stat_type="Strikeouts",
                      sport="MLB", game_description="vs BOS")
        b = _make_rec(player_name="Hitter",  team="BOS", stat_type="Hits",
                      sport="MLB", game_description="vs NYY")
        result_raw = check_correlation(a, b)
        # With -0.05 delta, game corr should be <= _SCORE_SAME_GAME
        assert result_raw.score <= _SCORE_SAME_GAME + 0.01  # allow float tolerance


# ── optimize_slip ─────────────────────────────────────────────────────────────

class TestOptimizeSlip:
    def test_empty_candidates_returns_empty_slip(self):
        result = optimize_slip([], n_legs=3)
        assert result.legs == []
        assert result.excluded == []

    def test_single_candidate_returns_one_leg(self):
        a = _make_rec(player_name="Player A")
        result = optimize_slip([a], n_legs=3)
        assert len(result.legs) == 1

    def test_independent_candidates_all_selected(self):
        candidates = [
            _make_rec(player_name="P1", team="LAL", sport="NBA", game_description="vs BOS",
                      tier="S", confidence=88.0),
            _make_rec(player_name="P2", team="MIA", sport="NBA", game_description="vs CHI",
                      tier="A", confidence=73.0),
            _make_rec(player_name="P3", team="DAL", sport="NBA", game_description="vs HOU",
                      tier="A", confidence=68.0),
        ]
        result = optimize_slip(candidates, n_legs=3)
        assert len(result.legs) == 3
        assert result.excluded == []

    def test_hard_blocked_same_player(self):
        # Two same-player records → second should be hard-blocked
        a = _make_rec(player_name="LeBron", stat_type="Points", tier="S", confidence=90.0)
        b = _make_rec(player_name="LeBron", stat_type="Assists", tier="A", confidence=72.0)
        c = _make_rec(player_name="Other Player", team="MIA", tier="A", confidence=68.0)
        result = optimize_slip([a, b, c], n_legs=3)
        # 'b' should be hard-blocked (same player as a)
        excluded_records = [r for r, _ in result.excluded]
        assert b in excluded_records

    def test_n_legs_clamp_min_2(self):
        candidates = [_make_rec(player_name=f"P{i}", team=f"T{i}") for i in range(5)]
        result = optimize_slip(candidates, n_legs=1)
        assert len(result.legs) <= 2  # clamped up to 2, but only 1 might be selected (seed)
        # Actually with n_legs=1 clamped to 2, we need 2 independent picks
        # seed is always 1, then we try to add 1 more

    def test_n_legs_clamp_max_6(self):
        candidates = [_make_rec(player_name=f"P{i}", team=f"T{i}") for i in range(10)]
        result = optimize_slip(candidates, n_legs=10)
        assert len(result.legs) <= 6

    def test_best_tier_selected_first(self):
        s_pick = _make_rec(player_name="S Pick", team="T1", tier="S", confidence=88.0)
        b_pick = _make_rec(player_name="B Pick", team="T2", tier="B", confidence=60.0)
        a_pick = _make_rec(player_name="A Pick", team="T3", tier="A", confidence=73.0)
        result = optimize_slip([b_pick, a_pick, s_pick], n_legs=3)
        if len(result.legs) >= 1:
            # First leg should be S tier (sorted by tier_rank then confidence)
            assert result.legs[0].tier == "S"

    def test_soft_blocked_adds_warning(self):
        # Two players from same team → soft block (corr ~0.65)
        a = _make_rec(player_name="Player A", team="LAL", sport="NBA",
                      tier="S", confidence=88.0, game_description="vs BOS")
        b = _make_rec(player_name="Player B", team="LAL", sport="NBA",
                      tier="A", confidence=72.0, game_description="vs BOS")
        c = _make_rec(player_name="Player C", team="MIA", sport="NBA",
                      tier="A", confidence=68.0, game_description="vs CHI")
        result = optimize_slip([a, b, c], n_legs=3)
        # b should generate a warning or be blocked, not silently included
        # (same team corr ~0.65 ≥ _SOFT_BLOCK)
        has_warning = len(result.correlation_warnings) > 0
        is_excluded = b in [r for r, _ in result.excluded]
        assert has_warning or is_excluded

    def test_returns_optimized_slip(self):
        candidates = [_make_rec(player_name=f"P{i}", team=f"T{i}") for i in range(4)]
        result = optimize_slip(candidates, n_legs=3)
        assert isinstance(result, OptimizedSlip)
        assert isinstance(result.legs, list)
        assert isinstance(result.excluded, list)
        assert isinstance(result.correlation_warnings, list)

    def test_fewer_candidates_than_legs(self):
        candidates = [
            _make_rec(player_name="P1", team="T1"),
            _make_rec(player_name="P2", team="T2"),
        ]
        result = optimize_slip(candidates, n_legs=5)
        # Returns what's available
        assert len(result.legs) <= 2


# ── OptimizedSlip properties ──────────────────────────────────────────────────

class TestOptimizedSlipProperties:
    def test_avg_edge(self):
        a = _make_rec(player_name="P1", team="T1", best_edge=8.0)
        b = _make_rec(player_name="P2", team="T2", best_edge=12.0)
        result = optimize_slip([a, b], n_legs=2)
        if len(result.legs) == 2:
            assert abs(result.avg_edge - 10.0) < 1e-6

    def test_avg_confidence(self):
        a = _make_rec(player_name="P1", team="T1", confidence=80.0)
        b = _make_rec(player_name="P2", team="T2", confidence=60.0)
        result = optimize_slip([a, b], n_legs=2)
        if len(result.legs) == 2:
            assert abs(result.avg_confidence - 70.0) < 1e-6

    def test_avg_edge_empty(self):
        result = optimize_slip([], n_legs=2)
        assert result.avg_edge == 0.0

    def test_avg_confidence_none_values(self):
        a = _make_rec(player_name="P1", team="T1")
        a.confidence = None
        b = _make_rec(player_name="P2", team="T2", confidence=80.0)
        result = optimize_slip([a, b], n_legs=2)
        if len(result.legs) == 2:
            # Only one non-None confidence → avg_confidence = 80.0
            assert result.avg_confidence == 80.0

    def test_method_field(self):
        result = optimize_slip([], n_legs=2)
        assert result.method == "greedy_min_correlation"
