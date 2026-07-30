"""
Tests for engine/player_results.py — WindowStats, PlayerHitRates, compute_hit_rates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from engine.player_results import (
    H2H_MIN_GAMES,
    PlayerHitRates,
    WindowStats,
    _fuzzy_team_match,
    compute_hit_rates,
)


# ── Minimal fake record matching the DB interface ─────────────────────────────

@dataclass
class _Rec:
    player_name:  str
    sport:        str
    stat_type:    str
    game_date:    str          # "YYYY-MM-DD"
    actual_value: float
    opponent:     Optional[str] = None


# ── WindowStats ───────────────────────────────────────────────────────────────

class TestWindowStats:
    def test_over_display_formats_correctly(self):
        w = WindowStats(games=10, over_count=7, under_count=3, hit_rate=0.7, average=2.6)
        d = w.over_display()
        assert "7/10" in d
        assert "70%" in d
        assert "avg 2.6" in d

    def test_over_display_zero_games(self):
        w = WindowStats(games=0, over_count=0, under_count=0, hit_rate=0.0, average=0.0)
        assert w.over_display() == "N/A"


# ── compute_hit_rates ─────────────────────────────────────────────────────────

def _recs(values: list[float], line: float = 2.5, opponent: str = "OPP") -> list[_Rec]:
    """Create fake records newest-first with the given actual values."""
    return [
        _Rec(
            player_name  = "Test Player",
            sport        = "NBA",
            stat_type    = "points",
            game_date    = f"2026-07-{30 - i:02d}",
            actual_value = v,
            opponent     = opponent,
        )
        for i, v in enumerate(values)
    ]


class TestComputeHitRates:
    def test_empty_returns_no_data(self):
        hr = compute_hit_rates([], 2.5)
        assert not hr.has_real_data
        assert hr.total_games == 0
        assert hr.l5 is None
        assert hr.season is None

    def test_five_games_populates_l5_and_season(self):
        recs = _recs([3, 1, 3, 2, 3], line=2.5)  # 3 OVER (3,3,3), 2 UNDER (1,2)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.has_real_data
        assert hr.total_games == 5
        assert hr.l5 is not None
        assert hr.l5.games == 5
        assert hr.l5.over_count == 3
        assert hr.l5.under_count == 2
        assert abs(hr.l5.hit_rate - 0.6) < 0.01
        assert hr.season is not None
        assert hr.season.games == 5

    def test_push_counted_as_under(self):
        """Exact-line value is a push, counted as UNDER."""
        recs = _recs([2.5, 3.0, 3.0], line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l5 is not None
        assert hr.l5.over_count == 2    # only strict > 2.5
        assert hr.l5.under_count == 1

    def test_l5_l10_windows_use_most_recent(self):
        # 15 games; first 5 all OVER (values=3), last 10 mixed
        values = [3.0] * 5 + [1.0] * 10
        recs = _recs(values, line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l5 is not None
        assert hr.l5.over_count == 5        # most recent 5 all over
        assert hr.l10 is not None
        assert hr.l10.over_count == 5       # recent 5 over + 5 under
        assert hr.season is not None
        assert hr.season.games == 15

    def test_l5_none_when_fewer_than_1_result(self):
        hr = compute_hit_rates([], 2.5)
        assert hr.l5 is None

    def test_l5_populated_with_1_game(self):
        recs = _recs([3.0], line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l5 is not None
        assert hr.l5.games == 1

    def test_average_computed_correctly(self):
        recs = _recs([2.0, 4.0, 3.0], line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l5 is not None
        assert abs(hr.l5.average - 3.0) < 0.01

    def test_l10_l20_l30_windows(self):
        recs = _recs([3.0] * 30, line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l10 is not None and hr.l10.games == 10
        assert hr.l20 is not None and hr.l20.games == 20
        assert hr.l30 is not None and hr.l30.games == 30
        assert hr.season is not None and hr.season.games == 30

    def test_season_larger_than_l30(self):
        """Season uses ALL records; l30 is capped at 30."""
        recs = _recs([3.0] * 35, line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l30 is not None and hr.l30.games == 30
        assert hr.season is not None and hr.season.games == 35

    def test_100_pct_over(self):
        recs = _recs([5.0, 5.0, 5.0, 5.0, 5.0], line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l5 is not None
        assert hr.l5.hit_rate == 1.0
        assert hr.l5.under_count == 0

    def test_0_pct_over(self):
        recs = _recs([1.0, 1.0, 1.0, 1.0, 1.0], line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.l5 is not None
        assert hr.l5.hit_rate == 0.0
        assert hr.l5.over_count == 0

    def test_sorting_newest_first(self):
        """Records out of order should still give correct L5 window."""
        recs = [
            _Rec("P", "NBA", "points", "2026-07-01", 1.0),  # oldest
            _Rec("P", "NBA", "points", "2026-07-10", 3.0),  # newest
            _Rec("P", "NBA", "points", "2026-07-05", 3.0),
        ]
        hr = compute_hit_rates(recs, 2.5)
        # Sorted newest first: 07-10 (3.0), 07-05 (3.0), 07-01 (1.0)
        # L5 includes all 3 → 2 over
        assert hr.l5 is not None
        assert hr.l5.over_count == 2

    # ── H2H tests ─────────────────────────────────────────────────────────────

    def test_h2h_none_when_opponent_not_provided(self):
        recs = _recs([3.0] * 5, line=2.5, opponent="BOS")
        hr = compute_hit_rates(recs, 2.5, opponent=None)
        assert hr.h2h is None

    def test_h2h_none_when_fewer_than_min(self):
        recs = _recs([3.0] * 5, line=2.5, opponent="BOS")
        hr = compute_hit_rates(recs, 2.5, opponent="BOS", h2h_min_games=4)
        # only 5 records but all vs "BOS" → 5 ≥ 4 → should exist
        assert hr.h2h is not None

    def test_h2h_none_when_not_enough_matchups(self):
        recs = [
            _Rec("P", "NBA", "pts", "2026-07-10", 3.0, "BOS"),
            _Rec("P", "NBA", "pts", "2026-07-09", 3.0, "BOS"),
            _Rec("P", "NBA", "pts", "2026-07-08", 1.0, "LAL"),
            _Rec("P", "NBA", "pts", "2026-07-07", 1.0, "LAL"),
            _Rec("P", "NBA", "pts", "2026-07-06", 1.0, "LAL"),
        ]
        hr = compute_hit_rates(recs, 2.5, opponent="BOS", h2h_min_games=3)
        # Only 2 BOS games → below min
        assert hr.h2h is None

    def test_h2h_computed_when_enough_matchups(self):
        recs = [
            _Rec("P", "NBA", "pts", "2026-07-10", 3.0, "BOS"),
            _Rec("P", "NBA", "pts", "2026-07-08", 3.0, "BOS"),
            _Rec("P", "NBA", "pts", "2026-07-06", 3.0, "BOS"),
            _Rec("P", "NBA", "pts", "2026-07-04", 1.0, "LAL"),
        ]
        hr = compute_hit_rates(recs, 2.5, opponent="BOS", h2h_min_games=3)
        assert hr.h2h is not None
        assert hr.h2h.games == 3
        assert hr.h2h.over_count == 3
        assert hr.h2h.hit_rate == 1.0

    def test_h2h_fuzzy_opponent_match(self):
        """"Boston Red Sox" should match stored "BOS"."""
        recs = [
            _Rec("P", "MLB", "hits", f"2026-07-{i:02d}", 2.0, "BOS")
            for i in range(1, 6)
        ]
        hr = compute_hit_rates(recs, 1.5, opponent="Boston Red Sox", h2h_min_games=3)
        assert hr.h2h is not None

    def test_player_name_stat_type_from_records(self):
        recs = _recs([3.0] * 3, line=2.5)
        hr = compute_hit_rates(recs, 2.5)
        assert hr.player_name == "Test Player"
        assert hr.stat_type   == "points"

    def test_current_line_stored(self):
        hr = compute_hit_rates(_recs([3.0], 2.5), 2.5)
        assert hr.current_line == 2.5


# ── _fuzzy_team_match ─────────────────────────────────────────────────────────

class TestFuzzyTeamMatch:
    def test_exact_match(self):
        assert _fuzzy_team_match("BOS", "BOS")

    def test_case_insensitive(self):
        assert _fuzzy_team_match("bos", "BOS")
        assert _fuzzy_team_match("BOS", "bos")

    def test_abbreviation_in_full_name(self):
        assert _fuzzy_team_match("BOS", "Boston Red Sox")
        assert _fuzzy_team_match("Boston Red Sox", "BOS")

    def test_close_names(self):
        assert _fuzzy_team_match("Golden State Warriors", "Golden St Warriors")

    def test_clearly_different(self):
        assert not _fuzzy_team_match("BOS", "LAL")
        assert not _fuzzy_team_match("New York Yankees", "Boston Red Sox")
