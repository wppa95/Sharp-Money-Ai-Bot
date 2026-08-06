"""
tests/test_soccer_provider.py — Unit tests for SoccerStatsProvider.

Key correctness property verified:
  EVERY finished match the player's team played produces a RawGameResult,
  including games where the stat value is 0.  This ensures the hit-rate engine
  receives an unbiased sample (not just positive-event games).

All network calls are mocked.  Tests verify:
  • No-key returns [] without any network hit
  • Zero-stat games ARE included in results (core correctness)
  • Goals, assists, goals+assists, yellow/red cards extracted correctly
  • Player team discovered from goal scorer, assister, and booking data
  • Competition cascade (searches PL → PD → BL1 → SA → FL1 in order)
  • Player-info cache prevents redundant cascade searches
  • Competition match cache prevents redundant HTTP calls
  • Player not found in any competition returns []
  • Unfinished matches excluded
  • Non-team matches excluded
  • Integration: RawGameResult fields compatible with DB upsert pipeline
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.soccer_stats import (
    SoccerStatsProvider,
    _COMPETITION_CODES,
    _SUPPORTED_STATS,
    _current_season,
)

# ── Shared event loop ─────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_provider(key: Optional[str] = "test-key") -> SoccerStatsProvider:
    provider = SoccerStatsProvider.__new__(SoccerStatsProvider)
    provider._api_key      = key
    provider._match_cache  = {}
    provider._player_info  = {}
    return provider


def _make_match(
    date: str = "2024-09-14",
    status: str = "FINISHED",
    home: str = "Arsenal FC",
    away: str = "Chelsea FC",
    goals: Optional[list] = None,
    bookings: Optional[list] = None,
) -> dict:
    return {
        "utcDate":  date + "T14:00:00Z",
        "status":   status,
        "homeTeam": {"id": 57,  "name": home},
        "awayTeam": {"id": 61,  "name": away},
        "goals":    goals    or [],
        "bookings": bookings or [],
    }


def _goal(
    scorer: str,
    assist: Optional[str] = None,
    team: str = "Arsenal FC",
) -> dict:
    g: dict = {
        "minute": 23,
        "type":   "REGULAR",
        "team":   {"id": 57, "name": team},
        "scorer": {"id": 100, "name": scorer},
        "assist": None,
    }
    if assist:
        g["assist"] = {"id": 101, "name": assist}
    return g


def _booking(player: str, card: str = "YELLOW_CARD", team: str = "Arsenal FC") -> dict:
    return {
        "minute": 45,
        "team":   {"id": 57, "name": team},
        "player": {"id": 200, "name": player},
        "card":   card,
    }


def _inject_player(
    provider: SoccerStatsProvider,
    player_norm: str = "harry kane",
    competition: str = "PL",
    team: str = "Arsenal FC",
    matches: Optional[list] = None,
) -> None:
    """Pre-cache player info and competition matches for unit testing."""
    # _player_info now stores a list of (competition, team) tuples to support
    # mid-season transfers where a player appears in multiple leagues.
    provider._player_info[player_norm] = [(competition, team)]
    if matches is not None:
        provider._match_cache[(competition, _current_season())] = matches


# ─────────────────────────────────────────────────────────────────────────────
# No-key behavior
# ─────────────────────────────────────────────────────────────────────────────

class TestNoKey:
    def test_returns_empty_without_key(self):
        provider = _make_provider(key=None)
        results  = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert results == []

    def test_no_network_hit_without_key(self):
        provider = _make_provider(key=None)
        called   = []

        async def _fail(url):
            called.append(url)
            return None

        with patch.object(provider, "_get_json", side_effect=_fail):
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))

        assert called == []

    def test_unsupported_stat_returns_empty(self):
        provider = _make_provider()
        results  = run(provider.fetch_results("Harry Kane", "SOCCER", "shots on target"))
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Supported stats
# ─────────────────────────────────────────────────────────────────────────────

class TestSupportedStats:
    def test_goals_supported(self):          assert "goals"          in _SUPPORTED_STATS
    def test_assists_supported(self):        assert "assists"        in _SUPPORTED_STATS
    def test_goals_plus_assists_supported(self): assert "goals + assists" in _SUPPORTED_STATS
    def test_yellow_cards_supported(self):   assert "yellow cards"   in _SUPPORTED_STATS
    def test_red_cards_supported(self):      assert "red cards"      in _SUPPORTED_STATS
    def test_shots_not_supported(self):      assert "shots on target" not in _SUPPORTED_STATS
    def test_saves_not_supported(self):      assert "saves"          not in _SUPPORTED_STATS


# ─────────────────────────────────────────────────────────────────────────────
# Competition codes
# ─────────────────────────────────────────────────────────────────────────────

class TestCompetitionCodes:
    def test_pl_included(self):   assert "PL"  in _COMPETITION_CODES
    def test_pd_included(self):   assert "PD"  in _COMPETITION_CODES
    def test_bl1_included(self):  assert "BL1" in _COMPETITION_CODES
    def test_sa_included(self):   assert "SA"  in _COMPETITION_CODES
    def test_fl1_included(self):  assert "FL1" in _COMPETITION_CODES
    def test_pl_is_first(self):   assert _COMPETITION_CODES[0] == "PL"


# ─────────────────────────────────────────────────────────────────────────────
# _current_season
# ─────────────────────────────────────────────────────────────────────────────

class TestCurrentSeason:
    def test_returns_int(self):          assert isinstance(_current_season(), int)
    def test_in_reasonable_range(self):  assert 2020 <= _current_season() <= 2030


# ─────────────────────────────────────────────────────────────────────────────
# Team discovery (_find_player_team)
# ─────────────────────────────────────────────────────────────────────────────

class TestFindPlayerTeam:
    def test_team_found_via_scorer(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane", team="Arsenal FC")])]
        team     = provider._find_player_team("harry kane", matches)
        assert team == "Arsenal FC"

    def test_team_found_via_assister(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[
            _goal("Bukayo Saka", assist="Harry Kane", team="Arsenal FC")
        ])]
        team = provider._find_player_team("harry kane", matches)
        assert team == "Arsenal FC"

    def test_team_found_via_booking(self):
        provider = _make_provider()
        matches  = [_make_match(bookings=[_booking("Harry Kane", team="Arsenal FC")])]
        team     = provider._find_player_team("harry kane", matches)
        assert team == "Arsenal FC"

    def test_returns_none_when_not_found(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Bukayo Saka")])]
        team     = provider._find_player_team("harry kane", matches)
        assert team is None

    def test_returns_none_on_empty_matches(self):
        provider = _make_provider()
        team     = provider._find_player_team("harry kane", [])
        assert team is None

    def test_scorer_team_returned_not_opponent_team(self):
        """The team field comes from the goal dict, not the opposing team."""
        provider = _make_provider()
        # Arsenal player (Harry Kane) scores against Chelsea — team = Arsenal
        matches = [_make_match(
            home="Arsenal FC", away="Chelsea FC",
            goals=[_goal("Harry Kane", team="Arsenal FC")],
        )]
        team = provider._find_player_team("harry kane", matches)
        assert team == "Arsenal FC"
        assert team != "Chelsea FC"


# ─────────────────────────────────────────────────────────────────────────────
# Zero-stat game inclusion (CRITICAL correctness property)
# ─────────────────────────────────────────────────────────────────────────────

class TestZeroStatGameInclusion:
    """
    The provider MUST include all finished team matches, even when the player's
    stat is 0.  Omitting zero-stat games would bias hit-rate computation toward
    inflated over-rates.
    """

    def test_goals_zero_included_for_team_match(self):
        """
        A game where the team played but the player didn't score must produce
        a RawGameResult with actual_value = 0.0 (not be omitted).
        """
        provider = _make_provider()
        matches  = [
            _make_match(date="2024-09-14", goals=[_goal("Harry Kane")]),  # 1 goal
            _make_match(date="2024-09-21", goals=[_goal("Bukayo Saka")]), # 0 goals for Kane
        ]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert len(results) == 2, "Both matches must produce a result"
        vals = sorted(r.actual_value for r in results)
        assert vals == [0.0, 1.0]

    def test_assists_zero_included_for_team_match(self):
        """A game with no assist from the player still produces a 0-value result."""
        provider = _make_provider()
        matches  = [
            _make_match(date="2024-09-14", goals=[_goal("Saka", assist="Harry Kane")]),
            _make_match(date="2024-09-21", goals=[_goal("Saka")]),  # 0 assists for Kane
        ]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "assists"))
        assert len(results) == 2
        vals = sorted(r.actual_value for r in results)
        assert vals == [0.0, 1.0]

    def test_yellow_cards_zero_included_for_team_match(self):
        """A game where the player wasn't booked produces value 0."""
        provider = _make_provider()
        matches  = [
            _make_match(date="2024-09-14", goals=[_goal("Harry Kane")],
                        bookings=[_booking("Harry Kane")]),   # booked
            _make_match(date="2024-09-21", goals=[_goal("Harry Kane")]),  # not booked
        ]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "yellow cards"))
        assert len(results) == 2
        vals = sorted(r.actual_value for r in results)
        assert vals == [0.0, 1.0]

    def test_multi_game_zero_history(self):
        """15-game history with only 3 scoring games → 15 results returned."""
        provider = _make_provider()
        matches = []
        for i in range(15):
            date   = f"2024-{9 + i // 10:02d}-{14 + (i % 10):02d}"
            goals  = [_goal("Harry Kane")] if i < 3 else []
            matches.append(_make_match(date=date, goals=goals))
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert len(results) == 15
        scoring = [r for r in results if r.actual_value > 0]
        zero    = [r for r in results if r.actual_value == 0.0]
        assert len(scoring) == 3
        assert len(zero) == 12

    def test_g_plus_a_zero_included(self):
        """goals + assists zero for team match must be recorded."""
        provider = _make_provider()
        matches  = [
            _make_match(date="2024-09-14", goals=[_goal("Harry Kane")]),        # 1 G
            _make_match(date="2024-09-21", goals=[_goal("Bukayo Saka")]),       # 0 G+A
        ]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals + assists"))
        assert len(results) == 2
        vals = sorted(r.actual_value for r in results)
        assert vals == [0.0, 1.0]


# ─────────────────────────────────────────────────────────────────────────────
# Stat accuracy
# ─────────────────────────────────────────────────────────────────────────────

class TestStatAccuracy:
    def test_multiple_goals_in_one_game(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane"), _goal("Harry Kane")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert results[0].actual_value == 2.0

    def test_goals_plus_assists_summed(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[
            _goal("Harry Kane"),                        # 1 goal
            _goal("Bukayo Saka", assist="Harry Kane"),  # 1 assist
        ])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals + assists"))
        assert results[0].actual_value == 2.0

    def test_yellow_card_counted(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane")],
                                bookings=[_booking("Harry Kane")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "yellow cards"))
        assert results[0].actual_value == 1.0

    def test_red_card_counted(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane")],
                                bookings=[_booking("Harry Kane", card="RED_CARD")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "red cards"))
        assert results[0].actual_value == 1.0

    def test_straight_red_counted(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane")],
                                bookings=[_booking("Harry Kane", card="STRAIGHT_RED")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "red cards"))
        assert results[0].actual_value == 1.0

    def test_other_team_goal_not_counted(self):
        """Goals scored by players on the opposing team don't affect player stats."""
        provider = _make_provider()
        matches  = [_make_match(
            goals=[_goal("Chelsea Scorer", team="Chelsea FC")]  # opponent's goal
        )]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert results[0].actual_value == 0.0

    def test_source_label(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert all(r.source == "football_data_org" for r in results)

    def test_sport_label(self):
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert all(r.sport == "SOCCER" for r in results)

    def test_game_date_extracted(self):
        provider = _make_provider()
        matches  = [_make_match(date="2024-11-02", goals=[_goal("Harry Kane")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert results[0].game_date == "2024-11-02"

    def test_opponent_correctly_identified_home(self):
        """When player's team is home, opponent = away team."""
        provider = _make_provider()
        matches  = [_make_match(
            home="Arsenal FC", away="Chelsea FC",
            goals=[_goal("Harry Kane", team="Arsenal FC")],
        )]
        _inject_player(provider, team="Arsenal FC", matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert results[0].opponent == "Chelsea FC"

    def test_opponent_correctly_identified_away(self):
        """When player's team is away, opponent = home team."""
        provider = _make_provider()
        matches  = [_make_match(
            home="Chelsea FC", away="Arsenal FC",
            goals=[_goal("Harry Kane", team="Arsenal FC")],
        )]
        _inject_player(provider, team="Arsenal FC", matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert results[0].opponent == "Chelsea FC"


# ─────────────────────────────────────────────────────────────────────────────
# Match filtering
# ─────────────────────────────────────────────────────────────────────────────

class TestMatchFiltering:
    def test_unfinished_matches_excluded(self):
        provider = _make_provider()
        matches  = [
            _make_match(date="2024-09-14", status="SCHEDULED",
                        goals=[_goal("Harry Kane")]),
            _make_match(date="2024-09-21", status="FINISHED",
                        goals=[_goal("Harry Kane")]),
        ]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert len(results) == 1
        assert results[0].game_date == "2024-09-21"

    def test_non_team_matches_excluded(self):
        """Matches where the player's team doesn't participate are ignored."""
        provider = _make_provider()
        # First match: Arsenal vs Chelsea (Arsenal is player's team)
        # Second match: Liverpool vs Man City (neither is Arsenal)
        matches  = [
            _make_match(home="Arsenal FC", away="Chelsea FC",
                        goals=[_goal("Harry Kane", team="Arsenal FC")]),
            _make_match(home="Liverpool FC", away="Manchester City",
                        goals=[_goal("Salah", team="Liverpool FC")]),
        ]
        _inject_player(provider, team="Arsenal FC", matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert len(results) == 1
        assert results[0].game_date == "2024-09-14"

    def test_player_not_found_returns_empty(self):
        provider = _make_provider()

        async def mock_get_competition_matches(code, season):
            return []

        with patch.object(provider, "_get_competition_matches",
                          side_effect=mock_get_competition_matches):
            results = run(provider.fetch_results("Unknown Player XYZ", "SOCCER", "goals"))

        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Competition cascade and caching
# ─────────────────────────────────────────────────────────────────────────────

class TestCompetitionCascade:
    def test_all_leagues_searched_for_transfer_support(self):
        """
        All competitions are searched so a player who transferred mid-season
        (e.g. EPL → Bundesliga) is found in both leagues and full history kept.
        """
        provider   = _make_provider()
        fetch_calls: list[str] = []

        async def mock_get_competition_matches(code, season):
            fetch_calls.append(code)
            if code == "PL":
                return [_make_match(goals=[_goal("Harry Kane", team="Arsenal FC")])]
            return []

        with patch.object(provider, "_get_competition_matches",
                          side_effect=mock_get_competition_matches):
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))

        # PL searched first; all other leagues also searched to catch transfers.
        assert fetch_calls[0] == "PL"
        assert set(fetch_calls) == {"PL", "PD", "BL1", "SA", "FL1"}

    def test_found_in_third_competition(self):
        """If player is only in BL1, both PL and PD are searched first."""
        provider   = _make_provider()
        fetch_calls: list[str] = []

        async def mock_get_competition_matches(code, season):
            fetch_calls.append(code)
            if code == "BL1":
                return [_make_match(goals=[_goal("Harry Kane", team="Bayern Munich")])]
            return []

        with patch.object(provider, "_get_competition_matches",
                          side_effect=mock_get_competition_matches):
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))

        assert fetch_calls.index("PL") < fetch_calls.index("BL1")

    def test_player_info_cached_after_first_call(self):
        """Second call for same player uses cached info — no extra cascade."""
        provider   = _make_provider()
        call_count = [0]

        async def mock_get_competition_matches(code, season):
            call_count[0] += 1
            if code == "PL":
                return [_make_match(goals=[_goal("Harry Kane", team="Arsenal FC")])]
            return []

        with patch.object(provider, "_get_competition_matches",
                          side_effect=mock_get_competition_matches):
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
            first_calls = call_count[0]
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
            # Second call: uses cached player info → only one competition fetch
            second_extra = call_count[0] - first_calls

        assert second_extra == 1  # only PL re-fetched (from match cache)

    def test_competition_matches_fetched_once(self):
        """Same competition+season is only fetched via HTTP once."""
        provider  = _make_provider()
        get_calls: list[str] = []

        async def mock_get_json(url):
            get_calls.append(url)
            return {"matches": [_make_match(goals=[_goal("Harry Kane", team="Arsenal FC")])]}

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
            run(provider.fetch_results("Harry Kane", "SOCCER", "assists"))

        pl_fetches = [u for u in get_calls if "PL/matches" in u]
        assert len(pl_fetches) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Integration: player_stats.py routes SOCCER to SoccerStatsProvider
# ─────────────────────────────────────────────────────────────────────────────

class TestSoccerDispatch:
    def test_soccer_routed_to_soccer_provider(self):
        import providers.player_stats as ps_mod
        from providers.player_stats import PlayerStatsProvider

        called_with: list = []

        async def fake_fetch(player_name, sport, stat_type):
            called_with.append((player_name, sport, stat_type))
            return []

        mock_soccer = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock_soccer
        try:
            provider = PlayerStatsProvider()
            run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert len(called_with) == 1
        assert called_with[0] == ("Harry Kane", "SOCCER", "goals")

    def test_lowercase_soccer_dispatch(self):
        import providers.player_stats as ps_mod
        from providers.player_stats import PlayerStatsProvider

        called_with: list = []

        async def fake_fetch(player_name, sport, stat_type):
            called_with.append(sport)
            return []

        mock_soccer = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock_soccer
        try:
            provider = PlayerStatsProvider()
            run(provider.fetch_results("Harry Kane", "soccer", "goals"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert called_with  # was called


# ─────────────────────────────────────────────────────────────────────────────
# Integration: config includes SOCCER
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigSoccer:
    def test_soccer_not_in_default_ud_alert_sports(self):
        """
        SOCCER is NOT in the default because the free API tier lacks lineup data;
        without it DNPs cannot be distinguished from zero-stat games.
        Enable manually via UD_ALERT_SPORTS env var + FOOTBALL_DATA_API_KEY.
        """
        import os
        import config as cfg
        if "UD_ALERT_SPORTS" not in os.environ:
            c = cfg.Config()
            assert "SOCCER" not in c.ud_alert_sports

    def test_nhl_still_in_ud_alert_sports(self):
        import config as cfg
        c = cfg.Config()
        assert "NHL" in c.ud_alert_sports


# ─────────────────────────────────────────────────────────────────────────────
# API response fixture (contract test)
# ─────────────────────────────────────────────────────────────────────────────

class TestApiResponseFixture:
    """
    Verify the provider correctly handles a realistic football-data.org
    /v4/competitions/{code}/matches response structure.

    football-data.org's competition matches collection endpoint includes
    goals (with scorer and assist) and bookings (with player and card type)
    inline in each match object.  This fixture test demonstrates the
    implementation is based on the correct API response format.
    """

    # Realistic subset of a football-data.org v4 collection response
    _FD_FIXTURE: dict = {
        "filters":     {"season": "2024"},
        "resultSet":   {"count": 380, "first": "2024-08-16", "last": "2025-05-25"},
        "competition": {"id": 2021, "code": "PL", "name": "Premier League"},
        "matches": [
            {
                "id": 497100,
                "utcDate": "2024-08-17T14:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "shortName": "Arsenal", "name": "Arsenal FC"},
                "awayTeam": {"id": 338, "shortName": "Wolves",
                             "name": "Wolverhampton Wanderers FC"},
                "score": {
                    "winner": "HOME_TEAM",
                    "fullTime": {"home": 2, "away": 0},
                },
                "goals": [
                    {
                        "minute": 4,
                        "type": "REGULAR",
                        "team": {"id": 57, "name": "Arsenal FC"},
                        "scorer": {"id": 44788, "name": "Kai Havertz"},
                        "assist": {"id": 44789, "name": "Bukayo Saka"},
                    },
                    {
                        "minute": 72,
                        "type": "REGULAR",
                        "team": {"id": 57, "name": "Arsenal FC"},
                        "scorer": {"id": 44789, "name": "Bukayo Saka"},
                        "assist": None,
                    },
                ],
                "bookings": [
                    {
                        "minute": 45,
                        "team": {"id": 338, "name": "Wolverhampton Wanderers FC"},
                        "player": {"id": 12345, "name": "Jean-Ricner Bellegarde"},
                        "card": "YELLOW_CARD",
                    }
                ],
            },
            # Second match: Arsenal 0-1 Chelsea (no Havertz goal, 0-stat game)
            {
                "id": 497101,
                "utcDate": "2024-08-25T14:00:00Z",
                "status": "FINISHED",
                "homeTeam": {"id": 57, "name": "Arsenal FC"},
                "awayTeam": {"id": 61, "name": "Chelsea FC"},
                "score": {
                    "winner": "AWAY_TEAM",
                    "fullTime": {"home": 0, "away": 1},
                },
                "goals": [
                    {
                        "minute": 55,
                        "type": "REGULAR",
                        "team": {"id": 61, "name": "Chelsea FC"},
                        "scorer": {"id": 99999, "name": "Cole Palmer"},
                        "assist": None,
                    }
                ],
                "bookings": [],
            },
        ],
    }

    def test_player_team_discovered_from_fixture(self):
        """Provider discovers Arsenal FC as Havertz's team from fixture data."""
        provider = _make_provider()
        matches  = self._FD_FIXTURE["matches"]
        team     = provider._find_player_team("kai havertz", matches)
        assert team == "Arsenal FC"

    def test_assister_team_discovered_from_fixture(self):
        """Provider identifies Saka's team (Arsenal) via his assist entry."""
        provider = _make_provider()
        matches  = self._FD_FIXTURE["matches"]
        team     = provider._find_player_team("bukayo saka", matches)
        assert team == "Arsenal FC"

    def test_goals_extracted_from_fixture(self):
        """Havertz scored 1 goal in match 1; 0 goals in match 2."""
        provider = _make_provider()
        matches  = self._FD_FIXTURE["matches"]
        _inject_player(provider, player_norm="kai havertz",
                       team="Arsenal FC", matches=matches)

        results  = run(provider.fetch_results("Kai Havertz", "SOCCER", "goals"))
        assert len(results) == 2, "Both Arsenal matches produce a result"
        vals = sorted(r.actual_value for r in results)
        assert vals == [0.0, 1.0]

    def test_assists_extracted_from_fixture(self):
        """Saka assisted 1 goal in match 1; 0 assists in match 2."""
        provider = _make_provider()
        matches  = self._FD_FIXTURE["matches"]
        _inject_player(provider, player_norm="bukayo saka",
                       team="Arsenal FC", matches=matches)

        results  = run(provider.fetch_results("Bukayo Saka", "SOCCER", "assists"))
        assert len(results) == 2
        vals = sorted(r.actual_value for r in results)
        assert vals == [0.0, 1.0]

    def test_booking_extracted_from_fixture(self):
        """Bellegarde (Wolves) got a yellow card in match 1; 0 in match 2."""
        provider = _make_provider()
        matches  = self._FD_FIXTURE["matches"]
        # Bellegarde appears only in bookings, so we pre-inject team
        _inject_player(provider, player_norm="jean-ricner bellegarde",
                       team="Wolverhampton Wanderers FC", matches=matches)

        results  = run(provider.fetch_results(
            "Jean-Ricner Bellegarde", "SOCCER", "yellow cards"
        ))
        # Wolves played both matches (away in match 1, no away match 2)
        # Match 1: Wolves is awayTeam → included; yellow card counted
        # Match 2: Arsenal vs Chelsea → Wolves NOT in match → excluded
        assert len(results) == 1
        assert results[0].actual_value == 1.0

    def test_non_arsenal_match_excluded(self):
        """Match 2 (Arsenal vs Chelsea) is excluded for Wolves players."""
        provider = _make_provider()
        matches  = self._FD_FIXTURE["matches"]
        _inject_player(provider, player_norm="jean-ricner bellegarde",
                       team="Wolverhampton Wanderers FC", matches=matches)

        results = run(provider.fetch_results(
            "Jean-Ricner Bellegarde", "SOCCER", "goals"
        ))
        # Only match 1 involves Wolves
        assert len(results) == 1
        assert results[0].game_date == "2024-08-17"


# ─────────────────────────────────────────────────────────────────────────────
# Integration: DB upsert pipeline compatibility
# ─────────────────────────────────────────────────────────────────────────────

class TestSoccerPipelineIntegration:
    """
    Verify that RawGameResult objects from SoccerStatsProvider are structurally
    compatible with upsert_player_result (field names and types).
    """

    def test_raw_game_result_fields_match_db_contract(self):
        from providers.player_stats import RawGameResult
        r = RawGameResult(
            player_name  = "Harry Kane",
            sport        = "SOCCER",
            stat_type    = "goals",
            game_date    = "2024-11-02",
            actual_value = 2.0,
            opponent     = "Chelsea FC",
            source       = "football_data_org",
        )
        assert isinstance(r.player_name,  str)
        assert isinstance(r.sport,        str)
        assert isinstance(r.stat_type,    str)
        assert isinstance(r.game_date,    str)
        assert isinstance(r.actual_value, float)
        assert r.source == "football_data_org"

    def test_fetch_results_returns_rawgameresult_list(self):
        from providers.player_stats import RawGameResult as BaseResult
        provider = _make_provider()
        matches  = [_make_match(goals=[_goal("Harry Kane")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert isinstance(results, list)
        if results:
            assert isinstance(results[0], BaseResult)

    def test_zero_stat_result_is_valid_rawgameresult(self):
        """A zero-stat game must still produce a valid RawGameResult (not None)."""
        from providers.player_stats import RawGameResult as BaseResult
        provider = _make_provider()
        # Game where player's team played but player didn't score
        matches  = [_make_match(goals=[_goal("Bukayo Saka")])]
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert len(results) == 1
        assert isinstance(results[0], BaseResult)
        assert results[0].actual_value == 0.0

    def test_complete_game_history_produced(self):
        """
        30-game history: 8 scoring, 22 non-scoring → 30 RawGameResult objects.
        Verifies the complete unbiased sample the hit-rate engine needs.
        """
        provider = _make_provider()
        matches  = []
        for i in range(30):
            date  = f"2024-{9 + i // 10:02d}-{1 + i % 10:02d}"
            goals = [_goal("Harry Kane")] if i < 8 else []
            matches.append(_make_match(date=date, goals=goals))
        _inject_player(provider, matches=matches)

        results = run(provider.fetch_results("Harry Kane", "SOCCER", "goals"))
        assert len(results) == 30
        over_half = [r for r in results if r.actual_value > 0.5]
        under_half = [r for r in results if r.actual_value <= 0.5]
        assert len(over_half) == 8
        assert len(under_half) == 22
