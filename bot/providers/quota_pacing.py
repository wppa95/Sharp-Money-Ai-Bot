"""Strict, restart-safe pacing for legitimate OddsAPI polling.

The OddsAPI plan is limited to 16 requests per local calendar day with at
least 45 minutes between requests.  This module owns that policy centrally;
it never creates requests by itself and never changes Underdog scheduling.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from threading import RLock
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_DEFAULT_DEADLINE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_DEFAULT_DAILY_LIMIT = 16
_DEFAULT_MIN_INTERVAL_SECONDS = 45 * 60
_DEFAULT_TIMEZONE = "America/Chicago"
_DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "odds_api_pacing.json"
)
_WINDOW_SECONDS = 3600.0


def parse_deadline(value: str | datetime | None) -> datetime:
    """Parse a configured UTC deadline, falling back to the task deadline."""
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            result = _DEFAULT_DEADLINE
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuotaTelemetry:
    """Immutable snapshot suitable for logs and status output."""

    quota_used: Optional[int]
    quota_remaining: Optional[int]
    quota_cost_per_request: float
    current_calls_per_hour: float
    required_calls_per_hour: float
    required_calls_per_minute: float
    deadline_utc: str
    hours_remaining: float
    latency_ms: Optional[float]
    status: str
    backoff_seconds: float
    daily_count: int = 0
    daily_limit: int = _DEFAULT_DAILY_LIMIT
    min_interval_seconds: float = _DEFAULT_MIN_INTERVAL_SECONDS
    last_request_at: Optional[datetime] = None
    next_eligible_at: Optional[datetime] = None
    last_request_age_seconds: Optional[float] = None
    seconds_until_eligible: float = 0.0
    pacing_timezone: str = _DEFAULT_TIMEZONE

    @property
    def summary(self) -> str:
        if self.last_request_age_seconds is None:
            last_request = "never"
        elif self.last_request_age_seconds < 60:
            last_request = f"{self.last_request_age_seconds:.0f}s ago"
        else:
            last_request = f"{self.last_request_age_seconds / 60:.1f}m ago"

        if self.seconds_until_eligible <= 0 and self.status == "READY":
            next_request = "now"
        elif self.next_eligible_at is None:
            next_request = "provider quota reset"
        elif self.seconds_until_eligible < 3600:
            next_request = f"in {self.seconds_until_eligible / 60:.1f}m"
        else:
            next_request = self.next_eligible_at.astimezone().strftime(
                "%Y-%m-%d %H:%M %Z"
            )

        return (
            f"Odds API: Daily usage: {self.daily_count}/{self.daily_limit}; "
            f"Last request: {last_request}; "
            f"Next eligible request: {next_request}; "
            f"Daily limit: {self.daily_limit}; "
            f"Minimum interval: {self.min_interval_seconds / 60:.0f}m; "
            f"status={self.status}"
        )


class OddsApiQuotaPacer:
    """Enforce the hard daily cap and minimum interval for OddsAPI requests."""

    def __init__(
        self,
        *,
        quota_limit: int = 500,
        deadline: str | datetime | None = None,
        min_interval_seconds: float = _DEFAULT_MIN_INTERVAL_SECONDS,
        max_interval_seconds: float = 900.0,
        daily_limit: int = _DEFAULT_DAILY_LIMIT,
        timezone_name: str = _DEFAULT_TIMEZONE,
        state_path: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        # quota_limit/deadline/max_interval_seconds remain accepted for
        # compatibility with older callers, but the plan's daily policy is the
        # authoritative pacing rule.
        self.quota_limit = max(0, int(quota_limit))
        self.deadline = parse_deadline(deadline)
        self.daily_limit = max(0, int(daily_limit))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_interval_seconds = max(self.min_interval_seconds, float(max_interval_seconds))
        try:
            self._timezone = ZoneInfo(timezone_name)
            self.timezone_name = timezone_name
        except ZoneInfoNotFoundError:
            logger.warning(
                "OddsAPI pacing timezone %r is invalid; using UTC", timezone_name
            )
            self._timezone = timezone.utc
            self.timezone_name = "UTC"
        self._state_path = state_path or _DEFAULT_STATE_PATH
        self._clock = now_fn or (lambda: datetime.now(timezone.utc))
        self._state_lock = RLock()
        self._state_date: str = ""
        self._daily_count = 0
        self._last_request_wall: Optional[datetime] = None
        self._pending_reservations = 0
        self._request_times: deque[float] = deque()
        self._last_request_at: Optional[float] = None
        self._quota_used: Optional[int] = None
        self._quota_remaining: Optional[int] = None
        # OddsAPI quota headers represent credits, and a request can cost
        # more than one credit depending on its market set.  Learn the live
        # cost from successive provider headers instead of assuming 1.
        self._quota_cost_per_request = 1.0
        self._last_latency_ms: Optional[float] = None
        self._backoff_until = 0.0
        self._backoff_seconds = 0.0
        self._consecutive_errors = 0
        self._load_state()

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def _local_date(self, current: Optional[datetime] = None) -> str:
        return (current or self._now()).astimezone(self._timezone).date().isoformat()

    def _reset_daily_state_if_needed(self, current: Optional[datetime] = None) -> None:
        today = self._local_date(current)
        if self._state_date != today:
            self._state_date = today
            self._daily_count = 0
            self._last_request_wall = None

    def _load_state(self) -> None:
        """Load only the current day's enforcement state after a restart."""
        with self._state_lock:
            try:
                with open(self._state_path) as fh:
                    state = json.load(fh)
                current = self._now()
                state_date = str(state.get("date", ""))
                if state_date == self._local_date(current):
                    self._state_date = state_date
                    self._daily_count = max(0, int(state.get("daily_count", 0)))
                    raw_last = state.get("last_request_at")
                    if raw_last:
                        parsed = datetime.fromisoformat(str(raw_last))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        self._last_request_wall = parsed.astimezone(timezone.utc)
                else:
                    self._state_date = self._local_date(current)
            except FileNotFoundError:
                self._state_date = self._local_date()
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning("OddsAPI pacing state could not be loaded: %s", exc)
                self._state_date = self._local_date()

    def _save_state(self) -> None:
        """Atomically persist the minimal state needed across normal restarts."""
        directory = os.path.dirname(self._state_path) or "."
        os.makedirs(directory, exist_ok=True)
        temp_path = f"{self._state_path}.{os.getpid()}.tmp"
        payload = {
            "date": self._state_date,
            "daily_count": self._daily_count,
            "last_request_at": (
                self._last_request_wall.isoformat()
                if self._last_request_wall is not None
                else None
            ),
        }
        with open(temp_path, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, self._state_path)

    def update_quota(
        self,
        *,
        remaining: Optional[int] = None,
        used: Optional[int] = None,
    ) -> None:
        """Store authoritative values from OddsAPI response headers."""
        with self._state_lock:
            previous_used = self._quota_used
            previous_remaining = self._quota_remaining
            if remaining is not None:
                self._quota_remaining = max(0, int(remaining))
            if used is not None:
                self._quota_used = max(0, int(used))
        deltas = []
        if used is not None and previous_used is not None:
            deltas.append(int(used) - previous_used)
        if remaining is not None and previous_remaining is not None:
            deltas.append(previous_remaining - int(remaining))
        positive_deltas = [delta for delta in deltas if 0 < delta <= 100]
        if positive_deltas:
            self._quota_cost_per_request = float(positive_deltas[-1])

    def record_request_started(self) -> None:
        """Record request timing after :meth:`reserve_request_slot` succeeds."""
        now = time.monotonic()
        self._request_times.append(now)
        self._last_request_at = now
        self._prune(now)
        with self._state_lock:
            if self._pending_reservations:
                self._pending_reservations -= 1

    def record_request_finished(self, started_at: float) -> None:
        self._last_latency_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
        self._consecutive_errors = 0
        self._backoff_seconds = 0.0

    def record_request_failed(
        self,
        started_at: float,
        *,
        status: int = 0,
        quota_exhausted: bool = False,
    ) -> None:
        """Record latency for a failed request without bypassing strict pacing."""
        self._last_latency_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
        # A 422 is a permanent request-shape/market error, not a transient
        # provider failure.  Retrying it only delays the useful game-line
        # poller and can turn a disabled market into pacing noise.
        if status == 422 and not quota_exhausted:
            self._backoff_seconds = 0.0
            return
        self.record_error(status, quota_exhausted=quota_exhausted)

    def record_error(self, status: int = 0, *, quota_exhausted: bool = False) -> None:
        """Retain failure telemetry; the fixed interval remains authoritative."""
        self._consecutive_errors += 1
        self._backoff_seconds = 0.0
        self._backoff_until = 0.0

    def hours_remaining(self, now: Optional[datetime] = None) -> float:
        current = now or datetime.now(timezone.utc)
        return max(0.0, (self.deadline - current.astimezone(timezone.utc)).total_seconds() / 3600.0)

    def _prune(self, now: Optional[float] = None) -> None:
        cutoff = (now or time.monotonic()) - _WINDOW_SECONDS
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()

    def current_calls_per_hour(self) -> float:
        self._prune()
        if len(self._request_times) < 2:
            return 0.0
        span = self._request_times[-1] - self._request_times[0]
        if span <= 0:
            return 0.0
        # Extrapolate the observed request cadence over an hour.  A raw
        # count under-reports a newly-started poller for most of its first
        # hour and incorrectly labels a healthy cadence as BEHIND.
        return float((len(self._request_times) - 1) * 3600.0 / span)

    def required_calls_per_hour(self) -> float:
        if self._quota_remaining is None:
            return 0.0
        hours = self.hours_remaining()
        if hours <= 0:
            return 0.0
        return self._quota_remaining / self._quota_cost_per_request / hours

    def required_interval_seconds(self) -> float:
        required = self.required_calls_per_hour()
        if required <= 0:
            return self.max_interval_seconds
        return min(
            self.max_interval_seconds,
            max(self.min_interval_seconds, 3600.0 / required),
        )

    def can_request(self) -> bool:
        """Return whether one new outgoing request may be reserved now."""
        with self._state_lock:
            current = self._now()
            self._reset_daily_state_if_needed(current)
            if self._daily_count >= self.daily_limit:
                return False
            if self._quota_remaining is not None and self._quota_remaining <= 0:
                return False
            if (
                self._last_request_wall is not None
                and (current - self._last_request_wall).total_seconds()
                < self.min_interval_seconds
            ):
                return False
            return True

    def block_reason(self) -> str:
        """Return a stable diagnostic for why a request is currently blocked."""
        with self._state_lock:
            current = self._now()
            self._reset_daily_state_if_needed(current)
            if self._daily_count >= self.daily_limit:
                return "daily limit reached"
            if self._quota_remaining is not None and self._quota_remaining <= 0:
                return "provider quota exhausted"
            if self._last_request_wall is not None:
                remaining = self.min_interval_seconds - (
                    current - self._last_request_wall
                ).total_seconds()
                if remaining > 0:
                    return f"minimum interval active ({remaining:.0f}s remaining)"
            return ""

    def reserve_request_slot(self) -> bool:
        """Atomically claim one daily request slot before making HTTP."""
        with self._state_lock:
            current = self._now()
            self._reset_daily_state_if_needed(current)
            if not self.can_request():
                return False
            self._daily_count += 1
            self._last_request_wall = current
            self._pending_reservations += 1
            self._save_state()
            return True

    async def wait_for_slot(self) -> bool:
        """Compatibility async check; never sleeps inside a scheduled poll."""
        return self.can_request()

    @property
    def daily_count(self) -> int:
        with self._state_lock:
            self._reset_daily_state_if_needed()
            return self._daily_count

    @property
    def last_request_wall(self) -> Optional[datetime]:
        with self._state_lock:
            return self._last_request_wall

    def next_eligible_at(self) -> Optional[datetime]:
        with self._state_lock:
            current = self._now()
            self._reset_daily_state_if_needed(current)
            if self._quota_remaining is not None and self._quota_remaining <= 0:
                return None
            local_now = current.astimezone(self._timezone)
            if self._daily_count >= self.daily_limit:
                next_day = local_now.date() + timedelta(days=1)
                return datetime.combine(
                    next_day, datetime_time.min, tzinfo=self._timezone
                ).astimezone(timezone.utc)
            if self._last_request_wall is None:
                return current
            return self._last_request_wall + timedelta(seconds=self.min_interval_seconds)

    def telemetry(self) -> QuotaTelemetry:
        current_time = self._now()
        with self._state_lock:
            self._reset_daily_state_if_needed(current_time)
            next_at = self.next_eligible_at()
            age = (
                max(0.0, (current_time - self._last_request_wall).total_seconds())
                if self._last_request_wall is not None
                else None
            )
            wait = (
                max(0.0, (next_at - current_time).total_seconds())
                if next_at is not None
                else 0.0
            )
            # The no-history case returns the current instant as the next
            # eligible time; avoid reporting a false cooldown from clock
            # movement between the two reads.
            if wait <= 1.0 and self._last_request_wall is None:
                wait = 0.0
            if self._daily_count >= self.daily_limit:
                status = "DAILY_LIMIT"
            elif self._quota_remaining is not None and self._quota_remaining <= 0:
                status = "EXHAUSTED"
            elif wait > 0:
                status = "COOLDOWN"
            else:
                status = "READY"
            hours = self.hours_remaining(current_time)
            required = 0.0
            current = self.current_calls_per_hour()
        return QuotaTelemetry(
            quota_used=self._quota_used,
            quota_remaining=self._quota_remaining,
            quota_cost_per_request=self._quota_cost_per_request,
            current_calls_per_hour=current,
            required_calls_per_hour=required,
            required_calls_per_minute=required / 60.0,
            deadline_utc=self.deadline.isoformat().replace("+00:00", "Z"),
            hours_remaining=hours,
            latency_ms=self._last_latency_ms,
            status=status,
            backoff_seconds=0.0,
            daily_count=self._daily_count,
            daily_limit=self.daily_limit,
            min_interval_seconds=self.min_interval_seconds,
            last_request_at=self._last_request_wall,
            next_eligible_at=next_at,
            last_request_age_seconds=age,
            seconds_until_eligible=wait,
            pacing_timezone=self.timezone_name,
        )


_pacer: Optional[OddsApiQuotaPacer] = None


def init_quota_pacer(**kwargs) -> OddsApiQuotaPacer:
    """Create the process-wide OddsAPI pacer."""
    global _pacer
    _pacer = OddsApiQuotaPacer(**kwargs)
    logger.info("OddsAPI quota pacer initialised: %s", _pacer.telemetry().summary)
    return _pacer


def get_quota_pacer() -> Optional[OddsApiQuotaPacer]:
    return _pacer