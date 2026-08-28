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

    Qualification is directional only.  The remaining arguments are retained
    for call-site compatibility and reporting, but score/tier/evidence values
    must not become hidden delivery gates.  Timing, scope, stale-line,
    provider, deduplication, and rate-limit protections run independently.
    """
    return (direction or "").strip().upper() in {"OVER", "UNDER"}