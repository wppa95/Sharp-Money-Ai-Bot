"""Shared delivery qualification rules for actionable prop alerts."""

from __future__ import annotations

def directional_evidence_gate(direction: str, evidence_available: bool = False) -> bool:
    """Return whether a prop has a direction and legitimate evidence.

    Delivery is intentionally independent of score, tier, bet quality, and
    market quality. Those classifications are not part of the active alert
    framework.
    """
    return (
        (direction or "").strip().upper() in {"OVER", "UNDER"}
        and bool(evidence_available)
    )