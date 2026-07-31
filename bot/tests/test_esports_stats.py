"""
Tests for providers/esports_stats.py

Covers:
  - _maps_count helper
  - _compute_dota_fantasy helper
  - _strip_none_prefix helper
  - DOTA stat map completeness (all spec-required stat types are mapped)
  - CS stat map completeness
  - EsportsStatsProvider._fetch_dota with mocked OpenDota HTTP
  - EsportsStatsProvider._fetch_cs returns [] without PANDASCORE_API_KEY
  - Player not found → returns []
  - HTTP error → returns []
  - Decision engine integration: with real hit_rates a CS/DOTA prop qualifies
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from providers.esports_stats import (
    EsportsStatsProvider,
    _CS_FIELD_MAP,
    _DOTA_FIELD_MAP,
    _compute_dota_fantasy,
    _maps_count,
    _opendota_game_date,
    _strip_none_prefix,
)
from providers.player_stats import RawGameResult
from engine.player_results import WindowStats, PlayerHitRates, compute_hit_rates
from engine.ud_bet_decision import make_ud_bet_decision


# ── _maps_count ───────────────────────────────────────────────────────────────

class TestMapsCount:
    def test_single_map(self):
        assert _maps_count("kills on map 1") == 1

    def test_maps_1_2(self):
        assert _maps_count("kills on maps 1+2") == 2

    def test_maps_1_2_3(self):
        assert _maps_count("kills on maps 1+2+3") == 3

    def test_fantasy_games_1_2(self):
        assert _maps_count("fantasy points in games 1+2") == 2

    def test_plain_kills(self):
        assert _maps_count("kills") == 1

    def test_case_insensitive_input(self):
        # stat_lower is always lowercased before calling
        assert _maps_count("kills on maps 1+2") == 2


# ── _compute_dota_fantasy ─────────────────────────────────────────────────────

class TestComputeDotaFantasy:
    def test_typical_carry(self):
        match = {"kills": 10, "assists": 8, "deaths": 2, "last_hits": 300, "gold_per_min": 650}
        result = _compute_dota_fantasy(match)
        # 10*4 + 8*2 + 2*(-2) + 300*0.15 + 650*0.05 = 40+16-4+45+32.5 = 129.5
        assert result is not None
        assert abs(result - 129.5) < 0.01

    def test_typical_support(self):
        match = {"kills": 3, "assists": 15, "deaths": 5, "last_hits": 80, "gold_per_min": 350}
        result = _compute_dota_fantasy(match)
        # 3*4 + 15*2 + 5*(-2) + 80*0.15 + 350*0.05 = 12+30-10+12+17.5 = 61.5
        assert result is not None
        assert abs(result - 61.5) < 0.01

    def test_missing_fields_treated_as_zero(self):
        match = {"kills": 5}
        result = _compute_dota_fantasy(match)
        assert result is not None
        assert result == 5 * 4.0  # only kills contributes

    def test_none_fields_treated_as_zero(self):
        match = {"kills": None, "assists": None, "deaths": None}
        result = _compute_dota_fantasy(match)
        assert result == 0.0

    def test_invalid_type_returns_none(self):
        match = {"kills": "bad", "assists": "data"}
        result = _compute_dota_fantasy(match)
        assert result is None


# ── _strip_none_prefix ────────────────────────────────────────────────────────

class TestStripNonePrefix:
    def test_removes_none_prefix(self):
        assert _strip_none_prefix("None Samppa") == "Samppa"

    def test_leaves_normal_names(self):
        assert _strip_none_prefix("s4") == "s4"

    def test_none_only_returns_original(self):
        # "None " alone shouldn't produce empty — returns the original
        assert _strip_none_prefix("None ") == "None "

    def test_multiple_spaces_stripped(self):
        assert _strip_none_prefix("None  AhMa") == "AhMa"


# ── _opendota_game_date ───────────────────────────────────────────────────────

class TestOpendotaGameDate:
    def test_valid_timestamp(self):
        assert _opendota_game_date({"start_time": 1722000000}) == "2024-07-26"

    def test_missing_start_time(self):
        assert _opendota_game_date({}) is None

    def test_invalid_timestamp(self):
        assert _opendota_game_date({"start_time": "bad"}) is None


# ── Stat map completeness ─────────────────────────────────────────────────────

class TestDotaStatMap:
    """All spec-required DOTA stat types must be present in _DOTA_FIELD_MAP."""

    REQUIRED = [
        "kills on map 1",
        "kills on maps 1+2",
        "assists on map 1",
        "assists on maps 1+2",
        "deaths on map 1",
        "fantasy points in games 1+2",
        "fantasy points in game 1",
    ]

    def test_all_required_types_mapped(self):
        for stat in self.REQUIRED:
            assert stat in _DOTA_FIELD_MAP, f"Missing DOTA stat: {stat!r}"


class TestCsStatMap:
    """All spec-required CS stat types must be present in _CS_FIELD_MAP."""

    REQUIRED = [
        "kills on map 1",
        "kills on maps 1+2",
        "assists on map 1",
        "headshots on map 1",
        "headshots on maps 1+2",
    ]

    def test_all_required_types_mapped(self):
        for stat in self.REQUIRED:
            assert stat in _CS_FIELD_MAP, f"Missing CS stat: {stat!r}"


# ── aiohttp mock helpers ──────────────────────────────────────────────────────

def _mock_json_response(json_data):
    """Build a mock aiohttp response returning json_data."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(response):
    """Build a mock aiohttp.ClientSession returning the given response."""
    session = MagicMock()
    session.get = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ── EsportsStatsProvider._fetch_dota ─────────────────────────────────────────

_FAKE_SEARCH_RESPONSE = [
    {"account_id": 87278757, "personaname": "s4", "last_match_time": "2026-07-28T12:00:00Z"},
]

_FAKE_RECENT_MATCHES = [
    {
        "match_id": 8001,
        "start_time": 1753574400,   # 2025-07-27
        "kills": 8, "deaths": 3, "assists": 12,
        "last_hits": 200, "gold_per_min": 580,
    },
    {
        "match_id": 8002,
        "start_time": 1753488000,   # 2025-07-26
        "kills": 12, "deaths": 2, "assists": 8,
        "last_hits": 350, "gold_per_min": 720,
    },
    {
        "match_id": 8003,
        "start_time": 1753401600,   # 2025-07-25
        "kills": 5, "deaths": 6, "assists": 18,
        "last_hits": 120, "gold_per_min": 410,
    },
]


class TestFetchDotaSingleMap:
    """DOTA single-map stat fetch via OpenDota (mocked)."""

    @pytest.fixture
    def provider(self):
        return EsportsStatsProvider()

    @pytest.mark.asyncio
    async def test_returns_raw_game_results(self, provider):
        call_count = 0

        def _get_response(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if "search" in url:
                return _mock_json_response(_FAKE_SEARCH_RESPONSE)
            return _mock_json_response(_FAKE_RECENT_MATCHES)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("providers.esports_stats.aiohttp.ClientSession", return_value=session):
            results = await provider.fetch_results("s4", "DOTA", "kills on map 1")

        assert len(results) == 3
        assert all(isinstance(r, RawGameResult) for r in results)
        assert all(r.sport == "DOTA" for r in results)
        assert all(r.stat_type == "kills on map 1" for r in results)
        assert all(r.source == "opendota" for r in results)
        # Values should match raw kills (scale × 1)
        actual_values = {r.actual_value for r in results}
        assert 8.0 in actual_values
        assert 12.0 in actual_values
        assert 5.0 in actual_values


class TestFetchDotaMultiMap:
    """DOTA maps-1+2 stat should scale values by 2."""

    @pytest.fixture
    def provider(self):
        return EsportsStatsProvider()

    @pytest.mark.asyncio
    async def test_values_scaled_by_map_count(self, provider):
        def _get_response(url, **kwargs):
            if "search" in url:
                return _mock_json_response(_FAKE_SEARCH_RESPONSE)
            return _mock_json_response(_FAKE_RECENT_MATCHES)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("providers.esports_stats.aiohttp.ClientSession", return_value=session):
            results = await provider.fetch_results("s4", "DOTA", "kills on maps 1+2")

        assert len(results) == 3
        # Each value should be raw kills × 2
        actual_values = {r.actual_value for r in results}
        assert 16.0 in actual_values   # 8 × 2
        assert 24.0 in actual_values   # 12 × 2
        assert 10.0 in actual_values   # 5 × 2


class TestFetchDotaFantasy:
    """DOTA fantasy points use _compute_dota_fantasy."""

    @pytest.fixture
    def provider(self):
        return EsportsStatsProvider()

    @pytest.mark.asyncio
    async def test_fantasy_in_range(self, provider):
        def _get_response(url, **kwargs):
            if "search" in url:
                return _mock_json_response(_FAKE_SEARCH_RESPONSE)
            return _mock_json_response(_FAKE_RECENT_MATCHES)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("providers.esports_stats.aiohttp.ClientSession", return_value=session):
            results = await provider.fetch_results(
                "s4", "DOTA", "fantasy points in game 1"
            )

        assert len(results) == 3
        # Fantasy values should be > 0 for typical DOTA stats
        assert all(r.actual_value > 0 for r in results)


class TestFetchDotaPlayerNotFound:
    """If OpenDota search returns no match, fetch returns []."""

    @pytest.fixture
    def provider(self):
        return EsportsStatsProvider()

    @pytest.mark.asyncio
    async def test_empty_on_not_found(self, provider):
        search_resp = _mock_json_response([])  # no players found

        session = MagicMock()
        session.get = MagicMock(return_value=search_resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("providers.esports_stats.aiohttp.ClientSession", return_value=session):
            results = await provider.fetch_results("unknown_player", "DOTA", "kills on map 1")

        assert results == []


class TestFetchDotaHttpError:
    """HTTP errors are handled gracefully."""

    @pytest.fixture
    def provider(self):
        return EsportsStatsProvider()

    @pytest.mark.asyncio
    async def test_empty_on_http_500(self, provider):
        resp = MagicMock()
        resp.status = 500
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("providers.esports_stats.aiohttp.ClientSession", return_value=session):
            results = await provider.fetch_results("s4", "DOTA", "kills on map 1")

        assert results == []


# ── EsportsStatsProvider._fetch_cs ───────────────────────────────────────────

class TestFetchCsNoKey:
    """CS fetch without PANDASCORE_API_KEY returns [] gracefully."""

    @pytest.mark.asyncio
    async def test_no_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("PANDASCORE_API_KEY", raising=False)
        provider = EsportsStatsProvider()
        results = await provider.fetch_results("s1mple", "CS", "kills on map 1")
        assert results == []


class TestFetchCsWithKey:
    """CS fetch routes through PandaScore when key is set."""

    @pytest.mark.asyncio
    async def test_unsupported_stat_returns_empty(self, monkeypatch):
        monkeypatch.setenv("PANDASCORE_API_KEY", "test-key")
        provider = EsportsStatsProvider()
        # "flash assists" is not in _CS_FIELD_MAP
        results = await provider.fetch_results("s1mple", "CS", "flash assists")
        assert results == []


class TestFetchDotaUnknownSport:
    """Unknown sport returns []."""

    @pytest.mark.asyncio
    async def test_unknown_sport(self):
        provider = EsportsStatsProvider()
        results = await provider.fetch_results("player", "VALORANT", "kills")
        assert results == []


# ── ID caching ────────────────────────────────────────────────────────────────

class TestIdCaching:
    """Second call with same player should not re-search OpenDota."""

    @pytest.mark.asyncio
    async def test_cache_prevents_double_search(self):
        provider = EsportsStatsProvider()
        search_call_count = 0

        def _get_response(url, **kwargs):
            nonlocal search_call_count
            if "search" in url:
                search_call_count += 1
                return _mock_json_response(_FAKE_SEARCH_RESPONSE)
            return _mock_json_response(_FAKE_RECENT_MATCHES)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("providers.esports_stats.aiohttp.ClientSession", return_value=session):
            await provider.fetch_results("s4", "DOTA", "kills on map 1")
            await provider.fetch_results("s4", "DOTA", "assists on map 1")

        assert search_call_count == 1, "Search should only be called once (cached)"


# ── Decision engine integration ───────────────────────────────────────────────

class TestDecisionEngineIntegration:
    """
    Confirm that when a DOTA or CS player has real historical data,
    the decision engine can produce OVER or UNDER (not PASS).

    This is the key test: it proves decision_pass can now reach qualification
    once the provider supplies real history.
    """

    def _make_window(self, games: int, hit_rate: float, avg: float = 10.0) -> WindowStats:
        oc = round(games * hit_rate)
        uc = games - oc
        return WindowStats(games=games, over_count=oc, under_count=uc,
                           hit_rate=hit_rate, average=avg)

    def _hit_rates_with_real_data(self, current_line: float = 10.5) -> PlayerHitRates:
        return PlayerHitRates(
            player_name  = "s4",
            stat_type    = "kills on map 1",
            current_line = current_line,
            l5           = self._make_window(5,  0.80, avg=12.5),
            l10          = self._make_window(10, 0.70, avg=11.8),
            l20          = self._make_window(20, 0.65, avg=11.2),
            l30          = None,
            season       = None,
            h2h          = None,
            has_real_data = True,
            total_games  = 20,
        )

    def _score_mock(self) -> MagicMock:
        s = MagicMock()
        s.tier                = "A"
        s.consistency         = 10
        s.historical_activity = 15
        s.n_history           = 20
        s.stars               = 4
        return s

    def _validation_mock(self) -> MagicMock:
        v = MagicMock()
        v.has_supporting_data = True
        v.avg_line            = 10.5
        v.min_line_seen       = 10.0
        v.l5_rate             = 0.80
        v.l10_rate            = 0.70
        v.l20_rate            = 0.65
        v.l30_rate            = None
        v.rate_at_or_below    = 0.2
        return v

    def test_dota_qualifies_with_real_history(self):
        hit_rates  = self._hit_rates_with_real_data(current_line=10.5)
        score      = self._score_mock()
        validation = self._validation_mock()

        decision = make_ud_bet_decision(
            score=score,
            validation=validation,
            hit_rates=hit_rates,
            current_line=10.5,
        )

        assert decision is not None, "Expected a decision, got None"
        assert decision.recommendation in ("OVER", "UNDER"), (
            f"Expected OVER or UNDER, got PASS — decision_pass not resolved"
        )

    def test_cs_qualifies_with_real_history(self):
        """Same logic applies to CS sport — sport label doesn't affect decision engine."""
        hit_rates = PlayerHitRates(
            player_name  = "s1mple",
            stat_type    = "kills on map 1",
            current_line = 20.5,
            l5           = self._make_window(5,  0.80, avg=23.0),
            l10          = self._make_window(10, 0.70, avg=22.5),
            l20          = self._make_window(20, 0.65, avg=21.8),
            l30          = None,
            season       = None,
            h2h          = None,
            has_real_data = True,
            total_games  = 20,
        )
        score      = self._score_mock()
        validation = self._validation_mock()
        validation.avg_line   = 20.5
        validation.min_line_seen = 20.0

        decision = make_ud_bet_decision(
            score=score,
            validation=validation,
            hit_rates=hit_rates,
            current_line=20.5,
        )

        assert decision is not None
        assert decision.recommendation in ("OVER", "UNDER"), (
            "CS prop with real history should not be PASS"
        )

    def test_pass_when_no_real_data(self):
        """Decision remains PASS when has_real_data=False (confirms gate still works)."""
        hit_rates = PlayerHitRates(
            player_name  = "unknown",
            stat_type    = "kills on map 1",
            current_line = 10.5,
            l5           = None,
            l10          = None,
            l20          = None,
            l30          = None,
            season       = None,
            h2h          = None,
            has_real_data = False,
            total_games  = 0,
        )
        score      = self._score_mock()
        validation = self._validation_mock()

        decision = make_ud_bet_decision(
            score=score,
            validation=validation,
            hit_rates=hit_rates,
            current_line=10.5,
        )

        assert decision is not None
        assert decision.recommendation == "PASS"
