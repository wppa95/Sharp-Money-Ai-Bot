"""
providers/soccer_stats.py — Soccer per-game player stat provider.

Data source: football-data.org v4 API
  Free token available at: https://www.football-data.org/client/register
  Set env var: FOOTBALL_DATA_API_KEY=<your_token>
  Returns [] gracefully when the key is absent (same pattern as PandaScore/CS2).

Verified accessible from this environment: api.football-data.org responds
to authenticated requests and returns match data including goal scorers,
assist providers, and booking (yellow/red card) data.

Supported competitions (free tier):
  PL  — Premier League
  PD  — La Liga
  BL1 — 1. Bundesliga
  SA  — Serie A
  FL1 — Ligue 1

Per-game stat generation
────────────────────────
For each player, the provider:
  1. Identifies the competition by finding the player in goals or bookings data.
  2. Determines the player's team from that event data.
  3. Returns a RawGameResult for EVERY finished match the team played — including
     games where the stat is 0 — so the hit-rate engine sees an unbiased sample.

Stats available per match (derived from match events):
  goals            ← count from goals[].scorer — 0 when player not a scorer
  assists          ← count from goals[].assist — 0 when player not an assister
  goals + assists  ← sum of above — 0 when player appears in neither
  yellow cards     ← count from bookings[] YELLOW_CARD — 0 when not booked
  red cards        ← count from bookings[] RED_CARD / STRAIGHT_RED — 0 when not

Stats NOT available (no per-player shot/keeper data in free tier):
  shots on target, saves, goals allowed, clean sheets, key passes

Limitation
──────────
Team membership is inferred from event data. A player who had 0 goals, 0
assists, and 0 bookings in the entire season dataset cannot be team-matched
and returns []. This is deliberate: the hit-rate engine requires real data;
returning [] triggers a PASS decision rather than fabricating a history.

Architecture mirrors EsportsStatsProvider:
  SoccerStatsProvider.fetch_results(player_name, sport, stat_type)
      → list[RawGameResult]

Caching
───────
Competition match lists are fetched once per process per (competition, season).
Player → (competition_code, team_name) is cached after first lookup.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

import aiohttp

from providers.player_stats import RawGameResult, _names_match, _normalize

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT  = aiohttp.ClientTimeout(total=12)
_BASE_URL         = "https://api.football-data.org/v4"

# football-data.org competition codes (free tier).
# Search order follows the existing _SOCCER_LEAGUE_PRIORITY in player_stats.py.
_COMPETITION_CODES: list[str] = ["PL", "PD", "BL1", "SA", "FL1"]

# Stat names this provider can answer
_SUPPORTED_STATS = frozenset({
    "goals",
    "assists",
    "goals + assists",
    "g+a",
    "yellow cards",
    "red cards",
})


def _current_season() -> int:
    """
    Return the starting year of the current (or most recent) soccer season.

    European seasons run ~Aug → May, so:
      month ≥ 8  → season start = current year   (e.g. Aug 2025 → 2025 for 25-26)
      month < 8  → season start = current year-1 (e.g. Apr 2026 → 2025 for 25-26)
    """
    now = datetime.utcnow()
    return now.year if now.month >= 8 else now.year - 1


class SoccerStatsProvider:
    """
    Fetch per-game soccer stats for players using football-data.org.

    Requires FOOTBALL_DATA_API_KEY env var.  Register free at:
    https://www.football-data.org/client/register

    Without the key every call returns [] immediately — no network hit.

    For each player request, the provider:
    1. Scans competition match data to discover which team the player belongs to
       (by finding them in goal scorers, assist providers, or booking data).
    2. Returns a RawGameResult for EVERY finished match that team played,
       including 0-stat games, so the hit-rate engine sees an unbiased sample.
    """

    def __init__(self) -> None:
        self._api_key: Optional[str] = os.environ.get("FOOTBALL_DATA_API_KEY")
        if not self._api_key:
            logger.info(
                "Soccer: FOOTBALL_DATA_API_KEY not set — "
                "soccer historical data disabled. "
                "Register free at https://www.football-data.org/client/register"
            )

        # (competition_code, season_year) → list of match dicts
        self._match_cache: dict[tuple[str, int], list[dict]] = {}

        # player_norm → list of (competition_code, team_name) found across all leagues.
        # A player who transferred mid-season may appear in MORE than one league;
        # we collect all occurrences and merge results so history is preserved.
        # Negative results are NOT cached: a player invisible in the current event
        # data may gain events later as the season progresses.
        self._player_info: dict[str, list[tuple[str, str]]] = {}

    # ── Public entry point ────────────────────────────────────────────────────

    async def fetch_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
    ) -> list[RawGameResult]:
        """Return per-game soccer stats. Returns [] on any error or missing key."""
        if not self._api_key:
            logger.debug("Soccer: no API key — skipping %r", player_name)
            return []

        stat_lower = stat_type.lower().strip()
        if stat_lower not in _SUPPORTED_STATS:
            logger.debug("Soccer: unsupported stat %r for %r", stat_type, player_name)
            return []

        try:
            season      = _current_season()
            player_norm = _normalize(player_name)
            placements  = await self._find_player_info(player_norm, season)
            if not placements:
                logger.debug(
                    "Soccer: %r not found in any competition — "
                    "no goal/assist/booking events available",
                    player_name,
                )
                return []

            # Merge results from every league the player has appeared in.
            # A player who transferred mid-season (e.g. EPL → Bundesliga) will
            # have entries in multiple competitions; merging preserves full history.
            all_results: list[RawGameResult] = []
            seen_dates: set[str] = set()
            for comp_code, team_name in placements:
                matches = await self._get_competition_matches(comp_code, season)
                leg_results = self._extract_team_results(
                    player_name, player_norm, stat_lower, team_name, matches
                )
                for r in leg_results:
                    if r.game_date not in seen_dates:
                        seen_dates.add(r.game_date)
                        all_results.append(r)
            return sorted(all_results, key=lambda r: r.game_date)

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Soccer: unexpected error for %r %r: %s", player_name, stat_type, exc
            )
            return []

    # ── Player / team discovery ───────────────────────────────────────────────

    async def _find_player_info(
        self, player_norm: str, season: int
    ) -> list[tuple[str, str]]:
        """
        Return all (competition_code, team_name) pairs for the player.

        Searches ALL competitions in _COMPETITION_CODES so that a player who
        transferred mid-season (e.g. EPL → Bundesliga) is found in both
        leagues and their full history is preserved.

        Positive results are cached.  Negative results (player has no events in
        any league yet) are NOT cached so subsequent calls can pick up new data
        as the season progresses.
        """
        if player_norm in self._player_info:
            return self._player_info[player_norm]

        found: list[tuple[str, str]] = []
        for code in _COMPETITION_CODES:
            matches   = await self._get_competition_matches(code, season)
            team_name = self._find_player_team(player_norm, matches)
            if team_name:
                found.append((code, team_name))
                logger.debug(
                    "Soccer: %r → team %r in competition %s (season %d)",
                    player_norm, team_name, code, season,
                )

        if found:
            self._player_info[player_norm] = found
        # Do NOT cache empty results — player may gain events later.
        return found

    @staticmethod
    def _find_player_team(player_norm: str, matches: list[dict]) -> Optional[str]:
        """
        Identify the player's team by scanning goals and bookings across all
        matches.  Returns the team name exactly as football-data.org reports it
        (so it can be used directly for homeTeam/awayTeam string comparisons).

        For goal events the assister is on the SAME team as the scorer.
        For booking events the player's team is provided directly.
        """
        for match in matches:
            for goal in (match.get("goals") or []):
                team_name = (goal.get("team") or {}).get("name", "")
                if not team_name:
                    continue
                scorer = _normalize((goal.get("scorer") or {}).get("name") or "")
                assist = _normalize((goal.get("assist") or {}).get("name") or "")
                if _names_match(player_norm, scorer, threshold=0.85):
                    return team_name
                if assist and _names_match(player_norm, assist, threshold=0.85):
                    return team_name
            for booking in (match.get("bookings") or []):
                team_name = (booking.get("team") or {}).get("name", "")
                if not team_name:
                    continue
                bplayer = _normalize((booking.get("player") or {}).get("name") or "")
                if _names_match(player_norm, bplayer, threshold=0.85):
                    return team_name
        return None

    # ── Complete-history stat extraction ──────────────────────────────────────

    def _extract_team_results(
        self,
        player_name: str,
        player_norm: str,
        stat_lower: str,
        team_name: str,
        matches: list[dict],
    ) -> list[RawGameResult]:
        """
        Return a RawGameResult for every finished match the player's team played,
        including matches where the player's stat value is 0.

        This produces an unbiased sample for hit-rate computation.  (Games the
        player missed due to injury will generate a false 0, which is a slight
        conservative bias — acceptable given it cannot be detected without
        roster/lineup data in the free API tier.)
        """
        results: list[RawGameResult] = []

        for match in matches:
            if match.get("status") != "FINISHED":
                continue

            home = (match.get("homeTeam") or {}).get("name", "")
            away = (match.get("awayTeam") or {}).get("name", "")

            # Only include matches where the player's team participated
            if home != team_name and away != team_name:
                continue

            game_date = (match.get("utcDate") or "")[:10]  # "YYYY-MM-DD"
            if not game_date:
                continue

            val      = self._stat_in_match(player_norm, stat_lower, match)
            opponent = away if home == team_name else home

            results.append(RawGameResult(
                player_name  = player_name,
                sport        = "SOCCER",
                stat_type    = stat_lower,
                game_date    = game_date,
                actual_value = val,
                opponent     = opponent,
                source       = "football_data_org",
            ))

        logger.debug(
            "Soccer: %r / %r / team=%r → %d results",
            player_name, stat_lower, team_name, len(results),
        )
        return results

    @staticmethod
    def _stat_in_match(player_norm: str, stat_lower: str, match: dict) -> float:
        """
        Compute the numeric stat value for a player in a single match.

        Returns 0.0 when the player's team played but the player did not appear
        in the relevant events (no goal/assist, no booking).  This is the correct
        behavior: zero events in a played game = stat value 0.
        """
        goals_scored  = 0
        goals_assisted = 0

        for goal in (match.get("goals") or []):
            scorer = _normalize((goal.get("scorer") or {}).get("name") or "")
            assist = _normalize((goal.get("assist") or {}).get("name") or "")
            if _names_match(player_norm, scorer, threshold=0.85):
                goals_scored  += 1
            if assist and _names_match(player_norm, assist, threshold=0.85):
                goals_assisted += 1

        yellow = 0
        red    = 0
        for booking in (match.get("bookings") or []):
            bplayer = _normalize((booking.get("player") or {}).get("name") or "")
            if _names_match(player_norm, bplayer, threshold=0.85):
                card = booking.get("card", "")
                if card == "YELLOW_CARD":
                    yellow += 1
                elif card in ("RED_CARD", "STRAIGHT_RED"):
                    red += 1

        if stat_lower == "goals":
            return float(goals_scored)
        if stat_lower == "assists":
            return float(goals_assisted)
        if stat_lower in ("goals + assists", "g+a"):
            return float(goals_scored + goals_assisted)
        if stat_lower == "yellow cards":
            return float(yellow)
        if stat_lower == "red cards":
            return float(red)

        return 0.0

    # ── Match cache ───────────────────────────────────────────────────────────

    async def _get_competition_matches(
        self, competition_code: str, season: int
    ) -> list[dict]:
        """
        Return all FINISHED matches for a competition+season, from cache or API.
        """
        key = (competition_code, season)
        if key in self._match_cache:
            return self._match_cache[key]

        url    = (
            f"{_BASE_URL}/competitions/{competition_code}/matches"
            f"?season={season}&status=FINISHED"
        )
        data   = await self._get_json(url)
        result = (data.get("matches") or []) if data else []
        self._match_cache[key] = result
        logger.debug(
            "Soccer: cached %d matches for %s/%d", len(result), competition_code, season
        )
        return result

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _get_json(self, url: str) -> Optional[dict]:
        """GET *url* with auth header; return parsed JSON or None on failure."""
        if not self._api_key:
            return None
        try:
            headers = {
                "X-Auth-Token": self._api_key,
                "User-Agent":   "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)",
            }
            async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 403:
                        logger.warning(
                            "Soccer: HTTP 403 — FOOTBALL_DATA_API_KEY may be invalid "
                            "or this competition requires a paid tier. URL: %s", url
                        )
                        return None
                    if resp.status != 200:
                        logger.debug("Soccer: HTTP %d for %s", resp.status, url)
                        return None
                    return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.debug("Soccer: timeout for %s", url)
            return None
        except aiohttp.ClientError as exc:
            logger.debug("Soccer: HTTP error for %s: %s", url, exc)
            return None
