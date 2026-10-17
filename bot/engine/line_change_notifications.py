"""Bounded, source-owned observation notifications, never betting picks.

Only complete persisted feed batches enter this module. A restart primes the
observation cache again; it never infers a move between unrelated events.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import timedelta
from html import escape
import logging
import math

from .timing import is_game_alertable

logger = logging.getLogger(__name__)
_MAX_OBSERVATIONS = 40_000
_MAX_PENDING = 1_000
_latest: OrderedDict[tuple, object] = OrderedDict()
_pending: OrderedDict[tuple, tuple] = OrderedDict()
_worker: asyncio.Task | None = None


def _identity(prop):
    return (
        prop.provider, prop.external_id, prop.game_id, prop.player_name,
        prop.sport, prop.stat_type, prop.line_variant,
    )


def _safe(prop) -> bool:
    from alerts import _is_fresh_delivery_line
    if (
        prop.provider not in {"Underdog", "PrizePicks"}
        or not prop.external_id or not prop.game_id or prop.removed
        or not math.isfinite(prop.line_value)
        or not _is_fresh_delivery_line(prop.fetched_at)
    ):
        return False
    from .sport_policy import is_disallowed_football_sport
    if is_disallowed_football_sport(prop.sport):
        return False
    return is_game_alertable(
        prop.game_time, game_status=prop.provider_status,
        require_known=True, min_minutes=0, max_minutes=None,
        urgent_edge=0, edge=0,
    )[0]


def format_line_change(previous, current) -> str:
    direction = str(current.bet_recommendation or current.direction or "").upper()
    if direction not in current.available_directions:
        direction = "Unavailable"
    return "\n".join([
        "<b>LINE CHANGE</b>",
        f"<b>Platform:</b> {escape(current.provider)}",
        f"<b>Player:</b> {escape(current.player_name)}",
        f"<b>Sport:</b> {escape(current.sport)}",
        f"<b>Market:</b> {escape(current.stat_type)}",
        f"<b>Previous line:</b> {previous.line_value:g}",
        f"<b>Current line:</b> {current.line_value:g}",
        f"<b>Direction:</b> {escape(direction)}",
        "Market observation only — not a new bet pick.",
        "",
        "Monitoring player props across multiple sports. Not betting advice.",
    ])


def observe_persisted_batch(db, bot, chat_ids, props) -> None:
    """Nonblocking producer; coalesce pending moves without delaying evaluation."""
    global _worker
    if db is None or bot is None or not chat_ids:
        return
    for current in props:
        if not _safe(current):
            continue
        key = _identity(current)
        previous = _latest.get(key)
        if previous is not None and current.fetched_at <= previous.fetched_at:
            continue
        _latest[key] = current
        _latest.move_to_end(key)
        if previous is not None and previous.line_value != current.line_value:
            # Never infer a continuous move across stale observations.
            if current.fetched_at - previous.fetched_at > timedelta(hours=6):
                continue
            if key in _pending:
                previous = _pending[key][0]
            elif len(_pending) >= _MAX_PENDING:
                logger.warning(
                    "LINE CHANGE notification backlog full; source=%s sport=%s",
                    current.provider, current.sport,
                )
                continue
            if previous.line_value == current.line_value:
                _pending.pop(key, None)
                continue
            _pending[key] = (previous, current, db, bot, tuple(chat_ids))
        while len(_latest) > _MAX_OBSERVATIONS:
            _latest.popitem(last=False)
    if _pending and (_worker is None or _worker.done()):
        _worker = asyncio.create_task(_drain(), name="line_change_notifications")


async def _drain() -> None:
    from alerts import broadcast_alert
    while _pending:
        key, (previous, current, db, bot, chat_ids) = _pending.popitem(last=False)
        try:
            if _latest.get(key) is not current or not _safe(current):
                continue
            path = f"line_change:{previous.line_value:g}->{current.line_value:g}"
            identity = dict(
                provider=current.provider, alert_kind="LINE_CHANGE",
                delivery_path=path, external_id=current.external_id,
                event_id=current.game_id, stat_type=current.stat_type,
                line_value=current.line_value,
            )
            if await db.has_telegram_notification(**identity):
                continue
            if _latest.get(key) is not current or not _safe(current):
                continue
            counts = await broadcast_alert(
                bot, chat_ids, format_line_change(previous, current),
                sport=current.sport,
            )
            await db.log_telegram_delivery(
                **identity, player_name=current.player_name, sport=current.sport,
                outcome="sent" if counts["sent"] else "failed",
                recipients_sent=counts["sent"], recipients_failed=counts["failed"],
            )
            logger.info(
                "LINE CHANGE: source=%s player=%s market=%s old=%g current=%g sent=%d",
                current.provider, current.player_name, current.stat_type,
                previous.line_value, current.line_value, counts["sent"],
            )
        except Exception:
            logger.exception("LINE CHANGE notification failed (not a betting pick)")


async def reset() -> None:
    global _worker
    if _worker is not None and not _worker.done():
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
    _worker = None
    _pending.clear()
    _latest.clear()
