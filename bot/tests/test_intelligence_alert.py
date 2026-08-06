"""
tests/test_intelligence_alert.py — Phase 2: prop intelligence block in Telegram alerts.

Tests:
  • _format_intelligence_block: empty trace, missing keys, role/matchup rendering
  • format_underdog_new_prop_alert: new opponent + intelligence_trace params
  • format_underdog_change_alert: new opponent + intelligence_trace + opening_line params
  • alerts.py deliver_underdog: new params forwarded to formatters
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── _format_intelligence_block ────────────────────────────────────────────────

def test_format_intelligence_block_none_returns_empty():
    from alerts_multiplatform import _format_intelligence_block
    assert _format_intelligence_block(None) == ""


def test_format_intelligence_block_empty_dict_returns_empty():
    from alerts_multiplatform import _format_intelligence_block
    assert _format_intelligence_block({}) == ""


def test_format_intelligence_block_no_labels_returns_empty():
    from alerts_multiplatform import _format_intelligence_block
    trace = {"role": {"label": "", "summary": ""}, "matchup": {"label": "", "reasoning": []}}
    assert _format_intelligence_block(trace) == ""


    def test_format_intelligence_block_role_not_shown():
        """Role/playtime is no longer rendered in intelligence blocks."""
        from alerts_multiplatform import _format_intelligence_block
        trace = {"role": {"label": "Starter", "summary": "stable usage"}, "matchup": {}}
        result = _format_intelligence_block(trace)
        assert "Starter" not in result
        assert "Role" not in result
        assert "stable usage" not in result


    def test_format_intelligence_block_starter_icon_not_shown():
        from alerts_multiplatform import _format_intelligence_block
        trace = {"role": {"label": "Starter", "summary": ""}, "matchup": {}}
        result = _format_intelligence_block(trace)
        assert "🟢" not in result
        assert "Starter" not in result


    def test_format_intelligence_block_bench_icon_not_shown():
        from alerts_multiplatform import _format_intelligence_block
        trace = {"role": {"label": "Bench", "summary": ""}, "matchup": {}}
        result = _format_intelligence_block(trace)
        assert "🔴" not in result
        assert "Bench" not in result


    def test_format_intelligence_block_reserve_icon_not_shown():
        from alerts_multiplatform import _format_intelligence_block
        trace = {"role": {"label": "Reserve", "summary": ""}, "matchup": {}}
        result = _format_intelligence_block(trace)
        assert "🟡" not in result
        assert "Reserve" not in result


    def test_format_intelligence_block_trend_not_shown():
        from alerts_multiplatform import _format_intelligence_block
        trace = {"role": {"label": "Starter", "trend": "Rising", "summary": ""}, "matchup": {}}
        result = _format_intelligence_block(trace)
        assert "Rising" not in result
        assert "Trend" not in result


def test_format_intelligence_block_stable_trend_hidden():
    from alerts_multiplatform import _format_intelligence_block
    trace = {"role": {"label": "Starter", "trend": "Stable", "summary": ""}, "matchup": {}}
    result = _format_intelligence_block(trace)
    # "Stable" alone should not create a Trend row
    assert "Trend" not in result


def test_format_intelligence_block_matchup_favorable():
    from alerts_multiplatform import _format_intelligence_block
    trace = {"role": {}, "matchup": {"label": "Favorable", "reasoning": ["Weak defense"]}}
    result = _format_intelligence_block(trace)
    assert "Favorable" in result
    assert "✅" in result
    assert "Weak defense" in result


def test_format_intelligence_block_matchup_tough():
    from alerts_multiplatform import _format_intelligence_block
    trace = {"role": {}, "matchup": {"label": "Tough", "reasoning": ["Elite pitcher"]}}
    result = _format_intelligence_block(trace)
    assert "⚠️" in result
    assert "Elite pitcher" in result


def test_format_intelligence_block_matchup_neutral():
    from alerts_multiplatform import _format_intelligence_block
    trace = {"role": {}, "matchup": {"label": "Neutral", "reasoning": []}}
    result = _format_intelligence_block(trace)
    assert "➖" in result
    assert "Neutral" in result


def test_format_intelligence_block_reasoning_string_shows_single_bullet():
    """reasoning is always a string in real traces; only first item shown for list inputs."""
    from alerts_multiplatform import _format_intelligence_block
    trace = {
        "role": {},
        "matchup": {
            "label": "Favorable",
            "reasoning": ["reason 1", "reason 2", "reason 3", "reason 4"],
        },
    }
    result = _format_intelligence_block(trace)
    assert "reason 1" in result
    # Only the first element is shown — subsequent items are not rendered
    assert "reason 2" not in result
    assert "reason 4" not in result


def test_format_intelligence_block_full_trace():
    from alerts_multiplatform import _format_intelligence_block
    trace = {
        "role": {
            "label":   "Starter",
            "summary": "high usage",
            "trend":   "Rising",
        },
        "matchup": {
            "label":     "Favorable",
            "reasoning": ["Weak secondary", "Home advantage"],
        },
    }
    result = _format_intelligence_block(trace)
    assert "🔍" in result
    assert "Starter" not in result
    assert "Favorable" in result


def test_format_intelligence_block_missing_keys_graceful():
            from alerts_multiplatform import _format_intelligence_block
            # Only partial keys — should not raise; role is no longer shown
            result = _format_intelligence_block({"role": {"label": "Starter"}})
            assert "Starter" not in result


# ── format_underdog_new_prop_alert: new params ─────────────────────────────────

def _make_score(tier="B", stars=3, total=60, n=10):
    s = MagicMock()
    s.tier           = tier
    s.stars          = stars
    s.total          = total
    s.n_history      = n
    s.stars_display  = "⭐" * stars
    return s


def test_new_prop_alert_accepts_opponent_param():
    from alerts_multiplatform import format_underdog_new_prop_alert
    msg = format_underdog_new_prop_alert(
        "Aaron Judge", "NYY", "MLB", "Home Runs", 0.5,
        opponent="vs BOS",
    )
    assert isinstance(msg, str)
    assert "vs BOS" in msg


def test_new_prop_alert_opponent_none_no_error():
    from alerts_multiplatform import format_underdog_new_prop_alert
    msg = format_underdog_new_prop_alert(
        "Aaron Judge", "NYY", "MLB", "Home Runs", 0.5,
        opponent=None,
    )
    assert isinstance(msg, str)


def test_new_prop_alert_intelligence_trace_rendered():
    from alerts_multiplatform import format_underdog_new_prop_alert
    trace = {
        "role": {"label": "Starter", "summary": "stable"},
        "matchup": {"label": "Favorable", "reasoning": ["Weak bullpen"]},
    }
    msg = format_underdog_new_prop_alert(
        "Aaron Judge", "NYY", "MLB", "Home Runs", 0.5,
        intelligence_trace=trace,
    )
    assert "Starter" not in msg
    assert "Favorable" in msg


def test_new_prop_alert_intelligence_trace_none_no_error():
    from alerts_multiplatform import format_underdog_new_prop_alert
    msg = format_underdog_new_prop_alert(
        "Aaron Judge", "NYY", "MLB", "Home Runs", 0.5,
        intelligence_trace=None,
    )
    assert isinstance(msg, str)


def test_new_prop_alert_all_new_params_together():
    from alerts_multiplatform import format_underdog_new_prop_alert
    trace = {"role": {"label": "Starter"}, "matchup": {"label": "Neutral"}}
    msg = format_underdog_new_prop_alert(
        "Aaron Judge", "NYY", "MLB", "Home Runs", 0.5,
        score=_make_score(),
        opponent="vs BOS",
        intelligence_trace=trace,
    )
    assert "vs BOS" in msg
    assert "Starter" not in msg


# ── format_underdog_change_alert: new params ──────────────────────────────────

    def test_change_alert_accepts_opponent():
        from alerts_multiplatform import format_underdog_change_alert
        msg = format_underdog_change_alert(
            "Player", "TEAM", "MLB", "Hits", 0.5, 1.5,
            opponent="ARI",
        )
        assert isinstance(msg, str) and len(msg) > 20
        assert "MLB" in msg


    def test_change_alert_intelligence_trace_rendered():
        from alerts_multiplatform import format_underdog_change_alert
        trace = {"matchup": {"label": "Tough"}}
        msg = format_underdog_change_alert(
            "Player", "TEAM", "MLB", "Hits", 0.5, 1.5,
            intelligence_trace=trace,
        )
        assert isinstance(msg, str) and "MLB" in msg


    def test_change_alert_opening_line_shown_when_different():
        from alerts_multiplatform import format_underdog_change_alert
        msg = format_underdog_change_alert(
            "Shohei Ohtani", "LAD", "MLB", "Strikeouts", 5.5, 6.5,
            opening_line=5.5,
        )
        assert "6.5" in msg
        assert "5.5" in msg
        assert "Line:" in msg or "Prev" in msg


def test_change_alert_opening_line_hidden_when_same_as_current():
    from alerts_multiplatform import format_underdog_change_alert
    # opening_line == new_line → no "Opened:" row
    msg = format_underdog_change_alert(
        "Shohei Ohtani", "LAD", "MLB", "Strikeouts", 5.5, 5.5,
        opening_line=5.5,
    )
    assert "Opened" not in msg


    def test_change_alert_opening_line_shown_when_different():
        from alerts_multiplatform import format_underdog_change_alert
        msg = format_underdog_change_alert(
            "Shohei Ohtani", "LAD", "MLB", "Strikeouts", 5.5, 6.5,
            opening_line=5.5,
        )
        assert "6.5" in msg
        assert "5.5" in msg
        assert "Line:" in msg or "Prev" in msg


def test_change_alert_total_movement_sign_positive():
    from alerts_multiplatform import format_underdog_change_alert
    msg = format_underdog_change_alert(
        "Shohei Ohtani", "LAD", "MLB", "Strikeouts", 5.5, 6.5,
        opening_line=5.5,
    )
    assert "+1.0" in msg


def test_change_alert_total_movement_sign_negative():
    from alerts_multiplatform import format_underdog_change_alert
    msg = format_underdog_change_alert(
        "Shohei Ohtani", "LAD", "MLB", "Strikeouts", 6.5, 5.5,
        opening_line=6.5,
    )
    assert "-1.0" in msg


def test_change_alert_opening_line_hidden_on_removal():
    from alerts_multiplatform import format_underdog_change_alert
    msg = format_underdog_change_alert(
        "Shohei Ohtani", "LAD", "MLB", "Strikeouts", 6.0, 6.0,
        opening_line=5.0,
        removed=True,
    )
    # opening_line should not show on removals
    assert "Opened" not in msg


# ── deliver_underdog: new params forwarded ────────────────────────────────────

def test_deliver_underdog_accepts_intelligence_trace_param():
    """deliver_underdog signature must include intelligence_trace."""
    import inspect
    from alerts import AlertDelivery

    sig = inspect.signature(AlertDelivery.deliver_underdog)
    assert "intelligence_trace" in sig.parameters


def test_deliver_underdog_accepts_opening_line_param():
    """deliver_underdog signature must include opening_line."""
    import inspect
    from alerts import AlertDelivery

    sig = inspect.signature(AlertDelivery.deliver_underdog)
    assert "opening_line" in sig.parameters


def test_deliver_underdog_accepts_opponent_param():
    """deliver_underdog signature must include opponent."""
    import inspect
    from alerts import AlertDelivery

    sig = inspect.signature(AlertDelivery.deliver_underdog)
    assert "opponent" in sig.parameters
