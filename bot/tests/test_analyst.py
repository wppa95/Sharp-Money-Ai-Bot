"""
test_analyst.py — Contract tests for engine/analyst.py (AI Analyst Layer).

Covers:
  - build_analyst_narrative() pure function contract
  - All four narrative components populated
  - Correct reading from Candidate.decision_trace artifacts
  - format_analyst_telegram() and format_analyst_console() output
  - Candidate.with_analyst_narrative() stores narrative in decision_trace
  - No live data or scoring recalculation
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

import pytest

from engine.analyst import (
    AnalystNarrative,
    build_analyst_narrative,
    format_analyst_telegram,
    format_analyst_console,
)
from engine.candidate import (
    ConfidenceDimensions,
    Candidate,
    candidate_from_ud_decision,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_candidate(
    tier: str = "A",
    decision: str = "OVER",
    risk: str = "MEDIUM",
    data_conf: int = 65,
    mkt_conf: int  = 70,
    bet_conf: int  = 75,
    overall: int   = 71,
    reason: str    = "Strong line movement detected.",
    trace: Optional[dict] = None,
) -> Candidate:
    dec = SimpleNamespace(
        confidence    = int(bet_conf * 95 / 100),
        decision_tier = tier,
        recommendation= decision,
        reason        = reason,
        hit_rates     = {},
        window_agreement = 0,
    )
    score = SimpleNamespace(total=mkt_conf, n_history=15)
    c = candidate_from_ud_decision(
        player_name = "LeBron James",
        sport       = "NBA",
        stat_type   = "points",
        line        = 25.5,
        decision    = dec,
        score       = score,
    )
    if trace:
        c = replace(c, decision_trace={**c.decision_trace, **trace})
    return c


def _make_narrative(
    recommended: str  = "Rec reason.",
    risk: str         = "Risk reason.",
    avoid: str        = "Would avoid if: 1. X.",
    final: str        = "Final recommendation.",
) -> AnalystNarrative:
    return AnalystNarrative(
        recommended_because  = recommended,
        risk_because         = risk,
        would_avoid_because  = avoid,
        final_recommendation = final,
    )


# ── AnalystNarrative dataclass ────────────────────────────────────────────────

class TestAnalystNarrative:
    def test_is_frozen(self):
        n = _make_narrative()
        with pytest.raises((AttributeError, TypeError)):
            n.recommended_because = "changed"

    def test_to_dict_has_all_keys(self):
        n = _make_narrative()
        d = n.to_dict()
        assert "recommended_because"  in d
        assert "risk_because"         in d
        assert "would_avoid_because"  in d
        assert "final_recommendation" in d

    def test_to_dict_values_match(self):
        n = _make_narrative("rec", "risk", "avoid", "final")
        d = n.to_dict()
        assert d["recommended_because"]  == "rec"
        assert d["risk_because"]         == "risk"
        assert d["would_avoid_because"]  == "avoid"
        assert d["final_recommendation"] == "final"


# ── build_analyst_narrative ───────────────────────────────────────────────────

class TestBuildAnalystNarrative:
    def test_returns_analyst_narrative_type(self):
        c = _make_candidate()
        n = build_analyst_narrative(c)
        assert isinstance(n, AnalystNarrative)

    def test_all_components_non_empty(self):
        c = _make_candidate()
        n = build_analyst_narrative(c)
        assert len(n.recommended_because) > 10
        assert len(n.risk_because) > 10
        assert len(n.would_avoid_because) > 10
        assert len(n.final_recommendation) > 10

    def test_player_name_in_some_component(self):
        c = _make_candidate()
        n = build_analyst_narrative(c)
        full = " ".join([n.recommended_because, n.final_recommendation])
        assert "LeBron James" in full

    def test_decision_in_some_component(self):
        c = _make_candidate(decision="OVER")
        n = build_analyst_narrative(c)
        full = " ".join([n.recommended_because, n.final_recommendation])
        assert "OVER" in full or "over" in full.lower()

    def test_pass_decision_reflected_in_final(self):
        c = _make_candidate(tier="PASS", decision="PASS")
        n = build_analyst_narrative(c)
        assert "pass" in n.final_recommendation.lower() or "PASS" in n.final_recommendation

    def test_block_tier_reflected_in_final(self):
        c = _make_candidate(tier="BLOCK", decision="BLOCK")
        n = build_analyst_narrative(c)
        assert "block" in n.final_recommendation.lower() or "not act" in n.final_recommendation.lower()

    def test_risk_level_in_risk_because(self):
        c = _make_candidate(risk="HIGH")
        n = build_analyst_narrative(c)
        # risk_because should mention risk level
        assert "risk" in n.risk_because.lower()

    def test_with_prop_intelligence_trace(self):
        pi_trace = {
            "historical": {
                "sample_strength": 75,
                "n": 20,
                "hit_rate": 0.70,
                "avg_vs_line": 1.5,
                "data_confidence_delta": +10,
            },
            "role": {"label": "Starter", "stability": "Stable", "trend": "Rising", "summary": "Starter role, stable minutes."},
            "matchup": {"label": "Favorable", "signal": +10, "reasoning": "Line moved up 1.5 units."},
        }
        c = _make_candidate(trace={"prop_intelligence": pi_trace})
        n = build_analyst_narrative(c)
        assert len(n.recommended_because) > 20
        assert len(n.final_recommendation) > 10

    def test_with_risk_assessment(self):
        from engine.risk_manager import assess_risk
        c    = _make_candidate()
        risk = assess_risk(c, [])
        n    = build_analyst_narrative(c, risk=risk)
        assert isinstance(n, AnalystNarrative)

    def test_pure_function_no_side_effects(self):
        """build_analyst_narrative must not modify the candidate."""
        c = _make_candidate()
        original_trace = dict(c.decision_trace)
        _ = build_analyst_narrative(c)
        assert c.decision_trace == original_trace

    def test_with_ev_in_trace(self):
        c = _make_candidate(trace={"ev_pct": 3.5})
        n = build_analyst_narrative(c)
        assert "EV" in n.recommended_because or "3.5" in n.recommended_because

    def test_low_confidence_shows_in_risk(self):
        c = _make_candidate(data_conf=30, mkt_conf=30, bet_conf=30)
        n = build_analyst_narrative(c)
        # With low market confidence (30/100) the risk should mention weakness or low scoring
        risk_lower = n.risk_because.lower()
        assert (
            "data quality" in risk_lower
            or "limited" in risk_lower
            or "weak" in risk_lower
            or "30" in n.risk_because
            or "low" in risk_lower
        )

    def test_would_avoid_has_conditions(self):
        c = _make_candidate()
        n = build_analyst_narrative(c)
        # must list at least one avoidance condition
        assert "1." in n.would_avoid_because or "avoid" in n.would_avoid_because.lower()

    def test_accepts_none_risk(self):
        c = _make_candidate()
        n = build_analyst_narrative(c, risk=None)
        assert isinstance(n, AnalystNarrative)

    def test_no_recalculation_of_confidence(self):
        """Analyst must not import or call UDPropScore, score_ud_prop, etc."""
        import engine.analyst as am
        import inspect
        src = inspect.getsource(am)
        forbidden = [
            "score_ud_prop(",
            "make_ud_bet_decision(",
            "from engine.ud_scoring",
            "from .ud_scoring",
            "compute_confidence(",
        ]
        for sym in forbidden:
            assert sym not in src, f"analyst.py must not reference {sym!r}"


# ── format_analyst_telegram ───────────────────────────────────────────────────

class TestFormatAnalystTelegram:
    def test_returns_string(self):
        n = _make_narrative()
        t = format_analyst_telegram(n)
        assert isinstance(t, str)

    def test_contains_all_sections(self):
        n = _make_narrative()
        t = format_analyst_telegram(n)
        assert "Recommended because" in t
        assert "Risk" in t
        assert "Would avoid" in t
        assert "Bottom line" in t

    def test_html_escaped_content(self):
        n = _make_narrative(recommended="<script>alert(1)</script>")
        t = format_analyst_telegram(n)
        assert "<script>" not in t
        assert "&lt;script&gt;" in t

    def test_bold_formatting_present(self):
        n = _make_narrative()
        t = format_analyst_telegram(n)
        assert "<b>" in t

    def test_narrative_content_present(self):
        n = _make_narrative("Custom rec reason", "Custom risk", "Custom avoid", "Custom final")
        t = format_analyst_telegram(n)
        assert "Custom rec reason" in t
        assert "Custom final" in t


# ── format_analyst_console ────────────────────────────────────────────────────

class TestFormatAnalystConsole:
    def test_returns_string(self):
        n = _make_narrative()
        c = format_analyst_console(n)
        assert isinstance(c, str)

    def test_contains_analyst_prefix(self):
        n = _make_narrative()
        c = format_analyst_console(n)
        assert "[ANALYST]" in c

    def test_compact_single_line(self):
        n = _make_narrative()
        c = format_analyst_console(n)
        # Console format should be relatively compact
        assert len(c) < 400


# ── Candidate.with_analyst_narrative ─────────────────────────────────────────

class TestCandidateWithAnalystNarrative:
    def test_returns_new_candidate(self):
        c = _make_candidate()
        n = _make_narrative()
        c2 = c.with_analyst_narrative(n)
        assert isinstance(c2, Candidate)
        assert c2 is not c

    def test_analyst_in_decision_trace(self):
        c  = _make_candidate()
        n  = _make_narrative()
        c2 = c.with_analyst_narrative(n)
        assert "analyst" in c2.decision_trace

    def test_analyst_trace_has_all_keys(self):
        c  = _make_candidate()
        n  = _make_narrative("rec", "risk", "avoid", "final")
        c2 = c.with_analyst_narrative(n)
        analyst = c2.decision_trace["analyst"]
        assert analyst["recommended_because"]  == "rec"
        assert analyst["risk_because"]         == "risk"
        assert analyst["would_avoid_because"]  == "avoid"
        assert analyst["final_recommendation"] == "final"

    def test_confidence_unchanged(self):
        c  = _make_candidate()
        n  = _make_narrative()
        c2 = c.with_analyst_narrative(n)
        assert c2.confidence == c.confidence

    def test_tier_unchanged(self):
        c  = _make_candidate(tier="S")
        n  = _make_narrative()
        c2 = c.with_analyst_narrative(n)
        assert c2.tier == "S"

    def test_existing_trace_preserved(self):
        c  = replace(_make_candidate(), decision_trace={"existing": "value"})
        n  = _make_narrative()
        c2 = c.with_analyst_narrative(n)
        assert "existing" in c2.decision_trace
        assert "analyst"  in c2.decision_trace

    def test_original_unchanged(self):
        c  = _make_candidate()
        n  = _make_narrative()
        c2 = c.with_analyst_narrative(n)
        assert "analyst" not in c.decision_trace

    def test_explanation_service_reads_analyst_trace(self):
        """ExplanationService._render_telegram() should include analyst if stored."""
        from engine.explanation import get_explanation_service, ExplanationFormat
        c  = _make_candidate()
        n  = build_analyst_narrative(c)
        c  = c.with_analyst_narrative(n)
        svc = get_explanation_service()
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert isinstance(text, str)
        assert "Analyst" in text or "bottom line" in text.lower() or "🎯" in text
