"""
tests/test_alert_quality.py — Alert Quality Cleanup Batch regression tests.

Covers all 6 fixes from the Final Alert Quality Cleanup Batch:
  1. Intelligence block character splitting fix (matchup.reasoning is a string)
  2. Tier / confidence consistency (final validation gate)
  3. Analyst language calibration (_confidence_label thresholds)
  4. Role risk adjustment (bench/volatile gate)
  5. Market Quality vs Bet Quality separation (label copy)
  6. Final recommendation gate (tier downgrade propagates to analyst block)
"""

from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 1. Intelligence block — character splitting fix
# ─────────────────────────────────────────────────────────────────────────────

from alerts_multiplatform import _format_intelligence_block


def _make_trace(
    role_label: str = "Starter",
    role_summary: str = "Starter role, stable minutes, flat usage.",
    role_trend: str = "Flat",
    matchup_label: str = "Neutral",
    matchup_reasoning: str = "Net line movement +0.10 — no significant matchup signal.",
    matchup_signal: int = 0,
    sample_strength: int = 60,
    n: int = 15,
) -> dict:
    return {
        "historical": {
            "sample_strength": sample_strength,
            "n": n,
            "windows": {
                "l5":  {"n": 5,  "hit_rate": 0.60},
                "l10": {"n": 10, "hit_rate": 0.70},
                "l20": None,
            },
        },
        "role": {
            "label":     role_label,
            "stability": "Stable",
            "trend":     role_trend,
            "signal":    5,
            "summary":   role_summary,
        },
        "matchup": {
            "label":     matchup_label,
            "signal":    matchup_signal,
            "reasoning": matchup_reasoning,   # STRING, not list
        },
    }


def test_intelligence_block_matchup_neutral_not_character_split():
    """Matchup reasoning must display as one bullet, not split into N/e/u/t characters."""
    trace = _make_trace(matchup_label="Neutral",
                        matchup_reasoning="Net line movement +0.10 — no significant matchup signal.")
    result = _format_intelligence_block(trace)
    # Should contain "Neutral" as a whole word
    assert "Neutral" in result
    # Must not be character-split
    assert "• N\n" not in result
    assert "• e\n" not in result
    assert "• u\n" not in result


def test_intelligence_block_matchup_reasoning_as_string_shown_as_single_bullet():
    """A string reasoning value must appear as a single <i>• …</i> bullet."""
    reasoning = "Market moved line up 1.5 units — opponent matchup signal is favorable."
    trace = _make_trace(matchup_label="Favorable", matchup_reasoning=reasoning)
    result = _format_intelligence_block(trace)
    # The full reasoning text should appear once
    assert reasoning[:30] in result
    # Only one bullet for this reasoning
    assert result.count("•") <= 2   # at most 1 role + 1 matchup bullet


def test_intelligence_block_matchup_reasoning_as_list_still_works():
    """If reasoning happens to arrive as a list, first element is shown (backward compat)."""
    trace = _make_trace()
    trace["matchup"]["reasoning"] = ["First bullet", "Second bullet"]
    result = _format_intelligence_block(trace)
    assert "First bullet" in result
    # Second element should NOT appear (only first taken)
    assert "Second bullet" not in result


def test_intelligence_block_tough_matchup_no_char_split():
    tough_rsn = "Market moved line down 2.0 units over recent snapshots — opponent matchup signal is tough."
    trace = _make_trace(matchup_label="Tough", matchup_reasoning=tough_rsn,
                        matchup_signal=-10)
    result = _format_intelligence_block(trace)
    assert "Tough" in result
    assert "• T\n" not in result


def test_intelligence_block_empty_reasoning_no_bullet():
    """Empty string reasoning must not produce a blank bullet."""
    trace = _make_trace(matchup_label="Neutral", matchup_reasoning="")
    result = _format_intelligence_block(trace)
    # Neutral label is shown but no bullet for empty text
    assert "Neutral" in result
    assert "• " not in result


def test_intelligence_block_role_starter_shown():
    trace = _make_trace(role_label="Starter", role_trend="Rising")
    result = _format_intelligence_block(trace)
    assert "Starter" in result
    assert "Rising" in result


def test_intelligence_block_role_bench_shown():
    trace = _make_trace(role_label="Bench", role_trend="Falling")
    result = _format_intelligence_block(trace)
    assert "Bench" in result


def test_intelligence_block_none_trace_returns_empty():
    assert _format_intelligence_block(None) == ""
    assert _format_intelligence_block({}) == ""


# ─────────────────────────────────────────────────────────────────────────────
# 2 & 3. Analyst language calibration — _confidence_label thresholds
# ─────────────────────────────────────────────────────────────────────────────

from engine.analyst import _confidence_label, build_analyst_from_alert_parts


def test_confidence_label_95_plus_is_elite():
    assert _confidence_label(95) == "Elite confidence signal"
    assert _confidence_label(100) == "Elite confidence signal"


def test_confidence_label_80_to_94_is_high():
    assert _confidence_label(80) == "High-confidence signal"
    assert _confidence_label(94) == "High-confidence signal"


def test_confidence_label_65_to_79_is_strong_but_monitored():
    assert _confidence_label(65) == "Strong but monitored signal"
    assert _confidence_label(79) == "Strong but monitored signal"


def test_confidence_label_55_to_64_is_moderate():
    assert _confidence_label(55) == "Moderate signal"
    assert _confidence_label(64) == "Moderate signal"


def test_confidence_label_below_55_is_low():
    assert _confidence_label(54) == "Low-confidence signal"
    assert _confidence_label(0) == "Low-confidence signal"


def test_analyst_narrative_79_conf_not_says_highest_confidence():
    """Jazz Chisholm scenario: S-tier 79/100 must NOT say 'highest-confidence'."""
    narrative = build_analyst_from_alert_parts(
        player_name   = "Jazz Chisholm",
        stat_type     = "hits",
        sport         = "MLB",
        line          = 1.5,
        decision_rec  = "OVER",
        decision_tier = "S",
        confidence    = 79,
    )
    assert "highest-confidence" not in narrative.recommended_because.lower()
    # Should contain the calibrated label instead
    assert "Strong but monitored" in narrative.recommended_because


def test_analyst_narrative_95_conf_uses_elite_language():
    narrative = build_analyst_from_alert_parts(
        player_name   = "Shohei Ohtani",
        stat_type     = "strikeouts",
        sport         = "MLB",
        line          = 8.5,
        decision_rec  = "OVER",
        decision_tier = "S",
        confidence    = 96,
    )
    assert "Elite confidence" in narrative.recommended_because


def test_analyst_narrative_80_conf_uses_high_language():
    narrative = build_analyst_from_alert_parts(
        player_name   = "LeBron James",
        stat_type     = "points",
        sport         = "NBA",
        line          = 25.5,
        decision_rec  = "OVER",
        decision_tier = "A",
        confidence    = 83,
    )
    assert "High-confidence" in narrative.recommended_because


def test_analyst_narrative_moderate_conf_uses_moderate_language():
    narrative = build_analyst_from_alert_parts(
        player_name   = "Player X",
        stat_type     = "rebounds",
        sport         = "NBA",
        line          = 7.5,
        decision_rec  = "UNDER",
        decision_tier = "B",
        confidence    = 60,
    )
    assert "Moderate" in narrative.recommended_because


# ─────────────────────────────────────────────────────────────────────────────
# 4. Role risk adjustment + 5. Final tier validation gate
# ─────────────────────────────────────────────────────────────────────────────

from alerts_multiplatform import _validate_final_tier


def _role_trace(role_label: str, stability: str = "Stable") -> dict:
    return {
        "role": {
            "label":     role_label,
            "stability": stability,
            "trend":     "Flat",
            "signal":    -5 if role_label == "Bench" else 5,
            "summary":   f"{role_label} role.",
        },
        "matchup": {"label": "Neutral", "signal": 0, "reasoning": ""},
    }


def test_validate_final_tier_s_low_conf_downgrades_to_a():
    """S-tier with confidence < 80 must be downgraded to A."""
    result = _validate_final_tier("S", 79, None)
    assert result == "A"


def test_validate_final_tier_s_conf_79_with_no_trace():
    """S-tier + 79 conf → A, even without intelligence trace."""
    assert _validate_final_tier("S", 79, {}) == "A"


def test_validate_final_tier_s_conf_80_passes():
    """S-tier with confidence == 80 is on the boundary — should pass."""
    assert _validate_final_tier("S", 80, None) == "S"


def test_validate_final_tier_s_conf_95_passes():
    assert _validate_final_tier("S", 95, None) == "S"


def test_validate_final_tier_s_bench_under_90_downgrades():
    """S-tier + Bench role + conf 85 (< 90) → A."""
    trace = _role_trace("Bench", "Volatile")
    result = _validate_final_tier("S", 85, trace)
    assert result == "A"


def test_validate_final_tier_s_bench_conf_90_or_above_passes():
    """S-tier + Bench + conf 90 → S (exceptional confidence)."""
    trace = _role_trace("Bench", "Volatile")
    result = _validate_final_tier("S", 90, trace)
    assert result == "S"


def test_validate_final_tier_a_bench_volatile_low_conf_downgrades_to_b():
    """A-tier + Bench + Volatile + conf < 65 → B."""
    trace = _role_trace("Bench", "Volatile")
    result = _validate_final_tier("A", 60, trace)
    assert result == "B"


def test_validate_final_tier_a_bench_volatile_conf_65_stays_a():
    """A-tier + Bench + Volatile + conf == 65 — boundary, stays A."""
    trace = _role_trace("Bench", "Volatile")
    result = _validate_final_tier("A", 65, trace)
    assert result == "A"


def test_validate_final_tier_a_bench_stable_stays_a():
    """A-tier + Bench but Stable minutes — no extra downgrade."""
    trace = _role_trace("Bench", "Stable")
    result = _validate_final_tier("A", 60, trace)
    assert result == "A"


def test_validate_final_tier_starter_role_no_penalty():
    """Starter role with high confidence should never be downgraded."""
    trace = _role_trace("Starter", "Stable")
    assert _validate_final_tier("S", 82, trace) == "S"


def test_validate_final_tier_b_tier_unchanged():
    """B-tier is already the lowest actionable tier — no further downgrade."""
    trace = _role_trace("Bench", "Volatile")
    assert _validate_final_tier("B", 50, trace) == "B"


def test_validate_final_tier_pass_unchanged():
    assert _validate_final_tier("PASS", 30, None) == "PASS"


def test_validate_final_tier_unknown_tier_unchanged():
    assert _validate_final_tier("BLOCK", 50, None) == "BLOCK"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Market Quality vs Bet Quality separation
# ─────────────────────────────────────────────────────────────────────────────

from alerts_multiplatform import _format_market_quality_block
from engine.ud_scoring import MarketQuality, MarketQualityLabel


def _make_market_quality(label_str: str, score: int) -> MarketQuality:
    label = MarketQualityLabel(label_str)
    return MarketQuality(label=label, score=score, reasons=("High-floor stat",))


def test_market_quality_block_contains_how_reliable_label():
    mq = _make_market_quality("HIGH", 72)
    result = _format_market_quality_block(mq)
    assert "How reliable is the market data?" in result


def test_market_quality_block_elite_shows_label():
    mq = _make_market_quality("ELITE", 90)
    result = _format_market_quality_block(mq)
    assert "ELITE" in result
    assert "🥇" in result


def test_market_quality_block_none_returns_empty():
    assert _format_market_quality_block(None) == ""


def test_decision_block_bet_quality_label_present():
    """The Bet Quality label must appear in the recommendation block."""
    from alerts_multiplatform import _format_decision_block
    import types

    decision = types.SimpleNamespace(
        recommendation        = "OVER",
        recommendation_emoji  = lambda: "✅",
        tier_display          = lambda: "🟢 A Tier",
        confidence_display    = lambda: "<code>82/100</code>",
        reason                = "L5: 80% (4/5) • A-tier",
        avg_vs_line_display   = lambda: "+0.5",
        window_display        = None,
        l5_games=5,   l5_over=4,  l5_under=1,  l5_hit_rate=0.80, l5_avg=0.5,
        l10_games=10, l10_over=7, l10_under=3, l10_hit_rate=0.70, l10_avg=0.3,
        l20_games=None, l20_over=None, l20_under=None, l20_hit_rate=None, l20_avg=None,
        l30_games=None, l30_over=None, l30_under=None, l30_hit_rate=None, l30_avg=None,
        season_games=None, season_over=None, season_under=None, season_hit_rate=None, season_avg=None,
        h2h_games=None, h2h_over=None, h2h_under=None, h2h_hit_rate=None, h2h_avg=None,
    )
    result = _format_decision_block(decision)
    assert "Bet Quality" in result
    assert "how strong is the actual recommendation" in result.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Final recommendation gate — tier downgrade note in analyst block
# ─────────────────────────────────────────────────────────────────────────────

from alerts_multiplatform import _format_analyst_inline_block


def _simple_decision(tier: str, conf: int, rec: str = "OVER"):
    import types
    return types.SimpleNamespace(
        recommendation = rec,
        decision_tier  = tier,
        confidence     = conf,
    )


def test_analyst_block_tier_note_when_downgraded():
    """When tier is downgraded (S→A), a ⚠️ note must appear in the analyst block."""
    decision = _simple_decision("S", 75, "OVER")
    result = _format_analyst_inline_block(
        player_name        = "Jazz Chisholm",
        stat_type          = "hits",
        sport              = "MLB",
        line               = 1.5,
        score              = None,
        decision           = decision,
        intelligence_trace = None,
    )
    # Should contain a downgrade note
    assert "S→A" in result or ("adjusted" in result and "S" in result and "A" in result)


def test_analyst_block_no_note_when_tier_unchanged():
    """When tier is valid (A-tier, conf=82), no downgrade note."""
    decision = _simple_decision("A", 82, "OVER")
    result = _format_analyst_inline_block(
        player_name        = "LeBron James",
        stat_type          = "points",
        sport              = "NBA",
        line               = 25.5,
        score              = None,
        decision           = decision,
        intelligence_trace = None,
    )
    assert "adjusted" not in result.lower() or "S→A" not in result


def test_analyst_block_bench_volatile_downgrade_note():
    """Bench + Volatile + S-tier (conf=85) must show downgrade note."""
    decision = _simple_decision("S", 85, "OVER")
    trace = _role_trace("Bench", "Volatile")
    result = _format_analyst_inline_block(
        player_name        = "Bench Player",
        stat_type          = "points",
        sport              = "NBA",
        line               = 12.5,
        score              = None,
        decision           = decision,
        intelligence_trace = trace,
    )
    assert "adjusted" in result.lower()


def test_analyst_block_pass_returns_empty():
    decision = _simple_decision("PASS", 40, "PASS")
    result = _format_analyst_inline_block(
        player_name        = "X",
        stat_type          = "points",
        sport              = "NBA",
        line               = 10.5,
        score              = None,
        decision           = decision,
        intelligence_trace = None,
    )
    assert result == ""


def test_analyst_block_none_decision_returns_empty():
    result = _format_analyst_inline_block(
        player_name        = "X",
        stat_type          = "points",
        sport              = "NBA",
        line               = 10.5,
        score              = None,
        decision           = None,
        intelligence_trace = None,
    )
    assert result == ""
