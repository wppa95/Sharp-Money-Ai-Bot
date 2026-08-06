"""
tests/test_nhl_provider.py — Unit tests for NHLStatsProvider.

All network calls are mocked.  Tests verify:
  • Stat maps (_SKATER_STAT_MAP, _GOALIE_STAT_MAP)
  • _extract_stat helpers (saves computation, goals+assists, TOI parsing)
  • _season_ids() returns correct season strings
  • Player registry loading (name→ID, skater/goalie position)
  • Fuzzy name matching
  • Game log fetching for skaters and goalies
  • fetch_results end-to-end
"""

from __future__ import annotations

import asyncio
import sys
import os
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.nhl_stats import (
    NHLStatsProvider,
    _SKATER_STAT_MAP,
    _GOALIE_STAT_MAP,
    _extract_stat,
    _season_ids,
)

# ── Shared event loop ─────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Stat maps
# ─────────────────────────────────────────────────────────────────────────────

class TestSkaterStatMap:
    def test_goals(self):
        assert _SKATER_STAT_MAP["goals"] == "goals"

    def test_assists(self):
        assert _SKATER_STAT_MAP["assists"] == "assists"

    def test_points(self):
        assert _SKATER_STAT_MAP["points"] == "points"

    def test_shots_on_goal(self):
        assert _SKATER_STAT_MAP["shots on goal"] == "shots"

    def test_shots_alias(self):
        assert _SKATER_STAT_MAP["shots"] == "shots"

    def test_power_play_goals(self):
        assert _SKATER_STAT_MAP["power play goals"] == "powerPlayGoals"

    def test_power_play_points(self):
        assert _SKATER_STAT_MAP["power play points"] == "powerPlayPoints"

    def test_time_on_ice(self):
        assert _SKATER_STAT_MAP["time on ice"] == "_toi_minutes"

    def test_goals_plus_assists_combo(self):
        assert _SKATER_STAT_MAP["goals + assists"] == "_goals_assists"

    def test_gpa_alias(self):
        assert _SKATER_STAT_MAP["g+a"] == "_goals_assists"

    def test_hits_is_none(self):
        """Hits are not in the game log — explicitly None."""
        assert _SKATER_STAT_MAP["hits"] is None


class TestGoalieStatMap:
    def test_saves(self):
        assert _GOALIE_STAT_MAP["saves"] == "_saves"

    def test_goalkeeper_saves(self):
        assert _GOALIE_STAT_MAP["goalkeeper saves"] == "_saves"

    def test_goals_allowed(self):
        assert _GOALIE_STAT_MAP["goals allowed"] == "goalsAgainst"

    def test_save_percentage(self):
        assert _GOALIE_STAT_MAP["save percentage"] == "savePctg"

    def test_shutouts(self):
        assert _GOALIE_STAT_MAP["shutouts"] == "shutouts"

    def test_shots_against(self):
        assert _GOALIE_STAT_MAP["shots against"] == "shotsAgainst"


# ─────────────────────────────────────────────────────────────────────────────
# _extract_stat
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractStat:
    def test_simple_field(self):
        assert _extract_stat({"goals": 2}, "goals") == 2.0

    def test_simple_field_float(self):
        assert _extract_stat({"savePctg": 0.9259}, "savePctg") == pytest.approx(0.9259)

    def test_missing_field_returns_none(self):
        assert _extract_stat({}, "goals") is None

    def test_saves_computed(self):
        game = {"shotsAgainst": 30, "goalsAgainst": 2}
        assert _extract_stat(game, "_saves") == 28.0

    def test_saves_clamps_to_zero(self):
        """shotsAgainst < goalsAgainst is impossible but should not return negative."""
        game = {"shotsAgainst": 1, "goalsAgainst": 3}
        assert _extract_stat(game, "_saves") == 0.0

    def test_saves_missing_shots_against_returns_none(self):
        assert _extract_stat({"goalsAgainst": 2}, "_saves") is None

    def test_saves_missing_goals_against_returns_none(self):
        assert _extract_stat({"shotsAgainst": 28}, "_saves") is None

    def test_goals_assists_combo(self):
        game = {"goals": 1, "assists": 2}
        assert _extract_stat(game, "_goals_assists") == 3.0

    def test_goals_assists_missing_field_returns_none(self):
        assert _extract_stat({"goals": 1}, "_goals_assists") is None

    def test_toi_parsed_to_minutes(self):
        game = {"toi": "24:49"}
        val = _extract_stat(game, "_toi_minutes")
        assert val == pytest.approx(24 + 49 / 60, rel=1e-3)

    def test_toi_whole_minutes(self):
        assert _extract_stat({"toi": "20:00"}, "_toi_minutes") == pytest.approx(20.0)

    def test_toi_missing_returns_none(self):
        assert _extract_stat({}, "_toi_minutes") is None

    def test_toi_malformed_returns_none(self):
        assert _extract_stat({"toi": "24"}, "_toi_minutes") is None

    def test_non_numeric_field_returns_none(self):
        assert _extract_stat({"goals": "n/a"}, "goals") is None


# ─────────────────────────────────────────────────────────────────────────────
# _season_ids
# ─────────────────────────────────────────────────────────────────────────────

class TestSeasonIds:
    def test_returns_two_seasons(self):
        seasons = _season_ids()
        assert len(seasons) == 2

    def test_most_recent_first(self):
        s1, s2 = _season_ids()
        assert int(s1[:4]) > int(s2[:4])

    def test_each_season_eight_chars(self):
        for s in _season_ids():
            assert len(s) == 8

    def test_season_format_consecutive_years(self):
        """Each season string encodes consecutive years: YYYYYYYY where Y2=Y1+1."""
        for s in _season_ids():
            y1, y2 = int(s[:4]), int(s[4:])
            assert y2 == y1 + 1


# ─────────────────────────────────────────────────────────────────────────────
# Registry loading
# ─────────────────────────────────────────────────────────────────────────────

_FAKE_SKATER_BIOS = {
    "data": [
        {"playerId": 8478402, "skaterFullName": "Connor McDavid"},
        {"playerId": 8481528, "skaterFullName": "Leon Draisaitl"},
    ],
    "total": 2,
}

_FAKE_GOALIE_BIOS = {
    "data": [
        {"playerId": 8480382, "goalieFullName": "Jake Oettinger"},
    ],
    "total": 1,
}


def _make_provider_with_registry(skater_bios=None, goalie_bios=None) -> NHLStatsProvider:
    """Build a provider with pre-populated registry (no HTTP needed)."""
    provider = NHLStatsProvider()
    sb = skater_bios or _FAKE_SKATER_BIOS
    gb = goalie_bios or _FAKE_GOALIE_BIOS

    for p in (sb.get("data") or []):
        pid = p["playerId"]
        name = p.get("skaterFullName") or p.get("fullName", "")
        if pid and name:
            from providers.nhl_stats import _normalize
            provider._name_to_id[_normalize(name)] = int(pid)
            provider._is_goalie[int(pid)] = False

    for p in (gb.get("data") or []):
        pid = p["playerId"]
        name = p.get("goalieFullName") or p.get("fullName", "")
        if pid and name:
            from providers.nhl_stats import _normalize
            provider._name_to_id[_normalize(name)] = int(pid)
            provider._is_goalie[int(pid)] = True

    provider._registry_ready = True
    return provider


class TestRegistryLoading:
    def test_registry_loaded_sets_ready(self):
        provider = NHLStatsProvider()
        call_count = 0

        async def mock_get_json(url: str):
            nonlocal call_count
            call_count += 1
            if "skater/bios" in url:
                return _FAKE_SKATER_BIOS
            if "goalie/bios" in url:
                return _FAKE_GOALIE_BIOS
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            run(provider._ensure_registry())

        assert provider._registry_ready is True
        assert "connor mcdavid" in provider._name_to_id
        assert "jake oettinger"  in provider._name_to_id

    def test_skater_marked_as_not_goalie(self):
        provider = _make_provider_with_registry()
        assert provider._is_goalie[8478402] is False

    def test_goalie_marked_as_goalie(self):
        provider = _make_provider_with_registry()
        assert provider._is_goalie[8480382] is True

    def test_registry_loaded_only_once(self):
        provider = NHLStatsProvider()
        call_count = 0

        async def mock_get_json(url: str):
            nonlocal call_count
            call_count += 1
            if "skater/bios" in url:
                return _FAKE_SKATER_BIOS
            return _FAKE_GOALIE_BIOS

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            run(provider._ensure_registry())
            run(provider._ensure_registry())  # second call — should be no-op

        assert call_count == 2  # skater + goalie, not 4

    def test_exact_name_lookup(self):
        provider = _make_provider_with_registry()
        assert provider._lookup_player_id("Connor McDavid") == 8478402

    def test_case_insensitive_lookup(self):
        provider = _make_provider_with_registry()
        assert provider._lookup_player_id("connor mcdavid") == 8478402

    def test_fuzzy_name_lookup(self):
        provider = _make_provider_with_registry()
        # Slight misspelling
        result = provider._lookup_player_id("Connor McDavids")
        assert result == 8478402

    def test_unknown_player_returns_none(self):
        provider = _make_provider_with_registry()
        assert provider._lookup_player_id("Totally Unknown Player XYZ") is None

    def test_empty_registry_returns_none(self):
        provider = NHLStatsProvider()
        assert provider._lookup_player_id("Connor McDavid") is None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for gamelog mock responses
# ─────────────────────────────────────────────────────────────────────────────

def _skater_game(date: str, goals=1, assists=2, points=3, shots=4,
                 pp_goals=0, pp_points=1, toi="22:30", opp="TOR") -> dict:
    return {
        "gameDate":        date,
        "goals":           goals,
        "assists":         assists,
        "points":          points,
        "shots":           shots,
        "powerPlayGoals":  pp_goals,
        "powerPlayPoints": pp_points,
        "toi":             toi,
        "opponentAbbrev":  opp,
    }


def _goalie_game(date: str, shots_against=32, goals_against=2, toi="59:00",
                 shutouts=0, opp="EDM") -> dict:
    return {
        "gameDate":      date,
        "shotsAgainst":  shots_against,
        "goalsAgainst":  goals_against,
        "savePctg":      round((shots_against - goals_against) / shots_against, 4),
        "shutouts":      shutouts,
        "toi":           toi,
        "opponentAbbrev": opp,
    }


def _gamelog_response(games: list) -> dict:
    return {"gameLog": games}


# ─────────────────────────────────────────────────────────────────────────────
# Skater fetch_results
# ─────────────────────────────────────────────────────────────────────────────

class TestSkaterFetchResults:
    _PLAYER = "Connor McDavid"

    def _provider(self) -> NHLStatsProvider:
        return _make_provider_with_registry()

    def test_goals(self):
        provider = self._provider()
        gamelog = _gamelog_response([
            _skater_game("2025-10-15", goals=1),
            _skater_game("2025-10-18", goals=2),
        ])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))

        assert len(results) == 2
        vals = sorted(r.actual_value for r in results)
        assert vals == [1.0, 2.0]

    def test_assists(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", assists=3)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "assists"))

        assert results[0].actual_value == 3.0

    def test_shots_on_goal(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", shots=5)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "shots on goal"))

        assert results[0].actual_value == 5.0

    def test_shots_alias(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", shots=3)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "shots"))

        assert results[0].actual_value == 3.0

    def test_goals_plus_assists_combo(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", goals=1, assists=2)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals + assists"))

        assert results[0].actual_value == 3.0

    def test_time_on_ice(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", toi="24:30")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "time on ice"))

        assert results[0].actual_value == pytest.approx(24.5)

    def test_source_label(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))

        assert all(r.source == "nhl_stats_api" for r in results)

    def test_sport_label(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))

        assert all(r.sport == "NHL" for r in results)

    def test_opponent_populated(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", opp="VGK")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))

        assert results[0].opponent == "VGK"

    def test_unmapped_stat_returns_empty(self):
        provider = self._provider()

        async def mock_get_json(url: str):
            return _gamelog_response([_skater_game("2025-10-15")])

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "fantasy points"))

        assert results == []

    def test_unknown_player_returns_empty(self):
        provider = self._provider()

        async def mock_get_json(url: str):
            return None

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results("Nobody XYZ", "NHL", "goals"))

        assert results == []

    def test_lowercase_sport_handled(self):
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", goals=1)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "nhl", "goals"))

        assert len(results) == 1

    def test_deduplication_across_seasons(self):
        """Same game_date appearing in two season responses is stored only once."""
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15", goals=1)])

        async def mock_get_json(url: str):
            return gamelog   # returns same game for both seasons

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))

        # Despite two seasons queried, duplicate date is deduplicated
        dates = [r.game_date for r in results]
        assert len(dates) == len(set(dates))

    def test_hits_stat_not_applicable_returns_empty(self):
        """Hits are not in the NHL game log — should return []."""
        provider = self._provider()
        gamelog = _gamelog_response([_skater_game("2025-10-15")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "hits"))

        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Goalie fetch_results
# ─────────────────────────────────────────────────────────────────────────────

class TestGoalieFetchResults:
    _PLAYER = "Jake Oettinger"

    def _provider(self) -> NHLStatsProvider:
        return _make_provider_with_registry()

    def test_saves_computed(self):
        provider = self._provider()
        gamelog = _gamelog_response([_goalie_game("2025-10-15", shots_against=32, goals_against=2)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "saves"))

        assert len(results) == 1
        assert results[0].actual_value == 30.0

    def test_goalkeeper_saves_alias(self):
        provider = self._provider()
        gamelog = _gamelog_response([_goalie_game("2025-10-15", shots_against=28, goals_against=1)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goalkeeper saves"))

        assert results[0].actual_value == 27.0

    def test_goals_allowed(self):
        provider = self._provider()
        gamelog = _gamelog_response([_goalie_game("2025-10-15", goals_against=3)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals allowed"))

        assert results[0].actual_value == 3.0

    def test_shutouts(self):
        provider = self._provider()
        gamelog = _gamelog_response([_goalie_game("2025-10-15", shutouts=1)])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "shutouts"))

        assert results[0].actual_value == 1.0

    def test_source_is_nhl_stats_api(self):
        provider = self._provider()
        gamelog = _gamelog_response([_goalie_game("2025-10-15")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "saves"))

        assert all(r.source == "nhl_stats_api" for r in results)

    def test_goalie_goals_stat_not_applicable(self):
        """'goals' is a skater stat — should return [] for a goalie."""
        provider = self._provider()
        gamelog = _gamelog_response([_goalie_game("2025-10-15")])

        async def mock_get_json(url: str):
            return gamelog

        with patch.object(provider, "_get_json", side_effect=mock_get_json):
            results = run(provider.fetch_results(self._PLAYER, "NHL", "goals"))

        # goals → skater field, but Jake is a goalie; field not in _GOALIE_STAT_MAP
        # → _GOALIE_STAT_MAP.get("goals", "__missing__") → "__missing__" → []
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Integration: player_stats.py routes NHL to NHLStatsProvider
# ─────────────────────────────────────────────────────────────────────────────

class TestNHLDispatchViaPlayerStats:
    """Confirm fetch_results() routes NHL to NHLStatsProvider (not ESPN)."""

    def test_nhl_routed_to_nhl_provider(self):
        from providers import player_stats as ps

        provider = ps.PlayerStatsProvider()
        called_with: list = []

        async def fake_fetch(player_name, sport, stat_type):
            called_with.append((player_name, sport, stat_type))
            return []

        # Replace the NHL provider singleton's fetch_results
        mock_nhl = type("MockNHL", (), {"fetch_results": staticmethod(fake_fetch)})()

        import providers.player_stats as ps_mod
        original = ps_mod._nhl_provider_instance
        ps_mod._nhl_provider_instance = mock_nhl
        try:
            run(provider.fetch_results("Connor McDavid", "NHL", "shots on goal"))
        finally:
            ps_mod._nhl_provider_instance = original

        assert len(called_with) == 1
        assert called_with[0] == ("Connor McDavid", "NHL", "shots on goal")


# ─────────────────────────────────────────────────────────────────────────────
# API contract: bios URL form (cayenneExp required)
# ─────────────────────────────────────────────────────────────────────────────

class TestBiosUrlContract:
    """
    Verify _ensure_registry() builds bios URLs with cayenneExp — NOT plain
    seasonId/gameTypeId query params.

    Background: api.nhle.com/stats/rest/en/skater/bios returns HTTP 500 when
    seasonId and gameTypeId are passed as ordinary query params.  The correct
    form is ?limit=N&start=K&cayenneExp=seasonId=SSSS+and+gameTypeId=G.
    Plain-param form silently breaks the registry (returns nothing but no error)
    so it must be caught at the test level before it reaches production.
    """

    def _capture_urls(self, provider: NHLStatsProvider) -> list[str]:
        """Run _ensure_registry(), capturing every URL _get_json receives."""
        captured: list[str] = []

        async def capturing_get_json(url: str):
            captured.append(url)
            # Return a one-player page so the loop terminates
            if "skater/bios" in url:
                return {"data": [{"playerId": 8478402, "skaterFullName": "Connor McDavid"}], "total": 1}
            if "goalie/bios" in url:
                return {"data": [{"playerId": 8479979, "goalieFullName": "Jake Oettinger"}], "total": 1}
            return None

        with patch.object(provider, "_get_json", side_effect=capturing_get_json):
            run(provider._ensure_registry())
        return captured

    def test_skater_bios_url_uses_cayenne_exp(self):
        provider = NHLStatsProvider()
        urls = self._capture_urls(provider)
        skater_url = next(u for u in urls if "skater/bios" in u)
        assert "cayenneExp" in skater_url, (
            f"skater/bios URL must use cayenneExp, got: {skater_url}"
        )

    def test_goalie_bios_url_uses_cayenne_exp(self):
        provider = NHLStatsProvider()
        urls = self._capture_urls(provider)
        goalie_url = next(u for u in urls if "goalie/bios" in u)
        assert "cayenneExp" in goalie_url, (
            f"goalie/bios URL must use cayenneExp, got: {goalie_url}"
        )

    def test_skater_bios_url_has_no_plain_season_id_param(self):
        """seasonId must NOT appear as a standalone query param (that form → HTTP 500)."""
        provider = NHLStatsProvider()
        urls = self._capture_urls(provider)
        skater_url = next(u for u in urls if "skater/bios" in u)
        # cayenneExp=seasonId=... is fine; &seasonId= (bare param) is wrong
        import re
        bare_param = re.search(r"[?&]seasonId=\d", skater_url)
        assert bare_param is None, (
            f"skater/bios URL must not pass seasonId as a bare param: {skater_url}"
        )

    def test_skater_bios_url_has_no_plain_game_type_id_param(self):
        """gameTypeId must NOT appear as a standalone query param."""
        provider = NHLStatsProvider()
        urls = self._capture_urls(provider)
        skater_url = next(u for u in urls if "skater/bios" in u)
        import re
        bare_param = re.search(r"[?&]gameTypeId=\d", skater_url)
        assert bare_param is None, (
            f"skater/bios URL must not pass gameTypeId as a bare param: {skater_url}"
        )

    def test_skater_bios_url_contains_season_id_in_cayenne(self):
        provider = NHLStatsProvider()
        urls = self._capture_urls(provider)
        skater_url = next(u for u in urls if "skater/bios" in u)
        # cayenneExp must encode seasonId=NNNN
        assert "seasonId=" in skater_url

    def test_registry_not_marked_ready_when_empty(self):
        """If both bio endpoints return empty data, registry stays un-ready for retry."""
        provider = NHLStatsProvider()

        async def empty_get_json(url: str):
            return {"data": [], "total": 0}

        with patch.object(provider, "_get_json", side_effect=empty_get_json):
            run(provider._ensure_registry())

        assert provider._registry_ready is False
        assert len(provider._name_to_id) == 0

    def test_registry_marked_ready_when_players_loaded(self):
        provider = NHLStatsProvider()

        async def ok_get_json(url: str):
            if "skater/bios" in url:
                return {"data": [{"playerId": 1, "skaterFullName": "Test Player"}], "total": 1}
            return {"data": [], "total": 0}

        with patch.object(provider, "_get_json", side_effect=ok_get_json):
            run(provider._ensure_registry())

        assert provider._registry_ready is True

    def test_pagination_fetches_multiple_pages(self):
        """When a page is full (len==page_size), the next page is fetched."""
        provider = NHLStatsProvider()
        calls: list[str] = []

        async def paged_get_json(url: str):
            calls.append(url)
            if "skater/bios" in url:
                import re
                start_m = re.search(r"start=(\d+)", url)
                start = int(start_m.group(1)) if start_m else 0
                if start == 0:
                    # Return full page of 100 to trigger next-page fetch
                    # IDs start at 1 (0 is falsy and would be skipped)
                    return {
                        "data": [{"playerId": i+1, "skaterFullName": f"Player {i+1}"} for i in range(100)],
                        "total": 150,
                    }
                else:
                    # Second page: 50 players (partial → stops)
                    return {
                        "data": [{"playerId": 101+i, "skaterFullName": f"Player {101+i}"} for i in range(50)],
                        "total": 150,
                    }
            return {"data": [], "total": 0}

        with patch.object(provider, "_get_json", side_effect=paged_get_json):
            run(provider._ensure_registry())

        skater_calls = [u for u in calls if "skater/bios" in u]
        assert len(skater_calls) == 2, "Expected exactly 2 skater-bio pages fetched"
        assert len(provider._name_to_id) >= 150


# ─────────────────────────────────────────────────────────────────────────────
# Integration: config has NHL in UD_ALERT_SPORTS
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigNHL:
    def test_nhl_in_ud_alert_sports(self):
        import config as cfg
        c = cfg.Config()
        assert "NHL" in c.ud_alert_sports

    def test_soccer_not_in_default_ud_alert_sports(self):
        """
        SOCCER is NOT in the default: the free API tier lacks lineup/appearance
        data, so DNPs can't be distinguished from zero-stat games.
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
            assert sport in c.ud_alert_sports, f"{sport} missing"
