"""
test_learning_labels.py — Contract tests for #85.

Verifies MissType enum and classify_miss() classification logic, including
the critical rule: Variance errors must NOT be classified as Model errors.
"""

from __future__ import annotations

import pytest
from engine.calibration import MissType, classify_miss


# ── MissType enum ──────────────────────────────────────────────────────────────

class TestMissType:
    def test_all_values_are_strings(self):
        for mt in MissType:
            assert isinstance(mt.value, str)

    def test_expected_members(self):
        values = {mt.value for mt in MissType}
        assert values == {"Model", "Market", "Settlement", "Variance"}

    def test_is_str_subclass(self):
        assert MissType.MODEL == "Model"
        assert MissType.MARKET == "Market"
        assert MissType.SETTLEMENT == "Settlement"
        assert MissType.VARIANCE == "Variance"


# ── classify_miss ──────────────────────────────────────────────────────────────

class TestClassifyMiss:

    # Settlement branch (actual_value is None)
    def test_none_actual_value_is_settlement(self):
        assert classify_miss("OVER", "S",    80, None) == "Settlement"

    def test_none_actual_value_is_settlement_regardless_of_tier(self):
        for tier in ("S", "A", "B", "PASS"):
            for conf in (0, 50, 90):
                result = classify_miss("OVER", tier, conf, None)
                assert result == "Settlement", f"Expected Settlement for tier={tier} conf={conf}"

    # Variance branch (high confidence, high tier)
    def test_s_tier_high_confidence_is_variance(self):
        assert classify_miss("OVER", "S", 75, 1.5) == "Variance"

    def test_a_tier_high_confidence_is_variance(self):
        assert classify_miss("OVER", "A", 80, 2.5) == "Variance"

    def test_confidence_74_is_not_variance_with_a_tier(self):
        # Just below variance threshold — should be Market
        result = classify_miss("OVER", "A", 74, 1.5)
        assert result in ("Market", "Model")  # not Variance

    def test_s_tier_low_confidence_is_not_variance(self):
        # S tier but confidence too low for variance
        result = classify_miss("OVER", "S", 60, 1.5)
        assert result in ("Market", "Model")

    def test_b_tier_high_confidence_is_not_variance(self):
        # High confidence but B tier is not high enough for variance
        result = classify_miss("OVER", "B", 90, 1.5)
        assert result in ("Market", "Model")

    # Market branch (moderate confidence, A/B tier)
    def test_a_tier_moderate_confidence_is_market(self):
        assert classify_miss("OVER", "A", 60, 2.0) == "Market"

    def test_b_tier_moderate_confidence_is_market(self):
        assert classify_miss("OVER", "B", 55, 1.5) == "Market"

    def test_b_tier_exactly_at_threshold_is_market(self):
        assert classify_miss("OVER", "B", 50, 1.5) == "Market"

    def test_b_tier_just_below_threshold_is_model(self):
        assert classify_miss("OVER", "B", 49, 1.5) == "Model"

    # Model branch (low confidence or PASS tier)
    def test_pass_tier_any_confidence_is_model(self):
        for conf in (0, 30, 50, 70):
            result = classify_miss("OVER", "PASS", conf, 1.5)
            assert result == "Model", f"Expected Model for PASS tier conf={conf}"

    def test_b_tier_zero_confidence_is_model(self):
        assert classify_miss("OVER", "B", 0, 1.5) == "Model"

    def test_a_tier_low_confidence_is_model(self):
        assert classify_miss("OVER", "A", 40, 2.0) == "Model"

    # Critical invariant: Variance never equals Model
    def test_variance_never_classified_as_model(self):
        """High-confidence picks that miss must never be punished as Model errors."""
        high_confidence_cases = [
            ("S", 75), ("S", 80), ("S", 90), ("S", 95),
            ("A", 75), ("A", 80), ("A", 90),
        ]
        for tier, conf in high_confidence_cases:
            result = classify_miss("OVER", tier, conf, 1.5)
            assert result != "Model", (
                f"VIOLATION: tier={tier} conf={conf} classified as Model — "
                f"should be Variance"
            )

    # Recommendation field does not affect classification
    def test_recommendation_under_classified_same(self):
        r1 = classify_miss("OVER",  "A", 80, 2.5)
        r2 = classify_miss("UNDER", "A", 80, 2.5)
        assert r1 == r2

    def test_recommendation_pass_classified_same(self):
        r1 = classify_miss("PASS", "B", 55, 1.5)
        r2 = classify_miss("OVER", "B", 55, 1.5)
        assert r1 == r2

    # line_value parameter is optional (reserved for future use)
    def test_line_value_none_does_not_affect_result(self):
        r1 = classify_miss("OVER", "A", 70, 2.0, line_value=None)
        r2 = classify_miss("OVER", "A", 70, 2.0, line_value=1.5)
        assert r1 == r2

    # Boundary conditions
    def test_exactly_at_variance_boundary(self):
        assert classify_miss("OVER", "S", 75, 1.0) == "Variance"
        assert classify_miss("OVER", "A", 75, 1.0) == "Variance"

    def test_exactly_at_market_boundary(self):
        assert classify_miss("OVER", "A", 50, 1.0) == "Market"
        assert classify_miss("OVER", "B", 50, 1.0) == "Market"

    def test_one_below_market_boundary_is_model(self):
        assert classify_miss("OVER", "A", 49, 1.0) == "Model"
        assert classify_miss("OVER", "B", 49, 1.0) == "Model"

    # Return type is always a str
    def test_returns_string(self):
        results = [
            classify_miss("OVER", "S", 80, 1.5),
            classify_miss("OVER", "B", 50, 1.5),
            classify_miss("OVER", "B", 0,  1.5),
            classify_miss("OVER", "S", 80, None),
        ]
        for r in results:
            assert isinstance(r, str)

    def test_returned_values_are_valid_miss_types(self):
        valid = {mt.value for mt in MissType}
        cases = [
            ("OVER", "S", 80,  1.5),
            ("OVER", "A", 70,  2.0),
            ("OVER", "B", 55,  1.5),
            ("OVER", "B", 0,   1.5),
            ("OVER", "S", 80,  None),
        ]
        for rec, tier, conf, actual in cases:
            result = classify_miss(rec, tier, conf, actual)
            assert result in valid, f"Unexpected result: {result!r} for ({rec},{tier},{conf},{actual})"
