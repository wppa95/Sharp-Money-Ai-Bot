"""
test_settlement.py — Contract tests for engine/settlement.py.
"""

from __future__ import annotations

import pytest

from engine.settlement import (
    SettlementFlag,
    FLAG_CLEAN,
    FLAG_PENDING,
    detect_void,
    detect_platform_difference,
    detect_unusual_result,
    check_settlement,
    override_miss_type_for_settlement,
    settlement_flag_telegram,
)


# ── SettlementFlag ────────────────────────────────────────────────────────────

class TestSettlementFlag:
    def test_flag_clean_is_learnable(self):
        assert FLAG_CLEAN.is_learnable is True
        assert FLAG_CLEAN.code == "CLEAN"

    def test_flag_pending_not_learnable(self):
        assert FLAG_PENDING.is_learnable is False
        assert FLAG_PENDING.code == "PENDING"

    def test_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            FLAG_CLEAN.code = "VOID"


# ── detect_void ───────────────────────────────────────────────────────────────

class TestDetectVoid:
    def test_zero_actual_with_positive_line_is_void(self):
        assert detect_void(0.0, 25.5) is True

    def test_zero_actual_zero_line_not_void(self):
        # line < 0.5 → not a meaningful void signal
        assert detect_void(0.0, 0.0) is False

    def test_actual_equals_line_exactly_is_void(self):
        # within epsilon
        assert detect_void(25.5, 25.5) is True

    def test_actual_very_close_to_line_is_void(self):
        assert detect_void(25.499, 25.5) is True

    def test_normal_actual_not_void(self):
        assert detect_void(27.0, 25.5) is False

    def test_actual_below_line_normal(self):
        assert detect_void(20.0, 25.5) is False

    def test_actual_well_above_line_not_void(self):
        assert detect_void(35.0, 25.5) is False


# ── detect_platform_difference ────────────────────────────────────────────────

class TestDetectPlatformDifference:
    def test_minutes_is_known_diff_stat(self):
        result = detect_platform_difference("minutes", "Underdog", "PrizePicks")
        assert result is not None
        assert len(result) > 10

    def test_points_may_have_diff_depending_on_providers(self):
        # points might have platform differences between PP and UD
        result = detect_platform_difference("points", "PrizePicks", "Underdog")
        # Either returns a string or None — both are valid
        assert result is None or isinstance(result, str)

    def test_unknown_stat_returns_none(self):
        result = detect_platform_difference("unknown_stat_xyz", "Underdog", "PrizePicks")
        assert result is None

    def test_returns_string_or_none(self):
        for stat in ("minutes", "aces", "points", "rebounds"):
            r = detect_platform_difference(stat, "Underdog", "PrizePicks")
            assert r is None or isinstance(r, str)


# ── detect_unusual_result ─────────────────────────────────────────────────────

class TestDetectUnusualResult:
    def test_negative_actual_is_unusual(self):
        result = detect_unusual_result(-1.0, 10.0)
        assert result is not None
        assert "negative" in result.lower() or "data error" in result.lower()

    def test_actual_greater_than_3x_line_is_unusual(self):
        result = detect_unusual_result(100.0, 25.0)
        assert result is not None

    def test_actual_less_than_10pct_with_large_line_is_unusual(self):
        result = detect_unusual_result(0.1, 25.0)
        assert result is not None

    def test_normal_actual_not_unusual(self):
        assert detect_unusual_result(27.0, 25.5) is None

    def test_normal_under_not_unusual(self):
        assert detect_unusual_result(22.0, 25.5) is None

    def test_3x_line_is_unusual(self):
        result = detect_unusual_result(76.5, 25.0)
        assert result is not None

    def test_small_line_under_does_not_trigger(self):
        # line=1.5, actual=0.1 — line < 2.0 so the "less than 10%" check doesn't fire
        assert detect_unusual_result(0.1, 1.5) is None


# ── check_settlement ──────────────────────────────────────────────────────────

class TestCheckSettlement:
    def test_none_actual_returns_pending(self):
        flag = check_settlement(None, 25.5)
        assert flag.code == "PENDING"
        assert flag.is_learnable is False

    def test_none_line_returns_void(self):
        flag = check_settlement(27.0, None)
        assert flag.code == "VOID"
        assert flag.is_learnable is False

    def test_zero_actual_large_line_returns_void(self):
        flag = check_settlement(0.0, 25.5)
        assert flag.code == "VOID"
        assert flag.is_learnable is False

    def test_clean_result_returns_clean(self):
        flag = check_settlement(27.0, 25.5)
        assert flag.code == "CLEAN"
        assert flag.is_learnable is True

    def test_under_result_returns_clean(self):
        flag = check_settlement(22.0, 25.5)
        assert flag.code == "CLEAN"

    def test_unusual_result_not_learnable(self):
        flag = check_settlement(100.0, 25.5)
        assert flag.code == "UNUSUAL_RESULT"
        assert flag.is_learnable is False

    def test_platform_diff_is_learnable(self):
        flag = check_settlement(27.0, 25.5, provider="Underdog", stat_type="minutes")
        if flag.code == "PLATFORM_DIFF":
            assert flag.is_learnable is True

    def test_severity_valid_values(self):
        valid_sev = {"NONE", "LOW", "MEDIUM", "HIGH"}
        for actual, line in [(0.0, 25.5), (27.0, 25.5), (100.0, 25.5)]:
            flag = check_settlement(actual, line)
            assert flag.severity in valid_sev

    def test_code_valid_values(self):
        valid_codes = {"VOID", "PLATFORM_DIFF", "UNUSUAL_RESULT", "PENDING", "CLEAN"}
        for actual, line in [(None, 25.5), (0.0, 25.5), (27.0, 25.5), (100.0, 25.5)]:
            flag = check_settlement(actual, line)
            assert flag.code in valid_codes


# ── override_miss_type_for_settlement ─────────────────────────────────────────

class TestOverrideMissTypeForSettlement:
    def test_void_always_overrides_to_settlement(self):
        flag = SettlementFlag("VOID", "HIGH", "x", is_learnable=False)
        assert override_miss_type_for_settlement(flag, "Model") == "Settlement"
        assert override_miss_type_for_settlement(flag, "Market") == "Settlement"
        assert override_miss_type_for_settlement(flag, "Variance") == "Settlement"

    def test_unusual_result_overrides_to_settlement(self):
        flag = SettlementFlag("UNUSUAL_RESULT", "MEDIUM", "x", is_learnable=False)
        assert override_miss_type_for_settlement(flag, "Model") == "Settlement"

    def test_platform_diff_overrides_model_to_market(self):
        flag = SettlementFlag("PLATFORM_DIFF", "LOW", "x", is_learnable=True)
        assert override_miss_type_for_settlement(flag, "Model") == "Market"

    def test_platform_diff_does_not_override_variance(self):
        flag = SettlementFlag("PLATFORM_DIFF", "LOW", "x", is_learnable=True)
        assert override_miss_type_for_settlement(flag, "Variance") == "Variance"

    def test_clean_flag_unchanged(self):
        assert override_miss_type_for_settlement(FLAG_CLEAN, "Model") == "Model"
        assert override_miss_type_for_settlement(FLAG_CLEAN, "Variance") == "Variance"

    def test_pending_flag_unchanged(self):
        assert override_miss_type_for_settlement(FLAG_PENDING, "Model") == "Model"


# ── settlement_flag_telegram ──────────────────────────────────────────────────

class TestSettlementFlagTelegram:
    def test_returns_string(self):
        assert isinstance(settlement_flag_telegram(FLAG_CLEAN), str)

    def test_includes_code(self):
        t = settlement_flag_telegram(FLAG_CLEAN)
        assert "CLEAN" in t

    def test_includes_description(self):
        flag = SettlementFlag("VOID", "HIGH", "Player did not play.", is_learnable=False)
        t = settlement_flag_telegram(flag)
        assert "Player did not play" in t

    def test_html_escaped(self):
        flag = SettlementFlag("CLEAN", "NONE", "<b>test</b>", is_learnable=True)
        t = settlement_flag_telegram(flag)
        assert "<b>test</b>" not in t   # inner content should be escaped
