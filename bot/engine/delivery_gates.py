"""Shared delivery qualification rules for actionable prop alerts."""

from __future__ import annotations

from typing import Optional


def tier_delivery_gate(
    sport: str,
    direction: str,
    bq_score: float,
    mq_score: float,
    evidence_available: bool = False,
    confidence: Optional[float] = None,
) -> bool:
    """Return whether an actionable prop has legitimate delivery evidence.

    Score, confidence, tier, and sport-tier values are retained for call-site
    compatibility and reporting only. Timing, scope, football suppression,
    stale-line, provider identity, deduplication, and rate-limit protections
    run independently at their respective delivery boundaries.
    """
    return (
        (direction or "").strip().upper() in {"OVER", "UNDER"}
        and bool(evidence_available)
    )