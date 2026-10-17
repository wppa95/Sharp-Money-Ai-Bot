"""Shared calendar-day reporting boundaries.

Database timestamps are stored as naive UTC datetimes.  User-facing reports
use one configured reporting timezone and convert its calendar boundaries back
to naive UTC before querying SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import config


def reporting_timezone() -> ZoneInfo:
    """Return the configured reporting timezone, defaulting to Missouri time."""
    name = getattr(config, "REPORTING_TIMEZONE", None) or "America/Chicago"
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/Chicago")


def reporting_now() -> datetime:
    """Return the current instant in the canonical reporting timezone."""
    return datetime.now(timezone.utc).astimezone(reporting_timezone())


def current_reporting_day_bounds(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return [start, end) of the current reporting day as naive UTC values."""
    local_now = to_reporting_timezone(now or reporting_now())
    local_start = local_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def to_reporting_timezone(value: datetime | None) -> datetime | None:
    """Interpret naive stored values as UTC and return an aware local value."""
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(reporting_timezone())


def is_current_reporting_day(
    value: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether an event falls on today's configured reporting date.

    Stored event timestamps are UTC-naive in SQLite, while the user's meaning
    of "today" follows REPORTING_TIMEZONE. Unknown event dates fail closed.
    """
    event_local = to_reporting_timezone(value)
    if event_local is None:
        return False
    local_now = to_reporting_timezone(now or reporting_now())
    return event_local.date() == local_now.date()