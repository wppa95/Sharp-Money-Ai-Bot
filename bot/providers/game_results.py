"""
providers/game_results.py — Game results framework and pick-grading scaffold.

Provides the data models and provider interface needed to grade PrizePicks
picks against final game scores.  The Odds API scores endpoint
(/v4/sports/{sport_key}/scores) returns TEAM-level scores only — per-player
stats are not available through this endpoint.  As a result, grade_pp_pick()
will return PropGradeResult.result == "NO_DATA" for player-prop picks until
a per-player stats provider is added (future work).

This module is intentionally NOT wired to any background job.  It is fully
tested and ready to be connected once a paid Odds API plan is purchased.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from .base import FailureType, ProviderHealth, ProviderStatus
from .health_monitor import get_health_monitor

logger = logging.getLogger(__name__)

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_PROVIDER_NAME = "OddsAPIResults"

_PUSH_TOLERANCE = 0.01  # treat |actual - line| < 0.01 as a push


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class GameResult:
    """
    Final (or in-progress) score for one game, as returned by the Odds API
    /v4/sports/{sport_key}/scores endpoint.

    Notes
    -----
    The scores endpoint returns TEAM scores only.  ``away_score`` and
    ``home_score`` are the final point totals for each side.  Per-player
    stats (e.g. "LeBron James scored 28 points") are NOT available here.
    """

    sport:        str
    event:        str            # canonical "Away @ Home" string
    away_team:    str
    home_team:    str
    away_score:   Optional[int]  # None when game is not yet completed
    home_score:   Optional[int]
    status:       str            # "scheduled" | "in_progress" | "final"
    completed_at: Optional[datetime]
    source:       str = "odds_api"


@dataclass
class PropGradeResult:
    """
    Output of grade_pp_pick() — maps a stored edge record against completed
    game results to determine WIN / LOSS / PUSH / PENDING / NO_DATA.

    ``actual_value`` will be None for player-prop picks graded via the Odds
    API scores endpoint (which only has team totals), yielding result="NO_DATA".
    It is typed Optional[float] so that a future per-player stats provider can
    supply the value and trigger the WIN/LOSS/PUSH path.
    """

    player_name:  str
    stat_type:    str
    line_value:   float
    best_side:    str             # "OVER" | "UNDER"
    actual_value: Optional[float] # None → result will be NO_DATA
    result:       str             # "WIN" | "LOSS" | "PUSH" | "PENDING" | "NO_DATA"
    source:       str


# ── Provider ABC ──────────────────────────────────────────────────────────────

class GameResultsProvider(abc.ABC):
    """
    Abstract base for any source that can return completed game scores.

    Implementors: OddsApiResultsProvider (below), and future per-player-stats
    providers.
    """

    name: str = "GameResultsProvider"

    @abc.abstractmethod
    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int = 1,
    ) -> list[GameResult]:
        """
        Return completed (and in-progress) game scores for a sport.

        Parameters
        ----------
        sport_key:
            Odds API sport key, e.g. ``"basketball_nba"``.
        days_from:
            How many days back to include completed games.
        """
        ...

    @abc.abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Return the current health of this provider."""
        ...


# ── Odds API implementation ───────────────────────────────────────────────────

class OddsApiResultsProvider(GameResultsProvider):
    """
    Fetches game scores from GET /v4/sports/{sport_key}/scores.

    This provider is NOT wired to any background job.  It is instantiated in
    main.py post_init and registered with the health monitor, but fetch_scores()
    is only called when explicitly invoked (e.g. a future /grade command or
    auto-grading job).

    IMPORTANT: The /scores endpoint returns team-level scores only.
    grade_pp_pick() will return NO_DATA for all player-prop picks graded via
    this provider until a per-player stats source is available.
    """

    name = "OddsAPIResults"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int = 1,
    ) -> list[GameResult]:
        """
        Call /v4/sports/{sport_key}/scores and parse the response.

        Records success/failure in the health monitor.
        Returns [] (not raises) on any failure so the caller can continue.
        """
        if not self._api_key:
            logger.debug("OddsApiResultsProvider: no API key — skipping scores fetch")
            return []

        url = f"{_ODDS_API_BASE}/sports/{sport_key}/scores"
        params = {
            "apiKey":   self._api_key,
            "daysFrom": days_from,
        }
        mon = get_health_monitor()

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 401:
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = {}
                        error_code = (body or {}).get("error_code", "")
                        message    = (body or {}).get("message", "401")
                        ftype = (
                            FailureType.QUOTA
                            if error_code == "OUT_OF_USAGE_CREDITS"
                            else FailureType.HTTP_ERROR
                        )
                        if mon:
                            mon.record_failure(_PROVIDER_NAME, f"401 {error_code or message}", ftype)
                        logger.warning(
                            "OddsApiResultsProvider: 401 for %s — %s", sport_key, message
                        )
                        return []

                    if resp.status != 200:
                        err = f"HTTP {resp.status}"
                        if mon:
                            mon.record_failure(_PROVIDER_NAME, err, FailureType.HTTP_ERROR)
                        logger.warning(
                            "OddsApiResultsProvider: HTTP %d for %s", resp.status, sport_key
                        )
                        return []

                    data: list[dict] = await resp.json()
                    if mon:
                        mon.record_success(_PROVIDER_NAME)

                    results = self._parse(data, sport_key)
                    logger.info(
                        "OddsApiResultsProvider: %d results for %s (%d completed)",
                        len(results), sport_key,
                        sum(1 for r in results if r.status == "final"),
                    )
                    return results

        except aiohttp.ClientError as exc:
            err = str(exc)
            if mon:
                mon.record_failure(_PROVIDER_NAME, err, FailureType.HTTP_ERROR)
            logger.warning("OddsApiResultsProvider fetch error (%s): %s", sport_key, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if mon:
                mon.record_failure(_PROVIDER_NAME, err, FailureType.UNKNOWN)
            logger.warning("OddsApiResultsProvider unexpected error (%s): %s", sport_key, exc)
            return []

    async def health_check(self) -> ProviderHealth:
        mon = get_health_monitor()
        if mon:
            return mon.get_health(_PROVIDER_NAME)
        # Fallback if monitor not initialised
        return ProviderHealth(
            name                 = _PROVIDER_NAME,
            status               = ProviderStatus.DISABLED,
            last_success         = None,
            last_failure         = None,
            consecutive_failures = 0,
            failure_type         = None,
            quota_remaining      = None,
            quota_used           = None,
        )

    def _parse(self, data: list[dict], sport_key: str) -> list[GameResult]:
        """Parse /v4/sports/{sport}/scores JSON into GameResult objects."""
        results: list[GameResult] = []

        for item in data:
            away_team = item.get("away_team", "Away")
            home_team = item.get("home_team", "Home")
            event     = f"{away_team} @ {home_team}"

            completed = item.get("completed", False)
            scores    = item.get("scores") or []

            away_score: Optional[int] = None
            home_score: Optional[int] = None

            if completed and scores:
                for entry in scores:
                    try:
                        score_val = int(entry["score"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    team_name = entry.get("name", "")
                    if team_name == away_team:
                        away_score = score_val
                    elif team_name == home_team:
                        home_score = score_val

            # Parse completed_at from last_update field
            completed_at: Optional[datetime] = None
            raw_lu = item.get("last_update")
            if raw_lu and completed:
                try:
                    completed_at = datetime.fromisoformat(
                        raw_lu.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass

            status = "final" if completed else (
                "in_progress" if scores else "scheduled"
            )

            results.append(GameResult(
                sport        = sport_key,
                event        = event,
                away_team    = away_team,
                home_team    = home_team,
                away_score   = away_score,
                home_score   = home_score,
                status       = status,
                completed_at = completed_at,
                source       = "odds_api",
            ))

        return results


# ── Pick-grading utility ──────────────────────────────────────────────────────

def grade_pp_pick(
    player_name:  str,
    stat_type:    str,
    line_value:   float,
    best_side:    str,
    game_results: list[GameResult],
    actual_value: Optional[float] = None,
) -> PropGradeResult:
    """
    Grade a PrizePicks prop pick against final game results.

    Parameters
    ----------
    player_name:
        Player's display name (e.g. "LeBron James").
    stat_type:
        Stat being picked (e.g. "Points").
    line_value:
        PrizePicks line (e.g. 25.5).
    best_side:
        "OVER" or "UNDER" — the side with positive edge.
    game_results:
        List of GameResult objects from OddsApiResultsProvider.fetch_scores().
    actual_value:
        Override the actual stat value.  Pass this when a per-player stats
        provider has already resolved the value.  When None (the default), the
        function uses game_results; if no per-player stats are available from
        team scores, it returns result="NO_DATA".

    Returns
    -------
    PropGradeResult
        result is one of: "WIN", "LOSS", "PUSH", "PENDING", "NO_DATA".

    Notes
    -----
    The Odds API /scores endpoint returns team-level scores only.
    grade_pp_pick() cannot resolve per-player stat values from team totals;
    it will return NO_DATA for all player props until a per-player stats
    provider supplies the actual_value argument.
    """
    # If the caller supplies an explicit actual_value, skip game_results lookup
    if actual_value is None:
        # Attempt to find the player's game in the results list.
        # Even if found, we can't extract per-player stats from team scores.
        # Return NO_DATA to signal that a per-player stats source is needed.
        _find_matching_game(player_name, game_results)   # logged but not used
        return PropGradeResult(
            player_name  = player_name,
            stat_type    = stat_type,
            line_value   = line_value,
            best_side    = best_side,
            actual_value = None,
            result       = "NO_DATA",
            source       = "odds_api_scores_team_only",
        )

    # Determine WIN / LOSS / PUSH from actual_value
    side = best_side.upper()
    if abs(actual_value - line_value) < _PUSH_TOLERANCE:
        verdict = "PUSH"
    elif side == "OVER" and actual_value > line_value:
        verdict = "WIN"
    elif side == "UNDER" and actual_value < line_value:
        verdict = "WIN"
    else:
        verdict = "LOSS"

    return PropGradeResult(
        player_name  = player_name,
        stat_type    = stat_type,
        line_value   = line_value,
        best_side    = best_side,
        actual_value = actual_value,
        result       = verdict,
        source       = "provided",
    )


def _find_matching_game(
    player_name: str,
    game_results: list[GameResult],
) -> Optional[GameResult]:
    """
    Fuzzy-match a player name against the teams in game_results.

    Since the /scores endpoint has no player-team mapping, this is a
    best-effort lookup that currently always returns None (no match).
    Logged at DEBUG level so operators can see the lookup was attempted.
    """
    if not game_results:
        logger.debug(
            "grade_pp_pick: no game results provided for player %r", player_name
        )
        return None

    logger.debug(
        "grade_pp_pick: %d game results available for sport %r; "
        "cannot resolve per-player stats for %r from team scores",
        len(game_results), game_results[0].sport if game_results else "?", player_name,
    )
    return None
