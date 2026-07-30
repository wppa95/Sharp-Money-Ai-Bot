"""
Tests for providers/odds_cache.py — OddsApiCache.

Covers:
  - Cache miss calls _fetch once and stores the result
  - Cache hit within TTL does NOT call _fetch again
  - TTL expiry causes a re-fetch
  - Concurrent awaits for the same key trigger only one _fetch call
  - parse_quota_headers updates the health monitor correctly
  - OddsApiError is raised on 401 QUOTA responses
  - stats() returns correct hit/miss counts and hit_rate
  - invalidate() removes a specific entry
  - init_odds_cache / get_odds_cache singleton pattern
"""

from __future__ import annotations

import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from providers.odds_cache import OddsApiCache, OddsApiError, init_odds_cache, get_odds_cache
from providers.base import FailureType
from providers.health_monitor import init_health_monitor, get_health_monitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_DATA = [{"id": "game-1", "home_team": "Lakers", "away_team": "Celtics"}]


def _make_cache(ttl: int = 60) -> OddsApiCache:
    cache = OddsApiCache(ttl_seconds=ttl)
    return cache


def _patch_fetch(cache: OddsApiCache, data=None, call_count_holder=None):
    """Replace cache._fetch with an async no-op that counts calls."""
    if data is None:
        data = _FAKE_DATA
    if call_count_holder is None:
        call_count_holder = [0]

    async def fake_fetch(sport_key, api_key, markets, regions, odds_format):
        call_count_holder[0] += 1
        return data

    cache._fetch = fake_fetch
    return call_count_holder


# ---------------------------------------------------------------------------
# Cache miss
# ---------------------------------------------------------------------------

class TestCacheMiss:
    @pytest.mark.asyncio
    async def test_miss_calls_fetch_once(self):
        cache = _make_cache()
        count = _patch_fetch(cache)
        result = await cache.get_or_fetch("basketball_nba", "test-key")
        assert result == _FAKE_DATA
        assert count[0] == 1

    @pytest.mark.asyncio
    async def test_miss_increments_miss_counter(self):
        cache = _make_cache()
        _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "test-key")
        assert cache.stats()["misses"] == 1
        assert cache.stats()["hits"] == 0

    @pytest.mark.asyncio
    async def test_miss_stores_data_in_cache(self):
        cache = _make_cache()
        _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "test-key")
        assert cache.stats()["entries"] == 1


# ---------------------------------------------------------------------------
# Cache hit
# ---------------------------------------------------------------------------

class TestCacheHit:
    @pytest.mark.asyncio
    async def test_hit_does_not_call_fetch_again(self):
        cache = _make_cache()
        count = _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "test-key")
        await cache.get_or_fetch("basketball_nba", "test-key")
        assert count[0] == 1   # still 1 — second call was a hit

    @pytest.mark.asyncio
    async def test_hit_returns_same_data(self):
        cache = _make_cache()
        _patch_fetch(cache)
        r1 = await cache.get_or_fetch("basketball_nba", "test-key")
        r2 = await cache.get_or_fetch("basketball_nba", "test-key")
        assert r1 is r2

    @pytest.mark.asyncio
    async def test_hit_increments_hit_counter(self):
        cache = _make_cache()
        _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "test-key")  # miss
        await cache.get_or_fetch("basketball_nba", "test-key")  # hit
        assert cache.stats()["hits"] == 1

    @pytest.mark.asyncio
    async def test_different_sport_keys_are_independent_entries(self):
        cache = _make_cache()
        count = _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "key")
        await cache.get_or_fetch("baseball_mlb",   "key")
        assert count[0] == 2      # two separate fetches
        assert cache.stats()["entries"] == 2


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

class TestTtlExpiry:
    @pytest.mark.asyncio
    async def test_expired_entry_triggers_refetch(self):
        cache = _make_cache(ttl=1)   # 1-second TTL
        count = _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "key")
        # Manually backdate the entry so it appears expired
        key = ("basketball_nba", "h2h,totals", "us")
        cache._entries[key].fetched_at = datetime.utcnow() - timedelta(seconds=2)
        await cache.get_or_fetch("basketball_nba", "key")
        assert count[0] == 2   # second fetch triggered

    @pytest.mark.asyncio
    async def test_fresh_entry_not_refetched(self):
        cache = _make_cache(ttl=3600)
        count = _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "key")
        await cache.get_or_fetch("basketball_nba", "key")
        assert count[0] == 1


# ---------------------------------------------------------------------------
# Concurrent access (lock safety)
# ---------------------------------------------------------------------------

class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_calls_trigger_only_one_fetch(self):
        """When several coroutines race for the same sport_key, only one fetch
        should happen.  The lock prevents duplicate in-flight requests."""
        cache = _make_cache()
        count = [0]

        async def slow_fetch(sport_key, api_key, markets, regions, odds_format):
            count[0] += 1
            await asyncio.sleep(0.01)   # simulate network latency
            return _FAKE_DATA

        cache._fetch = slow_fetch

        results = await asyncio.gather(
            cache.get_or_fetch("basketball_nba", "key"),
            cache.get_or_fetch("basketball_nba", "key"),
            cache.get_or_fetch("basketball_nba", "key"),
        )
        assert count[0] == 1                              # only ONE HTTP call
        assert all(r == _FAKE_DATA for r in results)     # all callers get data

    @pytest.mark.asyncio
    async def test_concurrent_different_sports_both_fetch(self):
        """Two different sport keys should each fetch once."""
        cache = _make_cache()
        count = [0]

        async def fast_fetch(sport_key, api_key, markets, regions, odds_format):
            count[0] += 1
            return [{"sport": sport_key}]

        cache._fetch = fast_fetch

        results = await asyncio.gather(
            cache.get_or_fetch("basketball_nba", "key"),
            cache.get_or_fetch("baseball_mlb",   "key"),
        )
        assert count[0] == 2
        # Each result contains the matching sport key
        sports = {r[0]["sport"] for r in results}
        assert sports == {"basketball_nba", "baseball_mlb"}


# ---------------------------------------------------------------------------
# parse_quota_headers
# ---------------------------------------------------------------------------

class TestParseQuotaHeaders:
    def test_quota_headers_update_health_monitor(self):
        mon = init_health_monitor()
        mon.register("OddsAPI")
        cache = _make_cache()
        cache.parse_quota_headers(
            {"x-requests-remaining": "400", "x-requests-used": "100"},
            provider_name="OddsAPI",
        )
        h = mon.get_health("OddsAPI")
        assert h.quota_remaining == 400
        assert h.quota_used == 100

    def test_quota_headers_missing_is_noop(self):
        mon = init_health_monitor()
        mon.register("OddsAPI")
        cache = _make_cache()
        cache.parse_quota_headers({}, provider_name="OddsAPI")
        h = mon.get_health("OddsAPI")
        assert h.quota_remaining is None
        assert h.quota_used is None

    def test_quota_headers_invalid_values_are_ignored(self):
        mon = init_health_monitor()
        mon.register("OddsAPI")
        cache = _make_cache()
        # Should not raise; just silently ignore bad values
        cache.parse_quota_headers(
            {"x-requests-remaining": "not-a-number", "x-requests-used": "also-bad"},
            provider_name="OddsAPI",
        )

    def test_quota_remaining_zero_stored(self):
        mon = init_health_monitor()
        mon.register("OddsAPI")
        cache = _make_cache()
        cache.parse_quota_headers(
            {"x-requests-remaining": "0", "x-requests-used": "500"},
            provider_name="OddsAPI",
        )
        h = mon.get_health("OddsAPI")
        assert h.quota_remaining == 0


# ---------------------------------------------------------------------------
# OddsApiError raised on error responses
# ---------------------------------------------------------------------------

class TestOddsApiError:
    @pytest.mark.asyncio
    async def test_error_raised_with_quota_failure_type(self):
        cache = _make_cache()
        # Patch _fetch to raise OddsApiError directly
        async def failing_fetch(sport_key, api_key, markets, regions, odds_format):
            raise OddsApiError(401, "OUT_OF_USAGE_CREDITS", "quota exhausted", FailureType.QUOTA)
        cache._fetch = failing_fetch

        with pytest.raises(OddsApiError) as exc_info:
            await cache.get_or_fetch("basketball_nba", "key")
        assert exc_info.value.failure_type == FailureType.QUOTA
        assert exc_info.value.status == 401

    @pytest.mark.asyncio
    async def test_error_does_not_cache_response(self):
        cache = _make_cache()
        async def failing_fetch(sport_key, api_key, markets, regions, odds_format):
            raise OddsApiError(401, "err", "msg", FailureType.HTTP_ERROR)
        cache._fetch = failing_fetch

        with pytest.raises(OddsApiError):
            await cache.get_or_fetch("basketball_nba", "key")
        assert cache.stats()["entries"] == 0


# ---------------------------------------------------------------------------
# stats()
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_hit_rate_zero_when_no_requests(self):
        cache = _make_cache()
        assert cache.stats()["hit_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_hit_rate_one_after_all_hits(self):
        cache = _make_cache()
        _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "key")  # miss
        await cache.get_or_fetch("basketball_nba", "key")  # hit
        await cache.get_or_fetch("basketball_nba", "key")  # hit
        st = cache.stats()
        # 2 hits / 3 total = 0.667
        assert abs(st["hit_rate"] - 2/3) < 0.01

    @pytest.mark.asyncio
    async def test_ttl_seconds_in_stats(self):
        cache = _make_cache(ttl=120)
        assert cache.stats()["ttl_seconds"] == 120


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------

class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self):
        cache = _make_cache()
        count = _patch_fetch(cache)
        await cache.get_or_fetch("basketball_nba", "key")
        cache.invalidate("basketball_nba")
        await cache.get_or_fetch("basketball_nba", "key")
        assert count[0] == 2

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_key_is_noop(self):
        cache = _make_cache()
        cache.invalidate("does_not_exist")   # should not raise


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_init_returns_instance(self):
        c = init_odds_cache(ttl_seconds=55)
        assert isinstance(c, OddsApiCache)

    def test_get_after_init_returns_same_instance(self):
        c = init_odds_cache(ttl_seconds=55)
        assert get_odds_cache() is c

    def test_get_before_init_returns_none_or_previous(self):
        result = get_odds_cache()
        assert result is None or isinstance(result, OddsApiCache)
