"""
providers/odds_cache.py — Shared async TTL cache for Odds API responses.

DraftKings and FanDuel connectors each previously made a separate HTTP call
per sport per 90-second cycle (e.g. ?bookmakers=draftkings and
?bookmakers=fanduel).  This module replaces both calls with one shared fetch
per (sport_key, markets, regions) key and lets each connector filter the
full response for its own bookmaker client-side.

This halves Odds API usage and makes the x-requests-remaining quota visible
in the health monitor.

Thread / task safety
--------------------
A per-cache-key asyncio.Lock prevents parallel fetch coroutines from racing
each other on first population (double-check-locking pattern).

Error handling
--------------
On non-2xx responses the cache records the failure in the health monitor and
raises OddsApiError so the calling connector can return [] cleanly.  On
success the cache records quota headers via the health monitor.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from .base import FailureType
from .health_monitor import get_health_monitor

logger = logging.getLogger(__name__)

_ODDS_API_BASE  = "https://api.the-odds-api.com/v4"
_PROVIDER_NAME  = "OddsAPI"


# ── Public exception ──────────────────────────────────────────────────────────

class OddsApiError(Exception):
    """
    Raised by OddsApiCache.get_or_fetch() when the upstream API returns a
    non-2xx status or a network error occurs.  The caller should log the
    message and return [] to keep the polling loop alive.
    """

    def __init__(
        self,
        status: int,
        error_code: str,
        message: str,
        failure_type: FailureType,
    ) -> None:
        super().__init__(f"Odds API {status}: {error_code or message}")
        self.status       = status
        self.error_code   = error_code
        self.message      = message
        self.failure_type = failure_type


# ── Cache internals ───────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    sport_key:  str
    data:       list[dict]
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    def is_expired(self, ttl_seconds: int) -> bool:
        age = (datetime.utcnow() - self.fetched_at).total_seconds()
        return age >= ttl_seconds


# ── Public cache class ────────────────────────────────────────────────────────

class OddsApiCache:
    """
    Async TTL cache for Odds API /sports/{sport_key}/odds responses.

    Parameters
    ----------
    ttl_seconds:
        How long a cached response is considered fresh.  Default 55 s keeps
        the cache warm inside the 90 s connector poll cycle with a small
        margin for clock drift.

    Usage::

        cache = init_odds_cache(ttl_seconds=55)

        # In DraftKingsConnector._fetch_sport():
        data = await cache.get_or_fetch(
            sport_key="americanfootball_nfl",
            api_key=self._api_key,
        )
        # data contains ALL bookmakers; filter for "draftkings" client-side.
    """

    def __init__(self, ttl_seconds: int = 55) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple, _CacheEntry] = {}
        self._locks:   dict[tuple, asyncio.Lock] = {}
        self._hits   = 0
        self._misses = 0

    # ── Public interface ──────────────────────────────────────────────────────

    async def get_or_fetch(
        self,
        sport_key:   str,
        api_key:     str,
        markets:     str = "h2h,spreads,totals",
        regions:     str = "us",
        odds_format: str = "american",
    ) -> list[dict]:
        """
        Return the cached response if it is still within the TTL window.
        Otherwise fetch from the Odds API, cache the result, update the health
        monitor with the quota headers, and return the data.

        Raises OddsApiError on any non-2xx response or network error.
        """
        cache_key = (sport_key, markets, regions)

        # Fast path — check without the lock; avoids lock contention when warm
        entry = self._entries.get(cache_key)
        if entry is not None and not entry.is_expired(self._ttl):
            self._hits += 1
            logger.debug("OddsApiCache HIT  %s (age %.1fs)", sport_key,
                         (datetime.utcnow() - entry.fetched_at).total_seconds())
            return entry.data

        # Slow path — acquire a per-key lock so only one coroutine fetches
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            # Double-check after acquiring the lock
            entry = self._entries.get(cache_key)
            if entry is not None and not entry.is_expired(self._ttl):
                self._hits += 1
                return entry.data

            self._misses += 1

            # ── Budget guard ──────────────────────────────────────────────────
            # Check usage budget + active-sport filter before making the HTTP
            # call.  Runs inside the lock so the check and the record are atomic
            # for this (sport_key, markets, regions) slot.
            try:
                from .usage_tracker import get_usage_tracker, infer_call_priority
                _tracker = get_usage_tracker()
                if _tracker is not None:
                    _priority = infer_call_priority(sport_key, markets)
                    _allowed, _reason = _tracker.should_allow(
                        _PROVIDER_NAME, _priority, sport_key=sport_key,
                    )
                    if not _allowed:
                        logger.info(
                            "OddsApiCache: request for %s blocked by usage tracker — %s",
                            sport_key, _reason,
                        )
                        return []
                    # Record the outgoing request (may return a newly-crossed threshold)
                    _tracker.record_request(_PROVIDER_NAME, _priority, sport_key=sport_key)
            except ImportError:
                pass
            # ─────────────────────────────────────────────────────────────────

            data = await self._fetch(sport_key, api_key, markets, regions, odds_format)
            self._entries[cache_key] = _CacheEntry(sport_key=sport_key, data=data)
            logger.debug("OddsApiCache MISS %s — stored %d events", sport_key, len(data))
            return data

    def parse_quota_headers(
        self,
        headers: dict,
        provider_name: str = _PROVIDER_NAME,
    ) -> None:
        """
        Read x-requests-remaining and x-requests-used from response headers
        and forward the values to the health monitor.

        Called automatically by _fetch(); exposed publicly so tests can call it
        directly and for any future caller that handles its own HTTP session.
        """
        try:
            raw_remaining = headers.get("x-requests-remaining")
            raw_used      = headers.get("x-requests-used")
            remaining = int(raw_remaining) if raw_remaining not in (None, "?", "") else None
            used      = int(raw_used)      if raw_used      not in (None, "?", "") else None
        except (ValueError, TypeError):
            return

        if remaining is None and used is None:
            return

        mon = get_health_monitor()
        if mon:
            mon.record_success(provider_name, quota_remaining=remaining, quota_used=used)

    def invalidate(
        self,
        sport_key: str,
        markets:   str = "h2h,spreads,totals",
        regions:   str = "us",
    ) -> None:
        """Remove a specific entry from the cache (forces the next call to re-fetch)."""
        self._entries.pop((sport_key, markets, regions), None)

    def clear(self) -> None:
        """Wipe all cached entries."""
        self._entries.clear()

    def stats(self) -> dict:
        """
        Return a snapshot of cache performance counters.

        Keys: hits, misses, hit_rate (0.0–1.0), entries, ttl_seconds.
        """
        total    = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "hits":       self._hits,
            "misses":     self._misses,
            "hit_rate":   hit_rate,
            "entries":    len(self._entries),
            "ttl_seconds": self._ttl,
        }

    # ── Internal HTTP fetch ───────────────────────────────────────────────────

    async def _fetch(
        self,
        sport_key:   str,
        api_key:     str,
        markets:     str,
        regions:     str,
        odds_format: str,
    ) -> list[dict]:
        """
        Fetch /sports/{sport_key}/odds from The Odds API.

        On success: parses quota headers and calls health monitor record_success.
        On error:   calls health monitor record_failure and raises OddsApiError.
        """
        url    = f"{_ODDS_API_BASE}/sports/{sport_key}/odds"
        params = {
            "apiKey":     api_key,
            "regions":    regions,
            "markets":    markets,
            "oddsFormat": odds_format,
        }
        mon = get_health_monitor()

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    # Parse quota headers even on error responses — they are
                    # present on 401 responses too.
                    self.parse_quota_headers(dict(resp.headers))

                    if resp.status in (401, 422, 429):
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = {}
                        error_code = (body or {}).get("error_code", "")
                        message    = (body or {}).get("message", f"HTTP {resp.status}")

                        if resp.status == 401 and error_code == "OUT_OF_USAGE_CREDITS":
                            ftype = FailureType.QUOTA
                        else:
                            ftype = FailureType.HTTP_ERROR

                        if mon:
                            mon.record_failure(
                                _PROVIDER_NAME,
                                f"{resp.status} {error_code or message}",
                                ftype,
                            )
                        raise OddsApiError(resp.status, error_code, message, ftype)

                    resp.raise_for_status()
                    data: list[dict] = await resp.json()

                    remaining = resp.headers.get("x-requests-remaining", "?")
                    used      = resp.headers.get("x-requests-used", "?")
                    logger.info(
                        "OddsAPI: fetched %d events for %s "
                        "(quota: %s remaining, %s used)",
                        len(data), sport_key, remaining, used,
                    )
                    return data

        except OddsApiError:
            raise
        except aiohttp.ClientError as exc:
            err = str(exc)
            if mon:
                mon.record_failure(_PROVIDER_NAME, err, FailureType.HTTP_ERROR)
            raise OddsApiError(0, "", err, FailureType.HTTP_ERROR) from exc
        except Exception as exc:
            err = str(exc)
            if mon:
                mon.record_failure(_PROVIDER_NAME, err, FailureType.UNKNOWN)
            raise OddsApiError(0, "", err, FailureType.UNKNOWN) from exc


# ── Module-level singleton ────────────────────────────────────────────────────

_cache: Optional[OddsApiCache] = None


def init_odds_cache(ttl_seconds: int = 55) -> OddsApiCache:
    """Create (or replace) the module-level singleton and return it."""
    global _cache
    _cache = OddsApiCache(ttl_seconds=ttl_seconds)
    logger.info("OddsApiCache initialised (TTL=%ds)", ttl_seconds)
    return _cache


def get_odds_cache() -> Optional[OddsApiCache]:
    """Return the singleton, or None if init_odds_cache() has not been called."""
    return _cache
