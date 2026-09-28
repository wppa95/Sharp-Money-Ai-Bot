"""Provider-neutral game-result models and pick-grading helpers.

The former Odds API score adapter is retained only as an import-compatible,
no-network stub. Live settlement remains owned by the existing PropLine and
player-result workflows.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .base import ProviderHealth, ProviderStatus

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
    """Retired compatibility stub; it never makes a network request."""

    name = "OddsAPIResults"

    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int = 1,
    ) -> list[GameResult]:
        return []

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            status=ProviderStatus.DISABLED,
            last_success=None,
            last_failure=None,
            consecutive_failures=0,
            failure_type=None,
            quota_remaining=None,
            quota_used=None,
            error_msg="retired",
        )


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