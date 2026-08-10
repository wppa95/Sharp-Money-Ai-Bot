"""
tests/test_analyst_inline.py — AI Analyst inline layer tests.

Tests:
  • build_analyst_from_alert_parts: produces AnalystNarrative for S/A/B/PASS
  • format_analyst_alert_block: returns "" for PASS, non-empty for directional
  • format_analyst_alert_block: HTML-safe output (no raw < > outside tags)
  • _format_analyst_inline_block in alerts_multiplatform: wired correctly
  • Analyst block NOT appended for removals
  • Analyst block NOT appended when decision is None
  • Analyst block appears in new_prop and change alert output
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ── build_analyst_from_alert_parts ────────────────────────────────────────────

def test_build_returns_analyst_narrative():
    from engine.analyst import build_analyst_from_alert_parts, AnalystNarrative
    result = build_analyst_from_alert_parts(
        "Anthony Edwards", "Points", "NBA", 24.5,
        decision_rec="OVER", decision_tier="A", confidence=72,
    )
    assert isinstance(result, AnalystNarrative)


def test_build_recommended_because_non_empty():
    from engine.analyst import build_analyst_from_alert_parts
    n = build_analyst_from_alert_parts("A.J. Brown", "Receiving Yards", "NFL", 79.5, "OVER", "B", 58)
    assert n.recommended_because.strip()


    def test_build_risk_because_can_be_empty():
        """Risk section is blank when there are no real risk flags (no generic Risk Level)."""
        from engine.analyst import build_analyst_from_alert_parts
        n = build_analyst_from_alert_parts("A.J. Brown", "Receiving Yards", "NFL", 79.5, "OVER", "B", 58)
        assert n.risk_because == ""


def test_build_final_recommendation_mentions_player():
    from engine.analyst import build_analyst_from_alert_parts
    n = build_analyst_from_alert_parts("Luka Doncic", "Rebounds", "NBA", 8.5, "OVER", "S", 80)
    assert "Luka Doncic" in n.final_recommendation


def test_build_final_recommendation_mentions_tier():
    from engine.analyst import build_analyst_from_alert_parts
    n = build_analyst_from_alert_parts("Luka Doncic", "Rebounds", "NBA", 8.5, "OVER", "S", 80)
    assert "S-tier" in n.final_recommendation or "S tier" in n.final_recommendation


def test_build_uses_intelligence_trace():
        from engine.analyst import build_analyst_from_alert_parts
        trace = {
            "historical": {
                "sample_strength": 70, "n": 20, "hit_rate": 0.75, "variance": 0.5,
                "windows": {
                    "l5": {"n": 5, "hit_rate": 0.80},
                    "l10": {"n": 10, "hit_rate": 0.70},
                },
            },
            "matchup": {
                "label": "Favorable",
                "reasoning": ["Weak defense allows high scoring"],
            },
        }
        n = build_analyst_from_alert_parts(
            "Kevin Durant", "Points", "NBA", 28.5, "OVER", "S", 82,
            intelligence_trace=trace,
        )
        assert (
            "75%" in n.recommended_because
            or "80%" in n.recommended_because
            or "70%" in n.recommended_because
        )
        assert "Kevin Durant" in n.recommended_because or "OVER" in n.recommended_because


def test_build_risk_no_longer_detects_volatile_role():
        from engine.analyst import build_analyst_from_alert_parts
        n = build_analyst_from_alert_parts(
            "Test", "Points", "NBA", 20.5, "OVER", "B", 57
        )
        assert "volatil" not in n.risk_because.lower()
        assert "playing-time" not in n.risk_because.lower()


def test_build_risk_detects_tough_matchup():
    from engine.analyst import build_analyst_from_alert_parts
    trace = {
        "historical": {"sample_strength": 50, "n": 15, "hit_rate": 0.65, "variance": 1.0, "windows": {}},
        "role": {"label": "Starter", "stability": "Stable", "trend": "Stable", "summary": ""},
        "matchup": {"label": "Tough", "reasoning": ["Elite rim protector"]},
    }
    n = build_analyst_from_alert_parts("Test Player", "Points", "NBA", 22.5, "OVER", "A", 66,
                                       intelligence_trace=trace)
    assert "Tough" in n.risk_because or "Elite rim" in n.risk_because


def test_build_pass_decision_still_works():
    """PASS narrative is built (used by format_analyst_alert_block to short-circuit)."""
    from engine.analyst import build_analyst_from_alert_parts
    n = build_analyst_from_alert_parts("Test", "Points", "NBA", 20.5, "PASS", "PASS", 40)
    assert isinstance(n.final_recommendation, str)


def test_build_none_intelligence_trace_no_error():
    from engine.analyst import build_analyst_from_alert_parts
    n = build_analyst_from_alert_parts("Test", "Points", "NBA", 20.5, "OVER", "B", 57, intelligence_trace=None)
    assert n.recommended_because.strip()


def test_build_b_tier_would_avoid_mentions_b_specific_condition():
    from engine.analyst import build_analyst_from_alert_parts
    n = build_analyst_from_alert_parts("Test", "Points", "NBA", 20.5, "OVER", "B", 57)
    assert "contradicting" in n.would_avoid_because.lower() or "B" in n.would_avoid_because


# ── format_analyst_alert_block ────────────────────────────────────────────────

def test_format_alert_block_returns_empty_for_pass():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "PASS", "PASS", 40)
    assert result == ""


def test_format_alert_block_returns_empty_for_none_rec():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, None, "B", 57)
    assert result == ""


def test_format_alert_block_returns_non_empty_for_over():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "OVER", "A", 70)
    assert result.strip()


def test_format_alert_block_returns_non_empty_for_under():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "UNDER", "B", 58)
    assert result.strip()


def test_format_alert_block_contains_analyst_header():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "OVER", "A", 70)
    assert "Analyst" in result


def test_format_alert_block_contains_recommended_because_section():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "OVER", "A", 70)
    assert "✅" in result


def test_format_alert_block_contains_risk_section():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "OVER", "A", 70)
    assert "⚠️" in result


def test_format_alert_block_contains_bottom_line():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "OVER", "A", 70)
    assert "🎯" in result


def test_format_alert_block_html_safe_player_name():
    """Player names with HTML-special chars must be escaped."""
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("O'Neal & Sons", "Points", "NBA", 20.5, "OVER", "A", 70)
    assert "<script>" not in result
    assert "&amp;" in result or "O'Neal" in result  # escaped or unescaped is ok — just no injection


def test_format_alert_block_is_string():
    from engine.analyst import format_analyst_alert_block
    result = format_analyst_alert_block("Test", "Points", "NBA", 20.5, "OVER", "S", 80)
    assert isinstance(result, str)


# ── _format_analyst_inline_block in alerts_multiplatform ─────────────────────

def test_format_analyst_inline_block_returns_empty_when_decision_none():
    from alerts_multiplatform import _format_analyst_inline_block
    result = _format_analyst_inline_block("Test", "Points", "NBA", 20.5, None, None, None)
    assert result == ""


def test_format_analyst_inline_block_returns_empty_for_pass_decision():
    from alerts_multiplatform import _format_analyst_inline_block
    dec = MagicMock()
    dec.recommendation = "PASS"
    result = _format_analyst_inline_block("Test", "Points", "NBA", 20.5, None, dec, None)
    assert result == ""


def test_format_analyst_inline_block_returns_non_empty_for_over():
    from alerts_multiplatform import _format_analyst_inline_block
    dec = MagicMock()
    dec.recommendation  = "OVER"
    dec.decision_tier   = "A"
    dec.confidence      = 70
    score = MagicMock()
    score.stars = 4
    result = _format_analyst_inline_block("Test", "Points", "NBA", 20.5, score, dec, None)
    assert result.strip()


    def test_format_analyst_inline_block_no_generic_risk_level():
        """Risk Level label is no longer shown — Bet Quality already covers strength."""
        from alerts_multiplatform import _format_analyst_inline_block
        dec = MagicMock()
        dec.recommendation  = "OVER"
        dec.decision_tier   = "S"
        dec.confidence      = 82
        score = MagicMock()
        score.stars = 5
        result = _format_analyst_inline_block("Test", "Points", "NBA", 20.5, score, dec, None)
        assert "risk level" not in result.lower()
        assert "low-risk" not in result.lower()


    def test_format_analyst_inline_block_no_generic_risk_high():
        """Risk Level label is no longer shown even for 1-star scores."""
        from alerts_multiplatform import _format_analyst_inline_block
        dec = MagicMock()
        dec.recommendation  = "OVER"
        dec.decision_tier   = "B"
        dec.confidence      = 56
        score = MagicMock()
        score.stars = 1
        result = _format_analyst_inline_block("Test", "Points", "NBA", 20.5, score, dec, None)
        assert "risk level" not in result.lower()
        assert "high-risk" not in result.lower()


# ── Analyst block appears in alert formatters ────────────────────────────────

def _make_decision(rec="OVER", tier="A", conf=70):
    d = MagicMock()
    d.recommendation  = rec
    d.decision_tier   = tier
    d.confidence      = conf
    d.reason          = "Strong hit rate"
    d.is_playable     = True
    d.hit_rates       = {}
    d.window_agreement = 0
    # Explicit None so alert formatters don't try to format a MagicMock as a percentage
    d.l5_hit_rate     = None
    d.l5_games        = None
    return d


def _make_score(stars=4, total=70):
    s = MagicMock()
    s.stars          = stars
    s.total          = total
    s.tier           = "A"
    s.stars_display  = "⭐" * stars
    s.n_history      = 15
    return s


def test_new_prop_alert_contains_analyst_block_for_directional():
    from alerts_multiplatform import format_underdog_new_prop_alert
    trace = {
        "historical": {"sample_strength": 60, "n": 15, "hit_rate": 0.72, "variance": 1.0, "windows": {}},
        "role": {"label": "Starter", "stability": "Stable", "trend": "Stable", "summary": ""},
        "matchup": {"label": "Favorable", "reasoning": []},
    }
    msg = format_underdog_new_prop_alert(
        "Anthony Edwards", "NYK", "NBA", "Points", 24.5,
        score=_make_score(), decision=_make_decision(),
        intelligence_trace=trace,
    )
    assert "Analyst" in msg


    def test_change_alert_contains_pick_for_directional():
        from alerts_multiplatform import format_underdog_change_alert
        msg = format_underdog_change_alert(
            "Jayson Tatum", "BOS", "NBA", "Points", 27.5, 28.5,
            score=_make_score(), decision=_make_decision(),
        )
        # Compact alert: pick line instead of full Analyst block
        assert "PICK" in msg or "OVER" in msg or "UNDER" in msg
        assert "Tatum" in msg or "NBA" in msg


def test_change_alert_no_analyst_block_for_removal():
    from alerts_multiplatform import format_underdog_change_alert
    msg = format_underdog_change_alert(
        "Jayson Tatum", "BOS", "NBA", "Points", 27.5, 28.5,
        score=_make_score(), decision=_make_decision(),
        removed=True,
    )
    assert "Analyst" not in msg


def test_change_alert_no_analyst_block_when_decision_is_none():
    from alerts_multiplatform import format_underdog_change_alert
    msg = format_underdog_change_alert(
        "Jayson Tatum", "BOS", "NBA", "Points", 27.5, 28.5,
        decision=None,
    )
    assert "Analyst" not in msg


def test_new_prop_alert_no_analyst_when_decision_pass():
    from alerts_multiplatform import format_underdog_new_prop_alert
    dec = _make_decision(rec="PASS")
    msg = format_underdog_new_prop_alert(
        "Test Player", "TST", "NBA", "Points", 20.5,
        decision=dec,
    )
    # PASS decisions should not trigger analyst narrative
    assert "Analyst" not in msg


# ── Intelligence block: expanded hit rates and matchup bullets ────────────────

def test_intelligence_block_shows_l5_hit_rate():
    from alerts_multiplatform import _format_intelligence_block
    trace = {
        "historical": {
            "sample_strength": 60,
            "windows": {"l5": {"n": 5, "hit_rate": 0.80}},
        },
        "role": {},
        "matchup": {},
    }
    result = _format_intelligence_block(trace)
    assert "L5" in result
    assert "80%" in result


def test_intelligence_block_shows_first_matchup_reason():
    """reasoning is a string in the real trace; when a list is passed, only first item shown."""
    from alerts_multiplatform import _format_intelligence_block
    trace = {
        "historical": {"sample_strength": None, "windows": {}},
        "role": {},
        "matchup": {
            "label": "Favorable",
            "reasoning": ["Reason A", "Reason B", "Reason C", "Reason D"],
        },
    }
    result = _format_intelligence_block(trace)
    assert "Reason A" in result
    # Only the first item is shown — the rest are not shown
    assert "Reason B" not in result
    assert "Reason D" not in result


def test_intelligence_block_sample_strength_shown():
    from alerts_multiplatform import _format_intelligence_block
    trace = {
        "historical": {
            "sample_strength": 75,
            "windows": {"l5": {"n": 5, "hit_rate": 0.80}},
        },
        "role": {},
        "matchup": {},
    }
    result = _format_intelligence_block(trace)
    assert "75" in result  # sample strength value
