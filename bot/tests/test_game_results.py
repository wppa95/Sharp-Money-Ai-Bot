"""
Tests for providers/game_results.py — game results framework and grade_pp_pick.

Covers:
  - OddsApiResultsProvider._parse with empty list
  - _parse with completed game including scores
  - _parse with in-progress game (has scores array but completed=False)
  - _parse with scheduled game (no scores yet)
  - _parse handles malformed score entries gracefully
  - grade_pp_pick returns NO_DATA when actual_value is None (team-scores-only path)
  - grade_pp_pick WIN: OVER + actual > line
  - grade_pp_pick WIN: UNDER + actual < line
  - grade_pp_pick LOSS: OVER + actual < line
  - grade_pp_pick LOSS: UNDER + actual > line
  - grade_pp_pick PUSH: |actual - line| < tolerance
  - grade_pp_pick with empty game_results still returns NO_DATA (not an error)
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from datetime import datetime

from providers.game_results import (
    GameResult,
    PropGradeResult,
    OddsApiResultsProvider,
    grade_pp_pick,
)


# ---------------------------------------------------------------------------
# Sample API payloads
# ---------------------------------------------------------------------------

COMPLETED_GAME = {
    "id": "game-001",
    "sport_key": "basketball_nba",
    "sport_title": "NBA",
    "home_team": "Los Angeles Lakers",
    "away_team": "Boston Celtics",
    "commence_time": "2025-01-15T19:00:00Z",
    "completed": True,
    "scores": [
        {"name": "Boston Celtics",      "score": "97"},
        {"name": "Los Angeles Lakers",  "score": "99"},
    ],
    "last_update": "2025-01-15T23:00:00Z",
}

IN_PROGRESS_GAME = {
    "id": "game-002",
    "sport_key": "basketball_nba",
    "sport_title": "NBA",
    "home_team": "Golden State Warriors",
    "away_team": "Phoenix Suns",
    "commence_time": "2025-01-15T21:00:00Z",
    "completed": False,
    "scores": [
        {"name": "Phoenix Suns",        "score": "54"},
        {"name": "Golden State Warriors", "score": "61"},
    ],
    "last_update": "2025-01-15T22:30:00Z",
}

SCHEDULED_GAME = {
    "id": "game-003",
    "sport_key": "basketball_nba",
    "home_team": "Chicago Bulls",
    "away_team": "Miami Heat",
    "commence_time": "2025-01-16T19:00:00Z",
    "completed": False,
    "scores": None,
    "last_update": None,
}

MALFORMED_GAME = {
    "id": "game-004",
    "sport_key": "basketball_nba",
    "home_team": "Memphis Grizzlies",
    "away_team": "Minnesota Timberwolves",
    "completed": True,
    "scores": [
        {"name": "Memphis Grizzlies",       "score": "not-a-number"},
        {"name": "Minnesota Timberwolves",  "score": None},
    ],
    "last_update": "2025-01-15T23:00:00Z",
}


# ---------------------------------------------------------------------------
# OddsApiResultsProvider._parse
# ---------------------------------------------------------------------------

class TestParseEmptyList:
    def test_empty_input_returns_empty_list(self):
        provider = OddsApiResultsProvider(api_key="test")
        results = provider._parse([], "basketball_nba")
        assert results == []


class TestParseCompletedGame:
    def test_parse_returns_one_result(self):
        provider = OddsApiResultsProvider(api_key="test")
        results = provider._parse([COMPLETED_GAME], "basketball_nba")
        assert len(results) == 1

    def test_event_string_format(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.event == "Boston Celtics @ Los Angeles Lakers"

    def test_teams_set_correctly(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.away_team == "Boston Celtics"
        assert r.home_team == "Los Angeles Lakers"

    def test_scores_extracted(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.away_score == 97
        assert r.home_score == 99

    def test_status_is_final(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.status == "final"

    def test_completed_at_parsed(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.completed_at is not None
        assert isinstance(r.completed_at, datetime)

    def test_sport_key_stored(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.sport == "basketball_nba"

    def test_source_is_odds_api(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([COMPLETED_GAME], "basketball_nba")[0]
        assert r.source == "odds_api"


class TestParseInProgressGame:
    def test_status_is_in_progress(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([IN_PROGRESS_GAME], "basketball_nba")[0]
        assert r.status == "in_progress"

    def test_scores_are_none_because_not_completed(self):
        """In-progress games have partial scores but we only store final scores."""
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([IN_PROGRESS_GAME], "basketball_nba")[0]
        # away_score and home_score only populated when completed=True
        assert r.away_score is None
        assert r.home_score is None

    def test_completed_at_is_none(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([IN_PROGRESS_GAME], "basketball_nba")[0]
        assert r.completed_at is None


class TestParseScheduledGame:
    def test_status_is_scheduled(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([SCHEDULED_GAME], "basketball_nba")[0]
        assert r.status == "scheduled"

    def test_scores_are_none(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([SCHEDULED_GAME], "basketball_nba")[0]
        assert r.away_score is None
        assert r.home_score is None


class TestParseMalformedScores:
    def test_malformed_scores_do_not_raise(self):
        """Malformed score entries should be silently skipped."""
        provider = OddsApiResultsProvider(api_key="test")
        results = provider._parse([MALFORMED_GAME], "basketball_nba")
        assert len(results) == 1

    def test_malformed_scores_result_in_none_scores(self):
        provider = OddsApiResultsProvider(api_key="test")
        r = provider._parse([MALFORMED_GAME], "basketball_nba")[0]
        # Both scores malformed — neither should be set
        assert r.away_score is None
        assert r.home_score is None


class TestParseMultipleGames:
    def test_multiple_games_all_parsed(self):
        provider = OddsApiResultsProvider(api_key="test")
        results = provider._parse(
            [COMPLETED_GAME, IN_PROGRESS_GAME, SCHEDULED_GAME],
            "basketball_nba",
        )
        assert len(results) == 3

    def test_statuses_mixed_correctly(self):
        provider = OddsApiResultsProvider(api_key="test")
        results = provider._parse(
            [COMPLETED_GAME, IN_PROGRESS_GAME, SCHEDULED_GAME],
            "basketball_nba",
        )
        statuses = {r.status for r in results}
        assert statuses == {"final", "in_progress", "scheduled"}


# ---------------------------------------------------------------------------
# grade_pp_pick — NO_DATA path (team-scores-only source)
# ---------------------------------------------------------------------------

class TestGradePpPickNoData:
    def test_returns_no_data_when_actual_value_is_none(self):
        result = grade_pp_pick(
            player_name  = "LeBron James",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            game_results = [],
        )
        assert result.result == "NO_DATA"

    def test_no_data_contains_correct_player_info(self):
        result = grade_pp_pick(
            player_name  = "Stephen Curry",
            stat_type    = "Assists",
            line_value   = 6.5,
            best_side    = "UNDER",
            game_results = [],
        )
        assert result.player_name == "Stephen Curry"
        assert result.stat_type   == "Assists"
        assert result.line_value  == 6.5
        assert result.best_side   == "UNDER"
        assert result.actual_value is None

    def test_no_data_with_nonempty_game_results(self):
        """Even with game results, NO_DATA is returned because scores are team-level."""
        provider = OddsApiResultsProvider(api_key="test")
        game_results = provider._parse([COMPLETED_GAME], "basketball_nba")
        result = grade_pp_pick(
            player_name  = "LeBron James",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            game_results = game_results,
        )
        assert result.result == "NO_DATA"

    def test_source_indicates_team_only(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 20.0,
            best_side    = "OVER",
            game_results = [],
        )
        assert "team" in result.source.lower() or "odds" in result.source.lower()


# ---------------------------------------------------------------------------
# grade_pp_pick — WIN path
# ---------------------------------------------------------------------------

class TestGradePpPickWin:
    def test_over_win_actual_above_line(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            game_results = [],
            actual_value = 30.0,
        )
        assert result.result == "WIN"

    def test_under_win_actual_below_line(self):
        result = grade_pp_pick(
            player_name  = "Player B",
            stat_type    = "Rebounds",
            line_value   = 8.5,
            best_side    = "UNDER",
            game_results = [],
            actual_value = 6.0,
        )
        assert result.result == "WIN"

    def test_over_win_actual_equal_to_line_plus_epsilon(self):
        result = grade_pp_pick(
            player_name  = "Player C",
            stat_type    = "Assists",
            line_value   = 5.5,
            best_side    = "OVER",
            game_results = [],
            actual_value = 5.5 + 0.5,
        )
        assert result.result == "WIN"

    def test_win_result_stores_actual_value(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            game_results = [],
            actual_value = 30.0,
        )
        assert result.actual_value == 30.0


# ---------------------------------------------------------------------------
# grade_pp_pick — LOSS path
# ---------------------------------------------------------------------------

class TestGradePpPickLoss:
    def test_over_loss_actual_below_line(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            game_results = [],
            actual_value = 20.0,
        )
        assert result.result == "LOSS"

    def test_under_loss_actual_above_line(self):
        result = grade_pp_pick(
            player_name  = "Player B",
            stat_type    = "Rebounds",
            line_value   = 8.5,
            best_side    = "UNDER",
            game_results = [],
            actual_value = 12.0,
        )
        assert result.result == "LOSS"

    def test_loss_result_stores_correct_fields(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            game_results = [],
            actual_value = 20.0,
        )
        assert result.player_name == "Player A"
        assert result.line_value  == 25.5
        assert result.best_side   == "OVER"


# ---------------------------------------------------------------------------
# grade_pp_pick — PUSH path
# ---------------------------------------------------------------------------

class TestGradePpPickPush:
    def test_push_when_actual_equals_line(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.0,
            best_side    = "OVER",
            game_results = [],
            actual_value = 25.0,
        )
        assert result.result == "PUSH"

    def test_push_within_tolerance(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.0,
            best_side    = "OVER",
            game_results = [],
            actual_value = 25.005,   # within 0.01 tolerance
        )
        assert result.result == "PUSH"

    def test_push_with_under_side(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Rebounds",
            line_value   = 8.5,
            best_side    = "UNDER",
            game_results = [],
            actual_value = 8.5,
        )
        assert result.result == "PUSH"

    def test_just_outside_push_tolerance_is_win(self):
        result = grade_pp_pick(
            player_name  = "Player A",
            stat_type    = "Points",
            line_value   = 25.0,
            best_side    = "OVER",
            game_results = [],
            actual_value = 25.02,   # 0.02 > 0.01 tolerance
        )
        assert result.result == "WIN"


# ---------------------------------------------------------------------------
# GameResult dataclass
# ---------------------------------------------------------------------------

class TestGameResultDataclass:
    def test_default_source_is_odds_api(self):
        r = GameResult(
            sport="basketball_nba", event="Celtics @ Lakers",
            away_team="Celtics", home_team="Lakers",
            away_score=97, home_score=99, status="final",
            completed_at=None,
        )
        assert r.source == "odds_api"

    def test_away_at_home_event_format(self):
        r = GameResult(
            sport="basketball_nba", event="Celtics @ Lakers",
            away_team="Celtics", home_team="Lakers",
            away_score=97, home_score=99, status="final",
            completed_at=None,
        )
        assert "@" in r.event


# ---------------------------------------------------------------------------
# PropGradeResult dataclass
# ---------------------------------------------------------------------------

class TestPropGradeResult:
    def test_fields_accessible(self):
        r = PropGradeResult(
            player_name  = "LeBron James",
            stat_type    = "Points",
            line_value   = 25.5,
            best_side    = "OVER",
            actual_value = 30.0,
            result       = "WIN",
            source       = "provided",
        )
        assert r.player_name  == "LeBron James"
        assert r.stat_type    == "Points"
        assert r.line_value   == 25.5
        assert r.best_side    == "OVER"
        assert r.actual_value == 30.0
        assert r.result       == "WIN"
        assert r.source       == "provided"
