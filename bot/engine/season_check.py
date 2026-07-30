"""
engine/season_check.py — Odds API season / market status checker.

Periodically fetches /v4/sports from The Odds API to learn which sport
keys currently have active markets.  Results are cached in memory for
``ttl_seconds`` (default: 3600 = 1 hour).

Fail-open contract
------------------
If the API call fails **or** the cache has never been populated, every
sport is treated as active so the polling cycle is unaffected.  The
caller never has to handle errors from this module.

Typical usage
-------------
::

    # In post_init:
    checker = SeasonChecker(api_key=config.ODDS_API_KEY,
                             ttl_seconds=config.SEASON_CHECK_INTERVAL)
    await checker.refresh()           # eager first load (optional)

    # In a periodic job:
    await checker.refresh_if_stale()  # no-op if cache is fresh

    # In poll logic:
    if not checker.is_sport_active("americanfootball_nfl"):
        logger.info("Skipping NFL — out of season / no active markets")
        continue
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"


class SeasonChecker:
    """
    In-memory cache of active Odds API sport keys.

    Parameters
    ----------
    api_key:
        The Odds API key.  Pass an empty string to disable (all sports
        will be treated as active).
    ttl_seconds:
        How long the cache is considered fresh before the next
        ``refresh_if_stale()`` call triggers a real HTTP request.
        Default 3 600 s (1 hour).
    """

    def __init__(self, api_key: str, ttl_seconds: int = 3600) -> None:
        self._api_key = api_key
        self._ttl = ttl_seconds
        # None  → never fetched (fail-open)
        # set() → successfully fetched; may be empty if no sports active
        self._active_keys: Optional[frozenset[str]] = None
        # Full set of ALL sport keys returned by /v4/sports (active + inactive).
        # Used by get_sport_summary() to show which sports are off-season.
        self._all_known_keys: Optional[frozenset[str]] = None
        self._last_refresh: Optional[datetime] = None

    # ── Public interface ──────────────────────────────────────────────────────

    def is_sport_active(self, odds_api_key: str) -> bool:
        """
        Return *True* if the given Odds API sport key currently has active
        markets, *or* if the cache has not been populated yet (fail-open).

        Parameters
        ----------
        odds_api_key:
            The Odds API sport key, e.g. ``"americanfootball_nfl"``.
        """
        if self._active_keys is None:
            # Cache not yet populated — allow all sports (fail-open)
            return True
        return odds_api_key in self._active_keys

    def is_stale(self) -> bool:
        """Return True if the cache is absent or past its TTL."""
        if self._last_refresh is None:
            return True
        return datetime.utcnow() - self._last_refresh > timedelta(seconds=self._ttl)

    async def refresh_if_stale(self) -> None:
        """Refresh the cache only when the TTL has expired."""
        if self.is_stale():
            await self.refresh()

    async def refresh(self) -> bool:
        """
        Unconditionally fetch ``/v4/sports`` and update the in-memory cache.

        Returns
        -------
        bool
            *True* on success, *False* on any error (cache unchanged on
            failure so the previous value — or the fail-open state — is
            preserved).
        """
        if not self._api_key:
            logger.debug("SeasonChecker: no API key configured — skipping refresh")
            return False

        url = f"{_ODDS_API_BASE}/sports"
        params = {"apiKey": self._api_key}

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "SeasonChecker: /v4/sports returned HTTP %d — keeping previous cache",
                            resp.status,
                        )
                        return False
                    data: list[dict] = await resp.json()
        except aiohttp.ClientError as exc:
            logger.warning(
                "SeasonChecker: network error fetching /v4/sports — keeping previous cache: %s", exc
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SeasonChecker: unexpected error fetching /v4/sports — keeping previous cache: %s", exc
            )
            return False

        active_keys   = frozenset(entry["key"] for entry in data if entry.get("active", False))
        all_known_keys = frozenset(entry["key"] for entry in data)
        total = len(data)
        prev_count = len(self._active_keys) if self._active_keys is not None else "?"

        self._active_keys    = active_keys
        self._all_known_keys = all_known_keys
        self._last_refresh   = datetime.utcnow()

        logger.info(
            "SeasonChecker: refreshed — %d/%d sport keys have active markets (was %s)",
            len(active_keys),
            total,
            prev_count,
        )
        return True

    # ── New public helpers ────────────────────────────────────────────────────

    def get_active_sport_keys(self) -> frozenset[str]:
        """
        Return the set of sport keys that currently have active markets.

        Returns an empty frozenset when the cache has never been populated
        (unlike is_sport_active which is fail-open).  Callers that need the
        fail-open behaviour should use is_sport_active() instead.
        """
        return self._active_keys if self._active_keys is not None else frozenset()

    def get_sport_summary(self) -> dict[str, bool]:
        """
        Return a mapping of ``{odds_api_sport_key: is_active}`` for every
        sport key seen in the last successful /v4/sports response.

        Returns an empty dict when the cache has never been populated.
        Useful for the /status display of which sports are in-season.
        """
        if self._all_known_keys is None:
            return {}
        active = self._active_keys or frozenset()
        return {key: (key in active) for key in sorted(self._all_known_keys)}

    # ── Diagnostic helpers ────────────────────────────────────────────────────

    @property
    def active_keys(self) -> Optional[frozenset[str]]:
        """The currently cached set of active keys, or *None* if not fetched."""
        return self._active_keys

    @property
    def last_refresh(self) -> Optional[datetime]:
        """UTC timestamp of the last successful refresh, or *None*."""
        return self._last_refresh
