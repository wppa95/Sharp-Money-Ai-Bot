"""
Tests for engine/season_check.py — SeasonChecker.

Covers:
  - fail-open when cache is not yet populated
  - is_sport_active returns True/False after a successful refresh
  - is_stale / TTL logic
  - graceful degradation on HTTP errors (cache unchanged, fail-open)
  - graceful degradation on network errors
  - SEASON_CHECK_INTERVAL=0 disables periodic job (config test)
"""

from __future__ import annotations

import asyncio
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest

from engine.season_check import SeasonChecker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sports_payload(active_keys: list[str], inactive_keys: list[str]) -> list[dict]:
    """Build a fake /v4/sports JSON response."""
    payload = []
    for k in active_keys:
        payload.append({"key": k, "title": k, "active": True})
    for k in inactive_keys:
        payload.append({"key": k, "title": k, "active": False})
    return payload


def _mock_resp(status: int, json_data) -> MagicMock:
    """Build a mock aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp) -> MagicMock:
    """Build a mock aiohttp.ClientSession context manager."""
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# Fail-open behaviour (no cache yet)
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_is_sport_active_before_any_refresh(self):
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        # Cache is None — must return True for every key (fail-open)
        assert checker.is_sport_active("americanfootball_nfl") is True
        assert checker.is_sport_active("basketball_nba") is True
        assert checker.is_sport_active("anything_unknown") is True

    def test_is_stale_when_never_refreshed(self):
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        assert checker.is_stale() is True

    def test_active_keys_none_before_refresh(self):
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        assert checker.active_keys is None
        assert checker.last_refresh is None


# ---------------------------------------------------------------------------
# Successful refresh
# ---------------------------------------------------------------------------

class TestSuccessfulRefresh:
    @pytest.mark.asyncio
    async def test_active_sport_detected(self):
        payload = _make_sports_payload(
            active_keys=["basketball_nba", "baseball_mlb"],
            inactive_keys=["americanfootball_nfl"],
        )
        resp = _mock_resp(200, payload)
        session = _mock_session(resp)

        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            result = await checker.refresh()

        assert result is True
        assert checker.is_sport_active("basketball_nba") is True
        assert checker.is_sport_active("baseball_mlb") is True

    @pytest.mark.asyncio
    async def test_inactive_sport_detected(self):
        payload = _make_sports_payload(
            active_keys=["basketball_nba"],
            inactive_keys=["americanfootball_nfl"],
        )
        resp = _mock_resp(200, payload)
        session = _mock_session(resp)

        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            await checker.refresh()

        assert checker.is_sport_active("americanfootball_nfl") is False

    @pytest.mark.asyncio
    async def test_unknown_key_treated_as_inactive_after_refresh(self):
        """After a successful refresh, an unknown key is NOT in the active set."""
        payload = _make_sports_payload(active_keys=["basketball_nba"], inactive_keys=[])
        resp = _mock_resp(200, payload)
        session = _mock_session(resp)

        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            await checker.refresh()

        assert checker.is_sport_active("soccer_epl") is False

    @pytest.mark.asyncio
    async def test_cache_populated_after_refresh(self):
        payload = _make_sports_payload(active_keys=["basketball_nba"], inactive_keys=[])
        resp = _mock_resp(200, payload)
        session = _mock_session(resp)

        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            await checker.refresh()

        assert checker.active_keys == frozenset(["basketball_nba"])
        assert checker.last_refresh is not None

    @pytest.mark.asyncio
    async def test_is_stale_false_immediately_after_refresh(self):
        payload = _make_sports_payload(active_keys=["basketball_nba"], inactive_keys=[])
        resp = _mock_resp(200, payload)
        session = _mock_session(resp)

        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            await checker.refresh()

        assert checker.is_stale() is False

    def test_is_stale_true_after_ttl_expires(self):
        checker = SeasonChecker(api_key="fake", ttl_seconds=60)
        checker._active_keys = frozenset(["basketball_nba"])
        checker._last_refresh = datetime.utcnow() - timedelta(seconds=61)
        assert checker.is_stale() is True


# ---------------------------------------------------------------------------
# Graceful degradation — HTTP errors
# ---------------------------------------------------------------------------

class TestHttpErrors:
    @pytest.mark.asyncio
    async def test_http_error_leaves_cache_unchanged(self):
        """On HTTP error, the previous cache is preserved (fail-open)."""
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        # Pre-populate the cache
        checker._active_keys = frozenset(["basketball_nba"])

        resp = _mock_resp(401, {"message": "quota exceeded"})
        session = _mock_session(resp)

        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            result = await checker.refresh()

        assert result is False
        # Previous cache unchanged
        assert checker.active_keys == frozenset(["basketball_nba"])

    @pytest.mark.asyncio
    async def test_http_error_before_first_fetch_stays_fail_open(self):
        """If the first fetch errors, the checker stays fail-open."""
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)

        resp = _mock_resp(500, {})
        session = _mock_session(resp)

        with patch("engine.season_check.aiohttp.ClientSession", return_value=session):
            result = await checker.refresh()

        assert result is False
        assert checker.active_keys is None
        # Must still be fail-open
        assert checker.is_sport_active("americanfootball_nfl") is True


# ---------------------------------------------------------------------------
# Graceful degradation — network errors
# ---------------------------------------------------------------------------

class TestNetworkErrors:
    @pytest.mark.asyncio
    async def test_network_exception_leaves_cache_unchanged(self):
        import aiohttp as _aiohttp

        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        checker._active_keys = frozenset(["baseball_mlb"])

        with patch(
            "engine.season_check.aiohttp.ClientSession",
            side_effect=_aiohttp.ClientConnectionError("refused"),
        ):
            result = await checker.refresh()

        assert result is False
        assert checker.active_keys == frozenset(["baseball_mlb"])

    @pytest.mark.asyncio
    async def test_generic_exception_leaves_cache_unchanged(self):
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        checker._active_keys = frozenset(["baseball_mlb"])

        with patch(
            "engine.season_check.aiohttp.ClientSession",
            side_effect=RuntimeError("unexpected"),
        ):
            result = await checker.refresh()

        assert result is False
        assert checker.active_keys == frozenset(["baseball_mlb"])


# ---------------------------------------------------------------------------
# No API key
# ---------------------------------------------------------------------------

class TestNoApiKey:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_false_and_stays_fail_open(self):
        checker = SeasonChecker(api_key="", ttl_seconds=3600)
        result = await checker.refresh()
        assert result is False
        assert checker.active_keys is None
        # Fail-open
        assert checker.is_sport_active("basketball_nba") is True


# ---------------------------------------------------------------------------
# refresh_if_stale
# ---------------------------------------------------------------------------

class TestRefreshIfStale:
    @pytest.mark.asyncio
    async def test_refresh_if_stale_skips_when_fresh(self):
        """refresh_if_stale must NOT make an HTTP call when the cache is fresh."""
        checker = SeasonChecker(api_key="fake", ttl_seconds=3600)
        checker._active_keys = frozenset(["basketball_nba"])
        checker._last_refresh = datetime.utcnow()

        with patch.object(checker, "refresh", new=AsyncMock()) as mock_refresh:
            await checker.refresh_if_stale()
            mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_if_stale_calls_refresh_when_stale(self):
        checker = SeasonChecker(api_key="fake", ttl_seconds=60)
        checker._active_keys = frozenset(["basketball_nba"])
        checker._last_refresh = datetime.utcnow() - timedelta(seconds=120)

        with patch.object(checker, "refresh", new=AsyncMock()) as mock_refresh:
            await checker.refresh_if_stale()
            mock_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

class TestConfig:
    def test_season_check_interval_default(self):
        from config import config
        assert config.SEASON_CHECK_INTERVAL == 3600

    def test_season_check_interval_env_override(self):
        from config import Config
        c = Config(SEASON_CHECK_INTERVAL=7200)
        assert c.SEASON_CHECK_INTERVAL == 7200

    def test_season_check_interval_zero_disables(self):
        """Setting interval to 0 should be supported (disables auto-refresh)."""
        from config import Config
        c = Config(SEASON_CHECK_INTERVAL=0)
        assert c.SEASON_CHECK_INTERVAL == 0
