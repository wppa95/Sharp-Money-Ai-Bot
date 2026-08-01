"""
providers/health_monitor.py — ProviderHealthMonitor singleton.

Tracks the real-time health of every data provider (PrizePicks, Odds API,
Underdog) based on success/failure signals from connectors and the Odds API
cache.  Results are surfaced in /status and /dashboard.

Status derivation rules
-----------------------
  failure_type == QUOTA    →  QUOTA_EXHAUSTED  (regardless of failure count)
  failure_type == BLOCKED  →  BLOCKED          (regardless of failure count)
  consecutive_failures ≥ 3 →  DOWN
  consecutive_failures ≥ 1 →  DEGRADED
  record_success called    →  OK

Consecutive-failure counter is reset to 0 on every record_success() call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .base import FailureType, ProviderHealth, ProviderStatus, RecoveryStrategy

logger = logging.getLogger(__name__)

# Thresholds for status escalation
_DOWN_THRESHOLD     = 3   # consecutive failures → DOWN
_DEGRADED_THRESHOLD = 1   # consecutive failures → DEGRADED


@dataclass
class _ProviderState:
    """Mutable internal state for a single provider."""

    name:                  str
    last_success:          Optional[datetime] = None
    last_failure:          Optional[datetime] = None
    consecutive_failures:  int = 0
    failure_type:          Optional[FailureType] = None
    quota_remaining:       Optional[int] = None
    quota_used:            Optional[int] = None
    error_msg:             str = ""
    disabled:              bool = False

    def derive_status(self) -> ProviderStatus:
        if self.disabled:
            return ProviderStatus.DISABLED
        # Sticky overrides based on last failure type
        if self.failure_type == FailureType.QUOTA:
            return ProviderStatus.QUOTA_EXHAUSTED
        if self.failure_type == FailureType.BLOCKED:
            return ProviderStatus.BLOCKED
        # Escalate by count
        if self.consecutive_failures >= _DOWN_THRESHOLD:
            return ProviderStatus.DOWN
        if self.consecutive_failures >= _DEGRADED_THRESHOLD:
            return ProviderStatus.DEGRADED
        return ProviderStatus.OK

    def to_health(self) -> ProviderHealth:
        return ProviderHealth(
            name                 = self.name,
            status               = self.derive_status(),
            last_success         = self.last_success,
            last_failure         = self.last_failure,
            consecutive_failures = self.consecutive_failures,
            failure_type         = self.failure_type,
            quota_remaining      = self.quota_remaining,
            quota_used           = self.quota_used,
            error_msg            = self.error_msg,
            checked_at           = datetime.utcnow(),
        )


class ProviderHealthMonitor:
    """
    Tracks health signals for named data providers.

    Usage::

        monitor = init_health_monitor()
        monitor.register("PrizePicks")
        monitor.register("OddsAPI")

        # In a connector on success:
        monitor.record_success("OddsAPI", quota_remaining=450, quota_used=50)

        # In a connector on failure:
        monitor.record_failure("OddsAPI", "401 OUT_OF_USAGE_CREDITS", FailureType.QUOTA)

        # In /status:
        for name, health in monitor.get_all_health().items():
            print(health.status_emoji, name, health.status.value)
    """

    def __init__(self) -> None:
        self._states: dict[str, _ProviderState] = {}

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, name: str, disabled: bool = False) -> None:
        """Pre-register a provider.  Idempotent; existing state is preserved."""
        if name not in self._states:
            self._states[name] = _ProviderState(name=name, disabled=disabled)
            logger.debug("ProviderHealthMonitor: registered provider %r", name)

    # ── Signal recording ──────────────────────────────────────────────────────

    def record_success(
        self,
        name: str,
        quota_remaining: Optional[int] = None,
        quota_used: Optional[int] = None,
    ) -> None:
        """
        Record a successful fetch.

        Resets consecutive_failures to 0 and clears the sticky failure_type
        (except QUOTA / BLOCKED which can only be cleared by a success with
        positive quota_remaining or an explicit register() call).
        """
        state = self._states.setdefault(name, _ProviderState(name=name))
        state.last_success         = datetime.utcnow()
        state.consecutive_failures = 0
        state.failure_type         = None
        state.error_msg            = ""
        if quota_remaining is not None:
            state.quota_remaining = quota_remaining
        if quota_used is not None:
            state.quota_used = quota_used

    def record_failure(
        self,
        name: str,
        error: str,
        failure_type: FailureType,
    ) -> None:
        """
        Record a failed fetch.

        Increments consecutive_failures and stores the failure type and
        message.  The status is re-derived on the next get_health() call.
        """
        state = self._states.setdefault(name, _ProviderState(name=name))
        state.last_failure          = datetime.utcnow()
        state.consecutive_failures += 1
        state.failure_type          = failure_type
        state.error_msg             = error[:200]
        logger.debug(
            "HealthMonitor: %s failure #%d (%s): %s",
            name, state.consecutive_failures, failure_type.value, error[:80],
        )

    # ── Health queries ────────────────────────────────────────────────────────

    def get_health(self, name: str) -> ProviderHealth:
        """Return the current health snapshot for a provider."""
        state = self._states.get(name)
        if state is None:
            state = _ProviderState(name=name)
            self._states[name] = state
        return state.to_health()

    def get_all_health(self) -> dict[str, ProviderHealth]:
        """Return health snapshots for all registered providers."""
        return {name: state.to_health() for name, state in self._states.items()}

    def is_quota_exhausted(self, name: str) -> bool:
        state = self._states.get(name)
        return state is not None and state.failure_type == FailureType.QUOTA

    def is_blocked(self, name: str) -> bool:
        state = self._states.get(name)
        return state is not None and state.failure_type == FailureType.BLOCKED

    def is_healthy(self, name: str) -> bool:
        """Return True when status is OK or DEGRADED (data is still coming in)."""
        state = self._states.get(name)
        if state is None:
            return True  # unknown provider — assume healthy (fail-open)
        return state.derive_status() in (ProviderStatus.OK, ProviderStatus.DEGRADED)


# ─────────────────────────────────────────────────────────────────────────────
# Error Taxonomy — recovery strategy registry (Framework v3.0 Layer 3)
# ─────────────────────────────────────────────────────────────────────────────

#: Streak thresholds that escalate the default recovery strategy.
_ESCALATE_BACKOFF_TO_DISABLE_STREAK = 5   # HTTP_ERROR: give up after 5 consecutive
_ESCALATE_SKIP_TO_BACKOFF_STREAK    = 3   # TIMEOUT / PARSE_ERROR: switch to backoff after 3


def recovery_strategy_for(
    failure_type: FailureType,
    streak: int = 0,
) -> RecoveryStrategy:
    """
    Return the prescribed ``RecoveryStrategy`` for a given ``FailureType``
    and current consecutive-failure streak.

    Rules
    ─────
    QUOTA       → WAIT      (always — never retry until natural quota reset)
    BLOCKED     → DISABLE   (always — requires manual intervention)
    HTTP_ERROR  → BACKOFF   (streak < 5)  or DISABLE (streak ≥ 5)
    TIMEOUT     → SKIP      (streak < 3)  or BACKOFF (streak ≥ 3)
    PARSE_ERROR → SKIP      (streak < 3)  or BACKOFF (streak ≥ 3)
    UNKNOWN     → SKIP      (always — safe default)

    Parameters
    ----------
    failure_type : FailureType
        The type of failure that just occurred.
    streak : int
        Current consecutive-failure count for this provider.
        Pass 0 (default) when the streak is unknown.
    """
    if failure_type == FailureType.QUOTA:
        return RecoveryStrategy.WAIT

    if failure_type == FailureType.BLOCKED:
        return RecoveryStrategy.DISABLE

    if failure_type == FailureType.HTTP_ERROR:
        if streak >= _ESCALATE_BACKOFF_TO_DISABLE_STREAK:
            return RecoveryStrategy.DISABLE
        return RecoveryStrategy.BACKOFF

    if failure_type in (FailureType.TIMEOUT, FailureType.PARSE_ERROR):
        if streak >= _ESCALATE_SKIP_TO_BACKOFF_STREAK:
            return RecoveryStrategy.BACKOFF
        return RecoveryStrategy.SKIP

    # FailureType.UNKNOWN and any future additions
    return RecoveryStrategy.SKIP


# ── Module-level singleton ────────────────────────────────────────────────────

_monitor: Optional[ProviderHealthMonitor] = None


def init_health_monitor() -> ProviderHealthMonitor:
    """Create (or replace) the module-level singleton and return it."""
    global _monitor
    _monitor = ProviderHealthMonitor()
    return _monitor


def get_health_monitor() -> Optional[ProviderHealthMonitor]:
    """Return the singleton, or None if init_health_monitor() has not been called."""
    return _monitor
