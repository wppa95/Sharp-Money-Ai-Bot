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
    """Return the configured reporting timezone, falling back safely to UTC."""
    name = (
        getattr(config, "REPORTING_TIMEZONE", None)
        or getattr(config, "ODDS_API_PACING_TIMEZONE", None)
        or "UTC"
    )
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def reporting_now() -> datetime:
    """Return the current instant in the canonical reporting timezone."""
    return datetime.now(timezone.utc).astimezone(reporting_timezone())


def current_reporting_day_bounds(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return [start, end) of the current reporting day as naive UTC values."""
    local_now = (now or reporting_now()).astimezone(reporting_timezone())
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