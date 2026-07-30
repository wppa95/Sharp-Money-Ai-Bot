"""
providers/usage_tracker.py — API credit usage tracking and budget enforcement.

Tracks outgoing Odds API request counts (our own per-day counter), enforces a
configurable monthly budget, and fires Telegram warning alerts at configured
thresholds (default 75 % / 90 % / 100 %).

The *authoritative* quota remaining (from x-requests-remaining headers) lives in
the health monitor.  This module adds:
  - our own day/month counts that survive across sessions (JSON file)
  - call-priority levels so non-essential calls are dropped first
  - active-sport filtering via a registered SeasonChecker
  - threshold tracking (which warnings have already been sent this month)

Call-priority levels
---------------------
  CRITICAL (1) — PrizePicks pipeline: never blocked, uses no Odds API quota
  HIGH     (2) — Player prop markets (markets string contains "player_props")
  MEDIUM   (3) — MLB / NBA moneylines  (sport_key contains baseball_mlb / basketball_nba)
  LOW      (4) — Everything else (other sports, spreads, totals)

Budget-enforcement rules (against budget_pct = quota_used / monthly_budget × 100)
--------------------------
  ≥ 75 %  → warn; all calls still allowed
  ≥ 90 %  → LOW priority blocked
  ≥ 100 % → LOW + MEDIUM blocked (CRITICAL and HIGH still pass)

Persistence: daily request counts are written to ``{data_dir}/api_usage.json``
so the monthly total survives bot restarts.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)

# Default storage directory — same folder that holds sharp_money.db
_DATA_DIR_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "data")

# Thresholds at which Telegram warnings fire (in percent of monthly budget)
WARN_THRESHOLDS: tuple[int, ...] = (75, 90, 100)


# ── Priority levels ────────────────────────────────────────────────────────────

class CallPriority(IntEnum):
    """Priority of a single Odds API call — lower value = higher priority."""

    CRITICAL = 1   # PrizePicks (no Odds API cost, guard kept for uniformity)
    HIGH     = 2   # Player prop markets
    MEDIUM   = 3   # MLB / NBA moneylines
    LOW      = 4   # Everything else


def infer_call_priority(sport_key: str, markets: str) -> CallPriority:
    """
    Derive a CallPriority from the sport key and markets query string.

    Rules (in order):
      1. ``"player_props"`` or any ``"player_"`` market key in *markets* → HIGH
      2. ``baseball_mlb`` in *sport_key*                                 → HIGH
         MLB is the only sport whose game-line alerts are currently in
         scope, so its odds requests must never be blocked by the budget
         guard (which stops MEDIUM calls at ≥ 90 % quota).
      3. ``basketball_nba`` in *sport_key*                               → MEDIUM
      4. Everything else                                                 → LOW
    """
    if "player_props" in markets or "player_" in markets:
        return CallPriority.HIGH
    s = sport_key.lower()
    if "baseball_mlb" in s:
        return CallPriority.HIGH
    if "basketball_nba" in s:
        return CallPriority.MEDIUM
    return CallPriority.LOW


# ── UsageStats snapshot ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UsageStats:
    """Read-only snapshot of usage for a single provider."""

    provider:        str
    today_count:     int           # our own counter for today
    month_count:     int           # our own counter for this calendar month
    month_budget:    int           # configured cap (0 = unlimited)
    budget_pct:      float         # 0.0–999.0 (uses real quota data when available)
    quota_remaining: Optional[int] # from API response headers; None if unknown
    quota_used:      Optional[int] # from API response headers; None if unknown
    last_request:    Optional[datetime]
    warning_level:   Optional[int] # highest threshold already crossed (75/90/100)

    @property
    def is_over_budget(self) -> bool:
        return self.budget_pct >= 100.0

    @property
    def budget_bar(self) -> str:
        """10-segment ASCII progress bar."""
        filled = min(10, int(self.budget_pct / 10))
        return "█" * filled + "░" * (10 - filled)

    @property
    def remaining_estimate(self) -> Optional[int]:
        """Best estimate of remaining requests this month."""
        if self.quota_remaining is not None:
            return self.quota_remaining
        if self.month_budget > 0:
            return max(0, self.month_budget - self.month_count)
        return None


# ── Main tracker class ─────────────────────────────────────────────────────────

class ApiUsageTracker:
    """
    Tracks API request counts by provider, enforces monthly budget limits,
    and exposes warning levels for Telegram alerts and /dashboard display.

    Instantiate once at startup via :func:`init_usage_tracker`; retrieve the
    singleton via :func:`get_usage_tracker`.
    """

    def __init__(
        self,
        monthly_budgets: dict[str, int],
        data_dir: str = _DATA_DIR_DEFAULT,
    ) -> None:
        """
        Parameters
        ----------
        monthly_budgets:
            Mapping of provider name → monthly request cap.  ``0`` means
            unlimited (budget enforcement is skipped for that provider).
        data_dir:
            Directory where ``api_usage.json`` is persisted.
        """
        self._budgets         = dict(monthly_budgets)
        self._data_file       = os.path.join(data_dir, "api_usage.json")
        # daily_counts[provider][ISO-date] = count
        self._daily:          dict[str, dict[str, int]]  = {}
        # last outgoing request timestamp per provider
        self._last_request:   dict[str, datetime]        = {}
        # thresholds already warned this calendar month — reset on new month
        self._warned:         dict[str, set[int]]        = {}
        # optional SeasonChecker for active-sport filtering
        self._season_checker  = None
        self._current_month   = date.today().strftime("%Y-%m")

        self._load()

    # ── Public interface ───────────────────────────────────────────────────────

    def set_season_checker(self, checker) -> None:
        """Register a SeasonChecker for active-sport filtering in should_allow()."""
        self._season_checker = checker

    def record_request(
        self,
        provider:   str,
        priority:   CallPriority = CallPriority.LOW,
        sport_key:  Optional[str] = None,
    ) -> Optional[int]:
        """
        Record one outgoing API request.

        Returns the first newly-crossed warning threshold (75, 90, or 100)
        if one was just crossed, otherwise ``None``.  The caller can use
        the return value to schedule a Telegram alert.
        """
        self._roll_month_if_needed()

        today_str = date.today().isoformat()
        bucket    = self._daily.setdefault(provider, {})
        bucket[today_str] = bucket.get(today_str, 0) + 1
        self._last_request[provider] = datetime.utcnow()
        self._save()

        return self._check_new_threshold(provider)

    def should_allow(
        self,
        provider:  str,
        priority:  CallPriority,
        sport_key: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Return ``(allowed: bool, reason: str)``.

        Two checks are applied (in order):

        1. **Active-sport filter** — if a SeasonChecker is registered and
           *sport_key* is not in the active-season set, block the call.
           (Only applied to MEDIUM and LOW priority; HIGH/CRITICAL always pass.)

        2. **Budget enforcement** — uses the authoritative quota from the
           health monitor when available, falls back to our own month count.
        """
        # ── 1. Active-sport filter ────────────────────────────────────────────
        if (
            sport_key is not None
            and priority.value >= CallPriority.MEDIUM.value
            and self._season_checker is not None
            and hasattr(self._season_checker, "get_active_sport_keys")
        ):
            active_keys = self._season_checker.get_active_sport_keys()
            # fail-open: if cache is empty the checker returns empty frozenset
            if active_keys and sport_key not in active_keys:
                return False, f"sport {sport_key!r} not in season"

        # ── 2. Budget enforcement ─────────────────────────────────────────────
        budget     = self._budgets.get(provider, 0)
        if budget <= 0:
            return True, ""   # unlimited

        budget_pct = self._get_authoritative_pct(provider)

        if budget_pct >= 100.0:
            if priority.value > CallPriority.HIGH.value:
                return (
                    False,
                    f"{provider} budget exhausted ({budget_pct:.0f}%) — "
                    f"{priority.name} priority blocked",
                )
        elif budget_pct >= 90.0:
            if priority == CallPriority.LOW:
                return (
                    False,
                    f"{provider} budget at {budget_pct:.0f}% — LOW priority blocked",
                )

        return True, ""

    def get_stats(self, provider: str) -> UsageStats:
        """Return the current usage snapshot for a provider."""
        self._roll_month_if_needed()

        today_count = self._get_today_count(provider)
        month_count = self._get_month_count(provider)
        budget      = self._budgets.get(provider, 0)
        budget_pct  = self._get_authoritative_pct(provider)

        # Pull quota info from the health monitor for the display fields
        quota_remaining = None
        quota_used      = None
        try:
            from .health_monitor import get_health_monitor
            mon = get_health_monitor()
            if mon:
                h = mon.get_health(provider)
                quota_remaining = h.quota_remaining
                quota_used      = h.quota_used
        except ImportError:
            pass

        warned = self._warned.get(provider, set())
        warning_level = max(warned) if warned else None

        return UsageStats(
            provider        = provider,
            today_count     = today_count,
            month_count     = month_count,
            month_budget    = budget,
            budget_pct      = budget_pct,
            quota_remaining = quota_remaining,
            quota_used      = quota_used,
            last_request    = self._last_request.get(provider),
            warning_level   = warning_level,
        )

    def get_all_stats(self) -> dict[str, UsageStats]:
        """Return usage snapshots for all known providers."""
        providers = set(self._budgets.keys()) | set(self._daily.keys())
        return {p: self.get_stats(p) for p in providers}

    def reset_monthly_warned(self, provider: str) -> None:
        """Clear the warned-threshold set for a provider (call at month rollover)."""
        self._warned.pop(provider, None)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _get_today_count(self, provider: str) -> int:
        return self._daily.get(provider, {}).get(date.today().isoformat(), 0)

    def _get_month_count(self, provider: str) -> int:
        prefix = date.today().strftime("%Y-%m")
        return sum(
            v for k, v in self._daily.get(provider, {}).items()
            if k.startswith(prefix)
        )

    def _get_authoritative_pct(self, provider: str) -> float:
        """
        Budget % using the authoritative quota_used from the health monitor
        when available; falls back to our own tracked month count.
        """
        budget = self._budgets.get(provider, 0)
        if budget <= 0:
            return 0.0

        # Prefer real quota data from API headers
        try:
            from .health_monitor import get_health_monitor
            mon = get_health_monitor()
            if mon:
                h = mon.get_health(provider)
                if h.quota_used is not None:
                    return min(h.quota_used / budget * 100.0, 999.0)
        except ImportError:
            pass

        # Fallback: our own tracked count
        return min(self._get_month_count(provider) / budget * 100.0, 999.0)

    def _check_new_threshold(self, provider: str) -> Optional[int]:
        """Return the first newly-crossed threshold for this provider, or None."""
        budget_pct = self._get_authoritative_pct(provider)
        warned     = self._warned.setdefault(provider, set())
        for thr in sorted(WARN_THRESHOLDS, reverse=True):
            if budget_pct >= thr and thr not in warned:
                warned.add(thr)
                logger.warning(
                    "ApiUsageTracker: %s crossed %d%% of monthly budget "
                    "(own-count: %d / %d)",
                    provider, thr,
                    self._get_month_count(provider),
                    self._budgets.get(provider, 0),
                )
                return thr
        return None

    def _roll_month_if_needed(self) -> None:
        """Reset per-month warning flags when a new calendar month starts."""
        current = date.today().strftime("%Y-%m")
        if current != self._current_month:
            self._current_month = current
            for provider in list(self._warned.keys()):
                self._warned.pop(provider, None)
            logger.info("ApiUsageTracker: new month %s — warning flags reset", current)

    # ── JSON persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self._data_file) as fh:
                data = json.load(fh)
            for provider, days in data.get("daily", {}).items():
                if isinstance(days, dict):
                    self._daily[provider] = {k: int(v) for k, v in days.items()}
            self._prune()
            logger.debug("ApiUsageTracker: loaded from %s", self._data_file)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("ApiUsageTracker: could not load %s: %s", self._data_file, exc)

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
            with open(self._data_file, "w") as fh:
                json.dump({"daily": self._daily}, fh, indent=2)
        except Exception as exc:
            logger.warning("ApiUsageTracker: could not save %s: %s", self._data_file, exc)

    def _prune(self, keep_days: int = 65) -> None:
        """Remove daily entries older than *keep_days* days."""
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        for provider in self._daily:
            self._daily[provider] = {
                k: v for k, v in self._daily[provider].items()
                if k >= cutoff
            }


# ── Module-level singleton ────────────────────────────────────────────────────

_tracker: Optional[ApiUsageTracker] = None


def init_usage_tracker(
    monthly_budgets: dict[str, int] | None = None,
    data_dir: str = _DATA_DIR_DEFAULT,
) -> ApiUsageTracker:
    """
    Create (or replace) the module-level singleton and return it.

    Parameters
    ----------
    monthly_budgets:
        Provider name → monthly request cap.  Defaults to
        ``{"OddsAPI": 500}`` (The Odds API free tier).
    data_dir:
        Directory for ``api_usage.json`` persistence.
    """
    global _tracker
    if monthly_budgets is None:
        from config import config as _cfg
        monthly_budgets = {"OddsAPI": _cfg.ODDS_API_MONTHLY_BUDGET}
    _tracker = ApiUsageTracker(monthly_budgets=monthly_budgets, data_dir=data_dir)
    logger.info(
        "ApiUsageTracker initialised — budgets: %s",
        {k: (f"{v:,}" if v > 0 else "unlimited") for k, v in monthly_budgets.items()},
    )
    return _tracker


def get_usage_tracker() -> Optional[ApiUsageTracker]:
    """Return the singleton, or ``None`` if :func:`init_usage_tracker` has not been called."""
    return _tracker
