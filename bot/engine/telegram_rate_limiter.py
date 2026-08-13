"""
engine/telegram_rate_limiter.py
────────────────────────────────────────────────────────────────────────────────
Global Telegram alert rate limiter.

Prevents alert floods by enforcing three nested layers:

  Layer 1 — Per-window rate limit
    At most TG_RATE_MAX_PER_WINDOW alerts may be sent in any rolling
    TG_RATE_WINDOW_SECONDS window.  Higher-tier props consume slots first:
    when only 1 slot is left in the window, it is reserved for S/A-tier.

  Layer 2 — Emergency flood-protection mode
    If TG_FLOOD_THRESHOLD alerts are sent inside the window, flood-protection
    mode is engaged automatically.  All delivery halts for
    TG_FLOOD_PROTECTION_DURATION seconds.  Nothing bypasses this layer —
    not even meaningful-change overrides.

  Layer 3 — Meaningful-change override (bypass Layer 1 only)
    A big line move or direction flip in an S/A-tier prop may bypass the
    per-window budget cap (Layer 1) exactly once per event, but it cannot
    bypass flood-protection mode (Layer 2).

Invariants
──────────
• Removals are NEVER rate-limited (they are cleanup, not spam).
• market_move_only alerts are NEVER rate-limited (internal only, no Telegram).
• The rate limiter is a pure in-memory singleton.  It resets on bot restart,
  which is intentional — the window clears on cold start.

Usage
─────
    from engine.telegram_rate_limiter import get_rate_limiter
    limiter = get_rate_limiter()

    # Before broadcast:
    result = limiter.check(tier, confidence, bq, is_meaningful_change=...)
    if not result.allowed:
        return DeliveryResult(sent=False, rate_limited=True, ...)

    # After successful broadcast:
    limiter.record_sent()

    # At start of each scan cycle:
    limiter.reset_cycle_counters()

    # At end of each scan cycle (log if deferred):
    limiter.log_cycle_summary("underdog_job")
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Tier rank for slot-priority decisions (lower = higher priority)
_TIER_RANK: dict[str, int] = {"S": 0, "A": 1, "B": 2, "C": 3, "PASS": 4}


# ── Result object ─────────────────────────────────────────────────────────────

@dataclass
class RateLimitResult:
    allowed: bool
    reason: str = ""
    flood_mode: bool = False
    window_count: int = 0
    window_max: int = 0


# ── Rate limiter ──────────────────────────────────────────────────────────────

class TelegramRateLimiter:
    """
    Sliding-window rate limiter with emergency flood protection.

    All internal operations are synchronous and GIL-safe for asyncio use.
    Do NOT await anything between check() and record_sent().
    """

    def __init__(
        self,
        window_seconds: int = 300,
        max_per_window: int = 5,
        flood_threshold: int = 10,
        flood_protection_duration: int = 600,
    ) -> None:
        self._window_seconds             = window_seconds
        self._max_per_window             = max_per_window
        self._flood_threshold            = flood_threshold
        self._flood_protection_duration  = flood_protection_duration

        # Sliding window: monotonic timestamps of sent alerts
        self._sent_times: collections.deque[float] = collections.deque()

        # Flood protection state
        self._flood_mode: bool                   = False
        self._flood_mode_entered_at: float | None = None

        # Per-scan-cycle counters (reset by reset_cycle_counters)
        self._cycle_qualified:  int = 0
        self._cycle_delivered:  int = 0
        self._cycle_deferred:   int = 0
        self._cycle_dedup:      int = 0
        self._cycle_cooldown:   int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def reset_cycle_counters(self) -> None:
        """Call at the start of each scan cycle (underdog_job, stable_refresh, etc.)."""
        self._cycle_qualified = 0
        self._cycle_delivered = 0
        self._cycle_deferred  = 0
        self._cycle_dedup     = 0
        self._cycle_cooldown  = 0

    def record_qualified(self) -> None:
        """Mark one prop as having passed all per-prop gates."""
        self._cycle_qualified += 1

    def record_dedup_suppressed(self) -> None:
        """Mark one prop suppressed by per-prop dedup."""
        self._cycle_dedup += 1

    def record_cooldown_suppressed(self) -> None:
        """Mark one prop suppressed by per-prop cooldown."""
        self._cycle_cooldown += 1

    def check(
        self,
        tier: str,
        confidence: float,
        bq: float,
        *,
        is_meaningful_change: bool = False,
    ) -> RateLimitResult:
        """
        Decide whether a prop may be delivered to Telegram right now.

        Returns RateLimitResult.  If allowed=False, the caller MUST return
        without calling broadcast_alert — and MUST NOT call record_sent().
        The prop remains available in /picks via the DB.

        Parameters
        ──────────
        tier               Decision tier ("S", "A", "B", "C").
        confidence         Confidence score 0–100.
        bq                 Bet Quality score (score.total) 0–100.
        is_meaningful_change
                           True when the caller has detected a significant line
                           move (≥ 2× min change) or a direction flip.  Grants
                           a budget bypass for S/A-tier (Layer 1 only).
        """
        now = time.monotonic()
        self._prune(now)
        window_count = len(self._sent_times)

        # ── Layer 2: Flood protection check ───────────────────────────────
        if self._flood_mode:
            elapsed = now - (self._flood_mode_entered_at or now)
            if elapsed >= self._flood_protection_duration:
                logger.info(
                    "TelegramRateLimiter: flood protection expired after %.0fs — "
                    "resuming normal delivery",
                    elapsed,
                )
                self._flood_mode           = False
                self._flood_mode_entered_at = None
                # Re-evaluate without flood mode
            else:
                remaining = self._flood_protection_duration - elapsed
                self._cycle_deferred += 1
                return RateLimitResult(
                    allowed     = False,
                    reason      = (
                        f"flood-protection active "
                        f"({remaining:.0f}s remaining, {window_count} in window)"
                    ),
                    flood_mode  = True,
                    window_count= window_count,
                    window_max  = self._max_per_window,
                )

        # ── Layer 2: Flood threshold gate — enter protection if breached ───
        if window_count >= self._flood_threshold:
            self._enter_flood_mode(now, window_count)
            self._cycle_deferred += 1
            return RateLimitResult(
                allowed     = False,
                reason      = (
                    f"flood-protection engaged "
                    f"({window_count} alerts in {self._window_seconds}s)"
                ),
                flood_mode  = True,
                window_count= window_count,
                window_max  = self._max_per_window,
            )

        # ── Layer 1: Per-window budget ─────────────────────────────────────
        slots_used      = window_count
        slots_remaining = self._max_per_window - slots_used

        if slots_remaining <= 0:
            # Budget exhausted — meaningful-change bypass for S/A only
            tier_rank = _TIER_RANK.get(tier, 3)
            if is_meaningful_change and tier_rank <= 1:
                logger.info(
                    "TelegramRateLimiter: meaningful-change bypass — "
                    "tier=%s conf=%.0f bq=%.0f "
                    "(window=%d/%d, flood-safe)",
                    tier, confidence, bq,
                    window_count, self._max_per_window,
                )
                return RateLimitResult(
                    allowed     = True,
                    reason      = "meaningful-change bypass",
                    window_count= window_count,
                    window_max  = self._max_per_window,
                )
            # Budget exhausted and no bypass
            self._cycle_deferred += 1
            return RateLimitResult(
                allowed     = False,
                reason      = (
                    f"rate-limited "
                    f"({slots_used}/{self._max_per_window} in {self._window_seconds}s)"
                ),
                window_count= window_count,
                window_max  = self._max_per_window,
            )

        # ── Layer 1: Tier-priority slot reservation ────────────────────────
        # When only 1 slot remains, reserve it for S or A.
        # When 2 slots remain, B can use one of them.
        tier_rank = _TIER_RANK.get(tier, 3)
        if tier_rank >= 2 and slots_remaining < 2:   # B/C with ≤1 slot left
            if not is_meaningful_change:
                self._cycle_deferred += 1
                return RateLimitResult(
                    allowed     = False,
                    reason      = (
                        f"rate-limited (tier={tier} — "
                        f"last slot reserved for S/A, {slots_remaining} remaining)"
                    ),
                    window_count= window_count,
                    window_max  = self._max_per_window,
                )

        # ── Allowed ────────────────────────────────────────────────────────
        return RateLimitResult(
            allowed     = True,
            reason      = "",
            window_count= window_count,
            window_max  = self._max_per_window,
        )

    def record_sent(self) -> None:
        """
        Call immediately after a successful broadcast_alert() call.
        Do NOT await anything between check() and record_sent().
        """
        now = time.monotonic()
        self._sent_times.append(now)
        self._cycle_delivered += 1
        self._prune(now)

    def log_cycle_summary(self, job_name: str = "underdog_job") -> None:
        """
        Log a delivery-protection summary if any alerts were deferred this cycle.
        Call at the end of each scan cycle.
        """
        if self._cycle_deferred == 0:
            return

        flood_label = "alert flood" if self._flood_mode else "rate limit"
        logger.warning(
            "\n🚨 Telegram Delivery Protection\n"
            "Reason:               %s\n"
            "Qualified:            %d\n"
            "Delivered:            %d\n"
            "Deferred:             %d\n"
            "Dedup suppressed:     %d\n"
            "Cooldown suppressed:  %d\n"
            "Window:               %d/%d (last %ds)\n"
            "Job:                  %s",
            flood_label,
            self._cycle_qualified,
            self._cycle_delivered,
            self._cycle_deferred,
            self._cycle_dedup,
            self._cycle_cooldown,
            len(self._sent_times),
            self._max_per_window,
            self._window_seconds,
            job_name,
        )

    @property
    def flood_mode(self) -> bool:
        """True while flood-protection mode is active."""
        return self._flood_mode

    @property
    def window_count(self) -> int:
        """Current number of sent alerts inside the sliding window."""
        self._prune(time.monotonic())
        return len(self._sent_times)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _prune(self, now: float) -> None:
        """Remove timestamps older than the window from the left of the deque."""
        cutoff = now - self._window_seconds
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()

    def _enter_flood_mode(self, now: float, window_count: int) -> None:
        if not self._flood_mode:
            self._flood_mode            = True
            self._flood_mode_entered_at = now
            logger.warning(
                "🚨 TelegramRateLimiter: FLOOD PROTECTION ENGAGED — "
                "%d alerts in %ds window (threshold=%d). "
                "Pausing all Telegram delivery for %ds. "
                "Bot continues scanning, scoring, and storing picks.",
                window_count,
                self._window_seconds,
                self._flood_threshold,
                self._flood_protection_duration,
            )


# ── Module-level singleton ────────────────────────────────────────────────────

_limiter: TelegramRateLimiter | None = None


def reset_limiter() -> None:
    """
    Discard the singleton so the next call to get_rate_limiter() creates a fresh one.
    Intended for testing only — never call in production code.
    """
    global _limiter
    _limiter = None


def get_rate_limiter() -> TelegramRateLimiter:
    """Return the process-wide singleton rate limiter, creating it on first call."""
    global _limiter
    if _limiter is None:
        from config import config as _cfg
        _limiter = TelegramRateLimiter(
            window_seconds           = _cfg.TG_RATE_WINDOW_SECONDS,
            max_per_window           = _cfg.TG_RATE_MAX_PER_WINDOW,
            flood_threshold          = _cfg.TG_FLOOD_THRESHOLD,
            flood_protection_duration= _cfg.TG_FLOOD_PROTECTION_DURATION,
        )
        logger.info(
            "TelegramRateLimiter initialised — "
            "max=%d per %ds, flood=%d, protection=%ds",
            _cfg.TG_RATE_MAX_PER_WINDOW,
            _cfg.TG_RATE_WINDOW_SECONDS,
            _cfg.TG_FLOOD_THRESHOLD,
            _cfg.TG_FLOOD_PROTECTION_DURATION,
        )
    return _limiter
