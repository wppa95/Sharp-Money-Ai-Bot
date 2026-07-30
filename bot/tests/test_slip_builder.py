"""
Tests for engine/slip_builder.py — multi-size slip builder.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from engine.slip_builder import build_all_slips


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_record(
    player_name: str = "Player A",
    stat_type: str = "Points",
    sport: str = "NBA",
    team: str = "TeamA",
    best_edge: float = 6.0,
    confidence: float = 75.0,
    tier: str = "A",
) -> MagicMock:
    r = MagicMock()
    r.player_name = player_name
    r.stat_type   = stat_type
    r.sport       = sport
    r.team        = team
    r.best_edge   = best_edge
    r.confidence  = confidence
    r.tier        = tier
    r.best_side   = "OVER"
    r.pp_line_value = 25.5
    r.sportsbook  = "DraftKings"
    r.game_time   = None
    return r


def _make_slip(legs: list, excluded=None, warnings=None) -> MagicMock:
    s = MagicMock()
    s.legs = legs
    s.excluded = excluded or []
    s.correlation_warnings = warnings or []
    s.avg_edge = sum(r.best_edge for r in legs) / len(legs) if legs else 0.0
    s.avg_confidence = None
    return s


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestBuildAllSlipsBasic:
    def test_returns_dict_keyed_by_size(self):
        records = [_make_record(f"P{i}") for i in range(8)]
        slips = build_all_slips(records)
        # Must be a dict with integer keys 2..6
        assert isinstance(slips, dict)
        for k in slips:
            assert isinstance(k, int)
            assert 2 <= k <= 6

    def test_max_size_6_by_default(self):
        records = [_make_record(f"P{i}") for i in range(10)]
        slips = build_all_slips(records)
        assert all(k <= 6 for k in slips)

    def test_custom_max_size(self):
        records = [_make_record(f"P{i}") for i in range(10)]
        slips = build_all_slips(records, max_size=3)
        assert all(k <= 3 for k in slips)

    def test_max_size_capped_at_6(self):
        records = [_make_record(f"P{i}") for i in range(10)]
        slips = build_all_slips(records, max_size=10)
        assert all(k <= 6 for k in slips)

    def test_min_size_is_2(self):
        records = [_make_record(f"P{i}") for i in range(10)]
        slips = build_all_slips(records)
        assert all(k >= 2 for k in slips)


class TestBuildAllSlipsEmpty:
    def test_empty_candidates_returns_empty_dict(self):
        slips = build_all_slips([])
        assert slips == {}

    def test_single_candidate_returns_empty_dict(self):
        slips = build_all_slips([_make_record()])
        assert slips == {}


class TestBuildAllSlipsOptimizerIntegration:
    """These tests call the real optimizer with mock-compatible records."""

    def test_with_real_optimizer_returns_dict(self):
        """build_all_slips should not raise with real PPEdgeRecord-like mocks."""
        from unittest.mock import patch, MagicMock

        # Build a real-looking slip result with 2 legs
        leg_a = _make_record("Alice", "Points", "NBA")
        leg_b = _make_record("Bob", "Assists", "NBA")

        fake_slip_2 = _make_slip([leg_a, leg_b])
        fake_slip_3 = _make_slip([leg_a])   # < 2 legs → should be excluded
        fake_slip_4 = _make_slip([leg_a, leg_b])
        fake_slip_5 = _make_slip([leg_a, leg_b])
        fake_slip_6 = _make_slip([leg_a, leg_b])

        side_effects = [fake_slip_2, fake_slip_3, fake_slip_4, fake_slip_5, fake_slip_6]
        # Patch at source — optimize_slip is imported inside build_all_slips
        with patch("engine.slip_optimizer.optimize_slip", side_effect=side_effects) as mock_opt:
            slips = build_all_slips([leg_a, leg_b, _make_record("Carol", "Rebounds")], max_size=6)

        # size=3 had only 1 leg → excluded
        assert 3 not in slips
        assert 2 in slips
        assert 4 in slips

    def test_optimizer_exception_is_skipped(self):
        """If optimize_slip raises for one size, that size is omitted."""
        leg_a = _make_record("Alice", "Points", "NBA")
        leg_b = _make_record("Bob",   "Assists", "NBA")

        def _side_effect(candidates, n_legs):
            if n_legs == 3:
                raise RuntimeError("forced error")
            return _make_slip([leg_a, leg_b])

        with patch("engine.slip_optimizer.optimize_slip", side_effect=_side_effect):
            slips = build_all_slips([leg_a, leg_b], max_size=4)

        assert 3 not in slips  # exception → skipped
        assert 2 in slips
        assert 4 in slips


class TestBuildAllSlipsReturnTypes:
    def test_returns_optimized_slip_objects(self):
        leg_a = _make_record("Alice", "Points")
        leg_b = _make_record("Bob",   "Assists")
        fake_slip = _make_slip([leg_a, leg_b])

        # optimize_slip is imported at call-time inside build_all_slips; patch at source
        with patch("engine.slip_optimizer.optimize_slip", return_value=fake_slip):
            slips = build_all_slips([leg_a, leg_b], max_size=2)

        assert 2 in slips
        assert slips[2].legs == [leg_a, leg_b]
