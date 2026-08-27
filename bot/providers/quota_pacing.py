"""Adaptive pacing and telemetry for legitimate OddsAPI polling.

The provider's quota headers are authoritative when available.  This module
only controls the cadence of the existing useful polling path; it never
creates requests by itself and never changes Underdog scheduling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DEADLINE = datetime(2026, 8, 31, tzinfo=timezone.utc)
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

    @property
    def summary(self) -> str:
        used = f"{self.quota_used:,}" if self.quota_used is not None else "?"
        remaining = (
            f"{self.quota_remaining:,}" if self.quota_remaining is not None else "?"
        )
        latency = f"{self.latency_ms:.0f}ms" if self.latency_ms is not None else "?"
        return (
            f"used={used} remaining={remaining} "
            f"cost={self.quota_cost_per_request:.1f}/request "
            f"current={self.current_calls_per_hour:.1f}/h "
            f"required={self.required_calls_per_hour:.1f}/h "
            f"({self.required_calls_per_minute:.2f}/min) "
            f"deadline={self.deadline_utc} latency={latency} "
            f"status={self.status}"
        )


class OddsApiQuotaPacer:
    """Pace poller requests toward the remaining quota without bursting."""

    def __init__(
        self,
        *,
        quota_limit: int = 30_000,
        deadline: str | datetime | None = None,
        min_interval_seconds: float = 1.0,
        max_interval_seconds: float = 900.0,
    ) -> None:
        self.quota_limit = max(0, int(quota_limit))
        self.deadline = parse_deadline(deadline)
        self.min_interval_seconds = max(0.1, float(min_interval_seconds))
        self.max_interval_seconds = max(
            self.min_interval_seconds, float(max_interval_seconds)
        )
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

    def update_quota(
        self,
        *,
        remaining: Optional[int] = None,
        used: Optional[int] = None,
    ) -> None:
        """Store authoritative values from OddsAPI response headers."""
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
        """Record one actual outgoing HTTP request, not a cache hit."""
        now = time.monotonic()
        self._request_times.append(now)
        self._last_request_at = now
        self._prune(now)

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
        """Record latency and apply safe backoff for a failed request."""
        self._last_latency_ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
        # A 422 is a permanent request-shape/market error, not a transient
        # provider failure.  Retrying it only delays the useful game-line
        # poller and can turn a disabled market into pacing noise.
        if status == 422 and not quota_exhausted:
            self._backoff_seconds = 0.0
            return
        self.record_error(status, quota_exhausted=quota_exhausted)

    def record_error(self, status: int = 0, *, quota_exhausted: bool = False) -> None:
        """Apply bounded backoff after 429s and transient provider failures."""
        self._consecutive_errors += 1
        if quota_exhausted or self._quota_remaining == 0:
            delay = self.max_interval_seconds
        elif status == 429:
            delay = min(900.0, 60.0 * (2 ** min(self._consecutive_errors - 1, 3)))
        else:
            delay = min(300.0, 15.0 * (2 ** min(self._consecutive_errors - 1, 4)))
        self._backoff_seconds = delay
        self._backoff_until = time.monotonic() + delay

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
        """Return false only when the deadline/quota/backoff makes a call unsafe."""
        if self._quota_remaining is not None and self._quota_remaining <= 0:
            return False
        if self.hours_remaining() <= 0:
            return False
        return time.monotonic() >= self._backoff_until

    async def wait_for_slot(self) -> bool:
        """Wait for the next paced slot and return whether it may be used."""
        if not self.can_request():
            return False
        now = time.monotonic()
        delay = max(0.0, self._backoff_until - now)
        if self._last_request_at is not None:
            delay = max(
                delay,
                self.required_interval_seconds() - (now - self._last_request_at),
            )
        if delay > 0:
            await asyncio.sleep(delay)
        return self.can_request()

    def telemetry(self) -> QuotaTelemetry:
        hours = self.hours_remaining()
        required = self.required_calls_per_hour()
        current = self.current_calls_per_hour()
        if self._quota_remaining is None:
            status = "WAITING_FOR_QUOTA"
        elif self._quota_remaining <= 0:
            status = "EXHAUSTED"
        elif hours <= 0:
            status = "DEADLINE_PASSED"
        elif current > required * 1.10:
            status = "AHEAD"
        elif required > 0 and current < required * 0.90:
            status = "BEHIND"
        else:
            status = "ON_PACE"
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
            backoff_seconds=max(0.0, self._backoff_until - time.monotonic()),
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