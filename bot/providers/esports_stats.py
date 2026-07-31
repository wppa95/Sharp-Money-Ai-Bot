"""
providers/esports_stats.py — Player stat history for esports (CS2, DOTA 2).

Data sources:
  DOTA 2 → OpenDota API  (https://api.opendota.com/api/)
            Free, no API key required.
  CS2    → PandaScore API (https://api.pandascore.co/)
            Requires PANDASCORE_API_KEY env var; returns [] gracefully without it.

Stat type semantics — multi-map cumulative lines
────────────────────────────────────────────────
Underdog esports props cover single-map and multi-map markets:

  "Kills on Map 1"        – kills in one map  → stored actual value
  "Kills on Maps 1+2"     – kills across 2 maps
                            Per-game values are scaled by map count so the
                            stored value is directly comparable to the line.
                            e.g. 8 kills in one game → stored as 16 for a
                            "Maps 1+2" prop at line 10.5 → OVER (16 > 10.5).

Architecture mirrors PlayerStatsProvider:
  EsportsStatsProvider.fetch_results(player_name, sport, stat_type)
      → list[RawGameResult]
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import aiohttp

from providers.player_stats import RawGameResult, _names_match, _normalize

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)

# ── DOTA 2: Underdog stat-type (lower) → OpenDota recentMatches field ────────

_DOTA_FIELD_MAP: dict[str, str] = {
    "kills":                          "kills",
    "assists":                        "assists",
    "deaths":                         "deaths",
    "kills on map 1":                 "kills",
    "kills on map 2":                 "kills",
    "kills on map 3":                 "kills",
    "kills on maps 1+2":              "kills",
    "kills on maps 1+2+3":            "kills",
    "assists on map 1":               "assists",
    "assists on map 2":               "assists",
    "assists on maps 1+2":            "assists",
    "deaths on map 1":                "deaths",
    "deaths on map 2":                "deaths",
    "deaths on maps 1+2":             "deaths",
    "fantasy points":                 "_fantasy",
    "fantasy points in game 1":       "_fantasy",
    "fantasy points in game 2":       "_fantasy",
    "fantasy points in games 1+2":    "_fantasy",
    "fantasy points in games 1+2+3":  "_fantasy",
}

# ── CS2: Underdog stat-type (lower) → PandaScore player-stat field ───────────

_CS_FIELD_MAP: dict[str, str] = {
    "kills":                    "kills",
    "assists":                  "assists",
    "deaths":                   "deaths",
    "headshots":                "headshots",
    "kills on map 1":           "kills",
    "kills on map 2":           "kills",
    "kills on maps 1+2":        "kills",
    "assists on map 1":         "assists",
    "assists on maps 1+2":      "assists",
    "headshots on map 1":       "headshots",
    "headshots on map 2":       "headshots",
    "headshots on maps 1+2":    "headshots",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _maps_count(stat_lower: str) -> int:
    """Return the number of maps/games the cumulative stat covers."""
    if "1+2+3" in stat_lower:
        return 3
    if "1+2" in stat_lower:
        return 2
    return 1


def _compute_dota_fantasy(match: dict) -> Optional[float]:
    """
    Approximate Underdog DOTA 2 fantasy points from an OpenDota recentMatches
    record.

    Underdog's exact formula is proprietary.  This weighting (kills, assists,
    deaths, last_hits, gold_per_min) produces values in the 40–130 per-game
    range observed in the wild.  Calibrate _FANTASY_WEIGHTS if the lines drift.
    """
    try:
        kills     = float(match.get("kills",       0) or 0)
        assists   = float(match.get("assists",      0) or 0)
        deaths    = float(match.get("deaths",       0) or 0)
        last_hits = float(match.get("last_hits",    0) or 0)
        gpm       = float(match.get("gold_per_min", 0) or 0)
        return (
            kills     * 4.0
            + assists * 2.0
            + deaths  * (-2.0)
            + last_hits * 0.15
            + gpm       * 0.05
        )
    except (TypeError, ValueError):
        return None


def _opendota_game_date(match: dict) -> Optional[str]:
    """Convert OpenDota Unix start_time to YYYY-MM-DD."""
    ts = match.get("start_time")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _strip_none_prefix(name: str) -> str:
    """Remove the 'None ' prefix Underdog injects when first_name is unknown."""
    stripped = name.removeprefix("None ").strip()
    return stripped if stripped else name


# ── Provider ──────────────────────────────────────────────────────────────────

class EsportsStatsProvider:
    """
    Fetches per-map esports player stats.

    DOTA 2: OpenDota API — free, no key required.
    CS2:    PandaScore API — set PANDASCORE_API_KEY env var to enable;
            returns [] gracefully when the key is absent.
    """

    def __init__(self) -> None:
        self._pandascore_key: Optional[str] = os.environ.get("PANDASCORE_API_KEY")
        # cache: (norm_name, sport) → id or None (None = not found)
        self._id_cache: dict[tuple[str, str], Optional[int]] = {}

    # ── Public entry point ────────────────────────────────────────────────────

    async def fetch_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
    ) -> list[RawGameResult]:
        """
        Fetch recent per-map results for *player_name* × *stat_type* in *sport*.
        Returns [] on any error — never raises.
        """
        sport_up   = sport.upper()
        stat_lower = stat_type.lower().strip()
        clean_name = _strip_none_prefix(player_name)

        try:
            if sport_up == "DOTA":
                return await self._fetch_dota(clean_name, player_name, stat_lower)
            if sport_up == "CS":
                return await self._fetch_cs(clean_name, player_name, stat_lower)
            logger.debug("EsportsStatsProvider: unsupported sport %r", sport)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EsportsStatsProvider: unexpected error for %r %r %r: %s",
                player_name, sport, stat_type, exc,
            )
            return []

    # ── DOTA 2 — OpenDota API ─────────────────────────────────────────────────

    async def _fetch_dota(
        self,
        clean_name: str,
        raw_name: str,
        stat_lower: str,
    ) -> list[RawGameResult]:
        field = _DOTA_FIELD_MAP.get(stat_lower)
        if field is None:
            logger.debug("DOTA: no mapping for stat %r", stat_lower)
            return []

        account_id = await self._opendota_account_id(clean_name)
        if account_id is None:
            return []

        url  = f"https://api.opendota.com/api/players/{account_id}/recentMatches"
        data = await self._get_json(url)
        if not isinstance(data, list):
            return []

        maps_n  = _maps_count(stat_lower)
        results: list[RawGameResult] = []

        for match in data:
            if not isinstance(match, dict):
                continue
            gd = _opendota_game_date(match)
            if not gd:
                continue

            if field == "_fantasy":
                raw_val = _compute_dota_fantasy(match)
            else:
                raw = match.get(field)
                try:
                    raw_val = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    raw_val = None

            if raw_val is None:
                continue

            # Scale to match the cumulative-maps line
            actual = raw_val * maps_n

            results.append(RawGameResult(
                player_name  = raw_name,
                sport        = "DOTA",
                stat_type    = stat_lower,
                game_date    = gd,
                actual_value = actual,
                opponent     = None,
                source       = "opendota",
            ))

        logger.debug(
            "DOTA: %s / %s → %d results (field=%s maps×%d)",
            clean_name, stat_lower, len(results), field, maps_n,
        )
        return results

    async def _opendota_account_id(self, player_name: str) -> Optional[int]:
        cache_key = (_normalize(player_name), "dota")
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        url  = f"https://api.opendota.com/api/search?q={quote_plus(player_name)}"
        data = await self._get_json(url)

        aid: Optional[int] = None
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                persona = item.get("personaname") or ""
                if _names_match(persona, player_name):
                    try:
                        aid = int(item["account_id"])
                        break
                    except (KeyError, TypeError, ValueError):
                        pass

        if aid is None:
            logger.debug("OpenDota: player not found for %r", player_name)
        self._id_cache[cache_key] = aid
        return aid

    # ── CS2 — PandaScore API ──────────────────────────────────────────────────

    async def _fetch_cs(
        self,
        clean_name: str,
        raw_name: str,
        stat_lower: str,
    ) -> list[RawGameResult]:
        if not self._pandascore_key:
            logger.debug(
                "CS: PANDASCORE_API_KEY not set — no results for %r "
                "(set the key to enable CS2 history)",
                clean_name,
            )
            return []

        field = _CS_FIELD_MAP.get(stat_lower)
        if field is None:
            logger.debug("CS: no mapping for stat %r", stat_lower)
            return []

        player_id = await self._pandascore_player_id(clean_name)
        if player_id is None:
            return []

        maps_n = _maps_count(stat_lower)
        url = (
            f"https://api.pandascore.co/csgo/players/{player_id}/stats"
            f"?token={self._pandascore_key}&per_page=30"
        )
        # PandaScore /players/{id}/stats returns match-level detail
        # Fall back to the matches endpoint for per-map stats
        matches_url = (
            f"https://api.pandascore.co/csgo/matches"
            f"?filter[opponent_id]={player_id}&sort=-begin_at&per_page=30"
            f"&token={self._pandascore_key}"
        )
        data = await self._get_json(matches_url)
        if not isinstance(data, list):
            return []

        results: list[RawGameResult] = []
        for match in data:
            gd = self._pandascore_game_date(match)
            if not gd:
                continue
            val = self._extract_cs_stat(match, player_id, field)
            if val is None:
                continue
            results.append(RawGameResult(
                player_name  = raw_name,
                sport        = "CS",
                stat_type    = stat_lower,
                game_date    = gd,
                actual_value = val * maps_n,
                opponent     = self._pandascore_opponent(match),
                source       = "pandascore",
            ))

        logger.debug(
            "CS: %s / %s → %d results (maps×%d)",
            clean_name, stat_lower, len(results), maps_n,
        )
        return results

    async def _pandascore_player_id(self, player_name: str) -> Optional[int]:
        cache_key = (_normalize(player_name), "cs")
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        url = (
            f"https://api.pandascore.co/csgo/players"
            f"?search[name]={quote_plus(player_name)}&per_page=5"
            f"&token={self._pandascore_key}"
        )
        data = await self._get_json(url)
        pid: Optional[int] = None

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("slug") or ""
                if _names_match(name, player_name):
                    try:
                        pid = int(item["id"])
                        break
                    except (KeyError, TypeError, ValueError):
                        pass

        if pid is None:
            logger.debug("PandaScore CS: player not found for %r", player_name)
        self._id_cache[cache_key] = pid
        return pid

    @staticmethod
    def _pandascore_game_date(match: dict) -> Optional[str]:
        raw = match.get("begin_at") or match.get("end_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.date().isoformat()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _extract_cs_stat(match: dict, player_id: int, field: str) -> Optional[float]:
        """Extract a per-map player stat from a PandaScore match record."""
        for game in (match.get("games") or []):
            if not isinstance(game, dict):
                continue
            for team in (game.get("teams") or []):
                if not isinstance(team, dict):
                    continue
                for player in (team.get("players") or []):
                    if not isinstance(player, dict):
                        continue
                    p_obj = player.get("player") or {}
                    if p_obj.get("id") == player_id:
                        stats = player.get("stats") or {}
                        raw   = stats.get(field)
                        try:
                            return float(raw) if raw is not None else None
                        except (TypeError, ValueError):
                            return None
        return None

    @staticmethod
    def _pandascore_opponent(match: dict) -> Optional[str]:
        opponents = match.get("opponents") or []
        names = [
            o.get("opponent", {}).get("name")
            for o in opponents
            if isinstance(o, dict)
        ]
        names = [n for n in names if n]
        return names[0] if names else None

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get_json(self, url: str) -> Optional[object]:
        """GET *url* with timeout; return parsed JSON or None on failure."""
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)"},
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            "EsportsStatsProvider: HTTP %d for %s", resp.status, url
                        )
                        return None
                    return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.debug("EsportsStatsProvider: timeout for %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.debug("EsportsStatsProvider: client error for %s: %s", url, exc)
            return None
