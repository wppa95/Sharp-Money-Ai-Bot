"""
alert_normalizer.py — build a normalised AlertObject from any alert type.

Factory functions for every alert source:
  normalize_ev(opp)                      → from EVOpportunity
  normalize_steam(alert)                 → from SteamAlert
  normalize_pp(opp)                      → from PPEdgeOpportunity
  normalize_underdog(...)                → from Underdog snap fields
  normalize_multibook_steam(...)         → from market_engine multi-book steam
  normalize_inefficiency(ineff)          → from MarketInefficiency
  normalize_clv(opp)                     → from CLVOpportunity
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from models import (
    AlertObject, AlertSource, AlertTier, AlertType,
    MarketType, Recommendation,
)

if TYPE_CHECKING:
    from models import EVOpportunity, SteamAlert


# ── Source inference ──────────────────────────────────────────────────────────

_BOOK_SOURCE: dict[str, AlertSource] = {
    "DraftKings": AlertSource.DRAFTKINGS,
    "FanDuel":    AlertSource.FANDUEL,
    "PrizePicks": AlertSource.PRIZEPICKS,
    "Underdog":   AlertSource.UNDERDOG,
}

def _source_from_book(book: str) -> AlertSource:
    return _BOOK_SOURCE.get(book, AlertSource.UNKNOWN)

def _source_from_books(books: list[str]) -> AlertSource:
    """Return the first recognised sportsbook source from a list."""
    for b in books:
        src = _BOOK_SOURCE.get(b)
        if src:
            return src
    return AlertSource.UNKNOWN


# ── Tier inference ────────────────────────────────────────────────────────────

_REC_TIER: dict[Recommendation, AlertTier] = {
    Recommendation.STRONG_BET: AlertTier.CRITICAL,
    Recommendation.BET:        AlertTier.HIGH,
    Recommendation.LEAN:       AlertTier.MEDIUM,
    Recommendation.PASS:       AlertTier.LOW,
    Recommendation.FADE:       AlertTier.LOW,
}

def _tier_from_recommendation(rec: Recommendation) -> AlertTier:
    return _REC_TIER.get(rec, AlertTier.LOW)

def _tier_from_steam_score(score: int) -> AlertTier:
    if score >= 90: return AlertTier.CRITICAL
    if score >= 75: return AlertTier.HIGH
    if score >= 60: return AlertTier.MEDIUM
    return AlertTier.LOW

def _tier_from_pp_edge(edge_pct: float) -> AlertTier:
    if edge_pct >= 10: return AlertTier.CRITICAL
    if edge_pct >= 7:  return AlertTier.HIGH
    if edge_pct >= 5:  return AlertTier.MEDIUM
    return AlertTier.LOW


# ── Public factory functions ──────────────────────────────────────────────────

def normalize_ev(opp: "EVOpportunity") -> AlertObject:
    """Normalise a DK/FD EVOpportunity into an AlertObject."""
    return AlertObject(
        source     = _source_from_book(opp.best_book),
        sport      = opp.sport.value,
        market     = opp.market_type.value,
        confidence = float(opp.ai_confidence),
        tier       = _tier_from_recommendation(opp.recommendation),
        reason     = "",
        timestamp  = opp.timestamp,
        alert_type = AlertType.EV_POSITIVE,
        event      = opp.event,
        selection  = opp.ev_result.selection,
    )


def normalize_steam(alert: "SteamAlert") -> AlertObject:
    """Normalise a SteamAlert (single-book) into an AlertObject."""
    return AlertObject(
        source     = _source_from_books(alert.books_moved),
        sport      = alert.sport.value,
        market     = alert.market_type.value,
        confidence = float(alert.steam_score),
        tier       = _tier_from_steam_score(alert.steam_score),
        reason     = "",
        timestamp  = alert.timestamp,
        alert_type = alert.alert_type,
        event      = alert.event,
        selection  = alert.selection,
    )


_PP_SCORE_TIER_TO_ALERT_TIER: dict[str, "AlertTier"] = {}

def _alert_tier_from_pp_score_tier(tier_str: str) -> "AlertTier":
    """Map PPScoreTier value ("S"/"A"/"B"/"PASS") → AlertTier."""
    # Lazily populated to avoid import-time circular issues.
    if not _PP_SCORE_TIER_TO_ALERT_TIER:
        _PP_SCORE_TIER_TO_ALERT_TIER.update({
            "S":    AlertTier.CRITICAL,
            "A":    AlertTier.HIGH,
            "B":    AlertTier.MEDIUM,
            "PASS": AlertTier.LOW,
        })
    return _PP_SCORE_TIER_TO_ALERT_TIER.get(tier_str, AlertTier.LOW)


def normalize_pp(opp, *, score=None) -> AlertObject:
    """Normalise a PPEdgeOpportunity into an AlertObject.

    Args:
        opp:   PPEdgeOpportunity to normalise.
        score: Optional PPAnalysisScore.  When provided its tier and total are
               used for the AlertObject; otherwise falls back to the legacy
               _tier_from_pp_edge / best_edge behaviour.
    """
    pp = opp.pp_line
    selection = f"{pp.player_name} {pp.stat_type} {pp.line_value}"

    if score is not None:
        tier       = _alert_tier_from_pp_score_tier(score.tier)
        confidence = float(score.total)
    else:
        tier       = _tier_from_pp_edge(opp.best_edge)
        confidence = round(float(opp.best_edge), 2)

    return AlertObject(
        source     = AlertSource.PRIZEPICKS,
        sport      = pp.sport,
        market     = MarketType.PLAYER_PROP.value,
        confidence = confidence,
        tier       = tier,
        reason     = "",
        timestamp  = datetime.utcnow(),
        alert_type = AlertType.PRIZEPICKS,
        event      = pp.game_description or "",
        selection  = selection,
    )


def normalize_underdog(
    player: str,
    stat_type: str,
    sport: str,
    is_removed: bool,
    timestamp: datetime | None = None,
) -> AlertObject:
    """Normalise an Underdog prop change into an AlertObject."""
    return AlertObject(
        source     = AlertSource.UNDERDOG,
        sport      = sport,
        market     = MarketType.PLAYER_PROP.value,
        confidence = 0.0,
        tier       = AlertTier.LOW,
        reason     = "",
        timestamp  = timestamp or datetime.utcnow(),
        alert_type = AlertType.UNDERDOG_REMOVED if is_removed else AlertType.UNDERDOG_LINE_CHANGE,
        event      = "",
        selection  = f"{player} {stat_type}",
    )


def normalize_multibook_steam(
    sport: str,
    market_type: str,
    event: str,
    selection: str,
) -> AlertObject:
    """Normalise a multi-book steam result into an AlertObject."""
    return AlertObject(
        source     = AlertSource.SYSTEM,
        sport      = sport,
        market     = market_type,
        confidence = 0.0,
        tier       = AlertTier.LOW,
        reason     = "",
        timestamp  = datetime.utcnow(),
        alert_type = AlertType.MULTI_BOOK_STEAM,
        event      = event,
        selection  = selection,
    )


def normalize_inefficiency(ineff) -> AlertObject:
    """Normalise a MarketInefficiency into an AlertObject."""
    return AlertObject(
        source     = AlertSource.SYSTEM,
        sport      = str(ineff.sport),
        market     = str(ineff.market_type),
        confidence = float(getattr(ineff, "abs_deviation", 0)),
        tier       = AlertTier.LOW,
        reason     = "",
        timestamp  = datetime.utcnow(),
        alert_type = AlertType.MARKET_INEFFICIENCY,
        event      = ineff.event,
        selection  = ineff.selection,
    )


def normalize_clv(opp) -> AlertObject:
    """Normalise a CLVOpportunity into an AlertObject.

    CLV is a system-generated analysis product, not a direct sportsbook alert,
    so it always carries AlertSource.SYSTEM regardless of the reference book.
    """
    return AlertObject(
        source     = AlertSource.SYSTEM,
        sport      = str(getattr(opp, "sport", "")),
        market     = str(getattr(opp, "market_type", "")),
        confidence = float(getattr(opp, "clv_lead", 0)),
        tier       = AlertTier.LOW,
        reason     = "",
        timestamp  = datetime.utcnow(),
        alert_type = AlertType.CLV_OPPORTUNITY,
        event      = getattr(opp, "event", ""),
        selection  = getattr(opp, "selection", ""),
    )
