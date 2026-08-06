"""
providers/sleeper_stats.py — Sleeper Stats API data provider.

Supplements the existing player stats pipeline with Sleeper's free public API:

  NFL (primary target):
    Per-week stats from api.sleeper.app/v1/stats/nfl/regular/{year}/{week}.
    One NFL game per week → weekly totals = per-game stats → ideal for hit rates.

  NBA / MLB (shadow mode):
    Multiple games per week → weekly totals are NOT per-game comparable.
    Data is stored with source="sleeper_stats_weekly" for audit only; it is NOT
    fed into gate decisions or hit-rate scoring.

  Pick'em lines:
    NOT available. All /picks, /lines, /odds, /projections endpoints return 404
    or empty objects. Sleeper pick'em is a mobile-only feature with no public API.
    PrizePicks line replacement via Sleeper is NOT possible.

Player registry:
    Built on first use from /v1/players/{sport} (~12,200 NFL, ~2,100 NBA).
    Cached for the process lifetime; no daily refresh needed (roster changes
    are within fuzzy-match tolerance).

Week stats (bulk):
    /v1/stats/{sport}/regular/{year}/{week} returns ALL players for that week.
    One fetch covers the entire roster — subsequent player lookups for the same
    week are O(1) dict lookups. Weeks are prefetched concurrently (≤5 parallel)
    the first time any player from that sport is requested.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import aiohttp

from providers.player_stats import RawGameResult, _names_match

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT   = aiohttp.ClientTimeout(total=10)
_CONCURRENCY_LIMIT = 5   # max simultaneous week-stat requests

# ── Sport slug mapping ────────────────────────────────────────────────────────
# Underdog sport key → Sleeper API sport slug
_SLEEPER_SPORT_SLUG: dict[str, str] = {
    "NFL": "nfl",
    "NBA": "nba",
    "MLB": "mlb",
    # WNBA not separately tracked on Sleeper public API
}

# ── Stat key mappings ─────────────────────────────────────────────────────────
# bot stat_type.lower() → list[Sleeper stat keys to sum]
# Verified against api.sleeper.app/v1/stats/nfl/regular/2025/1 response.

_NFL_STAT_KEYS: dict[str, list[str]] = {
    # Passing
    "passing yards":           ["pass_yd"],
    "pass yards":              ["pass_yd"],
    "passing touchdowns":      ["pass_td"],
    "pass touchdowns":         ["pass_td"],
    "interceptions":           ["pass_int"],
    "pass completions":        ["pass_cmp"],
    # Rushing
    "rushing yards":           ["rush_yd"],
    "rush yards":              ["rush_yd"],
    "rushing touchdowns":      ["rush_td"],
    "rush touchdowns":         ["rush_td"],
    "rushing attempts":        ["rush_att"],
    "rush attempts":           ["rush_att"],
    # Receiving
    "receiving yards":         ["rec_yd"],
    "rec yards":               ["rec_yd"],
    "receiving touchdowns":    ["rec_td"],
    "rec touchdowns":          ["rec_td"],
    "receptions":              ["rec"],
    "targets":                 ["rec_tgt"],
    # Defense / special teams
    "sacks":                   ["sack"],
    "tackles":                 ["tkl"],
    "field goals made":        ["fgm"],
    # Combo stats
    "passing + rushing yards": ["pass_yd", "rush_yd"],
    "rush + rec yards":        ["rush_yd", "rec_yd"],
    "rush + rec touchdowns":   ["rush_td", "rec_td"],
    "rush + rec tds":          ["rush_td", "rec_td"],
}

_NBA_STAT_KEYS: dict[str, list[str]] = {
    "points":                      ["pts"],
    "rebounds":                    ["reb"],
    "assists":                     ["ast"],
    "steals":                      ["stl"],
    "blocks":                      ["blk"],
    "turnovers":                   ["tov"],
    "3-pointers made":             ["tpm"],
    "three-pointers made":         ["tpm"],
    "3pt made":                    ["tpm"],
    "3-pt made":                   ["tpm"],
    "free throws made":            ["ftm"],
    "field goals made":            ["fgm"],
    "blocks + steals":             ["blk", "stl"],
    "blk+stl":                     ["blk", "stl"],
    "points + rebounds + assists": ["pts", "reb", "ast"],
    "pts+reb+ast":                 ["pts", "reb", "ast"],
    "pts + reb + ast":             ["pts", "reb", "ast"],
    "points + rebounds":           ["pts", "reb"],
    "pts+reb":                     ["pts", "reb"],
    "points + assists":            ["pts", "ast"],
    "pts+ast":                     ["pts", "ast"],
    "rebounds + assists":          ["reb", "ast"],
    "reb+ast":                     ["reb", "ast"],
    "defensive rebounds":          ["dreb"],
    "offensive rebounds":          ["oreb"],
}

_MLB_STAT_KEYS: dict[str, list[str]] = {
    "hits":               ["hits"],
    "home runs":          ["hr"],
    "hr":                 ["hr"],
    "rbis":               ["rbi"],
    "rbi":                ["rbi"],
    "stolen bases":       ["sb"],
    "walks":              ["bb"],
    "runs scored":        ["runs"],
    "runs":               ["runs"],
    "strikeouts":         ["so"],
    "batter strikeouts":  ["so"],
    "doubles":            ["doubles"],
    "triples":            ["triples"],
    "hits+runs+rbis":     ["hits", "runs", "rbi"],
    "h+r+rbi":            ["hits", "runs", "rbi"],
    "hits + runs + rbis": ["hits", "runs", "rbi"],
}

_STAT_KEY_MAP: dict[str, dict[str, list[str]]] = {
    "nfl": _NFL_STAT_KEYS,
    "nba": _NBA_STAT_KEYS,
    "mlb": _MLB_STAT_KEYS,
}

# Approximate NFL season week-1 Thursday start dates
_NFL_WEEK1: dict[int, date] = {
    2023: date(2023, 9, 7),
    2024: date(2024, 9, 5),
    2025: date(2025, 9, 4),
    2026: date(2026, 9, 3),   # estimated
}


# ── Provider ──────────────────────────────────────────────────────────────────

class SleeperStatsProvider:
    """
    Fetches player stats from the Sleeper public API.

    Thread-safe for concurrent asyncio tasks. Every method returns []
    (never raises) on network or parse failure.

    NFL usage:
        One game per NFL week → per-week totals = per-game result.
        Feeds directly into RawGameResult → DB → compute_hit_rates().

    NBA / MLB usage (shadow only):
        Weekly totals span multiple games — stored as source="sleeper_stats_weekly"
        for audit, but NOT used in hit-rate gate decisions.
    """

    BASE = "https://api.sleeper.app/v1"

    def __init__(self) -> None:
        # sport_slug → {player_id: player_dict}
        self._registry:       dict[str, dict[str, dict]]   = {}
        # sport_slug → {name_normalized: player_id}
        self._name_to_id:     dict[str, dict[str, str]]    = {}
        self._registry_ready: dict[str, bool]              = {}
        # (sport_slug, season_type, year, week) → {player_id: {stat_key: value}}
        self._week_cache:     dict[tuple, dict[str, dict]] = {}
        # Semaphore limiting concurrent API calls (created inside an event loop)
        self._sem: Optional[asyncio.Semaphore] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
    ) -> list[RawGameResult]:
        """
        Return historical game results for player_name × stat_type.

        NFL:  per-week = per-game → suitable for hit-rate scoring.
        NBA / MLB: per-week totals → stored shadow-only (not scored).

        Returns [] on any error.
        """
        sport_upper  = sport.upper()
        sport_slug   = _SLEEPER_SPORT_SLUG.get(sport_upper)
        if sport_slug is None:
            return []

        stat_lower   = stat_type.lower().strip()
        sleeper_keys = _STAT_KEY_MAP.get(sport_slug, {}).get(stat_lower, [])
        if not sleeper_keys:
            return []

        try:
            await self._ensure_registry(sport_slug)
            player_id = self._lookup_player_id(sport_slug, player_name)
            if player_id is None:
                logger.debug("Sleeper: player not found: %r (%s)", player_name, sport)
                return []

            if sport_upper == "NFL":
                return await self._fetch_nfl(player_id, player_name, stat_lower, sleeper_keys)
            else:
                return await self._fetch_weekly(
                    sport_slug, player_id, player_name, sport_upper,
                    stat_lower, sleeper_keys,
                )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Sleeper: unexpected error for %r %r: %s", player_name, stat_type, exc,
            )
            return []

    # ── NFL: per-week = per-game ──────────────────────────────────────────────

    async def _fetch_nfl(
        self,
        player_id: str,
        player_name: str,
        stat_lower: str,
        sleeper_keys: list[str],
    ) -> list[RawGameResult]:
        results: list[RawGameResult] = []
        now     = datetime.utcnow()

        # Always include 2025 season. Add current year if different.
        seasons: list[int] = [2025]
        if now.year > 2025:
            seasons.append(now.year)

        for season_year in seasons:
            weeks = list(range(1, 19))   # NFL regular season: 18 weeks
            # Pre-warm all 18 weeks concurrently (hits cache on subsequent players)
            await self._prefetch_weeks("nfl", "regular", season_year, weeks)

            for week in weeks:
                week_stats = self._week_cache.get(("nfl", "regular", season_year, week), {})
                if not week_stats:
                    break  # Empty week → season hasn't started yet
                player_stats = week_stats.get(player_id)
                if not player_stats:
                    continue
                val = _sum_stat_keys(player_stats, sleeper_keys)
                if val is None:
                    continue
                results.append(RawGameResult(
                    player_name  = player_name,
                    sport        = "NFL",
                    stat_type    = stat_lower,
                    game_date    = _nfl_week_to_date(season_year, week),
                    actual_value = val,
                    opponent     = None,
                    source       = "sleeper_stats",
                ))

        logger.debug(
            "Sleeper NFL: %r / %s → %d results",
            player_name, stat_lower, len(results),
        )
        return results

    # ── NBA / MLB: weekly aggregates (shadow only) ────────────────────────────

    async def _fetch_weekly(
        self,
        sport_slug: str,
        player_id: str,
        player_name: str,
        sport_upper: str,
        stat_lower: str,
        sleeper_keys: list[str],
    ) -> list[RawGameResult]:
        """
        Weekly aggregate results. source="sleeper_stats_weekly".
        One week may cover 3–5 games — values are NOT comparable to single-game
        prop lines and must not be used in hit-rate gate decisions.
        """
        results: list[RawGameResult] = []
        year  = 2025
        weeks = list(range(1, 29))   # NBA ~28 weeks, MLB ~26 weeks
        await self._prefetch_weeks(sport_slug, "regular", year, weeks)

        for week in weeks:
            week_stats = self._week_cache.get((sport_slug, "regular", year, week), {})
            if not week_stats:
                break
            player_stats = week_stats.get(player_id)
            if not player_stats:
                continue
            val = _sum_stat_keys(player_stats, sleeper_keys)
            if val is None:
                continue
            results.append(RawGameResult(
                player_name  = player_name,
                sport        = sport_upper,
                stat_type    = stat_lower,
                game_date    = _iso_week_monday(year, week),
                actual_value = val,
                opponent     = None,
                source       = "sleeper_stats_weekly",  # NOT per-game
            ))

        logger.debug(
            "Sleeper %s: %r / %s → %d weekly records (shadow)",
            sport_upper, player_name, stat_lower, len(results),
        )
        return results

    # ── Player registry ───────────────────────────────────────────────────────

    async def _ensure_registry(self, sport_slug: str) -> None:
        """Load and cache the player registry once per sport per process."""
        if self._registry_ready.get(sport_slug):
            return
        try:
            data = await self._get_json(f"{self.BASE}/players/{sport_slug}")
            if not isinstance(data, dict) or not data:
                logger.warning("Sleeper: empty player registry for %s", sport_slug)
                return
            self._registry[sport_slug] = data
            name_map: dict[str, str] = {}
            for pid, pdata in data.items():
                full = pdata.get("full_name") or (
                    (pdata.get("first_name") or "") + " " + (pdata.get("last_name") or "")
                ).strip()
                if full:
                    name_map[full.lower().strip()] = pid
            self._name_to_id[sport_slug] = name_map
            self._registry_ready[sport_slug] = True
            logger.info(
                "Sleeper: loaded %d %s players into registry",
                len(data), sport_slug.upper(),
            )
        except Exception as exc:
            logger.warning("Sleeper: registry load failed for %s: %s", sport_slug, exc)

    def _lookup_player_id(self, sport_slug: str, player_name: str) -> Optional[str]:
        """
        Find Sleeper player_id for player_name.

        Tries exact match first, then fuzzy match (≥ 0.82 ratio).
        """
        name_map = self._name_to_id.get(sport_slug, {})
        if not name_map:
            return None
        norm = player_name.lower().strip()
        # Exact match
        if norm in name_map:
            return name_map[norm]
        # Fuzzy match — pick the best-scoring candidate above threshold
        from difflib import SequenceMatcher
        best_id:    Optional[str] = None
        best_score: float         = 0.0
        for canon, pid in name_map.items():
            if not _names_match(canon, norm, threshold=0.82):
                continue
            score = SequenceMatcher(None, norm, canon).ratio()
            if score > best_score:
                best_score = score
                best_id    = pid
        if best_id and best_score < 1.0:
            logger.debug(
                "Sleeper: fuzzy-matched %r → player_id=%s (score=%.2f)",
                player_name, best_id, best_score,
            )
        return best_id

    # ── Week stats fetching (concurrent, cached) ──────────────────────────────

    async def _prefetch_weeks(
        self,
        sport_slug: str,
        season_type: str,
        year: int,
        weeks: list[int],
    ) -> None:
        """
        Fetch multiple weeks concurrently (≤_CONCURRENCY_LIMIT at once).
        Each week covers ALL players — subsequent per-player lookups are O(1).
        Already-cached weeks are skipped.
        """
        if self._sem is None:
            self._sem = asyncio.Semaphore(_CONCURRENCY_LIMIT)

        needed = [w for w in weeks if (sport_slug, season_type, year, w) not in self._week_cache]
        if not needed:
            return

        async def _fetch_one(week: int) -> None:
            key = (sport_slug, season_type, year, week)
            if key in self._week_cache:
                return
            async with self._sem:  # type: ignore[union-attr]
                if key in self._week_cache:
                    return
                data = await self._get_json(
                    f"{self.BASE}/stats/{sport_slug}/{season_type}/{year}/{week}"
                )
                self._week_cache[key] = data if isinstance(data, dict) else {}

        await asyncio.gather(*[_fetch_one(w) for w in needed])

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get_json(self, url: str) -> Optional[dict]:
        """GET url; return parsed JSON dict or None on any failure."""
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)"},
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Sleeper: HTTP %d for %s", resp.status, url)
                        return None
                    data = await resp.json(content_type=None)
                    # Empty dict {} = future/non-existent week — treat as None
                    if isinstance(data, dict) and not data:
                        return None
                    return data
        except asyncio.TimeoutError:
            logger.debug("Sleeper: timeout for %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.debug("Sleeper: HTTP error for %s: %s", url, exc)
            return None


# ── Module-level helpers ───────────────────────────────────────────────────────

def _sum_stat_keys(stats: dict, keys: list[str]) -> Optional[float]:
    """
    Sum Sleeper stat values for all keys.

    Returns None when no key has data (all absent = player had no stats recorded).
    Missing individual keys default to 0.0 — a player with 0 targets still has
    0 targets; absence from the dict means the same thing.
    """
    if not keys or not stats:
        return None
    total    = 0.0
    any_data = False
    for key in keys:
        raw = stats.get(key)
        if raw is not None:
            try:
                total   += float(raw)
                any_data = True
            except (TypeError, ValueError):
                pass
    return total if any_data else None


def _nfl_week_to_date(year: int, week: int) -> str:
    """
    Return the approximate game date (Thursday) for an NFL week.

    Uses known week-1 start dates; falls back to Sep 4 for unknown years.
    """
    week1 = _NFL_WEEK1.get(year, date(year, 9, 4))
    return (week1 + timedelta(weeks=week - 1)).isoformat()


def _iso_week_monday(year: int, week: int) -> str:
    """ISO calendar Monday for a given (year, week) pair."""
    try:
        return date.fromisocalendar(year, week, 1).isoformat()
    except ValueError:
        return date(year, 1, 1).isoformat()
