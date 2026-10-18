"""Provider-neutral team-score models and player-pick grading helpers."""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .base import ProviderHealth, ProviderStatus
from .odds_api import OddsApiClient, OddsApiDeferred, OddsApiError

logger = logging.getLogger(__name__)
_PUSH_TOLERANCE = 0.01


@dataclass
class GameResult:
    """Final or in-progress team-level result from a generic provider."""

    sport: str
    event: str
    away_team: str
    home_team: str
    away_score: Optional[int]
    home_score: Optional[int]
    status: str
    completed_at: Optional[datetime]
    source: str = "provider"


@dataclass
class PropGradeResult:
    """Result of comparing a player-prop line with an explicit actual value."""

    player_name: str
    stat_type: str
    line_value: float
    best_side: str
    actual_value: Optional[float]
    result: str
    source: str


class GameResultsProvider(abc.ABC):
    """Interface for providers that expose completed game results."""

    name = "GameResultsProvider"

    @abc.abstractmethod
    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int = 1,
    ) -> list[GameResult]:
        """Return completed or in-progress scores for a sport."""
        ...

    @abc.abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Return the current provider health."""
        ...


class OddsApiResultsProvider(GameResultsProvider):
    """Team-score adapter backed by the shared, quota-limited v4 client.

    Scores remain team-level context only. This adapter never turns them into
    player results or a player-prop settlement.
    """

    name = "OddsAPI"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: OddsApiClient | None = None,
    ) -> None:
        self.client = client or OddsApiClient(api_key=api_key)

    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int | None = 1,
    ) -> list[GameResult]:
        try:
            scores = await self.client.fetch_scores(sport_key, days_from=days_from)
        except (OddsApiDeferred, OddsApiError):
            return []

        results: list[GameResult] = []
        for item in scores:
            by_team = {team: score for team, score in item.scores}
            away = by_team.get(item.away_team)
            home = by_team.get(item.home_team)
            results.append(
                GameResult(
                    sport=item.sport_key,
                    event=item.event_id,
                    away_team=item.away_team,
                    home_team=item.home_team,
                    away_score=int(away) if away is not None and away.is_integer() else None,
                    home_score=int(home) if home is not None and home.is_integer() else None,
                    status="FINAL" if item.completed else "IN_PROGRESS",
                    completed_at=item.last_update if item.completed else None,
                    source="TheOddsAPI:team_scores",
                )
            )
        return results

    async def health_check(self) -> ProviderHealth:
        return await self.client.health_check()


def grade_pp_pick(
    player_name: str,
    stat_type: str,
    line_value: float,
    best_side: str,
    game_results: list[GameResult],
    actual_value: Optional[float] = None,
) -> PropGradeResult:
    """Grade only when an explicit player result is supplied.

    Team scores alone cannot settle player props, so unavailable player data
    remains ``NO_DATA`` rather than being inferred from a game score.
    """
    if actual_value is None:
        _find_matching_game(player_name, game_results)
        return PropGradeResult(
            player_name=player_name,
            stat_type=stat_type,
            line_value=line_value,
            best_side=best_side,
            actual_value=None,
            result="NO_DATA",
            source="player_result_unavailable",
        )

    side = best_side.upper()
    if abs(actual_value - line_value) < _PUSH_TOLERANCE:
        verdict = "PUSH"
    elif (side == "OVER" and actual_value > line_value) or (
        side == "UNDER" and actual_value < line_value
    ):
        verdict = "WIN"
    else:
        verdict = "LOSS"

    return PropGradeResult(
        player_name=player_name,
        stat_type=stat_type,
        line_value=line_value,
        best_side=best_side,
        actual_value=actual_value,
        result=verdict,
        source="provided",
    )


def _find_matching_game(
    player_name: str,
    game_results: list[GameResult],
) -> Optional[GameResult]:
    """Team-level game scores cannot identify a player's result."""
    if not game_results:
        logger.debug(
            "grade_pp_pick: no game results available for player %r",
            player_name,
        )
    else:
        logger.debug(
            "grade_pp_pick: team scores cannot resolve player %r",
            player_name,
        )
    return None