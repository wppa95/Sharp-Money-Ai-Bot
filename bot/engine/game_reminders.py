"""Durable two-hour reminders; no new picks, provider calls, or scheduler jobs.

The existing minute heartbeat drains bounded batches. Confirmed PLAYs and their
reminder state are committed atomically in the existing delivery-audit table.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import json
import logging
import time

from .timing import _naive_utc, is_game_alertable
from reporting import is_current_reporting_day, to_reporting_timezone

logger = logging.getLogger(__name__)
FOOTER = "Monitoring player props across multiple sports. Not betting advice."
_latest: OrderedDict[tuple, object] = OrderedDict()
_owners: dict[tuple, tuple] = {}
_unavailable: set[tuple] = set()
_cursor = 0
_running = False
MAX_LATENESS = timedelta(minutes=2)


def _identity(value):
    return (
        value.provider, value.external_id, value.game_id, value.player_name,
        value.sport, value.stat_type, value.line_variant or "standard",
    )


def observe(props, *, scope=None):
    """Retain source-owned latest quotes, including removal/invalid-state facts."""
    current_keys = set()
    for prop in props:
        key = _identity(prop)
        current_keys.add(key)
        old = _latest.get(key)
        if old is None or prop.fetched_at > old.fetched_at:
            _latest[key] = prop
            _latest.move_to_end(key)
            _owners[key] = scope
            _unavailable.discard(key)
    if scope is not None:
        # Absence from a complete snapshot invalidates only that ingestion
        # scope's quotes, never a different book, sport, or provider route.
        _unavailable.update(
            key for key in _latest
            if _owners.get(key) == scope and key not in current_keys
        )
    while len(_latest) > 40_000:
        key, _ = _latest.popitem(last=False)
        _owners.pop(key, None)
        _unavailable.discard(key)


def build_record(audit, now):
    """Build one immutable original-selection reminder from a confirmed PLAY."""
    try:
        data = json.loads(audit.evidence_snapshot_json or "{}")
        start = _naive_utc(datetime.fromisoformat(data["game_time_utc"]))
        direction = data["direction"]
        if direction not in {"OVER", "UNDER"} or start is None:
            return None
    except (KeyError, ValueError, TypeError):
        logger.warning("Reminder not scheduled: verified event time/selection unavailable")
        return None
    due = start - timedelta(hours=2)
    variant = data.get("line_variant") or "standard"
    source_event_id = data.get("source_event_id") or audit.event_id
    # Include player/sport/selection/classification in addition to the provider
    # audit identity. Delivery paths must not create duplicate reminders.
    identity = "|".join(map(str, (
        audit.provider, audit.external_id, audit.event_id, audit.player_name,
        audit.sport, audit.stat_type, audit.line_value, direction, variant,
        source_event_id,
    )))
    key = "reminder:" + sha256(identity.encode()).hexdigest()
    metadata = dict(
        original_delivery_key=audit.delivery_key, direction=direction,
        line_variant=variant, game_time_utc=start.isoformat(),
        source_event_id=source_event_id,
        due_utc=due.isoformat(),
        reason="due_time_already_passed" if due <= now else "scheduled",
    )
    return dict(
        provider=audit.provider, alert_kind="GAME_REMINDER",
        delivery_path="two_hour", player_name=audit.player_name, sport=audit.sport,
        stat_type=audit.stat_type, external_id=audit.external_id,
        event_id=audit.event_id, line_value=audit.line_value, delivery_key=key,
        outcome="elapsed" if due <= now else "pending", attempted_at=now,
        recipients_sent=0, recipients_failed=0,
        evidence_snapshot_json=json.dumps(metadata, sort_keys=True),
    )


def format_reminder(row, data):
    start = to_reporting_timezone(datetime.fromisoformat(data["game_time_utc"]))
    due = to_reporting_timezone(datetime.fromisoformat(data["due_utc"]))
    return "\n".join([
        "<b>GAME REMINDER</b>",
        f"<b>Platform:</b> {escape(row.provider)}",
        f"<b>Player:</b> {escape(row.player_name or '')}",
        f"<b>Event:</b> {escape(row.event_id or '')}",
        f"<b>Sport / market:</b> {escape(row.sport or '')} / {escape(row.stat_type or '')}",
        f"<b>Original selection:</b> {escape(data['direction'])} {row.line_value:g}",
        f"<b>Game starts:</b> {start.strftime('%b %-d, %-I:%M %p %Z')}",
        f"<b>Scheduled reminder:</b> {due.strftime('%b %-d, %-I:%M %p %Z')}",
        "Reminder of an existing pick — not a new betting pick.",
        "", FOOTER,
    ])


async def run_due_reminders(db, bot, chat_ids, *, now=None):
    """One bounded, restart-safe drain; require a fresh verified current quote.

    At-most-once claims deliberately remain ``sending`` after a crash: replaying
    an uncertain Telegram send could duplicate a reminder. Missing live quotes
    fail closed, not against a stale cached line.
    """
    global _running, _cursor
    if _running or db is None or bot is None or not chat_ids:
        return
    _running = True
    deadline = time.monotonic() + 20
    try:
        clock = lambda: _naive_utc(now or datetime.now(timezone.utc))
        rows = await db.get_pending_game_reminders(clock(), cursor=_cursor)
        for row in rows:
            if time.monotonic() >= deadline:
                break
            _cursor = row.id
            instant = clock()
            data = json.loads(row.evidence_snapshot_json)
            due = datetime.fromisoformat(data["due_utc"])
            key = (
                row.provider, row.external_id, data.get("source_event_id") or row.event_id, row.player_name,
                row.sport, row.stat_type, data.get("line_variant") or "standard",
            )
            quote = _latest.get(key)
            if key in _unavailable:
                await db.update_game_reminder(
                    row.id, outcome="cancelled",
                    data={**data, "reason": "absent_from_complete_source_snapshot"},
                )
                continue
            if quote is not None:
                from alerts import _is_fresh_delivery_line, is_telegram_suppressed_sport
                start = _naive_utc(quote.game_time)
                safe = (
                    not quote.removed and start is not None
                    and not is_telegram_suppressed_sport(quote.sport)
                    and quote.line_value == row.line_value
                    and data["direction"] in quote.available_directions
                    and _is_fresh_delivery_line(quote.fetched_at)
                    and is_game_alertable(
                        start, game_status=quote.provider_status, require_known=True,
                        min_minutes=0, max_minutes=None, urgent_edge=0, edge=0,
                    )[0]
                )
                if not safe:
                    await db.update_game_reminder(row.id, outcome="cancelled",
                                                  data={**data, "reason": "selection_or_event_invalid"})
                    continue
                new_due = start - timedelta(hours=2)
                if new_due != due:
                    # Recompute in UTC instants (including DST). Never mutate
                    # the original PLAY or its selection.
                    data.update(game_time_utc=start.isoformat(), due_utc=new_due.isoformat(),
                                reason="event_rescheduled")
                    outcome = "elapsed" if new_due <= instant else "pending"
                    await db.update_game_reminder(row.id, outcome=outcome, data=data)
                    continue
            if due > instant:
                continue
            if instant - due > MAX_LATENESS:
                await db.update_game_reminder(row.id, outcome="elapsed",
                                              data={**data, "reason": "missed_due_window"})
                continue
            if quote is None or not is_current_reporting_day(quote.game_time, now=instant):
                continue
            if not await db.claim_game_reminder(row.id):
                continue
            from alerts import broadcast_alert
            try:
                async with asyncio.timeout(10):
                    counts = await broadcast_alert(
                        bot, chat_ids, format_reminder(row, data), sport=row.sport,
                    )
                await db.update_game_reminder(
                    row.id, outcome="sent" if counts["sent"] else "failed",
                    counts=counts,
                )
            except Exception:
                # Retain the claim on uncertain transport failures.
                logger.exception("Game reminder transport unconfirmed; not retried")
    finally:
        _running = False


def reset():
    global _cursor
    _latest.clear()
    _owners.clear()
    _unavailable.clear()
    _cursor = 0
