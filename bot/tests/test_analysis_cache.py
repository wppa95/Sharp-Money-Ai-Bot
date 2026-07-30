"""
Tests for engine/analysis.py — verifies fetch_live_odds routes through
OddsApiCache instead of making direct aiohttp calls.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from models import Sport
from engine.analysis import AnalysisEngine


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cache(return_data=None) -> AsyncMock:
    cache = AsyncMock()
    cache.get_or_fetch = AsyncMock(return_value=return_data or [])
    return cache


# ── fetch_live_odds routing ────────────────────────────────────────────────────

class TestFetchLiveOddsUsesCache:
    @pytest.mark.asyncio
    async def test_routes_through_cache(self):
        """fetch_live_odds must call OddsApiCache.get_or_fetch, not aiohttp."""
        engine = AnalysisEngine()
        cache  = _make_cache(return_data=[])

        # get_odds_cache is imported inside fetch_live_odds, so patch at source
        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=cache),
        ):
            mock_cfg.ODDS_API_KEY = "test-key"
            result = await engine.fetch_live_odds(Sport.MLB)

        cache.get_or_fetch.assert_called_once()
        call_kwargs = cache.get_or_fetch.call_args
        # First positional arg is sport_key
        assert call_kwargs[0][0] is not None or call_kwargs[1].get("sport_key") is not None
        assert result == []  # no events in mock data

    @pytest.mark.asyncio
    async def test_passes_api_key_to_cache(self):
        """The API key from config must be forwarded to the cache."""
        engine = AnalysisEngine()
        cache  = _make_cache()

        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=cache),
        ):
            mock_cfg.ODDS_API_KEY = "my-secret-key"
            await engine.fetch_live_odds(Sport.MLB)

        call = cache.get_or_fetch.call_args
        assert "my-secret-key" in call[0] or call[1].get("api_key") == "my-secret-key"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_api_key(self):
        """If ODDS_API_KEY is empty, return [] without touching the cache."""
        engine = AnalysisEngine()
        cache  = _make_cache()

        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=cache),
        ):
            mock_cfg.ODDS_API_KEY = ""
            result = await engine.fetch_live_odds(Sport.MLB)

        assert result == []
        cache.get_or_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_cache_not_initialised(self):
        """Return [] gracefully when the cache singleton is None."""
        engine = AnalysisEngine()

        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=None),
        ):
            mock_cfg.ODDS_API_KEY = "key"
            result = await engine.fetch_live_odds(Sport.MLB)

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_odds_api_error(self):
        """OddsApiError from the cache is caught and returns []."""
        from providers.odds_cache import OddsApiError
        engine = AnalysisEngine()
        cache  = _make_cache()
        from providers.base import FailureType
        cache.get_or_fetch = AsyncMock(
            side_effect=OddsApiError(401, "OUT_OF_USAGE_CREDITS", "quota exceeded", FailureType.QUOTA)
        )

        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=cache),
        ):
            mock_cfg.ODDS_API_KEY = "key"
            result = await engine.fetch_live_odds(Sport.MLB)

        assert result == []

    @pytest.mark.asyncio
    async def test_no_direct_aiohttp_import_used(self):
        """Confirm aiohttp is NOT called during fetch_live_odds (it goes through cache)."""
        engine = AnalysisEngine()
        cache  = _make_cache()

        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=cache),
            patch("aiohttp.ClientSession") as mock_session,
        ):
            mock_cfg.ODDS_API_KEY = "key"
            await engine.fetch_live_odds(Sport.MLB)

        mock_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_parses_events_from_cache_data(self):
        """Events returned by the cache should be parsed into OddsLine objects."""
        engine = AnalysisEngine()
        event_data = [
            {
                "id": "abc123",
                "sport_key": "baseball_mlb",
                "away_team": "Boston Red Sox",
                "home_team": "New York Yankees",
                "commence_time": "2026-07-30T18:00:00Z",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Boston Red Sox", "price": -110},
                                    {"name": "New York Yankees", "price": -110},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
        cache = _make_cache(return_data=event_data)

        with (
            patch("engine.analysis.config") as mock_cfg,
            patch("providers.odds_cache.get_odds_cache", return_value=cache),
        ):
            mock_cfg.ODDS_API_KEY = "key"
            result = await engine.fetch_live_odds(Sport.MLB)

        # Should parse 2 outcomes (one per team)
        assert len(result) == 2
        names = {r.selection for r in result}
        assert "Boston Red Sox" in names
        assert "New York Yankees" in names


# ── Unknown sport key ─────────────────────────────────────────────────────────

class TestFetchLiveOddsUnknownSport:
    @pytest.mark.asyncio
    async def test_unknown_sport_returns_empty(self):
        """Sports without an Odds API key mapping return [] immediately."""
        engine = AnalysisEngine()

        with patch("engine.analysis.config") as mock_cfg:
            mock_cfg.ODDS_API_KEY = "key"
            # Use a sport that has no mapping in _SPORT_TO_ODDS_API_KEY
            # We create a mock Sport value
            fake_sport = MagicMock()
            fake_sport.value = "FAKE_SPORT"

            with patch.dict("engine.analysis._SPORT_TO_ODDS_API_KEY", {}, clear=False):
                result = await engine.fetch_live_odds(fake_sport)

        assert result == []
