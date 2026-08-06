"""
tests/test_soccer_nhl_stats.py — Unit tests for Soccer and NHL stat coverage.

All network calls are mocked.  Tests verify:
  • _SOCCER_STAT_MAP column mappings
  • _SOCCER_LEAGUE_PRIORITY ordering
  • Multi-league player search logic (first match wins, no subsequent leagues queried)
  • Unknown player / unmapped stat → []
  • Cache hit on second call (no extra HTTP requests)
  • NHL stat map (skater + goalie merge)
  • NHL dispatch path through fetch_results()
"""

from __future__ import annotations

import asyncio
import sys
import os
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.player_stats import (
    PlayerStatsProvider,
    RawGameResult,
    _SOCCER_LEAGUE_PRIORITY,
    _SOCCER_STAT_MAP,
    _NHL_STAT_MAP,
    _NHL_GOALIE_STAT_MAP,
    _get_stat_map,
)

# ── Shared event loop ─────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# _SOCCER_STAT_MAP
# ─────────────────────────────────────────────────────────────────────────────

class TestSoccerStatMap:
    def test_goals_maps_to_G(self):
        assert _SOCCER_STAT_MAP["goals"] == ["G"]

    def test_assists_maps_to_A(self):
        assert _SOCCER_STAT_MAP["assists"] == ["A"]

    def test_shots_on_target_maps_to_SOG(self):
        assert _SOCCER_STAT_MAP["shots on target"] == ["SOG"]

    def test_shots_maps_to_SH(self):
        assert _SOCCER_STAT_MAP["shots"] == ["SH"]

    def test_key_passes_maps_to_KP(self):
        assert _SOCCER_STAT_MAP["key passes"] == ["KP"]

    def test_yellow_cards_maps_to_YC(self):
        assert _SOCCER_STAT_MAP["yellow cards"] == ["YC"]

    def test_red_cards_maps_to_RC(self):
        assert _SOCCER_STAT_MAP["red cards"] == ["RC"]

    def test_goals_plus_assists_combo(self):
        assert _SOCCER_STAT_MAP["goals + assists"] == ["G", "A"]

    def test_gpa_alias(self):
        assert _SOCCER_STAT_MAP["g+a"] == ["G", "A"]

    def test_goalkeeper_saves_maps_to_SV(self):
        assert _SOCCER_STAT_MAP["goalkeeper saves"] == ["SV"]

    def test_saves_alias_maps_to_SV(self):
        assert _SOCCER_STAT_MAP["saves"] == ["SV"]

    def test_goals_allowed_maps_to_GA(self):
        assert _SOCCER_STAT_MAP["goals allowed"] == ["GA"]

    def test_clean_sheets_maps_to_CS(self):
        assert _SOCCER_STAT_MAP["clean sheets"] == ["CS"]

    def test_unmapped_stat_not_in_map(self):
        assert "tackles made" not in _SOCCER_STAT_MAP

    def test_get_stat_map_returns_soccer_map(self):
        assert _get_stat_map("SOCCER") is _SOCCER_STAT_MAP

    def test_get_stat_map_soccer_case_insensitive(self):
        assert _get_stat_map("soccer") is _SOCCER_STAT_MAP


# ─────────────────────────────────────────────────────────────────────────────
# _SOCCER_LEAGUE_PRIORITY
# ─────────────────────────────────────────────────────────────────────────────

class TestSoccerLeaguePriority:
    def test_premier_league_is_first(self):
        assert _SOCCER_LEAGUE_PRIORITY[0] == "eng.1"

    def test_la_liga_is_second(self):
        assert _SOCCER_LEAGUE_PRIORITY[1] == "esp.1"

    def test_bundesliga_is_third(self):
        assert _SOCCER_LEAGUE_PRIORITY[2] == "ger.1"

    def test_mls_is_present(self):
        assert "usa.1" in _SOCCER_LEAGUE_PRIORITY

    def test_nwsl_is_present(self):
        assert "usa.nwsl" in _SOCCER_LEAGUE_PRIORITY

    def test_at_least_five_leagues(self):
        assert len(_SOCCER_LEAGUE_PRIORITY) >= 5


# ─────────────────────────────────────────────────────────────────────────────
# _NHL_STAT_MAP (skater + goalie merged)
# ─────────────────────────────────────────────────────────────────────────────

class TestNhlStatMap:
    def test_goals_mapped(self):
        assert _NHL_STAT_MAP["goals"] == ["G"]

    def test_assists_mapped(self):
        assert _NHL_STAT_MAP["assists"] == ["A"]

    def test_points_mapped(self):
        assert _NHL_STAT_MAP["points"] == ["PTS"]

    def test_shots_on_goal_mapped(self):
        assert _NHL_STAT_MAP["shots on goal"] == ["SOG"]

    def test_hits_mapped(self):
        assert _NHL_STAT_MAP["hits"] == ["HIT"]

    def test_blocked_shots_mapped(self):
        assert _NHL_STAT_MAP["blocked shots"] == ["BLK"]

    def test_saves_merged_from_goalie_map(self):
        """Goalie stats merged into the unified NHL map."""
        assert _NHL_STAT_MAP["saves"] == ["SV"]

    def test_goals_allowed_merged_from_goalie_map(self):
        assert _NHL_STAT_MAP["goals allowed"] == ["GA"]

    def test_goalie_map_still_has_saves(self):
        """Original goalie map untouched."""
        assert _NHL_GOALIE_STAT_MAP["saves"] == ["SV"]

    def test_get_stat_map_returns_nhl_map(self):
        assert _get_stat_map("NHL") is _NHL_STAT_MAP

    def test_get_stat_map_nhl_case_insensitive(self):
        assert _get_stat_map("nhl") is _NHL_STAT_MAP


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_espn_gamelog_response(labels: list[str], stat_rows: list[list]) -> dict:
    """Build a minimal ESPN flat-mode gamelog dict for testing."""
    events: dict = {}
    for i, row in enumerate(stat_rows):
        event_id = str(1000 + i)
        events[event_id] = {
            "stats": row,
            "game":  {"date": f"2025-0{i+1}-10T20:00:00Z"},
        }
    return {
        "categories": [{"labels": labels}],
        "events":     events,
        "seasonTypes": [],  # empty → uses all events
    }


def _make_athlete_search_response(display_name: str, athlete_id: int) -> dict:
    return {
        "items": [{"displayName": display_name, "id": str(athlete_id)}]
    }


# ─────────────────────────────────────────────────────────────────────────────
# Soccer: multi-league search logic
# ─────────────────────────────────────────────────────────────────────────────

class TestSoccerMultiLeagueSearch:
    """
    Verify that _soccer_athlete_id() searches leagues in priority order,
    stops at the first match, caches the result, and correctly returns None
    when the player is not in any league.
    """

    def _make_provider(self) -> PlayerStatsProvider:
        return PlayerStatsProvider()

    def _athlete_search_side_effect(
        self,
        found_in_league: str,
        player_name: str = "Harry Kane",
        athlete_id: int = 3149391,
    ):
        """Return a side-effect fn: return athlete data only for found_in_league."""
        def _se(url: str):
            if f"/{found_in_league}/athletes" in url and player_name.split()[1].lower() in url.lower():
                return _make_athlete_search_response(player_name, athlete_id)
            # Other leagues: return None (player not found)
            return None
        return _se

    def test_found_in_first_league_stops_search(self):
        provider = self._make_provider()
        call_count = 0

        async def mock_get_json(url: str):
            nonlocal call_count
            call_count += 1
            if "eng.1/athletes" in url:
                return _make_athlete_search_response("Harry Kane", 3149391)
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            result = run(provider._soccer_athlete_id("Harry Kane"))

        assert result is not None
        athlete_id, league_slug = result
        assert league_slug == "eng.1"
        assert athlete_id == 3149391
        # Only one league was searched (eng.1 returned a match immediately)
        assert call_count == 1

    def test_found_in_third_league_searches_first_two_then_stops(self):
        provider = self._make_provider()
        searched_leagues: list[str] = []

        async def mock_get_json(url: str):
            for league in _SOCCER_LEAGUE_PRIORITY:
                if f"/{league}/athletes" in url:
                    searched_leagues.append(league)
                    if league == "ger.1":   # Bundesliga = 3rd
                        return _make_athlete_search_response("Harry Kane", 3149391)
                    return None
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            result = run(provider._soccer_athlete_id("Harry Kane"))

        assert result is not None
        _, league_slug = result
        assert league_slug == "ger.1"
        # Only eng.1, esp.1, ger.1 were searched — not ita.1 or later
        assert searched_leagues == ["eng.1", "esp.1", "ger.1"]

    def test_not_found_in_any_league_returns_none(self):
        provider = self._make_provider()

        async def mock_get_json(url: str):
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            result = run(provider._soccer_athlete_id("Completely Unknown Player XYZ"))

        assert result is None

    def test_second_call_uses_cache_no_extra_http(self):
        provider = self._make_provider()
        call_count = 0

        async def mock_get_json(url: str):
            nonlocal call_count
            call_count += 1
            if "eng.1/athletes" in url:
                return _make_athlete_search_response("Harry Kane", 3149391)
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            run(provider._soccer_athlete_id("Harry Kane"))   # first — searches
            run(provider._soccer_athlete_id("Harry Kane"))   # second — cache hit

        assert call_count == 1   # Only the first call hit the network

    def test_not_found_cached_no_retry(self):
        provider = self._make_provider()
        call_count = 0

        async def mock_get_json(url: str):
            nonlocal call_count
            call_count += 1
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            first  = run(provider._soccer_athlete_id("Unknown Player XYZ"))
            second = run(provider._soccer_athlete_id("Unknown Player XYZ"))

        assert first  is None
        assert second is None
        # All 7 leagues searched once, never repeated
        assert call_count == len(_SOCCER_LEAGUE_PRIORITY)


# ─────────────────────────────────────────────────────────────────────────────
# Soccer: fetch_results end-to-end (mock ESPN gamelog)
# ─────────────────────────────────────────────────────────────────────────────

class TestSoccerFetchResults:
    """
    End-to-end fetch_results for SOCCER — verifies that PlayerStatsProvider
    routes SOCCER to SoccerStatsProvider and returns results correctly.

    SoccerStatsProvider is injected via _soccer_provider_instance so no
    real HTTP calls are made.
    """

    _PLAYER = "Harry Kane"

    def _make_fake_result(
        self,
        player: str = "Harry Kane",
        stat: str = "goals",
        value: float = 1.0,
        date: str = "2024-09-14",
    ):
        from providers.player_stats import RawGameResult
        return RawGameResult(
            player_name  = player,
            sport        = "SOCCER",
            stat_type    = stat,
            game_date    = date,
            actual_value = value,
            opponent     = "Chelsea FC",
            source       = "football_data_org",
        )

    def _inject_mock_soccer(self, results_by_stat: dict):
        """Return a context manager that injects a mock SoccerStatsProvider."""
        import providers.player_stats as ps_mod

        async def fake_fetch(player_name, sport, stat_type):
            return results_by_stat.get(stat_type.lower(), [])

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        return mock, ps_mod

    def test_goals_returned_for_each_game(self):
        import providers.player_stats as ps_mod
        results_to_return = [
            self._make_fake_result(value=1.0, date="2024-09-14"),
            self._make_fake_result(value=2.0, date="2024-09-21"),
        ]

        async def fake_fetch(player_name, sport, stat_type):
            if stat_type.lower() == "goals":
                return results_to_return
            return []

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "SOCCER", "goals"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert len(results) == 2
        vals = sorted(r.actual_value for r in results)
        assert vals == [1.0, 2.0]

    def test_source_is_football_data_org(self):
        import providers.player_stats as ps_mod
        result = self._make_fake_result()

        async def fake_fetch(player_name, sport, stat_type):
            return [result]

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "SOCCER", "goals"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert all(r.source == "football_data_org" for r in results)

    def test_sport_is_soccer(self):
        import providers.player_stats as ps_mod
        result = self._make_fake_result(stat="assists", value=1.0)

        async def fake_fetch(player_name, sport, stat_type):
            return [result]

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "SOCCER", "assists"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert all(r.sport == "SOCCER" for r in results)

    def test_assists_value_passes_through(self):
        import providers.player_stats as ps_mod
        result = self._make_fake_result(stat="assists", value=2.0)

        async def fake_fetch(player_name, sport, stat_type):
            return [result]

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "SOCCER", "assists"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert results[0].actual_value == 2.0

    def test_goals_plus_assists_value_passes_through(self):
        import providers.player_stats as ps_mod
        result = self._make_fake_result(stat="goals + assists", value=3.0)

        async def fake_fetch(player_name, sport, stat_type):
            return [result]

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(
                provider.fetch_results(self._PLAYER, "SOCCER", "goals + assists")
            )
        finally:
            ps_mod._soccer_provider_instance = original

        assert results[0].actual_value == 3.0

    def test_unsupported_stat_returns_empty(self):
        """SoccerStatsProvider returns [] for unsupported stats (interceptions, etc.)."""
        import providers.player_stats as ps_mod

        async def fake_fetch(player_name, sport, stat_type):
            return []  # provider already filtered unsupported stat

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(
                provider.fetch_results(self._PLAYER, "SOCCER", "interceptions")
            )
        finally:
            ps_mod._soccer_provider_instance = original

        assert results == []

    def test_unknown_player_returns_empty(self):
        import providers.player_stats as ps_mod

        async def fake_fetch(player_name, sport, stat_type):
            return []

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(
                provider.fetch_results("Nobody Exists XYZ", "SOCCER", "goals")
            )
        finally:
            ps_mod._soccer_provider_instance = original

        assert results == []

    def test_lowercase_sport_handled(self):
        import providers.player_stats as ps_mod
        result = self._make_fake_result()

        async def fake_fetch(player_name, sport, stat_type):
            return [result]

        mock = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._soccer_provider_instance
        ps_mod._soccer_provider_instance = mock
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "soccer", "goals"))
        finally:
            ps_mod._soccer_provider_instance = original

        assert len(results) == 1


# ─────────────────────────────────────────────────────────────────────────────
# NHL: dispatch and stat map
# ─────────────────────────────────────────────────────────────────────────────

class TestNhlDispatch:
    """
    Verify NHL is dispatched to NHLStatsProvider (not ESPN).

    ESPN returns 403 from this environment; NHL now uses the official
    NHL public API (api-web.nhle.com) via NHLStatsProvider.
    """

    _PLAYER = "Connor McDavid"

    def test_shots_on_goal_returned(self):
        """NHL fetch_results routes to NHLStatsProvider and returns results."""
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        fake_result = _R(
            player_name="Connor McDavid",
            sport="NHL",
            stat_type="shots on goal",
            game_date="2025-10-15",
            actual_value=4.0,
            opponent="TOR",
            source="nhl_stats_api",
        )

        async def fake_fetch(player_name, sport, stat_type):
            return [fake_result]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "shots on goal"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert len(results) == 1
        assert results[0].actual_value == 4.0

    def test_goals_returned(self):
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=2.0,
                       opponent="VGK", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert results[0].actual_value == 2.0

    def test_assists_returned(self):
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=3.0,
                       opponent="CGY", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "assists"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert results[0].actual_value == 3.0

    def test_points_returned(self):
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=3.0,
                       opponent="VAN", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "points"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert results[0].actual_value == 3.0

    def test_source_is_nhl_stats_api(self):
        """Source label must be nhl_stats_api, not espn_gamelog."""
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=1.0,
                       opponent="ANA", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert all(r.source == "nhl_stats_api" for r in results)

    def test_sport_label_is_NHL(self):
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=1.0,
                       opponent="SEA", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert all(r.sport == "NHL" for r in results)

    def test_goalie_saves_stat(self):
        """Goalie 'saves' stat passes through to NHLStatsProvider."""
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=28.0,
                       opponent="EDM", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results("Jake Oettinger", "NHL", "saves"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert len(results) == 1
        assert results[0].actual_value == 28.0

    def test_unmapped_stat_returns_empty(self):
        import providers.player_stats as ps_mod

        async def fake_fetch(player_name, sport, stat_type):
            return []

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "NHL", "fantasy points"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert results == []

    def test_unknown_player_returns_empty(self):
        import providers.player_stats as ps_mod

        async def fake_fetch(player_name, sport, stat_type):
            return []

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results("Unknown Goalie XYZ", "NHL", "saves"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert results == []

    def test_lowercase_sport_handled(self):
        import providers.player_stats as ps_mod
        from providers.nhl_stats import RawGameResult as _R

        async def fake_fetch(player_name, sport, stat_type):
            return [_R(player_name=player_name, sport="NHL", stat_type=stat_type,
                       game_date="2025-10-15", actual_value=1.0,
                       opponent="STL", source="nhl_stats_api")]

        mock_nhl = type("M", (), {"fetch_results": staticmethod(fake_fetch)})()
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            provider = PlayerStatsProvider()
            results = run(provider.fetch_results(self._PLAYER, "nhl", "goals"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert len(results) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Integration: config UD_ALERT_SPORTS includes NHL (SOCCER removed from default)
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigSportsInclusion:
    def test_nhl_in_ud_alert_sports_default(self):
        import config as cfg
        c = cfg.Config()
        assert "NHL" in c.ud_alert_sports

    def test_soccer_not_in_default_ud_alert_sports(self):
        """
        SOCCER is NOT in the default UD_ALERT_SPORTS. The free API tier lacks
        lineup data; without it DNPs cannot be distinguished from zero-stat games.
        Enable manually: UD_ALERT_SPORTS=...,SOCCER + FOOTBALL_DATA_API_KEY.
        """
        import os
        import config as cfg
        if "UD_ALERT_SPORTS" not in os.environ:
            c = cfg.Config()
            assert "SOCCER" not in c.ud_alert_sports

    def test_existing_sports_still_present(self):
        import config as cfg
        c = cfg.Config()
        for sport in ("MLB", "WNBA", "NFL", "NBA", "DOTA", "TENNIS", "CS"):
            assert sport in c.ud_alert_sports, f"{sport} missing from UD_ALERT_SPORTS"


# ─────────────────────────────────────────────────────────────────────────────
# Integration: underdog_provider normalizes soccer/NHL stat types
# ─────────────────────────────────────────────────────────────────────────────

class TestUnderdogProviderNorms:
    def test_goals_normalizes(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("goals") == "goals"

    def test_shots_on_target_normalizes(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("shots on target") == "shots on target"

    def test_goalkeeper_saves_normalizes(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("goalkeeper saves") == "goalkeeper saves"

    def test_saves_normalizes_to_goalkeeper_saves(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("saves") == "goalkeeper saves"

    def test_shots_on_goal_nhl_normalizes(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("shots on goal") == "shots on goal"

    def test_shots_nhl_alias_normalizes(self):
        # "shots" is a hockey stat alias for "shots on goal" (Underdog sends "shots"
        # for NHL props).  Soccer uses the explicit "shots on target" key.
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("shots") == "shots on goal"  # preserved hockey mapping

    def test_yellow_cards_normalizes(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("yellow cards") == "yellow cards"

    def test_assists_soccer_normalizes(self):
        from providers.underdog_provider import _normalize_stat
        assert _normalize_stat("assists") == "assists"
