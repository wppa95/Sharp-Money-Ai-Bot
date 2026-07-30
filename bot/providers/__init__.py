"""
providers/ — Production data infrastructure.

Exports the shared health monitor, Odds API cache, game-results framework,
and API usage tracker used across all data connectors.
"""

from .base import FailureType, ProviderHealth, ProviderStatus
from .health_monitor import (
    ProviderHealthMonitor,
    get_health_monitor,
    init_health_monitor,
)
from .odds_cache import OddsApiCache, OddsApiError, get_odds_cache, init_odds_cache
from .usage_tracker import (
    ApiUsageTracker,
    CallPriority,
    UsageStats,
    get_usage_tracker,
    init_usage_tracker,
    infer_call_priority,
)

__all__ = [
    "FailureType",
    "ProviderHealth",
    "ProviderStatus",
    "ProviderHealthMonitor",
    "get_health_monitor",
    "init_health_monitor",
    "OddsApiCache",
    "OddsApiError",
    "get_odds_cache",
    "init_odds_cache",
    "ApiUsageTracker",
    "CallPriority",
    "UsageStats",
    "get_usage_tracker",
    "init_usage_tracker",
    "infer_call_priority",
]
