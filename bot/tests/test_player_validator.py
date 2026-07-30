"""
Tests for engine/player_validator.py

Covers:
  - Empty history → has_supporting_data=False
  - Insufficient history (< min_samples) → has_supporting_data=False
  - Sufficient history → has_supporting_data=True
  - Rate window computations (L5/L10/L20/L30)
  - None returned for windows < _MIN_WINDOW (3)
  - avg_line / min_line_seen / rate_at_or_below
  - season_hit_rate / h2h_hit_rate always None
  - to_json() serialises without error and round-trips
  - rate_summary() display helper
  - validate_player_prop honours min_samples kwarg
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from unittest.mock import MagicMock

from engine.player_validator import validate_player_prop, PlayerPropValidation


# ── Helpers ────────────────────────────────────────────────────────────────────

def _rec(line: float, moved: bool = False) -> MagicMock:
    r = MagicMock()
    r.line_value = line
    r.line_moved = moved
    return r


def _history(lines_moved: list[tuple[float, bool]]) -> list[MagicMock]:
    """Build a fake history list from (line, moved) pairs (most-recent first)."""
    return [_rec(line, moved) for line, moved in lines_moved]


# ── has_supporting_data ────────────────────────────────────────────────────────

def test_empty_history_no_data():
    v = validate_player_prop("Aaron Judge", "Home Runs", 0.5, [])
    assert v.has_supporting_data is False
    assert v.n_history == 0


def test_one_record_no_data():
    v = validate_player_prop("Aaron Judge", "Home Runs", 0.5, [_rec(0.5)])
    assert v.has_supporting_data is False
    assert v.n_history == 1


def test_four_records_below_default_threshold():
    hist = _history([(0.5, False)] * 4)
    v = validate_player_prop("Aaron Judge", "Home Runs", 0.5, hist)
    assert v.has_supporting_data is False   # need 5 by default
    assert v.n_history == 4


def test_five_records_meets_default_threshold():
    hist = _history([(0.5, False)] * 5)
    v = validate_player_prop("Aaron Judge", "Home Runs", 0.5, hist)
    assert v.has_supporting_data is True
    assert v.n_history == 5


def test_custom_min_samples():
    hist = _history([(0.5, False)] * 3)
    v = validate_player_prop("Player", "Hits", 1.5, hist, min_samples=3)
    assert v.has_supporting_data is True


def test_custom_min_samples_not_met():
    hist = _history([(0.5, False)] * 4)
    v = validate_player_prop("Player", "Hits", 1.5, hist, min_samples=10)
    assert v.has_supporting_data is False


# ── Rate window computations ───────────────────────────────────────────────────

def test_l5_rate_all_moved():
    hist = _history([(1.0, True)] * 5)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l5_rate == 1.0


def test_l5_rate_none_moved():
    hist = _history([(1.0, False)] * 5)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l5_rate == 0.0


def test_l5_rate_mixed():
    # 3 moved out of 5
    hist = _history([(1.0, True), (1.0, True), (1.0, True), (1.0, False), (1.0, False)])
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l5_rate == pytest.approx(0.6, abs=0.01)


def test_l5_none_when_window_too_small():
    hist = _history([(1.0, True)] * 2)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l5_rate is None


def test_l10_computed_with_ten_records():
    # First 5 all moved, next 5 none moved → l10 = 0.5
    hist = _history([(1.0, True)] * 5 + [(1.0, False)] * 5)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l10_rate == pytest.approx(0.5, abs=0.01)


def test_l20_none_when_fewer_than_three():
    hist = _history([(1.0, True)] * 2)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l20_rate is None


def test_l30_uses_at_most_30_records():
    # 35 records — l30 should only use first 30
    hist = _history([(1.0, True)] * 30 + [(1.0, False)] * 5)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.l30_rate == 1.0   # all 30 window records moved; extras ignored


# ── Line context ───────────────────────────────────────────────────────────────

def test_avg_line_computed():
    hist = _history([(1.0, False), (2.0, False), (3.0, False)])
    v = validate_player_prop("P", "Hits", 2.0, hist)
    assert v.avg_line == pytest.approx(2.0, abs=0.01)


def test_min_line_seen():
    hist = _history([(3.0, False), (0.5, False), (2.0, False)])
    v = validate_player_prop("P", "Home Runs", 0.5, hist)
    assert v.min_line_seen == 0.5


def test_rate_at_or_below_current():
    # Lines: 0.5, 1.0, 1.5, 2.0, 2.5  — 2 are ≤ 1.0
    hist = _history([(0.5, False), (1.0, False), (1.5, False), (2.0, False), (2.5, False)])
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.rate_at_or_below == pytest.approx(0.4, abs=0.01)


def test_avg_none_when_no_history():
    v = validate_player_prop("P", "Hits", 1.5, [])
    assert v.avg_line is None
    assert v.min_line_seen is None
    assert v.rate_at_or_below is None


# ── Reserved fields ────────────────────────────────────────────────────────────

def test_season_and_h2h_always_none():
    hist = _history([(1.0, False)] * 5)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert v.season_hit_rate is None
    assert v.h2h_hit_rate is None


# ── Serialisation ──────────────────────────────────────────────────────────────

def test_to_json_valid():
    hist = _history([(0.5, True)] * 10)
    v = validate_player_prop("Aaron Judge", "Home Runs", 0.5, hist)
    raw = v.to_json()
    parsed = json.loads(raw)
    assert parsed["n"] == 10
    assert parsed["has_data"] is True
    assert parsed["season"] is None
    assert parsed["h2h"] is None
    assert "l5" in parsed
    assert "l10" in parsed


def test_to_json_empty_history():
    v = validate_player_prop("P", "Hits", 1.0, [])
    parsed = json.loads(v.to_json())
    assert parsed["n"] == 0
    assert parsed["has_data"] is False
    assert parsed["l5"] is None


# ── Display helper ─────────────────────────────────────────────────────────────

def test_rate_summary_no_history():
    v = validate_player_prop("P", "Hits", 1.0, [])
    assert "no history" in v.rate_summary()


def test_rate_summary_with_data():
    hist = _history([(0.5, True)] * 10)
    v = validate_player_prop("P", "Home Runs", 0.5, hist)
    s = v.rate_summary()
    assert "n=10" in s
    assert "L5=" in s


# ── reason string ──────────────────────────────────────────────────────────────

def test_reason_no_history():
    v = validate_player_prop("P", "Hits", 1.0, [])
    assert "first appearance" in v.reason.lower() or "no history" in v.reason.lower()


def test_reason_insufficient():
    v = validate_player_prop("P", "Hits", 1.0, [_rec(1.0)] * 3)
    assert "insufficient" in v.reason.lower() or "need" in v.reason.lower()


def test_reason_has_data():
    hist = _history([(1.0, False)] * 5)
    v = validate_player_prop("P", "Hits", 1.0, hist)
    assert "supporting data" in v.reason.lower()
