"""
Contract tests — Explanation Service (Framework v3.0 Layer 4).

Verifies:
  • render() produces correct types per format.
  • render() uses ONLY stored decision artifacts (decision_trace, decision_reason).
  • render() does not mutate the Candidate.
  • All three formats produce consistent content.
  • Module singleton is accessible.
"""

import pytest
from engine.candidate import Candidate, ConfidenceDimensions
from engine.explanation import (
    ExplanationService,
    ExplanationFormat,
    get_explanation_service,
)
from datetime import datetime
from types import SimpleNamespace


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dims(**kw) -> ConfidenceDimensions:
    defaults = dict(data_confidence=60, market_confidence=55, betting_edge=70, overall=65)
    defaults.update(kw)
    return ConfidenceDimensions(**defaults)


def _candidate(
    player="Mike Trout",
    sport="MLB",
    stat_type="Hits",
    line=1.5,
    decision="OVER",
    tier="B",
    reason="L10: 70% (7/10)",
    trace=None,
    confidence=None,
) -> Candidate:
    return Candidate(
        player_name       = player,
        player_key        = f"{sport}:{player.lower().replace(' ', '_')}",
        sport             = sport,
        stat_type         = stat_type,
        stat_key          = stat_type.lower(),
        line              = float(line),
        provider          = "Underdog",
        confidence        = confidence or _dims(),
        decision          = decision,
        tier              = tier,
        risk_level        = "MEDIUM",
        decision_reason   = reason,
        decision_trace    = trace or {"l10": {"games": 10, "hit_rate": 0.70}},
    )


# ─────────────────────────────────────────────────────────────────────────────
# ExplanationFormat enum
# ─────────────────────────────────────────────────────────────────────────────

class TestExplanationFormat:
    def test_has_telegram(self):
        assert ExplanationFormat.TELEGRAM is not None

    def test_has_console(self):
        assert ExplanationFormat.CONSOLE is not None

    def test_has_dict(self):
        assert ExplanationFormat.DICT is not None

    def test_values_are_strings(self):
        for f in ExplanationFormat:
            assert isinstance(f.value, str)


# ─────────────────────────────────────────────────────────────────────────────
# ExplanationService.render — return type contract
# ─────────────────────────────────────────────────────────────────────────────

class TestExplanationServiceReturnTypes:
    @pytest.fixture
    def svc(self):
        return ExplanationService()

    def test_telegram_returns_str(self, svc):
        c = _candidate()
        result = svc.render(c, ExplanationFormat.TELEGRAM)
        assert isinstance(result, str)

    def test_console_returns_str(self, svc):
        c = _candidate()
        result = svc.render(c, ExplanationFormat.CONSOLE)
        assert isinstance(result, str)

    def test_dict_returns_dict(self, svc):
        c = _candidate()
        result = svc.render(c, ExplanationFormat.DICT)
        assert isinstance(result, dict)

    def test_default_format_is_telegram(self, svc):
        c = _candidate()
        result = svc.render(c)
        assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM format — content contract
# ─────────────────────────────────────────────────────────────────────────────

class TestTelegramFormat:
    @pytest.fixture
    def svc(self):
        return ExplanationService()

    def test_contains_player_name(self, svc):
        c = _candidate(player="Shohei Ohtani")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "Shohei Ohtani" in text

    def test_contains_sport(self, svc):
        c = _candidate(sport="NFL")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "NFL" in text

    def test_contains_decision(self, svc):
        c = _candidate(decision="UNDER", tier="A")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "UNDER" in text

    def test_contains_tier(self, svc):
        c = _candidate(tier="A", decision="OVER")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "A" in text

    def test_contains_line(self, svc):
        c = _candidate(line=2.5)
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "2.5" in text

    def test_contains_reason(self, svc):
        c = _candidate(reason="Strong window agreement")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "Strong window agreement" in text

    def test_contains_overall_confidence(self, svc):
        c = _candidate(confidence=_dims(overall=82))
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "82" in text

    def test_no_raw_exception_strings(self, svc):
        """Explanation must never leak raw Python exception strings."""
        c = _candidate()
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "KeyError" not in text
        assert "Traceback" not in text

    def test_html_special_chars_escaped(self, svc):
        """Player names with < > & must be escaped for Telegram HTML mode."""
        c = _candidate(player="O'Brien & <Test>")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert "<Test>" not in text   # the < > must be escaped
        assert "&amp;" in text or "&lt;" in text  # HTML entities present

    def test_not_empty(self, svc):
        c = _candidate()
        assert len(svc.render(c, ExplanationFormat.TELEGRAM)) > 20


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLE format — content contract
# ─────────────────────────────────────────────────────────────────────────────

class TestConsoleFormat:
    @pytest.fixture
    def svc(self):
        return ExplanationService()

    def test_contains_player_name(self, svc):
        c = _candidate(player="Ja Morant")
        text = svc.render(c, ExplanationFormat.CONSOLE)
        assert "Ja Morant" in text

    def test_contains_decision(self, svc):
        c = _candidate(decision="UNDER", tier="A")
        text = svc.render(c, ExplanationFormat.CONSOLE)
        assert "UNDER" in text

    def test_contains_tier(self, svc):
        c = _candidate(tier="S", decision="OVER")
        text = svc.render(c, ExplanationFormat.CONSOLE)
        assert "[S]" in text

    def test_contains_confidence(self, svc):
        c = _candidate(confidence=_dims(overall=75))
        text = svc.render(c, ExplanationFormat.CONSOLE)
        assert "75" in text

    def test_single_line(self, svc):
        """Console output should be compact — one logical line."""
        c = _candidate()
        text = svc.render(c, ExplanationFormat.CONSOLE)
        assert "\n" not in text


# ─────────────────────────────────────────────────────────────────────────────
# DICT format — content contract
# ─────────────────────────────────────────────────────────────────────────────

class TestDictFormat:
    @pytest.fixture
    def svc(self):
        return ExplanationService()

    def test_required_keys_present(self, svc):
        c = _candidate()
        d = svc.render(c, ExplanationFormat.DICT)
        for key in ("player", "sport", "stat", "line", "decision", "tier",
                    "risk", "actionable", "confidence", "reason", "trace"):
            assert key in d, f"Missing key: {key}"

    def test_confidence_is_dict(self, svc):
        c = _candidate()
        d = svc.render(c, ExplanationFormat.DICT)
        assert isinstance(d["confidence"], dict)

    def test_confidence_keys(self, svc):
        c = _candidate(confidence=_dims(overall=70, betting_edge=65))
        d = svc.render(c, ExplanationFormat.DICT)
        conf = d["confidence"]
        assert "overall" in conf
        assert "betting_edge" in conf
        assert conf["overall"] == 70

    def test_actionable_bool(self, svc):
        over_c  = _candidate(decision="OVER")
        pass_c  = _candidate(decision="PASS")
        assert svc.render(over_c,  ExplanationFormat.DICT)["actionable"] is True
        assert svc.render(pass_c,  ExplanationFormat.DICT)["actionable"] is False

    def test_values_serialisable(self, svc):
        import json
        c = _candidate()
        d = svc.render(c, ExplanationFormat.DICT)
        json.dumps(d)  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# No live data / no recalculation contract
# ─────────────────────────────────────────────────────────────────────────────

class TestNoLiveDataContract:
    """
    Verify that render() uses only stored artifacts — never calls external
    functions or modifies the Candidate.
    """

    @pytest.fixture
    def svc(self):
        return ExplanationService()

    def test_candidate_not_mutated(self, svc):
        c = _candidate(reason="Original reason")
        original_reason = c.decision_reason
        svc.render(c, ExplanationFormat.TELEGRAM)
        assert c.decision_reason == original_reason

    def test_same_candidate_same_output(self, svc):
        """Same candidate → identical output across multiple calls (no randomness)."""
        c = _candidate()
        out1 = svc.render(c, ExplanationFormat.TELEGRAM)
        out2 = svc.render(c, ExplanationFormat.TELEGRAM)
        assert out1 == out2

    def test_none_confidence_does_not_crash(self, svc):
        c = _candidate(confidence=None)
        # Rendering a candidate with no confidence dims must not raise
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert isinstance(text, str)

    def test_empty_trace_does_not_crash(self, svc):
        c = _candidate(trace={})
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert isinstance(text, str)

    def test_empty_reason_does_not_crash(self, svc):
        c = _candidate(reason="")
        text = svc.render(c, ExplanationFormat.TELEGRAM)
        assert isinstance(text, str)


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_explanation_service_returns_instance(self):
        svc = get_explanation_service()
        assert isinstance(svc, ExplanationService)

    def test_singleton_same_object(self):
        svc1 = get_explanation_service()
        svc2 = get_explanation_service()
        assert svc1 is svc2

    def test_singleton_render_works(self):
        svc = get_explanation_service()
        c = _candidate()
        result = svc.render(c, ExplanationFormat.CONSOLE)
        assert isinstance(result, str)
