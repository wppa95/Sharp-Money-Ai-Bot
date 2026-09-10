"""
AlertScopeFilter — final gate before every Telegram delivery.

Single entry point:  check(obj: AlertObject) -> FilterResult

Rules
─────
PrizePicks / Underdog  : always allowed  (player props by definition)
DraftKings / FanDuel   : Moneylines and totals for active sports ✓ | other markets ✗
SteamAlert (any book)  : always blocked  ("sportsbook sharp money alerts")
System alerts          : always blocked  (multi-book steam, inefficiency, CLV)

When blocked, ``obj.reason`` is stamped with the human-readable explanation
so callers get a complete record without needing a second inspection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from models import AlertObject, AlertSource, AlertType, MarketType, Sport
from engine.sport_policy import is_football_related_sport

logger = logging.getLogger(__name__)

# ── Canonical scope rules ─────────────────────────────────────────────────────
# This constant is the single source of truth for the provider priority policy.
# Keep DK/FD limited to moneylines and totals; do not widen this to spreads or props.
#
# Provider order: native Underdog (PRIMARY) → PropLine sources (SUPPORTING)
#
SCOPE_RULES: dict[str, str] = {
    "PrizePicks": "ALLOW — player props, all sports (primary source)",
    "Underdog":   "ALLOW — player props, all sports (secondary source)",
    "DraftKings": "Moneyline (h2h) + totals for active sports only — other markets BLOCKED",
    "FanDuel":    "Moneyline (h2h) + totals for active sports only — other markets BLOCKED",
    "System":     "ALWAYS BLOCKED — multi-book steam, CLV, inefficiency alerts excluded",
}

# ── Allowed DK / FD scope ─────────────────────────────────────────────────────
_APPROVED_MARKETS = frozenset({
    MarketType.MONEYLINE.value,
    MarketType.TOTAL.value,
})

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

    # Football remains in the monitored/tracked universe, but no provider or
    # legacy path may turn it into Telegram betting output.
    if src in (AlertSource.PRIZEPICKS, AlertSource.UNDERDOG) and is_football_related_sport(obj.sport):
        return _block(obj, "Blocked: football-related betting output")

    # ── PrizePicks / Underdog: player props ──────────────────────────────────
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

    # Only moneylines for configured active sports are allowed through the
    # sportsbook EV pipeline.
    try:
        import config as _cfg_mod
        _sport_val = obj.sport.value if hasattr(obj.sport, "value") else str(obj.sport)
        _market_val = (
            obj.market.value if hasattr(obj.market, "value") else str(obj.market)
        )
        if _market_val not in _APPROVED_MARKETS:
            reason = f"Blocked: {obj.sport} {obj.market} — only moneylines are enabled"
            return _block(obj, reason)
        _active_sports = getattr(_cfg_mod.config, "active_sports", None)
        if isinstance(_active_sports, (list, tuple, set, frozenset)) and _active_sports:
            if _sport_val not in _active_sports:
                reason = (
                    f"Blocked: {obj.sport} {obj.market} outside active-sport scope"
                )
                return _block(obj, reason)
    except Exception:
        reason = (
            f"Blocked: {obj.sport} {obj.market} — unable to verify Tier 1 scope"
        )
        return _block(obj, reason)

    return FilterResult(allowed=True)


# ── Cheap line-level pre-filter (call before constructing AlertObject) ────────

def is_ev_line_in_scope(sport: Sport, market_type: MarketType) -> bool:
    """
    Fast pre-filter for raw OddsLine objects.

    The caller already iterates configured active sports. This filter keeps
    the sportsbook pipeline restricted to the explicitly enabled moneyline
    market and does not impose the old Tier-1-only restriction.
    """
    try:
        import config as _cfg_mod
        return market_type.value in _APPROVED_MARKETS
    except Exception:
        return False


# ── Internal ──────────────────────────────────────────────────────────────────

def _block(obj: AlertObject, reason: str) -> FilterResult:
    obj.reason = reason
    logger.warning(
        "AlertScopeFilter [%s | %s] BLOCKED | %s / %s | %s",
        obj.source, obj.alert_type, obj.event, obj.selection, reason,
    )
    return FilterResult(allowed=False, reason=reason)
