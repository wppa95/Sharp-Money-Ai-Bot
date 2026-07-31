"""
engine/score_validation.py — Score clamping helpers.

Ensures scoring values are kept within valid ranges before being persisted.
A WARNING is emitted the first time a value is clamped so misconfigurations
in scoring components surface clearly in logs.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def clamp_score(
    value: Optional[float],
    label: str,
    min_: float = 0.0,
    max_: float = 100.0,
) -> Optional[float]:
    """
    Clamp *value* to [min_, max_] and log a WARNING if clamping was needed.

    Returns None unchanged so callers that accept Optional[float] don't need
    a None-guard before calling this.

    Args:
        value:  The raw score to validate (may be None).
        label:  Human-readable description used in the warning log.
        min_:   Lower bound (inclusive).  Default 0.
        max_:   Upper bound (inclusive).  Default 100.

    Returns:
        The clamped value, or None if value is None.

    Example::

        score.total = clamp_score(score.total, "ud_score.total")
    """
    if value is None:
        return None

    if value < min_:
        logger.warning(
            "score_validation: %s=%s is below minimum %s — clamped to %s",
            label, value, min_, min_,
        )
        return min_

    if value > max_:
        logger.warning(
            "score_validation: %s=%s is above maximum %s — clamped to %s",
            label, value, max_, max_,
        )
        return max_

    return value
