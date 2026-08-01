"""
test_confidence_dimensions_wiring.py — Contract tests for #84.

Verifies that dims_from_ud_components() correctly wires UDPropScore + UDBetDecision
into the 4-dimension ConfidenceDimensions contract, and that
candidate_from_ud_decision() uses real dimensions when a score is supplied.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from engine.candidate import (
    ConfidenceDimensions,
    Candidate,
    _data_conf_from_n,
    dims_from_ud_components,
    candidate_from_ud_decision,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mock_score(total: int = 70, n_history: int = 20) -> SimpleNamespace:
    return SimpleNamespace(total=total, n_history=n_history)


def _mock_decision(confidence: int = 75, tier: str = "A", rec: str = "OVER") -> SimpleNamespace:
    return SimpleNamespace(
        confidence       = confidence,
        decision_tier    = tier,
        recommendation   = rec,
        reason           = "test reason",
        hit_rates        = {},
        window_agreement = 0,
    )


# ── _data_conf_from_n ──────────────────────────────────────────────────────────

class TestDataConfFromN:
    def test_zero_samples_returns_twenty(self):
        assert _data_conf_from_n(0) == 20

    def test_below_validation_threshold(self):
        assert _data_conf_from_n(4) == 20

    def test_at_validation_threshold_five(self):
        assert _data_conf_from_n(5) == 40

    def test_nine(self):
        assert _data_conf_from_n(9) == 40

    def test_ten(self):
        assert _data_conf_from_n(10) == 60

    def test_fifteen(self):
        assert _data_conf_from_n(15) == 70

    def test_twenty(self):
        assert _data_conf_from_n(20) == 80

    def test_thirty_plus(self):
        assert _data_conf_from_n(30) == 90
        assert _data_conf_from_n(100) == 90

    def test_monotonic(self):
        """Confidence must never decrease as sample size grows."""
        values = [_data_conf_from_n(n) for n in range(0, 35)]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], f"Not monotonic at n={i}"

    def test_capped_at_ninety(self):
        assert _data_conf_from_n(999) == 90


# ── dims_from_ud_components ────────────────────────────────────────────────────

class TestDimsFromUDComponents:
    def test_returns_confidence_dimensions(self):
        score    = _mock_score(total=70, n_history=20)
        decision = _mock_decision(confidence=76)
        dims = dims_from_ud_components(score, decision)
        assert isinstance(dims, ConfidenceDimensions)

    def test_data_confidence_maps_n_history(self):
        score    = _mock_score(n_history=20)
        decision = _mock_decision(confidence=60)
        dims = dims_from_ud_components(score, decision)
        assert dims.data_confidence == 80  # n=20 → 80

    def test_market_confidence_maps_score_total(self):
        score    = _mock_score(total=65, n_history=10)
        decision = _mock_decision(confidence=50)
        dims = dims_from_ud_components(score, decision)
        assert dims.market_confidence == 65

    def test_betting_edge_scales_from_95_to_100(self):
        score    = _mock_score()
        decision = _mock_decision(confidence=95)  # max UDBetDecision value
        dims = dims_from_ud_components(score, decision)
        assert dims.betting_edge == 100  # 95 * 100 / 95 = 100

    def test_betting_edge_zero(self):
        score    = _mock_score()
        decision = _mock_decision(confidence=0)
        dims = dims_from_ud_components(score, decision)
        assert dims.betting_edge == 0

    def test_betting_edge_mid_range(self):
        score    = _mock_score()
        decision = _mock_decision(confidence=57)  # 57 * 100 / 95 = 60
        dims = dims_from_ud_components(score, decision)
        assert dims.betting_edge == 60

    def test_overall_weighted_formula(self):
        # data=80 (n=20), market=70 (total=70), bet=80 (conf=76→79.xx≈80 scaled)
        # overall = int(0.25*80 + 0.25*70 + 0.50*80) = int(20+17.5+40) = int(77.5) = 77
        score    = _mock_score(total=70, n_history=20)
        decision = _mock_decision(confidence=76)  # 76*100/95 = 80
        dims = dims_from_ud_components(score, decision)
        expected_bet  = min(100, int(76 * 100 / 95))
        expected_over = int(0.25 * 80 + 0.25 * 70 + 0.50 * expected_bet)
        assert dims.overall == expected_over

    def test_all_dims_in_valid_range(self):
        for total in (0, 50, 100):
            for n in (0, 5, 20, 30):
                for conf in (0, 50, 95):
                    dims = dims_from_ud_components(
                        _mock_score(total=total, n_history=n),
                        _mock_decision(confidence=conf),
                    )
                    assert 0 <= dims.data_confidence   <= 100
                    assert 0 <= dims.market_confidence <= 100
                    assert 0 <= dims.betting_edge      <= 100
                    assert 0 <= dims.overall           <= 100

    def test_score_total_capped_at_100(self):
        score    = _mock_score(total=999)  # bogus high value
        decision = _mock_decision(confidence=50)
        dims = dims_from_ud_components(score, decision)
        assert dims.market_confidence == 100

    def test_score_total_floored_at_zero(self):
        score    = _mock_score(total=-10)
        decision = _mock_decision(confidence=50)
        dims = dims_from_ud_components(score, decision)
        assert dims.market_confidence == 0

    def test_missing_attrs_default_to_zero(self):
        """dims_from_ud_components must not raise on objects missing fields."""
        dims = dims_from_ud_components(SimpleNamespace(), SimpleNamespace())
        assert dims.data_confidence == 20   # n=0 → 20
        assert dims.market_confidence == 0
        assert dims.betting_edge == 0


# ── candidate_from_ud_decision with score ──────────────────────────────────────

class TestCandidateFromUDDecisionWithScore:
    def _make_candidate(self, score=None, decision=None) -> Candidate:
        if decision is None:
            decision = _mock_decision(confidence=76, tier="A", rec="OVER")
        return candidate_from_ud_decision(
            player_name = "Shohei Ohtani",
            sport       = "MLB",
            stat_type   = "home_runs",
            line        = 0.5,
            decision    = decision,
            score       = score,
        )

    def test_without_score_uses_proxy(self):
        c = self._make_candidate(score=None)
        assert c.confidence.market_confidence == 50      # neutral proxy
        assert c.confidence.data_confidence   == 70      # tier A → 70

    def test_with_score_uses_real_dims(self):
        score    = _mock_score(total=68, n_history=20)
        decision = _mock_decision(confidence=76, tier="A", rec="OVER")
        c = self._make_candidate(score=score, decision=decision)
        assert c.confidence.market_confidence == 68   # real score.total
        assert c.confidence.data_confidence   == 80   # n=20 → 80

    def test_with_score_betting_edge_scaled(self):
        score    = _mock_score(total=70, n_history=10)
        decision = _mock_decision(confidence=95, tier="A", rec="OVER")
        c = self._make_candidate(score=score, decision=decision)
        assert c.confidence.betting_edge == 100       # 95 * 100 / 95 = 100

    def test_candidate_is_fully_valid(self):
        score    = _mock_score(total=72, n_history=15)
        decision = _mock_decision(confidence=70, tier="A", rec="OVER")
        c = self._make_candidate(score=score, decision=decision)
        assert c.player_name == "Shohei Ohtani"
        assert c.sport       == "MLB"
        assert c.stat_type   == "home_runs"
        assert c.line        == 0.5
        assert c.decision    == "OVER"
        assert isinstance(c.confidence, ConfidenceDimensions)

    def test_score_none_still_produces_valid_candidate(self):
        c = self._make_candidate(score=None)
        assert c.decision in ("OVER", "UNDER", "PASS", "BLOCK")
        assert isinstance(c.confidence, ConfidenceDimensions)
