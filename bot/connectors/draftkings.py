"""
connectors/draftkings.py — DraftKings Sportsbook connector.

DraftKings does not publish a public API. This connector extracts
DraftKings-specific odds from the existing Odds API feed (which already
aggregates many books including DraftKings) and normalizes them into
MarketSnapshot objects.

Opening odds are tracked in an in-memory dict keyed by (event, selection).
The database is NOT queried on every call to keep fetch() fast; the
consensus/steam engines use the snapshots with opening_odds set.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import aiohttp

from .base import BaseConnector, ConnectorStatus, MarketSnapshot
from engine.season_check import SeasonChecker

logger = logging.getLogger(__name__)

# Odds API sport-key mapping (subset relevant to this connector)
_SPORT_KEYS: dict[str, str] = {
    "NFL":   "americanfootball_nfl",
    "NBA":   "basketball_nba",
    "MLB":   "baseball_mlb",
    "NHL":   "icehockey_nhl",
    "NCAAF": "americanfootball_ncaaf",
    "NCAAB": "basketball_ncaab",
    "UFC":   "mma_mixed_martial_arts",
    "WNBA":  "basketball_wnba",
    # Soccer — one Odds API key per league ("Soccer" is a legacy EPL alias)
    "EPL":        "soccer_epl",
    "LaLiga":     "soccer_spain_la_liga",
    "SerieA":     "soccer_italy_serie_a",
    "Bundesliga": "soccer_germany_bundesliga",
    "Ligue1":     "soccer_france_ligue_one",
    "MLS":        "soccer_usa_mls",
    "UCL":        "soccer_uefa_champs_league",
    "Soccer":     "soccer_epl",
}

_MARKET_MAP: dict[str, str] = {
    "h2h":     "Moneyline",
    "spreads":  "Spread",
    "totals":   "Total (O/U)",
    "player_props": "Player Prop",
}

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_BOOK_KEY      = "draftkings"   # Odds API bookmaker key
_BOOK_TITLE    = "DraftKings"


class DraftKingsConnector(BaseConnector):
    """
    Fetches DraftKings moneylines and player props via The Odds API.

    Opening odds are tracked in memory per session. On first sight of a
    (event, selection) pair the current odds become the opening odds.
    Subsequent calls record movement against that opening.
    """

    name         = "DraftKings"
    is_pickem    = False
    poll_interval = 60

    def __init__(
        self,
        odds_api_key: str,
        active_sports: list[str],
        enabled: bool = True,
        season_checker: SeasonChecker | None = None,
    ) -> None:
        self._api_key        = odds_api_key
        self._active_sports  = active_sports
        self.enabled         = enabled
        self._season_checker = season_checker
        # (event, selection) -> opening American odds
        self._opening: dict[tuple[str, str], int] = {}

    # ── Public interface ──────────────────────────────────────────────────────

    async def fetch(self) -> list[MarketSnapshot]:
        if not self.enabled:
            return []
        if not self._api_key:
            logger.debug("DraftKings connector: no ODDS_API_KEY, skipping")
            return []

        snapshots: list[MarketSnapshot] = []
        for sport in self._active_sports:
            sport_key = _SPORT_KEYS.get(sport)
            if not sport_key:
                continue
            if self._season_checker and not self._season_checker.is_sport_active(sport_key):
                logger.info(
                    "DraftKings: skipping %s (%s) — out of season / no active markets",
                    sport, sport_key,
                )
                continue
            snaps = await self._fetch_sport(sport, sport_key)
            snapshots.extend(snaps)

        logger.info("DraftKings: fetched %d snapshots", len(snapshots))
        return snapshots

    async def health_check(self) -> ConnectorStatus:
        if not self._api_key:
            return ConnectorStatus.NO_KEY
        url = f"{_ODDS_API_BASE}/sports"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, params={"apiKey": self._api_key}) as resp:
                    if resp.status == 200:
                        return ConnectorStatus.OK
                    return ConnectorStatus.ERROR
        except Exception:
            return ConnectorStatus.ERROR

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fetch_sport(self, sport: str, sport_key: str) -> list[MarketSnapshot]:
        """
        Fetch odds for one sport via the shared OddsApiCache when available.

        Falls back to _fetch_sport_direct() when the cache singleton has not
        been initialised (e.g. in unit-test environments).  The cached path
        fetches ALL bookmakers in one call; _normalize() then filters for
        DraftKings data client-side, halving API quota usage vs. the old
        bookmakers= param approach.
        """
        try:
            from providers.odds_cache import get_odds_cache, OddsApiError
        except ImportError:
            return await self._fetch_sport_direct(sport, sport_key)

        cache = get_odds_cache()
        if cache is None:
            return await self._fetch_sport_direct(sport, sport_key)

        try:
            data = await cache.get_or_fetch(
                sport_key   = sport_key,
                api_key     = self._api_key,
                markets     = "h2h,totals,player_props",
                regions     = "us",
            )
        except OddsApiError as exc:
            logger.warning(
                "DraftKings/OddsAPI error for %s: %s", sport, exc
            )
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("DraftKings unexpected error (%s): %s", sport, exc)
            return []

        return self._normalize(data, sport)

    async def _fetch_sport_direct(self, sport: str, sport_key: str) -> list[MarketSnapshot]:
        """
        Direct Odds API call with per-bookmaker filtering.

        Used as the fallback when the shared OddsApiCache is not available
        (test environments, early startup before init_odds_cache() is called).
        """
        url = f"{_ODDS_API_BASE}/sports/{sport_key}/odds"
        params = {
            "apiKey":     self._api_key,
            "regions":    "us",
            "markets":    "h2h,totals,player_props",
            "oddsFormat": "american",
            "bookmakers": _BOOK_KEY,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, params=params) as resp:
                    if resp.status in (401, 422, 429):
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = {}
                        error_code = (body or {}).get("error_code", "")
                        message    = (body or {}).get("message", "")
                        if resp.status == 401 and error_code == "OUT_OF_USAGE_CREDITS":
                            logger.warning(
                                "DraftKings/OddsAPI: usage quota exhausted (401) for %s — %s",
                                sport, message,
                            )
                        else:
                            logger.warning(
                                "DraftKings/OddsAPI HTTP %d for %s%s",
                                resp.status, sport,
                                f" — {error_code or message}" if (error_code or message) else "",
                            )
                        return []
                    resp.raise_for_status()
                    data: list[dict] = await resp.json()
        except aiohttp.ClientError as exc:
            logger.warning("DraftKings fetch error (%s): %s", sport, exc)
            return []

        return self._normalize(data, sport)

    def _normalize(self, data: list[dict], sport: str) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        now = datetime.utcnow()

        for event in data:
            away  = event.get("away_team", "Away")
            home  = event.get("home_team", "Home")
            event_name = f"{away} @ {home}"

            game_time: Optional[datetime] = None
            raw_t = event.get("commence_time")
            if raw_t:
                try:
                    game_time = datetime.fromisoformat(raw_t.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass

            for bm in event.get("bookmakers", []):
                if bm.get("key", "").lower() != _BOOK_KEY:
                    continue
                for market in bm.get("markets", []):
                    market_key = market.get("key", "")
                    mtype = _MARKET_MAP.get(market_key)
                    if mtype is None:
                        continue
                    is_player_prop = market_key == "player_props"

                    for outcome in market.get("outcomes", []):
                        try:
                            odds = int(outcome["price"])
                            line = outcome.get("point")
                            if is_player_prop:
                                # Odds API player prop outcomes:
                                # "description" = player name, "name" = "Over"/"Under"
                                player = str(outcome.get("description") or "").strip()
                                direction = str(outcome.get("name") or "").strip()
                                if not player or not direction:
                                    continue
                                sel = f"{player} {direction}"
                            else:
                                sel    = str(outcome["name"])
                                player = None
                        except (KeyError, TypeError, ValueError):
                            continue

                        key = (event_name, sel)
                        opening = self._opening.setdefault(key, odds)

                        snapshots.append(MarketSnapshot(
                            sportsbook   = _BOOK_TITLE,
                            sport        = sport,
                            league       = sport,
                            event        = event_name,
                            market_type  = mtype,
                            selection    = sel,
                            player       = player if is_player_prop else None,
                            odds         = odds,
                            timestamp    = now,
                            line         = float(line) if line is not None else None,
                            game_time    = game_time,
                            opening_odds = opening,
                            is_pickem    = False,
                        ))

        return snapshots
