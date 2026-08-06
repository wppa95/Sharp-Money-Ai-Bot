"""
providers/nhl_stats.py — NHL per-game player stat provider.

Data source: NHL public API — no authentication required.
  Registry:  api.nhle.com/stats/rest/en/{skater,goalie}/bios
  Game logs: api-web.nhle.com/v1/player/{id}/game-log/{season}/2

Verified accessible from this environment (August 2026).

Architecture mirrors PlayerStatsProvider:
    NHLStatsProvider.fetch_results(player_name, sport, stat_type)
        → list[RawGameResult]

Registry:
    All active NHL skater + goalie names and IDs are bulk-loaded once from
    the bios endpoint on first use, then cached for the process lifetime.
    Fuzzy name matching handles minor spelling differences.

Game logs:
    The last two NHL regular seasons are fetched per player.  Skater and
    goalie game logs share the same endpoint but return different fields
    (e.g. shotsAgainst / goalsAgainst for goalies vs shots / points for
    skaters).  The provider detects position from the registry.

Goalie "saves":
    The game log does not include a direct saves field.  It is computed as
    shotsAgainst − goalsAgainst, which equals saves by definition.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiohttp

from providers.player_stats import RawGameResult, _names_match, _normalize

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
_BASE_WEB   = "https://api-web.nhle.com/v1"
_BASE_STATS = "https://api.nhle.com/stats/rest/en"
_GAME_TYPE_REGULAR = 2   # 2 = regular season, 3 = playoffs

# ── Stat maps ─────────────────────────────────────────────────────────────────
# bot stat_type.lower() → NHL game-log field name (or special sentinel)
#   "_saves"        → computed: shotsAgainst - goalsAgainst
#   "_goals_assists" → computed: goals + assists
#   "_toi_minutes"  → parsed from "MM:SS" string

_SKATER_STAT_MAP: dict[str, Optional[str]] = {
    "goals":               "goals",
    "assists":             "assists",
    "points":              "points",
    "shots on goal":       "shots",
    "shots":               "shots",       # "shots" normalised from Underdog
    "power play goals":    "powerPlayGoals",
    "power play points":   "powerPlayPoints",
    "time on ice":         "_toi_minutes",
    "goals + assists":     "_goals_assists",
    "g+a":                 "_goals_assists",
    # skater stats not available in game log
    "hits":                None,
    "blocked shots":       None,
}

_GOALIE_STAT_MAP: dict[str, Optional[str]] = {
    "saves":               "_saves",
    "goalkeeper saves":    "_saves",
    "goals allowed":       "goalsAgainst",
    "save percentage":     "savePctg",
    "shutouts":            "shutouts",
    "shots against":       "shotsAgainst",
}


def _season_ids() -> list[str]:
    """
    Return the 1-2 most-recent NHL season ID strings, most recent first.

    NHL season format: YYYYYYYY (e.g. "20252026" for the 2025-26 season).
    The season runs October → April/May, so:
      • Month ≥ 10 → current season = this year → next year
      • Month <  10 → current season = last year → this year
    """
    now = datetime.utcnow()
    y, m = now.year, now.month
    if m >= 10:
        current = f"{y}{y+1}"
        prev    = f"{y-1}{y}"
    else:
        current = f"{y-1}{y}"
        prev    = f"{y-2}{y-1}"
    return [current, prev]


# ── Provider ──────────────────────────────────────────────────────────────────

class NHLStatsProvider:
    """
    Fetch per-game NHL stats from the official NHL public API.

    Thread-safe for concurrent asyncio tasks.  Every method returns []
    (never raises) on network or parse failure.
    """

    def __init__(self) -> None:
        # name_norm → player_id
        self._name_to_id:     dict[str, int]  = {}
        # player_id → True if the player is a goalie
        self._is_goalie:      dict[int, bool] = {}
        self._registry_ready: bool            = False

    # ── Public entry point ────────────────────────────────────────────────────

    async def fetch_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
    ) -> list[RawGameResult]:
        """Return per-game NHL stats for *player_name* × *stat_type*."""
        stat_lower = stat_type.lower().strip()

        # Reject unknown stats early (before hitting the network)
        in_skater = stat_lower in _SKATER_STAT_MAP
        in_goalie = stat_lower in _GOALIE_STAT_MAP
        if not in_skater and not in_goalie:
            logger.debug("NHL: no mapping for stat %r", stat_lower)
            return []

        try:
            await self._ensure_registry()
            player_id = self._lookup_player_id(player_name)
            if player_id is None:
                return []

            is_g = self._is_goalie.get(player_id, False)
            field: Optional[str]
            if is_g:
                field = _GOALIE_STAT_MAP.get(stat_lower, "__missing__")
            else:
                field = _SKATER_STAT_MAP.get(stat_lower, "__missing__")

            if field == "__missing__" or field is None:
                logger.debug(
                    "NHL: stat %r not applicable for %s (player %d)",
                    stat_lower, "goalie" if is_g else "skater", player_id,
                )
                return []

            return await self._fetch_gamelog(player_name, player_id, stat_lower, field)

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NHL: unexpected error for %r %r: %s", player_name, stat_type, exc
            )
            return []

    # ── Game log ──────────────────────────────────────────────────────────────

    async def _fetch_gamelog(
        self,
        player_name: str,
        player_id: int,
        stat_lower: str,
        field: str,
    ) -> list[RawGameResult]:
        results: list[RawGameResult] = []
        seen_dates: set[str] = set()

        for season_id in _season_ids():
            url  = (
                f"{_BASE_WEB}/player/{player_id}"
                f"/game-log/{season_id}/{_GAME_TYPE_REGULAR}"
            )
            data = await self._get_json(url)
            if data is None:
                continue

            for game in (data.get("gameLog") or []):
                game_date = game.get("gameDate", "")
                if not game_date or game_date in seen_dates:
                    continue
                val = _extract_stat(game, field)
                if val is None:
                    continue
                seen_dates.add(game_date)
                results.append(RawGameResult(
                    player_name  = player_name,
                    sport        = "NHL",
                    stat_type    = stat_lower,
                    game_date    = game_date,
                    actual_value = val,
                    opponent     = game.get("opponentAbbrev"),
                    source       = "nhl_stats_api",
                ))

        logger.debug(
            "NHL: %r / %r → %d results", player_name, stat_lower, len(results)
        )
        return results

    # ── Registry ──────────────────────────────────────────────────────────────

    async def _ensure_registry(self) -> None:
        """Bulk-load all active NHL player names+IDs once per process."""
        if self._registry_ready:
            return

        season_id = _season_ids()[0]
        loaded = 0

        try:
            # IMPORTANT: these bios endpoints require filters inside cayenneExp.
            # Passing seasonId / gameTypeId as plain query-string params returns HTTP 500.
            # Verified working form (August 2026):
            #   ?limit=N&start=K&cayenneExp=seasonId=SSSS+and+gameTypeId=G
            #
            # The API hard-caps results at 100 per page regardless of limit=.
            # Paginate with start=0, 100, 200, … until returned < page_size.
            cayenne   = f"seasonId={season_id}+and+gameTypeId={_GAME_TYPE_REGULAR}"
            page_size = 100

            # Skaters — paginated
            start = 0
            while True:
                url  = (
                    f"{_BASE_STATS}/skater/bios"
                    f"?limit={page_size}&start={start}&cayenneExp={cayenne}"
                )
                data = await self._get_json(url)
                if not data:
                    break
                page = data.get("data") or []
                for p in page:
                    pid  = p.get("playerId")
                    name = p.get("skaterFullName") or p.get("fullName") or ""
                    if pid and name:
                        self._name_to_id[_normalize(name)] = int(pid)
                        self._is_goalie[int(pid)] = False
                        loaded += 1
                if len(page) < page_size:
                    break   # last page
                start += page_size

            # Goalies — typically <100; paginate for safety
            start = 0
            while True:
                url  = (
                    f"{_BASE_STATS}/goalie/bios"
                    f"?limit={page_size}&start={start}&cayenneExp={cayenne}"
                )
                data = await self._get_json(url)
                if not data:
                    break
                page = data.get("data") or []
                for p in page:
                    pid  = p.get("playerId")
                    name = (
                        p.get("goalieFullName")
                        or p.get("skaterFullName")
                        or p.get("fullName")
                        or ""
                    )
                    if pid and name:
                        self._name_to_id[_normalize(name)] = int(pid)
                        self._is_goalie[int(pid)] = True
                        loaded += 1
                if len(page) < page_size:
                    break
                start += page_size

            # Only mark ready if we actually loaded players.  An empty result
            # (e.g. wrong season ID, transient failure) stays un-ready so the
            # next fetch_results call retries the registry load.
            if loaded > 0:
                self._registry_ready = True
                logger.info(
                    "NHL: loaded %d players into registry (season %s)",
                    loaded, season_id,
                )
            else:
                logger.warning(
                    "NHL: registry returned 0 players for season %s — will retry on next call",
                    season_id,
                )
        except Exception as exc:
            logger.warning("NHL: registry load failed: %s", exc)

    def _lookup_player_id(self, player_name: str) -> Optional[int]:
        """Find NHL player ID by name — exact match then fuzzy."""
        if not self._name_to_id:
            return None
        norm = _normalize(player_name)
        if norm in self._name_to_id:
            return self._name_to_id[norm]

        from difflib import SequenceMatcher
        best_id:    Optional[int] = None
        best_score: float         = 0.0
        for canon, pid in self._name_to_id.items():
            if not _names_match(canon, norm, threshold=0.82):
                continue
            score = SequenceMatcher(None, norm, canon).ratio()
            if score > best_score:
                best_score = score
                best_id    = pid

        if best_id:
            logger.debug(
                "NHL: fuzzy-matched %r (score=%.2f)", player_name, best_score
            )
        else:
            logger.debug("NHL: player not found: %r", player_name)
        return best_id

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get_json(self, url: str) -> Optional[dict]:
        """GET *url*; return parsed JSON dict or None on any failure."""
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)",
                    },
                ) as resp:
                    if resp.status != 200:
                        logger.debug("NHL: HTTP %d for %s", resp.status, url)
                        return None
                    return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.debug("NHL: timeout for %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.debug("NHL: HTTP error for %s: %s", url, exc)
            return None


# ── Module-level helpers ───────────────────────────────────────────────────────

def _extract_stat(game: dict, field: str) -> Optional[float]:
    """
    Extract a numeric value from an NHL game-log entry dict.

    Special field sentinels:
      _saves          → shotsAgainst − goalsAgainst
      _goals_assists  → goals + assists
      _toi_minutes    → parse "MM:SS" string to fractional minutes
    """
    if field == "_saves":
        sa = game.get("shotsAgainst")
        ga = game.get("goalsAgainst")
        if sa is None or ga is None:
            return None
        try:
            return max(0.0, float(sa) - float(ga))
        except (TypeError, ValueError):
            return None

    if field == "_goals_assists":
        g = game.get("goals")
        a = game.get("assists")
        if g is None or a is None:
            return None
        try:
            return float(g) + float(a)
        except (TypeError, ValueError):
            return None

    if field == "_toi_minutes":
        toi = str(game.get("toi", ""))
        if not toi or ":" not in toi:
            return None
        try:
            mins, secs = toi.split(":", 1)
            return float(mins) + float(secs) / 60.0
        except (ValueError, TypeError):
            return None

    raw = game.get(field)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
