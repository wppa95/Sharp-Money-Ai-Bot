"""
AlertScopeFilter — final gate before every Telegram delivery.

Rules
─────
PrizePicks   : player props only  → always allowed (PP is inherently player-prop)
Underdog     : player props only  → always allowed (all Underdog pick'em are player-level)
DraftKings / FanDuel (EVOpportunity)
             : MLB Moneyline  ✓
             : MLB Totals     ✓
             : everything else ✗
SteamAlert   : ALL blocked        ("sportsbook sharp money alerts")
Multi-book steam, inefficiency, CLV alerts
             : ALL blocked        ("generic betting alerts")

Any blocked call is logged at WARNING with a human-readable reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models import MarketType, Sport

if TYPE_CHECKING:
    from models import EVOpportunity, SteamAlert
    from prizepicks import PPEdgeOpportunity

logger = logging.getLogger(__name__)

# ── Approved DK / FD scope ────────────────────────────────────────────────────
_APPROVED_SPORT       = Sport.MLB
_APPROVED_MARKETS     = frozenset({MarketType.MONEYLINE, MarketType.TOTAL})


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    reason:  str = ""          # non-empty only when blocked


# ── Public helpers ────────────────────────────────────────────────────────────

def check_ev_opportunity(opp: "EVOpportunity") -> FilterResult:
    """
    DraftKings / FanDuel EVOpportunity: allow only MLB Moneyline or MLB Total.
    """
    sport  = opp.sport
    mtype  = opp.market_type

    if sport != _APPROVED_SPORT:
        reason = (
            f"Blocked: {sport.value} {mtype.value} outside approved scope "
            f"(DK/FD only: MLB Moneyline / MLB Totals)"
        )
        _log_blocked("EV", opp.event, opp.ev_result.selection, reason)
        return FilterResult(allowed=False, reason=reason)

    if mtype not in _APPROVED_MARKETS:
        reason = (
            f"Blocked: MLB {mtype.value} outside approved scope "
            f"(DK/FD only: MLB Moneyline / MLB Totals)"
        )
        _log_blocked("EV", opp.event, opp.ev_result.selection, reason)
        return FilterResult(allowed=False, reason=reason)

    return FilterResult(allowed=True)


def check_steam_alert(alert: "SteamAlert") -> FilterResult:
    """
    All SteamAlerts are blocked — sportsbook sharp money alerts are outside scope.
    """
    reason = (
        f"Blocked: sportsbook sharp money alert outside allowed scope "
        f"({alert.sport.value} {alert.market_type.value} — {alert.selection})"
    )
    _log_blocked("Steam", alert.event, alert.selection, reason)
    return FilterResult(allowed=False, reason=reason)


def check_pp_opportunity(opp: "PPEdgeOpportunity") -> FilterResult:
    """
    PrizePicks player-prop opportunities are always allowed.
    """
    return FilterResult(allowed=True)


def check_underdog_alert(player: str, stat_type: str, sport: str) -> FilterResult:
    """
    Underdog pick'em prop changes are always allowed (inherently player-level).
    """
    return FilterResult(allowed=True)


def check_multibook_steam(sport: str, market_type: str, event: str, selection: str) -> FilterResult:
    """
    Multi-book steam / sportsbook consensus alerts are outside scope.
    """
    reason = (
        f"Blocked: sportsbook sharp money alert outside allowed scope "
        f"({sport} {market_type} — {event} / {selection})"
    )
    _log_blocked("MultiBookSteam", event, selection, reason)
    return FilterResult(allowed=False, reason=reason)


def check_inefficiency_alert(sport: str, market_type: str, event: str, selection: str) -> FilterResult:
    """
    Market inefficiency alerts are outside scope.
    """
    reason = (
        f"Blocked: market inefficiency alert outside allowed scope "
        f"({sport} {market_type} — {event} / {selection})"
    )
    _log_blocked("Inefficiency", event, selection, reason)
    return FilterResult(allowed=False, reason=reason)


def check_clv_alert(event: str, selection: str, sportsbook: str) -> FilterResult:
    """
    CLV opportunity alerts are outside scope.
    """
    reason = (
        f"Blocked: CLV opportunity alert outside allowed scope "
        f"({sportsbook} — {event} / {selection})"
    )
    _log_blocked("CLV", event, selection, reason)
    return FilterResult(allowed=False, reason=reason)


# ── Internal ──────────────────────────────────────────────────────────────────

def _log_blocked(alert_kind: str, event: str, selection: str, reason: str) -> None:
    logger.warning(
        "AlertScopeFilter [%s] BLOCKED | %s / %s | %s",
        alert_kind, event, selection, reason,
    )
