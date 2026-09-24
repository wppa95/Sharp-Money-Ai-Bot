"""
engine/timing.py — Game timing filter for PrizePicks and Underdog alerts.

Public API
──────────
  is_game_alertable(game_time, *, min_minutes, max_minutes, urgent_edge, edge)
      → (bool, str)   True = send, False = block with reason string

  format_game_time_label(game_time) → str
      Human-readable "starts in Xm" / "🔴 IN PROGRESS" label for display.

Rules
─────
  • game_time is None → allow by default for legacy callers
    (``require_known=True`` blocks unless the provider says pregame)
  • game already started (now ≥ game_time) → always block
  • minutes_to_start > max_minutes → block (too far out)
  • minutes_to_start < min_minutes AND edge < urgent_edge → block
    (too close to start and not a major-edge move)
  • otherwise → allow

Callers supply config values explicitly so this module stays stateless and
fully unit-testable without any config import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple


_BLOCKED_EVENT_STATUSES = frozenset({
    "started",
    "live",
    "in_progress",
    "in-progress",
    "final",
    "completed",
    "closed",
    "ended",
})
_PREGAME_EVENT_STATUSES = frozenset({
    "scheduled",
    "pre_game",
    "pre-game",
    "pregame",
    "upcoming",
    "open",
    "not_started",
    "not-started",
})


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize aware timestamps to naive UTC; treat naive values as UTC."""
    if value is None:
        return None
    try:
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.replace(tzinfo=None)
    except (OverflowError, TypeError, ValueError):
        return None


def is_event_pregame(
    game_time: Optional[datetime],
    *,
    game_status: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return whether an event is safely known to be pregame.

    A missing start time is safe only when the provider explicitly identifies
    the event as pregame.  Unknown event state is retained for tracking but
    cannot reach Telegram.
    """
    status = str(game_status or "").strip().lower().replace(" ", "_")
    if status in _BLOCKED_EVENT_STATUSES:
        return False, f"Event status is {status}"
    if status in _PREGAME_EVENT_STATUSES:
        return True, ""
    if game_time is None:
        return False, "Event start/state is unknown"

    now = _naive_utc(datetime.now(timezone.utc))
    gt = _naive_utc(game_time)
    if now is None or gt is None:
        return False, "Event start/state is unknown"
    try:
        if gt <= now:
            return False, "🔴 Game already in progress"
    except Exception:
        return False, "Event start/state is unknown"
    return True, ""


def is_game_alertable(
    game_time: Optional[datetime],
    *,
    min_minutes: int,
    max_minutes: int,
    urgent_edge: float,
    edge: float,
    game_status: Optional[str] = None,
    require_known: bool = False,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Decide whether an alert is within the acceptable timing window.

    Parameters
    ----------
    game_time:    UTC datetime of game start (naive or aware).  None → allow
                  unless ``require_known`` is true.
    game_status:  Optional provider event state; live/final states always block.
    require_known: Require a reliable pregame timestamp or explicit pregame state.
    min_minutes:  Minimum minutes before start to send (default 30).
    max_minutes:  Maximum minutes before start to send (default 120).
    urgent_edge:  Edge % at or above which min_minutes gate is bypassed.
    edge:         Current edge % for this pick.

    Returns
    -------
    (True, "")                           → send the alert
    (False, "<human-readable reason>")   → suppress the alert
    """
    status = str(game_status or "").strip().lower().replace(" ", "_")
    if status in _BLOCKED_EVENT_STATUSES:
        return False, f"Event status is {status}"

    if game_time is None:
        if require_known:
            if status not in _PREGAME_EVENT_STATUSES:
                return False, "Event start/state is unknown"
        return True, ""

    # Normalise both instants to naive UTC. Naive provider timestamps are
    # already stored as UTC; aware timestamps must be converted, not stripped.
    now_utc = _naive_utc(now or datetime.now(timezone.utc))
    gt = _naive_utc(game_time)
    if now_utc is None or gt is None:
        return False, "Event start/state is unknown"

    minutes_to_start = (gt - now_utc).total_seconds() / 60.0

    # Game already started
    if minutes_to_start <= 0:
        return False, "🔴 Game already in progress"

    # Too far in the future
    if minutes_to_start > max_minutes:
        return False, (
            f"⏳ Game starts in {_fmt_mins(minutes_to_start)} "
            f"(window closes at {max_minutes}m)"
        )

    # Too close but not urgent
    if minutes_to_start < min_minutes and edge < urgent_edge:
        return False, (
            f"⚡ Only {_fmt_mins(minutes_to_start)} to start — "
            f"edge {edge:.1f}% below urgent threshold {urgent_edge:.1f}%"
        )

    return True, ""


def format_game_time_label(game_time: Optional[datetime]) -> str:
    """
    Return a concise display label for a pick's game time.

    Examples:
      "starts in 47m"
      "starts in 1h 12m"
      "🔴 IN PROGRESS"
      ""   (when game_time is None)
    """
    if game_time is None:
        return ""

    now = _naive_utc(datetime.now(timezone.utc))
    gt = _naive_utc(game_time)
    if now is None or gt is None:
        return ""

    minutes_to_start = (gt - now).total_seconds() / 60.0

    if minutes_to_start <= 0:
        return "🔴 IN PROGRESS"

    if minutes_to_start < 60:
        return f"starts in {int(minutes_to_start)}m"

    hours = int(minutes_to_start // 60)
    mins  = int(minutes_to_start % 60)
    return f"starts in {hours}h {mins}m" if mins else f"starts in {hours}h"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fmt_mins(minutes: float) -> str:
    """Format a minute count as 'Xm' or 'Xh Ym'."""
    if minutes < 60:
        return f"{int(minutes)}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m" if m else f"{h}h"
