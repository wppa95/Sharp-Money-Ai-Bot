"""
engine/graded_history.py — Prop-graded evidence helpers.

PROP-GRADED HISTORY is the bot's own record of how previously evaluated props
performed against their lines. It is separate from PLAYER-GAME HISTORY
(provider gamelogs in player_game_results).

Both may feed L5/L10/L20/L30/Season windows. Provider rows always take
precedence for a given player/sport/stat/date.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


async def get_graded_prop_results(
    db: "Database",
    player_name: str,
    sport: str,
    stat_type: str,
    limit: int = 40,
) -> list:
    """
    Return graded PropOpportunityLog rows (HIT/MISS/PUSH with actual_value).

    Prefers Database.get_graded_prop_results when available; otherwise queries
    PropOpportunityLog directly so this works before database.py is updated.
    """
    if hasattr(db, "get_graded_prop_results"):
        return await db.get_graded_prop_results(player_name, sport, stat_type, limit=limit)

    from sqlalchemy import select
    from database import PropOpportunityLog

    async with db.session() as s:
        result = await s.execute(
            select(PropOpportunityLog)
            .where(
                PropOpportunityLog.player_name == player_name,
                PropOpportunityLog.sport == (sport or "").upper(),
                PropOpportunityLog.stat_type == (stat_type or "").lower().strip(),
                PropOpportunityLog.result.in_(("HIT", "MISS", "PUSH")),
                PropOpportunityLog.actual_value.isnot(None),
            )
            .order_by(
                PropOpportunityLog.graded_at.desc().nullslast(),
                PropOpportunityLog.game_time.desc().nullslast(),
            )
            .limit(limit)
        )
        return list(result.scalars().all())


async def persist_graded_player_result(
    db: "Database",
    *,
    player_name: str,
    sport: str,
    stat_type: str,
    game_date: str,
    actual_value: float,
    opponent: Optional[str] = None,
) -> None:
    """
    Upsert a graded actual into player_game_results with source=graded_prop.

    Provider sources are never demoted: if a non-graded row already exists for
    the same key, the provider row is left intact.
    """
    try:
        from providers.player_stats import RawGameResult
        await db.upsert_player_result(RawGameResult(
            player_name  = player_name,
            sport        = (sport or "UNKNOWN").upper(),
            stat_type    = (stat_type or "").lower().strip(),
            game_date    = game_date,
            actual_value = float(actual_value),
            opponent     = opponent,
            source       = "graded_prop",
        ))
    except Exception as exc:
        logger.debug("persist_graded_player_result failed: %s", exc)
