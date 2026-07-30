"""
AlertScopeFilter — final gate before every Telegram delivery.

Single entry point:  check(obj: AlertObject) -> FilterResult

Rules
─────
PrizePicks / Underdog  : always allowed  (player props by definition)
DraftKings / FanDuel   : MLB Moneyline ✓ | MLB Totals ✓ | everything else ✗
SteamAlert (any book)  : always blocked  ("sportsbook sharp money alerts")
System alerts          : always blocked  (multi-book steam, inefficiency, CLV)

When blocked, ``obj.reason`` is stamped with the human-readable explanation
so callers get a complete record without needing a second inspection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from models import AlertObject, AlertSource, AlertType, MarketType, Sport

logger = logging.getLogger(__name__)

# ── Canonical scope rules ─────────────────────────────────────────────────────
# This constant is the single source of truth for the provider priority policy.
# Never widen the DK/FD scope without also updating the tests.
#
# Priority order: PrizePicks (PRIMARY) → Underdog (SECONDARY) → DK/FD (SUPPORTING ONLY)
#
SCOPE_RULES: dict[str, str] = {
    "PrizePicks": "ALWAYS PASS — player props, all sports (primary source)",
    "Underdog":   "ALWAYS PASS — player props, all sports (secondary source)",
    "DraftKings": "MLB Moneyline (h2h) + MLB Totals only — all other sports/markets BLOCKED",
    "FanDuel":    "MLB Moneyline (h2h) + MLB Totals only — all other sports/markets BLOCKED",
    "System":     "ALWAYS BLOCKED — multi-book steam, CLV, inefficiency alerts excluded",
}

# ── Allowed DK / FD scope ─────────────────────────────────────────────────────
_APPROVED_SPORT   = Sport.MLB.value
_APPROVED_MARKETS = frozenset({MarketType.MONEYLINE.value, MarketType.TOTAL.value})

# AlertType values that are always blocked regardless of source
_STEAM_ALERT_TYPES = frozenset({
    AlertType.STEAM.value,
    AlertType.SHARP.value,
    AlertType.MULTI_BOOK_STEAM.value,
})


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    reason:  str = ""   # non-empty only when blocked


# ── Public entry point ────────────────────────────────────────────────────────

def check(obj: AlertObject) -> FilterResult:
    """
    Evaluate scope rules against a normalised AlertObject.

    Side-effect: stamps ``obj.reason`` with the block explanation when blocked.
    """
    src = obj.source

    # ── PrizePicks / Underdog: player props — always pass ────────────────────
    if src in (AlertSource.PRIZEPICKS, AlertSource.UNDERDOG):
        return FilterResult(allowed=True)

    # ── System alerts (multi-book steam, inefficiency, CLV): always block ────
    if src == AlertSource.SYSTEM:
        reason = (
            f"Blocked: {obj.alert_type.value} outside allowed scope "
            f"({obj.sport} {obj.market} — {obj.event} / {obj.selection})"
        )
        return _block(obj, reason)

    # ── DraftKings / FanDuel ─────────────────────────────────────────────────

    # All steam / sharp-money moves are blocked regardless of sport
    if obj.alert_type in _STEAM_ALERT_TYPES:
        reason = (
            f"Blocked: sportsbook sharp money alert outside allowed scope "
            f"({obj.sport} {obj.market} — {obj.selection})"
        )
        return _block(obj, reason)

    # Non-MLB sport
    if obj.sport != _APPROVED_SPORT:
        reason = (
            f"Blocked: {obj.sport} {obj.market} outside approved scope "
            f"(DK/FD only: MLB Moneyline / MLB Totals)"
        )
        return _block(obj, reason)

    # MLB but wrong market type
    if obj.market not in _APPROVED_MARKETS:
        reason = (
            f"Blocked: MLB {obj.market} outside approved scope "
            f"(DK/FD only: MLB Moneyline / MLB Totals)"
        )
        return _block(obj, reason)

    return FilterResult(allowed=True)


# ── Cheap line-level pre-filter (call before constructing AlertObject) ────────

def is_ev_line_in_scope(sport: Sport, market_type: MarketType) -> bool:
    """
    Fast pre-filter for raw OddsLine objects — call this before any DB write
    or analysis-engine call to drop data that can never pass ``check()``.

    Rules mirror the DK/FD block in ``check()``:
      • Only ``Sport.MLB`` + (``MarketType.MONEYLINE`` or ``MarketType.TOTAL``) → True
      • Everything else → False
      • PrizePicks / Underdog lines are handled by their own pipelines;
        do not pass them here.
    """
    return sport.value == _APPROVED_SPORT and market_type.value in _APPROVED_MARKETS


# ── Internal ──────────────────────────────────────────────────────────────────

def _block(obj: AlertObject, reason: str) -> FilterResult:
    obj.reason = reason
    logger.warning(
        "AlertScopeFilter [%s | %s] BLOCKED | %s / %s | %s",
        obj.source, obj.alert_type, obj.event, obj.selection, reason,
    )
    return FilterResult(allowed=False, reason=reason)
