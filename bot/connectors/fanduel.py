"""
connectors/fanduel.py — FanDuel Sportsbook connector.

Like DraftKings, FanDuel has no public API. This connector filters
The Odds API response for FanDuel's bookmaker key and normalizes it
into MarketSnapshot objects, tracking opening vs. current odds
in memory per session.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import aiohttp

from .base import BaseConnector, ConnectorStatus, MarketSnapshot

logger = logging.getLogger(__name__)

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
    "h2h":          "Moneyline",
    "spreads":      "Spread",
    "totals":       "Total (O/U)",
    "player_props": "Player Prop",
}

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_BOOK_KEY      = "fanduel"
_BOOK_TITLE    = "FanDuel"


class FanDuelConnector(BaseConnector):
    """
    Fetches FanDuel moneylines and props via The Odds API.

    Opening odds tracking is in-memory per session (same pattern as
    DraftKingsConnector). The normalization layer is shared with DraftKings
    — only the book key and title differ.
    """

    name          = "FanDuel"
    is_pickem     = False
    poll_interval = 60

    def __init__(
        self,
        odds_api_key: str,
        active_sports: list[str],
        enabled: bool = True,
    ) -> None:
        self._api_key       = odds_api_key
        self._active_sports = active_sports
        self.enabled        = enabled
        self._opening: dict[tuple[str, str], int] = {}

    async def fetch(self) -> list[MarketSnapshot]:
        if not self.enabled:
            return []
        if not self._api_key:
            logger.debug("FanDuel connector: no ODDS_API_KEY, skipping")
            return []

        snapshots: list[MarketSnapshot] = []
        for sport in self._active_sports:
            sport_key = _SPORT_KEYS.get(sport)
            if not sport_key:
                continue
            snaps = await self._fetch_sport(sport, sport_key)
            snapshots.extend(snaps)

        logger.info("FanDuel: fetched %d snapshots", len(snapshots))
        return snapshots

    async def health_check(self) -> ConnectorStatus:
        if not self._api_key:
            return ConnectorStatus.NO_KEY
        url = f"{_ODDS_API_BASE}/sports"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, params={"apiKey": self._api_key}) as resp:
                    return ConnectorStatus.OK if resp.status == 200 else ConnectorStatus.ERROR
        except Exception:
            return ConnectorStatus.ERROR

    async def _fetch_sport(self, sport: str, sport_key: str) -> list[MarketSnapshot]:
        url = f"{_ODDS_API_BASE}/sports/{sport_key}/odds"
        params = {
            "apiKey":     self._api_key,
            "regions":    "us",
            "markets":    "h2h,spreads,totals",
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
                        message = (body or {}).get("message", "")
                        if resp.status == 401 and error_code == "OUT_OF_USAGE_CREDITS":
                            logger.warning(
                                "FanDuel/OddsAPI: usage quota exhausted (401) for %s — %s",
                                sport, message,
                            )
                        else:
                            logger.warning(
                                "FanDuel/OddsAPI HTTP %d for %s%s",
                                resp.status, sport,
                                f" — {error_code or message}" if (error_code or message) else "",
                            )
                        return []
                    resp.raise_for_status()
                    data: list[dict] = await resp.json()
        except aiohttp.ClientError as exc:
            logger.warning("FanDuel fetch error (%s): %s", sport, exc)
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
                    game_time = datetime.fromisoformat(
                        raw_t.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass

            for bm in event.get("bookmakers", []):
                if bm.get("key", "").lower() != _BOOK_KEY:
                    continue
                for market in bm.get("markets", []):
                    mtype = _MARKET_MAP.get(market.get("key", ""))
                    if mtype is None:
                        continue
                    for outcome in market.get("outcomes", []):
                        try:
                            sel  = str(outcome["name"])
                            odds = int(outcome["price"])
                            line = outcome.get("point")
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
                            odds         = odds,
                            timestamp    = now,
                            line         = float(line) if line is not None else None,
                            game_time    = game_time,
                            opening_odds = opening,
                            is_pickem    = False,
                        ))

        return snapshots
