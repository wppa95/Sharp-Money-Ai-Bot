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

# ── DOTA 2: Known aliases — Underdog handle (lower) → OpenDota search term ───
# Covers pros whose Steam personaname differs from their competitive handle or
# whose names include characters that break fuzzy matching (dashes, digits, etc.)
# Add new entries when a player's OpenDota personaname search returns no results.
_DOTA_PLAYER_ALIASES: dict[str, str] = {
    "33":           "33",           # Neta Shapira — number handle
    "miracle":      "Miracle-",     # dash suffix breaks substring match
    "miracle-":     "Miracle-",
    "matumbaman":   "MATUMBAMAN",   # all-caps steam name
    "arteezy":      "Arteezy",
    "s4":           "s4",
    "zai":          "zai",
    "qojqva":       "qojqva",
    "sneyking":     "Sneyking",
    "quinn":        "Quinn",
    "mason":        "Mason",
    "shine":        "SHiNE",        # stylised casing
    "ahma":         "AhMa",         # mixed case
    "samppa":       "Samppa",
    "p3kko":        "p3kko",
    "kreaz":        "kreaz",
    "pakazs":       "pakazs",
    "gunnar":       "Gunnar",
    "swiftly":      "Swiiftly",
    "yowe":         "yowe",
    "bzm":          "bZm",
    "bryle":        "Bryle",
}

# ── CS2: Known aliases — Underdog handle (lower) → PandaScore name search ────
_CS_PLAYER_ALIASES: dict[str, str] = {
    "niko":         "NiKo",         # PandaScore uses their in-game handle
    "s1mple":       "s1mple",
    "device":       "device",
    "zywoo":        "ZywOo",        # mixed case
    "sh1ro":        "sh1ro",
    "m0nesy":       "m0nesy",
    "jl":           "jL",
    "ax1le":        "ax1Le",
    "electronic":   "electronic",
    "blamef":       "blameF",       # capital F
    "ropz":         "ropz",
    "hunter":       "huNter-",
    "broky":        "broky",
    "karrigan":     "karrigan",
    "twistzz":      "Twistzz",
    "magisk":       "Magisk",
    "dupreeh":      "dupreeh",
}

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


def _resolve_alias(name: str, alias_table: dict[str, str]) -> str:
    """
    Return the canonical search term for *name* using *alias_table*.

    Looks up by lowercased name; returns the original name unchanged when
    no alias is found so the normal fuzzy search still runs.
    """
    return alias_table.get(name.lower().strip(), name)


def _group_into_series(
    matches: list[dict],
    max_gap_seconds: int = 14400,
) -> list[list[dict]]:
    """
    Group OpenDota recentMatches into series by start_time proximity.

    Consecutive matches within *max_gap_seconds* (default 4 h) of each other
    are assumed to be from the same series (BO3/BO5 games played on the same day).
    Returns a list of series, each series being a list of match dicts sorted by
    start_time ascending (game 1 first).
    """
    valid = [m for m in matches if isinstance(m.get("start_time"), (int, float))]
    if not valid:
        return []

    sorted_m = sorted(valid, key=lambda m: float(m["start_time"]))
    series: list[list[dict]] = []
    current: list[dict] = [sorted_m[0]]

    for match in sorted_m[1:]:
        gap = float(match["start_time"]) - float(current[-1]["start_time"])
        if gap <= max_gap_seconds:
            current.append(match)
        else:
            series.append(current)
            current = [match]
    series.append(current)

    # Return newest series first to match the existing newest-first convention
    return list(reversed(series))


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

        search_name = _resolve_alias(clean_name, _DOTA_PLAYER_ALIASES)
        account_id  = await self._opendota_account_id(search_name, original_name=clean_name)
        if account_id is None:
            return []

        url  = f"https://api.opendota.com/api/players/{account_id}/recentMatches"
        data = await self._get_json(url)
        if not isinstance(data, list):
            return []

        maps_n  = _maps_count(stat_lower)
        results: list[RawGameResult] = []

        if maps_n > 1:
            # ── Series-pairing mode (Maps 1+2 / 1+2+3 cumulative props) ──────
            # Group matches into series by start_time proximity so we sum the
            # real per-game values instead of multiplying a single average.
            all_matches = [m for m in data if isinstance(m, dict)]
            for series in _group_into_series(all_matches):
                if len(series) < maps_n:
                    continue  # incomplete series — not enough maps played
                # Use the first maps_n games of the series (Map 1, Map 2, …)
                series_games = series[:maps_n]
                total: float = 0.0
                valid = True
                for match in series_games:
                    if field == "_fantasy":
                        val = _compute_dota_fantasy(match)
                    else:
                        raw = match.get(field)
                        try:
                            val = float(raw) if raw is not None else None
                        except (TypeError, ValueError):
                            val = None
                    if val is None:
                        valid = False
                        break
                    total += val

                if not valid:
                    continue

                gd = _opendota_game_date(series_games[0])
                if not gd:
                    continue

                results.append(RawGameResult(
                    player_name  = raw_name,
                    sport        = "DOTA",
                    stat_type    = stat_lower,
                    game_date    = gd,
                    actual_value = total,
                    opponent     = None,
                    source       = "opendota_series",
                ))
        else:
            # ── Single-map mode ───────────────────────────────────────────────
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

                results.append(RawGameResult(
                    player_name  = raw_name,
                    sport        = "DOTA",
                    stat_type    = stat_lower,
                    game_date    = gd,
                    actual_value = raw_val,
                    opponent     = None,
                    source       = "opendota",
                ))

        logger.debug(
            "DOTA: %s / %s → %d results (field=%s maps_n=%d mode=%s)",
            clean_name, stat_lower, len(results), field, maps_n,
            "series" if maps_n > 1 else "single",
        )
        return results

    async def _opendota_account_id(
        self,
        player_name: str,
        original_name: Optional[str] = None,
    ) -> Optional[int]:
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
            display = original_name or player_name
            logger.warning(
                "OpenDota: player not found for %r (searched %r) — "
                "add to _DOTA_PLAYER_ALIASES if this is a known pro",
                display, player_name,
            )
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

        search_name = _resolve_alias(clean_name, _CS_PLAYER_ALIASES)
        player_id   = await self._pandascore_player_id(search_name, original_name=clean_name)
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

    async def _pandascore_player_id(
        self,
        player_name: str,
        original_name: Optional[str] = None,
    ) -> Optional[int]:
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
            display = original_name or player_name
            logger.warning(
                "PandaScore CS: player not found for %r (searched %r) — "
                "add to _CS_PLAYER_ALIASES if this is a known pro",
                display, player_name,
            )
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
