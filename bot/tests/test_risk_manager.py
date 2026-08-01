"""
test_risk_manager.py — Contract tests for engine/risk_manager.py.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from engine.risk_manager import (
    RiskFactor,
    RiskAssessment,
    assess_risk,
    assess_portfolio_risk,
    portfolio_risk_summary,
    RISK_CODES,
    _HIGH_VARIANCE_SPORTS,
    _HIGH_VARIANCE_STATS,
)
from engine.candidate import candidate_from_ud_decision
from types import SimpleNamespace


def _cand(player="LeBron James", sport="NBA", stat="points", tier="A",
          event_key=None, player_key_suffix=""):
    dec = SimpleNamespace(
        confidence=70, decision_tier=tier,
        recommendation="OVER", reason="test",
        hit_rates={}, window_agreement=0,
    )
    score = SimpleNamespace(total=65, n_history=10)
    c = candidate_from_ud_decision(
        player_name=player, sport=sport, stat_type=stat,
        line=25.5, decision=dec, score=score,
    )
    if event_key:
        c = replace(c, event_key=event_key)
    return c


class TestRiskFactor:
    def test_frozen(self):
        rf = RiskFactor(code="CORRELATION", severity="MEDIUM", description="x")
        with pytest.raises((AttributeError, TypeError)):
            rf.code = "OTHER"

    def test_valid_code(self):
        rf = RiskFactor(code="SAME_GAME", severity="HIGH", description="x")
        assert rf.code in RISK_CODES


class TestAssessRisk:
    def test_returns_risk_assessment(self):
        c = _cand()
        ra = assess_risk(c)
        assert isinstance(ra, RiskAssessment)

    def test_player_key_matches(self):
        c = _cand()
        ra = assess_risk(c)
        assert ra.player_key == c.player_key

    def test_valid_composite_risk(self):
        c = _cand()
        ra = assess_risk(c)
        assert ra.composite_risk in ("LOW", "MEDIUM", "HIGH")

    def test_valid_recommendation_adjustment(self):
        c = _cand()
        ra = assess_risk(c)
        assert ra.recommendation_adjustment in ("NONE", "REDUCE", "AVOID")

    def test_high_variance_sport_detected(self):
        c = _cand(sport="MLB", stat="home_runs")
        ra = assess_risk(c)
        codes = [f.code for f in ra.factors]
        assert "HIGH_VARIANCE" in codes

    def test_low_variance_sport_no_hv_factor(self):
        c = _cand(sport="NBA", stat="points")
        ra = assess_risk(c, portfolio=[])
        # NBA is MEDIUM variance — HIGH_VARIANCE factor may still appear for stat
        # but should not appear for NBA itself (only MLB/NFL/NHL are high-var sports)
        sport_factors = [f for f in ra.factors if "sport" in f.description.lower() and "high-variance" in f.description.lower()]
        nba_sport_factors = [f for f in sport_factors if "NBA" in f.description]
        assert len(nba_sport_factors) == 0

    def test_player_dependency_detected(self):
        c1 = _cand("Shohei Ohtani", "MLB", "home_runs")
        c2 = _cand("Shohei Ohtani", "MLB", "strikeouts")
        ra = assess_risk(c1, portfolio=[c2])
        codes = [f.code for f in ra.factors]
        assert "PLAYER_DEPENDENCY" in codes

    def test_same_game_detected(self):
        c1 = _cand("LeBron James", "NBA", "points",  event_key="game-123")
        c2 = _cand("Anthony Davis", "NBA", "rebounds", event_key="game-123")
        ra = assess_risk(c1, portfolio=[c2])
        codes = [f.code for f in ra.factors]
        assert "SAME_GAME" in codes

    def test_no_portfolio_no_game_factors(self):
        c = _cand(sport="NBA", stat="points")
        ra = assess_risk(c, portfolio=None)
        codes = [f.code for f in ra.factors]
        assert "SAME_GAME" not in codes
        assert "PLAYER_DEPENDENCY" not in codes

    def test_position_size_detected_for_3plus(self):
        c0 = _cand("Player0", event_key="game-X")
        c1 = _cand("Player1", event_key="game-X")
        c2 = _cand("Player2", event_key="game-X")
        c3 = _cand("Player3", event_key="game-X")
        ra = assess_risk(c0, portfolio=[c1, c2, c3])
        codes = [f.code for f in ra.factors]
        assert "POSITION_SIZE" in codes

    def test_recommendation_adjustment_none_for_clean(self):
        c = _cand(sport="TENNIS", stat="aces")
        ra = assess_risk(c, portfolio=[])
        # Only HIGH_VARIANCE might appear; no SAME_GAME, no PLAYER_DEPENDENCY
        assert ra.recommendation_adjustment in ("NONE", "REDUCE")

    def test_high_portfolio_risk_returns_avoid(self):
        c1 = _cand("A", event_key="g")
        c2 = _cand("A", event_key="g")  # same player + same game
        c3 = _cand("A", event_key="g")
        c4 = _cand("A", event_key="g")
        ra = assess_risk(c1, portfolio=[c2, c3, c4])
        assert ra.recommendation_adjustment in ("REDUCE", "AVOID")

    def test_factors_are_tuple(self):
        c = _cand()
        ra = assess_risk(c)
        assert isinstance(ra.factors, tuple)

    def test_correlated_players_are_tuple(self):
        c = _cand()
        ra = assess_risk(c)
        assert isinstance(ra.correlated_players, tuple)

    def test_all_factor_codes_valid(self):
        c1 = _cand("X", event_key="g")
        c2 = _cand("X", event_key="g")
        ra = assess_risk(c1, portfolio=[c2])
        for f in ra.factors:
            assert f.code in RISK_CODES
            assert f.severity in ("LOW", "MEDIUM", "HIGH")

    def test_severity_descriptions_non_empty(self):
        c1 = _cand("A", event_key="g")
        c2 = _cand("A", event_key="g")
        ra = assess_risk(c1, portfolio=[c2])
        for f in ra.factors:
            assert len(f.description) > 0


class TestAssessPortfolioRisk:
    def test_same_length_as_input(self):
        candidates = [_cand(f"P{i}") for i in range(5)]
        assessments = assess_portfolio_risk(candidates)
        assert len(assessments) == 5

    def test_all_returns_are_risk_assessments(self):
        candidates = [_cand(f"P{i}") for i in range(3)]
        assessments = assess_portfolio_risk(candidates)
        for a in assessments:
            assert isinstance(a, RiskAssessment)

    def test_empty_portfolio(self):
        assert assess_portfolio_risk([]) == []

    def test_single_candidate(self):
        assessments = assess_portfolio_risk([_cand()])
        assert len(assessments) == 1


class TestPortfolioRiskSummary:
    def test_returns_string(self):
        assessments = assess_portfolio_risk([_cand()])
        s = portfolio_risk_summary(assessments)
        assert isinstance(s, str)

    def test_empty_assessments(self):
        s = portfolio_risk_summary([])
        assert "not applicable" in s.lower() or len(s) > 0

    def test_includes_candidate_count(self):
        assessments = assess_portfolio_risk([_cand(f"P{i}") for i in range(4)])
        s = portfolio_risk_summary(assessments)
        assert "4" in s
