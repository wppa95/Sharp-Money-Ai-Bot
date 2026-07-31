"""
Tests for engine/score_validation.py — clamp_score().
"""
import os
import sys
import logging

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.score_validation import clamp_score


class TestClampScore:
    def test_value_within_range_unchanged(self):
        assert clamp_score(50.0, "test") == 50.0

    def test_value_at_min_boundary(self):
        assert clamp_score(0.0, "test") == 0.0

    def test_value_at_max_boundary(self):
        assert clamp_score(100.0, "test") == 100.0

    def test_value_below_min_clamped(self):
        assert clamp_score(-5.0, "score") == 0.0

    def test_value_above_max_clamped(self):
        assert clamp_score(150.0, "score") == 100.0

    def test_none_returns_none(self):
        assert clamp_score(None, "score") is None

    def test_custom_min_max(self):
        assert clamp_score(-1.0, "x", min_=-10.0, max_=10.0) == -1.0
        assert clamp_score(-15.0, "x", min_=-10.0, max_=10.0) == -10.0
        assert clamp_score(20.0, "x", min_=-10.0, max_=10.0) == 10.0

    def test_clamping_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="engine.score_validation"):
            clamp_score(999.0, "ai_confidence")
        assert any("ai_confidence" in r.message for r in caplog.records)

    def test_no_warning_within_range(self, caplog):
        with caplog.at_level(logging.WARNING, logger="engine.score_validation"):
            clamp_score(42.0, "score")
        assert len(caplog.records) == 0

    def test_exact_integer_values(self):
        assert clamp_score(75, "stars", min_=0, max_=100) == 75

    def test_float_precision_preserved(self):
        result = clamp_score(33.333, "pct")
        assert abs(result - 33.333) < 1e-9
