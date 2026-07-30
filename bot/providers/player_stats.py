"""
providers/player_stats.py — Universal player game-result fetcher.

Fetches per-game stat results for every sport supported by Underdog / PrizePicks
from free public APIs (no API key required):

  MLB  → MLB Stats API  (https://statsapi.mlb.com/api/v1/)
  NBA, WNBA, NFL, NHL → ESPN unofficial athlete gamelog endpoint

Results are returned as a list of dicts suitable for upsert into the
``player_game_results`` table via ``Database.upsert_player_result()``.

Architecture
────────────
  PlayerStatsProvider.fetch_results(player_name, sport, stat_type)
      → list[RawGameResult]

A lightweight in-memory cache prevents re-querying the same player's
athlete ID within the same process lifetime.  Actual game-result records
are stored in the database (the DB is the durable cache).

Robustness
──────────
• Every network/parse failure returns [] (never raises).
• Per-request timeout: 6 s.
• ID look-up failures are cached as None so we don't retry every cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import NamedTuple, Optional
from urllib.parse import quote_plus

import aiohttp

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6)

# ── Raw result type returned by this provider ─────────────────────────────────

class RawGameResult(NamedTuple):
    """One game's result for a specific player × stat."""
    player_name:  str
    sport:        str
    stat_type:    str
    game_date:    str          # "YYYY-MM-DD"
    actual_value: float
    opponent:     Optional[str]  # abbreviation or display name; None if unknown
    source:       str            # "mlb_stats_api" | "espn_gamelog"


# ── Stat-type mapping tables ──────────────────────────────────────────────────

# NBA / WNBA  →  ESPN gamelog label(s) to sum
_NBA_STAT_MAP: dict[str, Optional[list[str]]] = {
    "points":                       ["PTS"],
    "rebounds":                     ["REB"],
    "assists":                      ["AST"],
    "steals":                       ["STL"],
    "blocks":                       ["BLK"],
    "turnovers":                    ["TO"],
    "3-pointers made":              ["3PM"],
    "three-pointers made":          ["3PM"],
    "3pt made":                     ["3PM"],
    "3-pt made":                    ["3PM"],
    "free throws made":             ["FTM"],
    "field goals made":             ["FGM"],
    "points + rebounds + assists":  ["PTS", "REB", "AST"],
    "pts+reb+ast":                  ["PTS", "REB", "AST"],
    "pts + reb + ast":              ["PTS", "REB", "AST"],
    "points + rebounds":            ["PTS", "REB"],
    "pts+reb":                      ["PTS", "REB"],
    "points + assists":             ["PTS", "AST"],
    "pts+ast":                      ["PTS", "AST"],
    "rebounds + assists":           ["REB", "AST"],
    "reb+ast":                      ["REB", "AST"],
    "blocks + steals":              ["BLK", "STL"],
    "blk+stl":                      ["BLK", "STL"],
    "defensive rebounds":           ["DREB"],
    "offensive rebounds":           ["OREB"],
    "fantasy score":                None,  # skip — complex formula
    "fantasy points":               None,
}

# NHL skaters  →  ESPN gamelog label(s) to sum
_NHL_STAT_MAP: dict[str, Optional[list[str]]] = {
    "goals":              ["G"],
    "assists":            ["A"],
    "points":             ["PTS"],   # G+A
    "shots on goal":      ["SOG"],
    "blocked shots":      ["BLK"],
    "hits":               ["HIT"],
    "goals + assists":    ["G", "A"],
    "g+a":                ["G", "A"],
    "power play points":  ["PPP"],
    "time on ice":        ["MIN"],
}

# NHL goalies  →  ESPN gamelog label(s)
_NHL_GOALIE_STAT_MAP: dict[str, Optional[list[str]]] = {
    "saves":           ["SV"],
    "goals allowed":   ["GA"],
}

# NFL  →  (category_key, [label(s)])  (ESPN multi-category gamelog)
_NFL_STAT_MAP: dict[str, tuple[str, list[str]]] = {
    "passing yards":          ("passing",   ["YDS"]),
    "pass yards":             ("passing",   ["YDS"]),
    "passing touchdowns":     ("passing",   ["TD"]),
    "pass touchdowns":        ("passing",   ["TD"]),
    "interceptions":          ("passing",   ["INT"]),
    "pass completions":       ("passing",   ["C"]),     # "C/ATT" → numerator
    "completion percentage":  ("passing",   ["PCT"]),
    "rushing yards":          ("rushing",   ["YDS"]),
    "rush yards":             ("rushing",   ["YDS"]),
    "rushing touchdowns":     ("rushing",   ["TD"]),
    "rush touchdowns":        ("rushing",   ["TD"]),
    "rushing attempts":       ("rushing",   ["CAR"]),
    "rush attempts":          ("rushing",   ["CAR"]),
    "receiving yards":        ("receiving", ["YDS"]),
    "rec yards":              ("receiving", ["YDS"]),
    "receiving touchdowns":   ("receiving", ["TD"]),
    "receptions":             ("receiving", ["REC"]),
    "targets":                ("receiving", ["TGT"]),
    "sacks":                  ("defense",   ["SACKS"]),
    "tackles":                ("defense",   ["TOT"]),
    "field goals made":       ("kicking",   ["FGM"]),
}

# MLB hitting  →  MLB Stats API stat name  (special: prefixed with "_" = computed)
_MLB_HITTING_MAP: dict[str, str] = {
    "hits":               "hits",
    "home runs":          "homeRuns",
    "hr":                 "homeRuns",
    "rbis":               "rbi",
    "rbi":                "rbi",
    "total bases":        "totalBases",
    "stolen bases":       "stolenBases",
    "walks":              "baseOnBalls",
    "runs scored":        "runs",
    "runs":               "runs",
    "singles":            "_singles",    # hits - 2B - 3B - HR
    "doubles":            "doubles",
    "triples":            "triples",
    "strikeouts":         "strikeOuts",  # batter strikeouts
    "batter strikeouts":  "strikeOuts",
    "hits+runs+rbis":     "_hrr",        # H + R + RBI
    "hits + runs + rbis": "_hrr",
    "h+r+rbi":            "_hrr",
}

# MLB pitching  →  MLB Stats API stat name
_MLB_PITCHING_MAP: dict[str, str] = {
    "strikeouts":           "strikeOuts",
    "pitcher strikeouts":   "strikeOuts",
    "ks":                   "strikeOuts",
    "hits allowed":         "hits",
    "walks allowed":        "baseOnBalls",
    "earned runs allowed":  "earnedRuns",
    "earned runs":          "earnedRuns",
    "innings pitched":      "inningsPitched",
    "outs recorded":        "_outs",     # inningsPitched * 3 (fractional → outs)
}

# Stat types that belong to the MLB pitching group
_MLB_PITCHING_STATS: frozenset[str] = frozenset({
    "strikeouts", "pitcher strikeouts", "ks",
    "hits allowed", "walks allowed",
    "earned runs allowed", "earned runs",
    "innings pitched", "outs recorded",
})

# ESPN sport routing:  Underdog sport key → (sport_slug, league_slug)
_ESPN_ROUTE: dict[str, tuple[str, str]] = {
    "NBA":    ("basketball",     "nba"),
    "WNBA":   ("basketball",     "wnba"),
    "NFL":    ("football",       "nfl"),
    "NHL":    ("hockey",         "nhl"),
    "MLB":    ("baseball",       "mlb"),   # fallback only — prefer MLB Stats API
    "NCAAB":  ("basketball",     "mens-college-basketball"),
    "NCAAF":  ("football",       "college-football"),
    "MLS":    ("soccer",         "usa.1"),
}


# ── Provider ──────────────────────────────────────────────────────────────────

class PlayerStatsProvider:
    """
    Fetches per-game player stats from free public APIs.

    Thread-safe for concurrent asyncio tasks.
    """

    def __init__(self) -> None:
        # (player_name_norm, sport, league) → athlete_id or None if not found
        self._id_cache: dict[tuple[str, str, str], Optional[int]] = {}

    # ── Public entry point ────────────────────────────────────────────────────

    async def fetch_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
    ) -> list[RawGameResult]:
        """
        Fetch recent game results for *player_name* × *stat_type* in *sport*.

        Returns an empty list on any error (never raises).
        """
        sport_upper = sport.upper()
        stat_lower  = stat_type.lower().strip()

        try:
            if sport_upper == "MLB":
                return await self._fetch_mlb(player_name, stat_lower)
            elif sport_upper in _ESPN_ROUTE:
                sport_slug, league_slug = _ESPN_ROUTE[sport_upper]
                return await self._fetch_espn(
                    player_name, sport_upper, stat_lower, sport_slug, league_slug
                )
            else:
                logger.debug("PlayerStatsProvider: unsupported sport %r", sport)
                return []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PlayerStatsProvider: unexpected error for %r %r: %s",
                player_name, stat_type, exc,
            )
            return []

    # ── MLB Stats API ─────────────────────────────────────────────────────────

    async def _fetch_mlb(
        self, player_name: str, stat_lower: str
    ) -> list[RawGameResult]:
        is_pitching = stat_lower in _MLB_PITCHING_STATS
        group = "pitching" if is_pitching else "hitting"
        field = (
            _MLB_PITCHING_MAP.get(stat_lower)
            if is_pitching
            else _MLB_HITTING_MAP.get(stat_lower)
        )
        if field is None:
            logger.debug("MLB: no mapping for stat %r", stat_lower)
            return []

        player_id = await self._mlb_player_id(player_name)
        if player_id is None:
            return []

        season = datetime.utcnow().year
        url = (
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats"
            f"?stats=gameLog&season={season}&group={group}"
        )
        data = await self._get_json(url)
        if data is None:
            return []

        splits = []
        for stat_group in (data.get("stats") or []):
            splits.extend(stat_group.get("splits") or [])

        results: list[RawGameResult] = []
        for split in splits:
            stat_obj = split.get("stat") or {}
            raw_val  = self._mlb_extract(stat_obj, field, stat_lower)
            if raw_val is None:
                continue

            game_obj  = split.get("game") or {}
            game_date = _parse_date(game_obj.get("gameDate", ""))
            if not game_date:
                continue

            opponent_obj = split.get("opponent") or {}
            opponent     = opponent_obj.get("abbreviation") or opponent_obj.get("name")

            results.append(RawGameResult(
                player_name  = player_name,
                sport        = "MLB",
                stat_type    = stat_lower,
                game_date    = game_date,
                actual_value = raw_val,
                opponent     = opponent,
                source       = "mlb_stats_api",
            ))

        logger.debug(
            "MLB: %s / %s → %d game results (group=%s)",
            player_name, stat_lower, len(results), group,
        )
        return results

    @staticmethod
    def _mlb_extract(
        stat_obj: dict, field: str, stat_lower: str
    ) -> Optional[float]:
        """Extract a numeric stat value from an MLB Stats API stat dict."""
        if field == "_singles":
            h  = float(stat_obj.get("hits",       0) or 0)
            d  = float(stat_obj.get("doubles",     0) or 0)
            t  = float(stat_obj.get("triples",     0) or 0)
            hr = float(stat_obj.get("homeRuns",    0) or 0)
            return max(0.0, h - d - t - hr)

        if field == "_hrr":
            h = float(stat_obj.get("hits", 0) or 0)
            r = float(stat_obj.get("runs", 0) or 0)
            b = float(stat_obj.get("rbi",  0) or 0)
            return h + r + b

        if field == "_outs":
            ip = stat_obj.get("inningsPitched")
            if ip is None:
                return None
            try:
                ip_f = float(ip)
            except (TypeError, ValueError):
                return None
            # 6.2 innings = 6 full + 2 outs = 20 outs
            full   = int(ip_f)
            frac   = round((ip_f - full) * 10)   # MLB stores 0.1 = 1 out, 0.2 = 2 outs
            return float(full * 3 + frac)

        if field == "inningsPitched":
            ip = stat_obj.get("inningsPitched")
            if ip is None:
                return None
            try:
                return float(ip)
            except (TypeError, ValueError):
                return None

        raw = stat_obj.get(field)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def _mlb_player_id(self, player_name: str) -> Optional[int]:
        cache_key = (_normalize(player_name), "mlb", "mlb")
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        url  = f"https://statsapi.mlb.com/api/v1/people/search?names={quote_plus(player_name)}&sportIds=1"
        data = await self._get_json(url)
        pid: Optional[int] = None

        if data:
            for person in (data.get("people") or []):
                if _names_match(person.get("fullName", ""), player_name):
                    pid = person.get("id")
                    break

        if pid is None:
            logger.debug("MLB: player ID not found for %r", player_name)
        self._id_cache[cache_key] = pid
        return pid

    # ── ESPN unofficial game log API ──────────────────────────────────────────

    async def _fetch_espn(
        self,
        player_name: str,
        sport: str,
        stat_lower: str,
        sport_slug: str,
        league_slug: str,
    ) -> list[RawGameResult]:
        athlete_id = await self._espn_athlete_id(player_name, sport, sport_slug, league_slug)
        if athlete_id is None:
            return []

        url  = (
            f"https://site.api.espn.com/apis/site/v2/sports"
            f"/{sport_slug}/{league_slug}/athletes/{athlete_id}/gamelog"
        )
        data = await self._get_json(url)
        if data is None:
            return []

        return self._parse_espn_gamelog(data, player_name, sport, stat_lower)

    def _parse_espn_gamelog(
        self,
        data: dict,
        player_name: str,
        sport: str,
        stat_lower: str,
    ) -> list[RawGameResult]:
        """
        Parse the ESPN athlete gamelog response.

        Handles two structures:
          • Flat: categories has a single labels list, events[id].stats is a flat list.
          • Multi-category (NFL): events[id] has keys per category type.
        """
        categories  = data.get("categories") or []
        events_obj  = data.get("events") or {}
        season_types = data.get("seasonTypes") or []

        # Regular-season event IDs
        regular_ids: set[str] = set()
        for st in season_types:
            if isinstance(st, dict) and st.get("id") in (2, "2"):
                for eid in (st.get("eventIds") or []):
                    regular_ids.add(str(eid))
        # If no regular-season info, use all events
        if not regular_ids:
            regular_ids = set(str(k) for k in events_obj.keys())

        results: list[RawGameResult] = []

        # Determine parsing mode
        is_nfl = sport.upper() == "NFL"

        if is_nfl:
            labels_by_cat = {
                cat["type"]: cat["labels"]
                for cat in categories
                if isinstance(cat, dict) and "type" in cat and "labels" in cat
            }
            stat_info = _NFL_STAT_MAP.get(stat_lower)
            if stat_info is None:
                logger.debug("ESPN NFL: no mapping for stat %r", stat_lower)
                return []
            cat_key, target_labels = stat_info

            for event_id, event in events_obj.items():
                if str(event_id) not in regular_ids:
                    continue
                cat_stats = event.get(cat_key) or []
                cat_labels = labels_by_cat.get(cat_key) or []
                val = _sum_espn_labels(cat_labels, cat_stats, target_labels, cat_key)
                if val is None:
                    continue
                gd = _event_game_date(event)
                if not gd:
                    continue
                opp = _event_opponent(event)
                results.append(RawGameResult(
                    player_name  = player_name,
                    sport        = sport,
                    stat_type    = stat_lower,
                    game_date    = gd,
                    actual_value = val,
                    opponent     = opp,
                    source       = "espn_gamelog",
                ))
        else:
            # Flat mode: one category block covers all stats
            if not categories:
                return []
            # Try to find the right stat map
            stat_map = _get_stat_map(sport)
            target_labels = stat_map.get(stat_lower) if stat_map else None
            if target_labels is None:
                logger.debug("ESPN %s: no mapping for stat %r", sport, stat_lower)
                return []

            # Use first category's labels (flat structure)
            flat_labels = categories[0].get("labels") or [] if categories else []

            for event_id, event in events_obj.items():
                if str(event_id) not in regular_ids:
                    continue
                flat_stats = event.get("stats") or []
                val = _sum_espn_labels(flat_labels, flat_stats, target_labels, stat_lower)
                if val is None:
                    continue
                gd = _event_game_date(event)
                if not gd:
                    continue
                opp = _event_opponent(event)
                results.append(RawGameResult(
                    player_name  = player_name,
                    sport        = sport,
                    stat_type    = stat_lower,
                    game_date    = gd,
                    actual_value = val,
                    opponent     = opp,
                    source       = "espn_gamelog",
                ))

        logger.debug(
            "ESPN %s: %s / %s → %d game results",
            sport, player_name, stat_lower, len(results),
        )
        return results

    async def _espn_athlete_id(
        self,
        player_name: str,
        sport: str,
        sport_slug: str,
        league_slug: str,
    ) -> Optional[int]:
        cache_key = (_normalize(player_name), sport_slug, league_slug)
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        url = (
            f"https://site.api.espn.com/apis/site/v2/sports"
            f"/{sport_slug}/{league_slug}/athletes"
            f"?searchTerm={quote_plus(player_name)}&limit=5&active=true"
        )
        data = await self._get_json(url)
        aid: Optional[int] = None

        if data:
            items = (data.get("items") or data.get("athletes") or [])
            for item in items:
                full_name = (
                    item.get("displayName")
                    or item.get("fullName")
                    or item.get("name", "")
                )
                if _names_match(full_name, player_name):
                    raw_id = item.get("id")
                    if raw_id is not None:
                        try:
                            aid = int(raw_id)
                        except (TypeError, ValueError):
                            pass
                    break

        if aid is None:
            logger.debug(
                "ESPN: athlete ID not found for %r (%s/%s)",
                player_name, sport_slug, league_slug,
            )
        self._id_cache[cache_key] = aid
        return aid

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get_json(self, url: str) -> Optional[dict]:
        """GET *url* with timeout; return parsed JSON or None on failure."""
        try:
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)"},
                ) as resp:
                    if resp.status != 200:
                        logger.debug("PlayerStatsProvider: HTTP %d for %s", resp.status, url)
                        return None
                    return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.debug("PlayerStatsProvider: timeout for %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.debug("PlayerStatsProvider: client error for %s: %s", url, exc)
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_stat_map(sport: str) -> Optional[dict]:
    """Return the stat map for a flat (non-NFL) sport."""
    s = sport.upper()
    if s in ("NBA", "WNBA", "NCAAB"):
        return _NBA_STAT_MAP
    if s == "NHL":
        return _NHL_STAT_MAP
    return None


def _sum_espn_labels(
    labels: list[str],
    stats: list,
    target_labels: list[str],
    context: str = "",
) -> Optional[float]:
    """
    Sum values from *stats* at the positions where *labels* match *target_labels*.

    Handles "C/ATT" style fractions (takes numerator for "C", denominator for "ATT").
    Returns None if any target label is not found or the value is non-numeric.
    """
    total = 0.0
    for tl in target_labels:
        idx = None
        # Direct match
        if tl in labels:
            idx = labels.index(tl)
        else:
            # Check slash-format (e.g. "C/ATT" contains "C")
            for i, lbl in enumerate(labels):
                if "/" in lbl:
                    parts = lbl.split("/")
                    if tl == parts[0]:
                        idx = i
                        break
                    if tl == parts[1]:
                        idx = i
                        break

        if idx is None or idx >= len(stats):
            logger.debug(
                "_sum_espn_labels: label %r not found in %r (context=%s)",
                tl, labels, context,
            )
            return None

        raw = stats[idx]
        if raw is None or raw == "--" or raw == "":
            return None

        # Handle "C/ATT" value (e.g. "22/32")
        raw_str = str(raw)
        if "/" in raw_str:
            # Determine whether we want num or denom
            corresponding_label = labels[idx] if idx < len(labels) else ""
            if "/" in corresponding_label:
                parts = corresponding_label.split("/")
                val_parts = raw_str.split("/")
                if tl == parts[0] and len(val_parts) >= 1:
                    try:
                        total += float(val_parts[0])
                    except (ValueError, TypeError):
                        return None
                    continue
                if tl == parts[1] and len(val_parts) >= 2:
                    try:
                        total += float(val_parts[1])
                    except (ValueError, TypeError):
                        return None
                    continue
            return None

        try:
            total += float(raw_str)
        except (ValueError, TypeError):
            return None

    return total


def _event_game_date(event: dict) -> Optional[str]:
    """Extract a YYYY-MM-DD date from an ESPN event dict."""
    game_obj = event.get("game") or {}
    raw = game_obj.get("date") or event.get("date")
    return _parse_date(raw)


def _event_opponent(event: dict) -> Optional[str]:
    """Extract opponent abbreviation or display name from an ESPN event dict."""
    opp = event.get("opponent") or {}
    return opp.get("abbreviation") or opp.get("displayName")


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Parse an ISO datetime string to YYYY-MM-DD, or return None."""
    if not raw:
        return None
    try:
        # Accept "2026-07-28T18:10:00Z" or "2026-07-28" etc.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except (ValueError, AttributeError):
        return None


def _normalize(name: str) -> str:
    return name.lower().strip()


def _names_match(api_name: str, query_name: str, threshold: float = 0.82) -> bool:
    """
    Return True when api_name and query_name refer to the same player.

    Uses a SequenceMatcher ratio for fuzzy matching.
    """
    from difflib import SequenceMatcher
    a = _normalize(api_name)
    b = _normalize(query_name)
    if a == b:
        return True
    # One-way substring
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold
