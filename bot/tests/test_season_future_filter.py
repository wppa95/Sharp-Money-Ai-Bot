"""
Tests for _is_season_future() display filter.

Verifies:
  - Season-long futures are correctly identified and filtered from /picks display
  - Single-game props are NOT filtered
  - Case insensitivity
  - Edge cases (empty string, whitespace, mixed case)
"""

import pytest
from commands import _is_season_future


class TestSeasonFutureDetection:
    """Props that SHOULD be hidden from /picks display."""

    @pytest.mark.parametrize("stat_type", [
        # Exact examples from the user report
        "Season Receiving Yards",
        "Season Receiving TDs",
        "Regular Season Games Started",
        # Variations
        "Season Rushing Yards",
        "Season Passing Yards",
        "Season Touchdowns",
        "Season Home Runs",
        "Season Strikeouts",
        "Season Points",
        "Regular Season Wins",
        "Regular Season Points",
        "Playoff Season Yards",
        "Career Home Runs",
        "Career Points",
        "Career Strikeouts",
    ])
    def test_season_future_detected(self, stat_type: str) -> None:
        assert _is_season_future(stat_type), (
            f"Expected '{stat_type}' to be identified as a season future"
        )

    @pytest.mark.parametrize("stat_type", [
        # All-lowercase variants
        "season receiving yards",
        "regular season games started",
        "career home runs",
        # Mixed case
        "SEASON RUSHING YARDS",
        "Regular SEASON Wins",
    ])
    def test_season_future_case_insensitive(self, stat_type: str) -> None:
        assert _is_season_future(stat_type), (
            f"Expected case-insensitive match for '{stat_type}'"
        )


class TestSingleGameProps:
    """Props that SHOULD appear in /picks (must NOT be filtered)."""

    @pytest.mark.parametrize("stat_type", [
        # MLB
        "Hits",
        "Home Runs",
        "Total Bases",
        "Strikeouts",
        "Walks",
        "Stolen Bases",
        "RBIs",
        "Pitching Outs",
        "Earned Runs Allowed",
        # NBA / WNBA
        "Points",
        "Rebounds",
        "Assists",
        "3-Pointers Made",
        "Fantasy Score",
        "Blocks",
        "Steals",
        "Pts+Reb+Ast",
        # Tennis
        "Games Won",
        "Sets Won",
        "Aces",
        "Double Faults",
        # Esports
        "Kills",
        "Assists",
        "Deaths",
        "Fantasy Points",
        "Headshots",
        "Towers Destroyed",
        # Generic
        "Goals",
        "Shots on Goal",
        "Saves",
        # Props whose name contains "game" but are not season futures
        "Games Won",           # Tennis single-match stat
    ])
    def test_single_game_prop_not_filtered(self, stat_type: str) -> None:
        assert not _is_season_future(stat_type), (
            f"Expected '{stat_type}' to be shown (not filtered as season future)"
        )


class TestEdgeCases:
    def test_empty_string(self) -> None:
        assert not _is_season_future("")

    def test_whitespace_only(self) -> None:
        assert not _is_season_future("   ")

    def test_leading_trailing_whitespace_ignored_for_futures(self) -> None:
        assert _is_season_future("  Season Receiving Yards  ")

    def test_leading_trailing_whitespace_ignored_for_singles(self) -> None:
        assert not _is_season_future("  Hits  ")
