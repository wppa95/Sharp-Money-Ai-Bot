"""Shared delivery qualification rules for actionable Underdog alerts."""

from __future__ import annotations

from typing import Optional


# Tier 2 is an exact alias set.  Every other value, including unknown future
# sports and CFL, is Tier 1.  Exact aliases avoid accidentally classifying a
# sport merely because its name contains "football".
TIER2_SPORTS: frozenset[str] = frozenset(
    {"MLB", "NBA", "NFL", "FOOTBALL", "CFB", "NCAA", "NCAAF"}
)


def is_tier2_sport(sport: Optional[str]) -> bool:
    """Return whether a sport uses the stricter Telegram delivery gate."""
    return (sport or "").strip().upper() in TIER2_SPORTS


def tier_delivery_gate(
    sport: str,
    direction: str,
    bq_score: float,
    mq_score: float,
    evidence_available: bool = False,
    confidence: Optional[float] = None,
) -> bool:
    """Return whether an actionable prop may reach Telegram.

    Tier 1 requires only a real direction and strong supporting evidence.
    Tier 2 additionally requires strong BQ and MQ plus a
    high-confidence decision.  Decision labels such as S/A/B/C are not used
    as delivery gates; they remain evaluation and reporting metadata.
    """
    if (direction or "").upper() not in {"OVER", "UNDER"}:
        return False
    if not evidence_available:
        return False
    if not is_tier2_sport(sport):
        return True
    return (
        float(confidence or 0.0) >= 90.0
        and float(bq_score or 0.0) >= 85.0
        and float(mq_score or 0.0) >= 85.0
    )