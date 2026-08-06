"""
tests/test_sleeper_stats.py — Unit tests for SleeperStatsProvider.

All tests are pure unit tests — no real HTTP calls. The provider's _get_json
and _ensure_registry methods are patched so the entire network layer is mocked.
"""

from __future__ import annotations

import asyncio
import sys
import os
from datetime import date
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure bot/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.sleeper_stats import (
    SleeperStatsProvider,
    _iso_week_monday,
    _nfl_week_to_date,
    _sum_stat_keys,
)
from providers.player_stats import RawGameResult, _merge_game_results as _ps_merge
# _merge_game_results lives in player_stats; alias it for convenience in this module
_merge_game_results = _ps_merge


# ── Shared event loop ─────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


def run(coro):
    return _loop.run_until_complete(coro)


# ── _sum_stat_keys ─────────────────────────────────────────────────────────────

class TestSumStatKeys:
    def test_single_key_found(self):
        assert _sum_stat_keys({"rush_yd": 87.0}, ["rush_yd"]) == 87.0

    def test_combo_keys_summed(self):
        stats = {"blk": 2.0, "stl": 1.0}
        assert _sum_stat_keys(stats, ["blk", "stl"]) == 3.0

    def test_missing_key_treated_as_zero_when_other_present(self):
        # "blk" present but "stl" absent → stl treated as 0
        stats = {"blk": 2.0}
        assert _sum_stat_keys(stats, ["blk", "stl"]) == 2.0

    def test_all_keys_absent_returns_none(self):
        assert _sum_stat_keys({"pts": 20.0}, ["rush_yd"]) is None

    def test_empty_keys_returns_none(self):
        assert _sum_stat_keys({"rush_yd": 50.0}, []) is None

    def test_empty_stats_returns_none(self):
        assert _sum_stat_keys({}, ["rush_yd"]) is None

    def test_non_numeric_value_skipped(self):
        stats = {"rush_yd": "bad", "rec_yd": 30.0}
        result = _sum_stat_keys(stats, ["rush_yd", "rec_yd"])
        # "bad" skipped, rec_yd counted as any_data=True
        assert result == 30.0

    def test_zero_value_counted_as_data(self):
        # A player who played but got 0 yards — that's data, not missing
        assert _sum_stat_keys({"rush_yd": 0.0}, ["rush_yd"]) == 0.0

    def test_integer_values_converted_to_float(self):
        assert _sum_stat_keys({"rec": 7}, ["rec"]) == 7.0


# ── _nfl_week_to_date ─────────────────────────────────────────────────────────

class TestNflWeekToDate:
    def test_2025_week1(self):
        # Week 1 2025 starts Sept 4 (Thursday)
        assert _nfl_week_to_date(2025, 1) == "2025-09-04"

    def test_2025_week2(self):
        # Week 2 = one week later
        d = date(2025, 9, 4) + __import__("datetime").timedelta(weeks=1)
        assert _nfl_week_to_date(2025, 2) == d.isoformat()

    def test_2025_week18(self):
        d = date(2025, 9, 4) + __import__("datetime").timedelta(weeks=17)
        assert _nfl_week_to_date(2025, 18) == d.isoformat()

    def test_2026_week1(self):
        assert _nfl_week_to_date(2026, 1) == "2026-09-03"

    def test_unknown_year_fallback(self):
        result = _nfl_week_to_date(2030, 1)
        assert result.startswith("2030-09")


# ── _iso_week_monday ──────────────────────────────────────────────────────────

class TestIsoWeekMonday:
    def test_week_1_2025(self):
        d = _iso_week_monday(2025, 1)
        assert d == "2024-12-30"   # ISO week 1 2025 starts Dec 30 2024

    def test_week_10_2025(self):
        d = _iso_week_monday(2025, 10)
        from datetime import date
        expected = date.fromisocalendar(2025, 10, 1).isoformat()
        assert d == expected

    def test_invalid_week_returns_fallback(self):
        # Week 99 is invalid — should not raise
        result = _iso_week_monday(2025, 99)
        assert result == "2025-01-01"


# ── _merge_game_results (module-level helper) ─────────────────────────────────

def _make_result(game_date: str, val: float, source: str = "espn_gamelog") -> RawGameResult:
    return RawGameResult(
        player_name  = "Test Player",
        sport        = "NFL",
        stat_type    = "rushing yards",
        game_date    = game_date,
        actual_value = val,
        opponent     = None,
        source       = source,
    )


class TestMergeGameResults:
    def test_empty_secondary_returns_primary(self):
        primary = [_make_result("2025-09-04", 100.0)]
        assert _merge_game_results(primary, []) == primary

    def test_non_overlapping_dates_merged(self):
        primary   = [_make_result("2025-09-04", 100.0)]
        secondary = [_make_result("2025-09-11", 80.0, "sleeper_stats")]
        merged = _merge_game_results(primary, secondary)
        assert len(merged) == 2
        dates = {r.game_date for r in merged}
        assert dates == {"2025-09-04", "2025-09-11"}

    def test_overlapping_dates_primary_wins(self):
        primary   = [_make_result("2025-09-04", 100.0, "espn_gamelog")]
        secondary = [_make_result("2025-09-04", 55.0,  "sleeper_stats")]
        merged = _merge_game_results(primary, secondary)
        assert len(merged) == 1
        assert merged[0].source == "espn_gamelog"
        assert merged[0].actual_value == 100.0

    def test_empty_primary_returns_secondary(self):
        secondary = [_make_result("2025-09-04", 80.0, "sleeper_stats")]
        merged = _merge_game_results([], secondary)
        assert len(merged) == 1
        assert merged[0].source == "sleeper_stats"

    def test_player_stats_module_merge_also_works(self):
        """The same helper exported from player_stats.py behaves identically."""
        primary   = [_make_result("2025-09-04", 100.0)]
        secondary = [_make_result("2025-09-11", 40.0, "sleeper_stats")]
        merged = _ps_merge(primary, secondary)
        assert len(merged) == 2


# ── SleeperStatsProvider — stat key mapping ────────────────────────────────────

class TestStatKeyMapping:
    """Verify that stat types used by the bot map to valid Sleeper keys."""

    def test_nfl_rushing_yards(self):
        from providers.sleeper_stats import _NFL_STAT_KEYS
        assert _NFL_STAT_KEYS.get("rushing yards") == ["rush_yd"]

    def test_nfl_receiving_yards(self):
        from providers.sleeper_stats import _NFL_STAT_KEYS
        assert _NFL_STAT_KEYS.get("receiving yards") == ["rec_yd"]

    def test_nfl_passing_yards(self):
        from providers.sleeper_stats import _NFL_STAT_KEYS
        assert _NFL_STAT_KEYS.get("passing yards") == ["pass_yd"]

    def test_nfl_receptions(self):
        from providers.sleeper_stats import _NFL_STAT_KEYS
        assert _NFL_STAT_KEYS.get("receptions") == ["rec"]

    def test_nfl_targets(self):
        from providers.sleeper_stats import _NFL_STAT_KEYS
        assert _NFL_STAT_KEYS.get("targets") == ["rec_tgt"]

    def test_nba_points_blocks_steals_combo(self):
        from providers.sleeper_stats import _NBA_STAT_KEYS
        assert _NBA_STAT_KEYS.get("blocks + steals") == ["blk", "stl"]

    def test_nba_pra_combo(self):
        from providers.sleeper_stats import _NBA_STAT_KEYS
        assert _NBA_STAT_KEYS.get("points + rebounds + assists") == ["pts", "reb", "ast"]

    def test_mlb_hits_runs_rbis(self):
        from providers.sleeper_stats import _MLB_STAT_KEYS
        assert _MLB_STAT_KEYS.get("hits+runs+rbis") == ["hits", "runs", "rbi"]

    def test_no_mapping_for_unsupported_stat(self):
        from providers.sleeper_stats import _NFL_STAT_KEYS
        assert _NFL_STAT_KEYS.get("fantasy points") is None


# ── SleeperStatsProvider — player lookup ─────────────────────────────────────

class TestPlayerLookup:
    def _make_provider_with_registry(self, players: dict) -> SleeperStatsProvider:
        p = SleeperStatsProvider()
        p._registry["nfl"]       = players
        p._registry_ready["nfl"] = True
        name_map = {}
        for pid, pdata in players.items():
            full = pdata.get("full_name", "")
            if full:
                name_map[full.lower().strip()] = pid
        p._name_to_id["nfl"] = name_map
        return p

    def test_exact_match(self):
        provider = self._make_provider_with_registry({
            "4046": {"full_name": "Lamar Jackson"},
        })
        assert provider._lookup_player_id("nfl", "Lamar Jackson") == "4046"

    def test_case_insensitive_match(self):
        provider = self._make_provider_with_registry({
            "4046": {"full_name": "Lamar Jackson"},
        })
        assert provider._lookup_player_id("nfl", "lamar jackson") == "4046"

    def test_fuzzy_match(self):
        provider = self._make_provider_with_registry({
            "4046": {"full_name": "Lamar Jackson"},
        })
        # Slight misspelling
        result = provider._lookup_player_id("nfl", "Lamar Jacks")
        # Should still find the player via fuzzy match
        assert result == "4046"

    def test_unknown_player_returns_none(self):
        provider = self._make_provider_with_registry({
            "4046": {"full_name": "Lamar Jackson"},
        })
        assert provider._lookup_player_id("nfl", "Totally Unknown Person XYZ") is None

    def test_empty_registry_returns_none(self):
        provider = SleeperStatsProvider()
        assert provider._lookup_player_id("nfl", "Lamar Jackson") is None


# ── SleeperStatsProvider — fetch_results ──────────────────────────────────────

class TestFetchResults:
    """End-to-end fetch_results tests with mocked HTTP layer."""

    _FAKE_REGISTRY = {
        "4046": {"full_name": "Lamar Jackson", "first_name": "Lamar", "last_name": "Jackson"},
        "7564": {"full_name": "CeeDee Lamb",   "first_name": "CeeDee", "last_name": "Lamb"},
    }
    _FAKE_WEEK1_STATS = {
        "4046": {"rush_yd": 108.0, "rush_att": 12.0, "rush_td": 1.0,
                 "pass_yd": 250.0, "pass_td": 2.0, "rec": 0.0},
        "7564": {"rec": 7.0, "rec_yd": 110.0, "rec_td": 1.0},
    }
    _FAKE_WEEK2_STATS = {
        "4046": {"rush_yd": 55.0, "rush_att": 8.0, "rush_td": 0.0,
                 "pass_yd": 312.0, "pass_td": 3.0},
        "7564": {"rec": 5.0, "rec_yd": 72.0, "rec_td": 0.0},
    }

    def _make_provider(self) -> SleeperStatsProvider:
        p = SleeperStatsProvider()
        # Pre-load registry
        p._registry["nfl"]       = self._FAKE_REGISTRY
        p._registry_ready["nfl"] = True
        name_map = {
            v["full_name"].lower(): k
            for k, v in self._FAKE_REGISTRY.items()
        }
        p._name_to_id["nfl"] = name_map
        # Pre-load two weeks
        for yr in [2025]:
            p._week_cache[("nfl", "regular", yr, 1)] = self._FAKE_WEEK1_STATS
            p._week_cache[("nfl", "regular", yr, 2)] = self._FAKE_WEEK2_STATS
            # Weeks 3–18 are empty (season not played yet in this mock)
            for w in range(3, 19):
                p._week_cache[("nfl", "regular", yr, w)] = {}
        return p

    def test_nfl_rushing_yards_basic(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("Lamar Jackson", "NFL", "rushing yards"))
        assert len(results) == 2
        vals = {r.game_date: r.actual_value for r in results}
        assert vals["2025-09-04"] == 108.0   # week 1
        assert vals["2025-09-11"] == 55.0    # week 2

    def test_nfl_source_label(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("Lamar Jackson", "NFL", "rushing yards"))
        assert all(r.source == "sleeper_stats" for r in results)

    def test_nfl_sport_label(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("CeeDee Lamb", "NFL", "receptions"))
        assert all(r.sport == "NFL" for r in results)

    def test_nfl_receiving_yards(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("CeeDee Lamb", "NFL", "receiving yards"))
        assert len(results) == 2
        vals = {r.game_date: r.actual_value for r in results}
        assert vals["2025-09-04"] == 110.0
        assert vals["2025-09-11"] == 72.0

    def test_nfl_passing_yards(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("Lamar Jackson", "NFL", "passing yards"))
        assert len(results) == 2
        vals = {r.game_date: r.actual_value for r in results}
        assert vals["2025-09-04"] == 250.0
        assert vals["2025-09-11"] == 312.0

    def test_unknown_player_returns_empty(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("Nobody Exists", "NFL", "rushing yards"))
        assert results == []

    def test_unsupported_sport_returns_empty(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("Some Player", "DOTA", "kills"))
        assert results == []

    def test_unmapped_stat_type_returns_empty(self):
        provider = self._make_provider()
        results = run(provider.fetch_results("Lamar Jackson", "NFL", "fantasy score"))
        assert results == []

    def test_stat_type_case_insensitive(self):
        provider = self._make_provider()
        r1 = run(provider.fetch_results("Lamar Jackson", "NFL", "Rushing Yards"))
        r2 = run(provider.fetch_results("Lamar Jackson", "NFL", "rushing yards"))
        assert len(r1) == len(r2)

    def test_nfl_combo_stat_rush_rec_tds(self):
        """Rush + rec TDs should sum both keys."""
        provider = self._make_provider()
        results = run(provider.fetch_results("Lamar Jackson", "NFL", "rush + rec touchdowns"))
        # Week 1: rush_td=1, rec_td absent → 1.0
        assert len(results) == 2
        week1 = next(r for r in results if r.game_date == "2025-09-04")
        assert week1.actual_value == 1.0  # only rush_td present; rec_td absent → 0

    def test_week_with_no_player_data_skipped(self):
        """A week where the player has no stats entry is not included."""
        provider = self._make_provider()
        # Remove Lamar from week 2
        provider._week_cache[("nfl", "regular", 2025, 2)] = {
            "9999": {"rush_yd": 50.0}
        }
        results = run(provider.fetch_results("Lamar Jackson", "NFL", "rushing yards"))
        assert len(results) == 1
        assert results[0].game_date == "2025-09-04"


# ── SleeperStatsProvider — registry loading ────────────────────────────────────

class TestRegistryLoading:
    def test_registry_loaded_sets_ready_flag(self):
        provider = SleeperStatsProvider()

        async def _fake_get_json(url: str):
            return {
                "4046": {"full_name": "Lamar Jackson"},
                "7564": {"full_name": "CeeDee Lamb"},
            }

        with patch.object(provider, "_get_json", side_effect=_fake_get_json):
            run(provider._ensure_registry("nfl"))

        assert provider._registry_ready["nfl"] is True
        assert "lamar jackson" in provider._name_to_id["nfl"]
        assert "ceedee lamb"   in provider._name_to_id["nfl"]

    def test_registry_loaded_only_once(self):
        provider = SleeperStatsProvider()
        call_count = 0

        async def _fake_get_json(url: str):
            nonlocal call_count
            call_count += 1
            return {"4046": {"full_name": "Lamar Jackson"}}

        with patch.object(provider, "_get_json", side_effect=_fake_get_json):
            run(provider._ensure_registry("nfl"))
            run(provider._ensure_registry("nfl"))  # second call — should be skipped

        assert call_count == 1

    def test_empty_registry_response_does_not_set_ready(self):
        provider = SleeperStatsProvider()

        async def _fake_get_json(url: str):
            return None   # simulates HTTP failure

        with patch.object(provider, "_get_json", side_effect=_fake_get_json):
            run(provider._ensure_registry("nfl"))

        assert not provider._registry_ready.get("nfl")


# ── SleeperStatsProvider — week prefetch ──────────────────────────────────────

class TestWeekPrefetch:
    def test_weeks_cached_after_prefetch(self):
        provider = SleeperStatsProvider()
        fetched_urls: list[str] = []

        async def _fake_get_json(url: str):
            fetched_urls.append(url)
            return {"4046": {"rush_yd": 80.0}}

        with patch.object(provider, "_get_json", side_effect=_fake_get_json):
            run(provider._prefetch_weeks("nfl", "regular", 2025, [1, 2, 3]))

        assert len(fetched_urls) == 3
        assert ("nfl", "regular", 2025, 1) in provider._week_cache
        assert ("nfl", "regular", 2025, 2) in provider._week_cache
        assert ("nfl", "regular", 2025, 3) in provider._week_cache

    def test_already_cached_weeks_not_re_fetched(self):
        provider = SleeperStatsProvider()
        provider._week_cache[("nfl", "regular", 2025, 1)] = {"4046": {"rush_yd": 80.0}}
        fetched_urls: list[str] = []

        async def _fake_get_json(url: str):
            fetched_urls.append(url)
            return {"4046": {"rush_yd": 80.0}}

        with patch.object(provider, "_get_json", side_effect=_fake_get_json):
            run(provider._prefetch_weeks("nfl", "regular", 2025, [1, 2]))

        # Only week 2 should be fetched (week 1 already cached)
        assert len(fetched_urls) == 1
        assert "week=2" not in fetched_urls[0] or "/2025/2" in fetched_urls[0]

    def test_empty_response_cached_as_empty_dict(self):
        """Empty response (future week) → cached as {} so it won't be re-fetched."""
        provider = SleeperStatsProvider()

        async def _fake_get_json(url: str):
            return None  # empty week

        with patch.object(provider, "_get_json", side_effect=_fake_get_json):
            run(provider._prefetch_weeks("nfl", "regular", 2025, [5]))

        assert provider._week_cache[("nfl", "regular", 2025, 5)] == {}


# ── player_stats._merge_game_results (integration) ────────────────────────────

class TestPlayerStatsMergeIntegration:
    """Ensure the merge helper is correctly exported from player_stats."""

    def test_sleeper_supplements_espn(self):
        espn = [_make_result("2025-09-04", 87.0, "espn_gamelog")]
        sleeper = [
            _make_result("2025-09-11", 63.0, "sleeper_stats"),
            _make_result("2025-09-18", 91.0, "sleeper_stats"),
        ]
        merged = _ps_merge(espn, sleeper)
        assert len(merged) == 3
        sources = {r.game_date: r.source for r in merged}
        assert sources["2025-09-04"] == "espn_gamelog"
        assert sources["2025-09-11"] == "sleeper_stats"

    def test_duplicate_dates_espn_wins(self):
        espn    = [_make_result("2025-09-04", 87.0, "espn_gamelog")]
        sleeper = [_make_result("2025-09-04", 50.0, "sleeper_stats")]
        merged  = _ps_merge(espn, sleeper)
        assert len(merged) == 1
        assert merged[0].source == "espn_gamelog"
        assert merged[0].actual_value == 87.0
