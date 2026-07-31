"""
Tests for:
  • _resolve_alias   — alias table look-up helper
  • _group_into_series — series grouping by start_time proximity
  • DOTA multi-map cumulative via real series pairing (not scaling)
  • WARNING logs when player is not found in OpenDota / PandaScore
  • _DOTA_PLAYER_ALIASES and _CS_PLAYER_ALIASES completeness
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.esports_stats import (
    EsportsStatsProvider,
    _DOTA_PLAYER_ALIASES,
    _CS_PLAYER_ALIASES,
    _DOTA_FIELD_MAP,
    _CS_FIELD_MAP,
    _group_into_series,
    _resolve_alias,
    _strip_none_prefix,
)


# ── _resolve_alias ─────────────────────────────────────────────────────────────

class TestResolveAlias:
    def test_known_dota_alias_returns_canonical(self):
        assert _resolve_alias("miracle", _DOTA_PLAYER_ALIASES) == "Miracle-"

    def test_known_dota_alias_case_insensitive(self):
        assert _resolve_alias("Miracle", _DOTA_PLAYER_ALIASES) == "Miracle-"

    def test_known_dota_alias_with_whitespace(self):
        assert _resolve_alias("  shine  ", _DOTA_PLAYER_ALIASES) == "SHiNE"

    def test_unknown_name_returns_original(self):
        assert _resolve_alias("SomeUnknownPro", _DOTA_PLAYER_ALIASES) == "SomeUnknownPro"

    def test_known_cs_alias(self):
        assert _resolve_alias("zywoo", _CS_PLAYER_ALIASES) == "ZywOo"

    def test_known_cs_alias_hunter(self):
        assert _resolve_alias("hunter", _CS_PLAYER_ALIASES) == "huNter-"

    def test_unknown_cs_name_passthrough(self):
        assert _resolve_alias("SomeCSPro", _CS_PLAYER_ALIASES) == "SomeCSPro"

    def test_empty_string_passthrough(self):
        # empty string → .lower().strip() == "" → not in dict → returns original ""
        assert _resolve_alias("", _DOTA_PLAYER_ALIASES) == ""

    def test_digit_only_alias(self):
        assert _resolve_alias("33", _DOTA_PLAYER_ALIASES) == "33"


# ── _group_into_series ─────────────────────────────────────────────────────────

class TestGroupIntoSeries:
    BASE_TIME = 1_700_000_000  # arbitrary Unix timestamp

    def _match(self, offset_seconds: int, **extra) -> dict:
        return {"start_time": self.BASE_TIME + offset_seconds, **extra}

    def test_empty_returns_empty(self):
        assert _group_into_series([]) == []

    def test_single_match_is_single_series(self):
        matches = [self._match(0)]
        groups  = _group_into_series(matches)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_two_close_matches_are_one_series(self):
        matches = [self._match(0), self._match(3600)]  # 1 h apart
        groups  = _group_into_series(matches)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_two_distant_matches_are_two_series(self):
        matches = [self._match(0), self._match(86400)]  # 24 h apart
        groups  = _group_into_series(matches)
        assert len(groups) == 2

    def test_three_games_bo3_grouped_correctly(self):
        # BO3: games 45 min apart
        matches = [
            self._match(0),
            self._match(2700),   # 45 min
            self._match(5400),   # 90 min total
        ]
        groups = _group_into_series(matches)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_series_boundary_exactly_at_max_gap(self):
        # Gap exactly equals max (14400 s) → same series (≤ boundary)
        matches = [self._match(0), self._match(14400)]
        groups  = _group_into_series(matches, max_gap_seconds=14400)
        assert len(groups) == 1

    def test_series_boundary_one_second_over(self):
        matches = [self._match(0), self._match(14401)]
        groups  = _group_into_series(matches, max_gap_seconds=14400)
        assert len(groups) == 2

    def test_matches_without_start_time_skipped(self):
        matches = [
            {"kills": 5},                     # no start_time
            self._match(0),
            self._match(3600),
        ]
        groups = _group_into_series(matches)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_newest_series_first(self):
        """_group_into_series returns newest series first (consistent with other providers)."""
        older = [self._match(0), self._match(1800)]                          # day 1
        newer = [self._match(86400), self._match(90000)]                     # day 2
        all_  = older + newer
        groups = _group_into_series(all_)
        assert len(groups) == 2
        # first group should have newer timestamps
        first_time  = groups[0][0]["start_time"]
        second_time = groups[1][0]["start_time"]
        assert first_time > second_time

    def test_mixed_order_input_handled(self):
        # Input is not sorted by time — function should sort internally
        matches = [self._match(3600), self._match(0)]
        groups  = _group_into_series(matches)
        assert len(groups) == 1
        # Should be sorted ascending within the series
        assert groups[0][0]["start_time"] <= groups[0][1]["start_time"]

    def test_custom_max_gap(self):
        matches = [self._match(0), self._match(7200)]  # 2 h apart
        groups_tight = _group_into_series(matches, max_gap_seconds=3600)   # 1 h → 2 series
        groups_wide  = _group_into_series(matches, max_gap_seconds=14400)  # 4 h → 1 series
        assert len(groups_tight) == 2
        assert len(groups_wide)  == 1


# ── DOTA series pairing — _fetch_dota ─────────────────────────────────────────

BASE_T = 1_700_000_000


def _mk_dota_match(offset: int, kills: int = 8, assists: int = 5, deaths: int = 2,
                   last_hits: int = 100, gpm: int = 400) -> dict:
    return {
        "start_time": BASE_T + offset,
        "kills":      kills,
        "assists":    assists,
        "deaths":     deaths,
        "last_hits":  last_hits,
        "gold_per_min": gpm,
    }


class TestDotaSeriesPairing:
    """Verify that Maps 1+2 props use true cumulative sums, not per-game * 2."""

    def _provider(self) -> EsportsStatsProvider:
        return EsportsStatsProvider()

    @pytest.fixture
    def two_game_series_data(self):
        """Two games 45 min apart (same series) + two games 24 h later (new series)."""
        return [
            _mk_dota_match(0,     kills=10),   # series 1 game 1
            _mk_dota_match(2700,  kills=6),    # series 1 game 2  (sum=16)
            _mk_dota_match(86400, kills=12),   # series 2 game 1
            _mk_dota_match(89100, kills=4),    # series 2 game 2  (sum=16)
        ]

    @patch("providers.esports_stats.EsportsStatsProvider._opendota_account_id")
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_maps_1_plus_2_uses_real_sum(self, mock_get_json, mock_account_id, two_game_series_data):
        mock_account_id.side_effect = AsyncMock(return_value=12345)
        mock_get_json.side_effect   = AsyncMock(return_value=two_game_series_data)

        provider = self._provider()
        loop     = asyncio.new_event_loop()
        results  = loop.run_until_complete(
            provider._fetch_dota("Samppa", "None Samppa", "kills on maps 1+2")
        )
        loop.close()

        # Should produce 2 results (one per series)
        assert len(results) == 2
        # Each should be the TRUE sum (10+6=16, 12+4=16), NOT per-game*2 (8*2=16 coincidence)
        actual_vals = sorted([r.actual_value for r in results])
        assert actual_vals == [16.0, 16.0]

    @patch("providers.esports_stats.EsportsStatsProvider._opendota_account_id")
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_maps_1_plus_2_source_tag_is_series(self, mock_get_json, mock_account_id, two_game_series_data):
        mock_account_id.side_effect = AsyncMock(return_value=12345)
        mock_get_json.side_effect   = AsyncMock(return_value=two_game_series_data)

        provider = self._provider()
        loop     = asyncio.new_event_loop()
        results  = loop.run_until_complete(
            provider._fetch_dota("Samppa", "None Samppa", "kills on maps 1+2")
        )
        loop.close()

        for r in results:
            assert r.source == "opendota_series"

    @patch("providers.esports_stats.EsportsStatsProvider._opendota_account_id")
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_single_map_still_uses_individual_games(self, mock_get_json, mock_account_id, two_game_series_data):
        mock_account_id.side_effect = AsyncMock(return_value=12345)
        mock_get_json.side_effect   = AsyncMock(return_value=two_game_series_data)

        provider = self._provider()
        loop     = asyncio.new_event_loop()
        results  = loop.run_until_complete(
            provider._fetch_dota("Samppa", "None Samppa", "kills on map 1")
        )
        loop.close()

        # 4 individual games → 4 results
        assert len(results) == 4
        for r in results:
            assert r.source == "opendota"

    @patch("providers.esports_stats.EsportsStatsProvider._opendota_account_id")
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_incomplete_series_skipped(self, mock_get_json, mock_account_id):
        """A series with only 1 game is skipped for a Maps 1+2 prop."""
        # Only 1 game — series has insufficient maps for Maps 1+2
        data = [_mk_dota_match(0, kills=10)]
        mock_account_id.side_effect = AsyncMock(return_value=12345)
        mock_get_json.side_effect   = AsyncMock(return_value=data)

        provider = self._provider()
        loop     = asyncio.new_event_loop()
        results  = loop.run_until_complete(
            provider._fetch_dota("Samppa", "None Samppa", "kills on maps 1+2")
        )
        loop.close()

        assert results == []

    @patch("providers.esports_stats.EsportsStatsProvider._opendota_account_id")
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_maps_1_2_3_uses_three_game_sum(self, mock_get_json, mock_account_id):
        """Maps 1+2+3 sums the first 3 games of each series."""
        data = [
            _mk_dota_match(0,    kills=5),
            _mk_dota_match(2700, kills=7),
            _mk_dota_match(5400, kills=9),  # 3-game series: sum = 21
        ]
        mock_account_id.side_effect = AsyncMock(return_value=12345)
        mock_get_json.side_effect   = AsyncMock(return_value=data)

        provider = self._provider()
        loop     = asyncio.new_event_loop()
        results  = loop.run_until_complete(
            provider._fetch_dota("Samppa", "None Samppa", "kills on maps 1+2+3")
        )
        loop.close()

        assert len(results) == 1
        assert results[0].actual_value == 21.0


# ── Warning log when player not found ─────────────────────────────────────────

class TestPlayerNotFoundWarning:
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_opendota_not_found_emits_warning(self, mock_get_json, caplog):
        mock_get_json.side_effect = AsyncMock(return_value=[])  # empty search results

        provider = EsportsStatsProvider()
        loop     = asyncio.new_event_loop()
        with caplog.at_level(logging.WARNING, logger="providers.esports_stats"):
            loop.run_until_complete(
                provider._opendota_account_id("NonExistentPro", original_name="NonExistentPro")
            )
        loop.close()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("not found" in m for m in warning_msgs)
        assert any("_DOTA_PLAYER_ALIASES" in m for m in warning_msgs)

    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_pandascore_not_found_emits_warning(self, mock_get_json, caplog):
        mock_get_json.side_effect = AsyncMock(return_value=[])

        provider = EsportsStatsProvider()
        provider._pandascore_key = "fake-key"
        loop = asyncio.new_event_loop()
        with caplog.at_level(logging.WARNING, logger="providers.esports_stats"):
            loop.run_until_complete(
                provider._pandascore_player_id("NonExistentCS2Pro", original_name="NonExistentCS2Pro")
            )
        loop.close()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("not found" in m for m in warning_msgs)
        assert any("_CS_PLAYER_ALIASES" in m for m in warning_msgs)


# ── Alias table completeness ───────────────────────────────────────────────────

class TestAliasTableCompleteness:
    def test_dota_alias_values_are_non_empty_strings(self):
        for key, val in _DOTA_PLAYER_ALIASES.items():
            assert isinstance(val, str) and val, f"DOTA alias {key!r} maps to empty/non-str"

    def test_cs_alias_values_are_non_empty_strings(self):
        for key, val in _CS_PLAYER_ALIASES.items():
            assert isinstance(val, str) and val, f"CS alias {key!r} maps to empty/non-str"

    def test_dota_alias_keys_are_lowercase(self):
        for key in _DOTA_PLAYER_ALIASES:
            assert key == key.lower(), f"DOTA alias key {key!r} is not lowercase"

    def test_cs_alias_keys_are_lowercase(self):
        for key in _CS_PLAYER_ALIASES:
            assert key == key.lower(), f"CS alias key {key!r} is not lowercase"

    def test_dota_alias_table_non_empty(self):
        assert len(_DOTA_PLAYER_ALIASES) >= 10

    def test_cs_alias_table_non_empty(self):
        assert len(_CS_PLAYER_ALIASES) >= 10


# ── Alias integration: resolve_alias flows through _opendota_account_id ───────

class TestAliasIntegration:
    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_miracle_alias_searches_with_dash(self, mock_get_json):
        """'miracle' → 'Miracle-' in the OpenDota search URL."""
        captured_urls: list[str] = []

        async def _mock_get(url: str):
            captured_urls.append(url)
            # Return a fake matching result for the first call (search)
            if "search" in url:
                return [{"personaname": "Miracle-", "account_id": 99999}]
            # recentMatches
            return []

        mock_get_json.side_effect = _mock_get

        provider = EsportsStatsProvider()
        loop     = asyncio.new_event_loop()
        loop.run_until_complete(
            provider._fetch_dota("miracle", "None miracle", "kills")
        )
        loop.close()

        search_urls = [u for u in captured_urls if "search" in u]
        assert search_urls, "No OpenDota search URL captured"
        # Miracle- should appear in the query
        assert "Miracle" in search_urls[0] or "miracle" in search_urls[0].lower()

    @patch("providers.esports_stats.EsportsStatsProvider._get_json")
    def test_unknown_name_falls_back_to_original(self, mock_get_json):
        """An unaliased name is searched as-is (no error)."""
        async def _mock_get(url: str):
            if "search" in url:
                return []
            return []

        mock_get_json.side_effect = _mock_get

        provider = EsportsStatsProvider()
        loop     = asyncio.new_event_loop()
        results  = loop.run_until_complete(
            provider._fetch_dota("XYZunknown", "XYZunknown", "kills")
        )
        loop.close()

        assert results == []
