"""
providers/base.py — Shared status types for provider health monitoring.

These types flow through the ProviderHealthMonitor and are surfaced in
/status output and the cmd_dashboard health section.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class ProviderStatus(str, enum.Enum):
    OK              = "ok"
    DEGRADED        = "degraded"        # partial failures, still returning data
    DOWN            = "down"            # 3+ consecutive failures; no data
    QUOTA_EXHAUSTED = "quota_exhausted" # 401 OUT_OF_USAGE_CREDITS
    BLOCKED         = "blocked"         # 403 / DataDome bot-detection wall
    DISABLED        = "disabled"        # connector turned off in config


class FailureType(str, enum.Enum):
    HTTP_ERROR  = "http_error"
    QUOTA       = "quota"       # OUT_OF_USAGE_CREDITS / 401
    BLOCKED     = "blocked"     # 403 or DataDome interstitial
    TIMEOUT     = "timeout"
    PARSE_ERROR = "parse_error"
    UNKNOWN     = "unknown"


class RecoveryStrategy(str, enum.Enum):
    """
    Prescribed recovery behaviour for each FailureType (Framework v3.0 Layer 3).

    SKIP     — Skip this cycle and retry on the next scheduled run.
               Use for transient issues (parse errors, unknown failures).
    BACKOFF  — Exponential or stepped back-off before the next retry.
               Use for recoverable network/HTTP errors.
    WAIT     — Hold off until a natural refresh occurs (quota reset).
               Do not aggressively retry; log the quota state.
    DISABLE  — Stop fetching for this provider until manual review or
               an explicit re-enable signal.  Use for hard blocks.

    Callers retrieve the strategy via
    ``providers.health_monitor.recovery_strategy_for(failure_type, streak)``.
    """

    SKIP    = "skip"
    BACKOFF = "backoff"
    WAIT    = "wait"
    DISABLE = "disable"


@dataclass(frozen=True)
class ProviderHealth:
    """
    Immutable snapshot of a provider's health at a point in time.

    Created by ProviderHealthMonitor.get_health() / get_all_health().
    """

    name:                  str
    status:                ProviderStatus
    last_success:          Optional[datetime]
    last_failure:          Optional[datetime]
    consecutive_failures:  int
    failure_type:          Optional[FailureType]
    quota_remaining:       Optional[int]   # from x-requests-remaining header
    quota_used:            Optional[int]   # from x-requests-used header
    error_msg:             str = ""
    checked_at:            datetime = field(default_factory=datetime.utcnow)

    # ── Display helpers ───────────────────────────────────────────────────────

    @property
    def status_emoji(self) -> str:
        return {
            ProviderStatus.OK:              "🟢",
            ProviderStatus.DEGRADED:        "🟡",
            ProviderStatus.DOWN:            "🔴",
            ProviderStatus.QUOTA_EXHAUSTED: "⛔",
            ProviderStatus.BLOCKED:         "🚫",
            ProviderStatus.DISABLED:        "⚪",
        }.get(self.status, "❓")

    @property
    def last_success_age_seconds(self) -> Optional[int]:
        """Seconds since the last successful fetch, or None if never."""
        if self.last_success is None:
            return None
        return max(0, int((datetime.utcnow() - self.last_success).total_seconds()))

    def format_last_success(self) -> str:
        """Human-readable time since last success (e.g. '3m ago', 'never')."""
        age = self.last_success_age_seconds
        if age is None:
            return "never"
        if age < 60:
            return f"{age}s ago"
        if age < 3600:
            return f"{age // 60}m ago"
        return f"{age // 3600}h {(age % 3600) // 60}m ago"
