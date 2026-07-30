"""
engine/slip_builder.py — Multi-size PrizePicks slip builder.

Public API
──────────
  build_all_slips(candidates, max_size=6) -> dict[int, OptimizedSlip]

Wraps the existing slip_optimizer.optimize_slip() and calls it once per
desired leg count (2..max_size).  The optimizer's correlation-filtering
logic and scoring are unchanged — this module only adds the multi-size
fan-out so /slip can show all five sizes simultaneously.

Each returned OptimizedSlip is fully independent — the same candidate can
appear in multiple sizes.  Sizes for which the optimizer returns fewer than
2 confirmed legs (e.g. insufficient candidates) are omitted from the result.

Usage
─────
  from engine.slip_builder import build_all_slips
  slips = build_all_slips(candidates, max_size=6)
  # slips[2] → best 2-man, slips[3] → best 3-man, …
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import PPEdgeRecord
    from engine.slip_optimizer import OptimizedSlip

logger = logging.getLogger(__name__)


def build_all_slips(
    candidates: "list[PPEdgeRecord]",
    max_size: int = 6,
) -> "dict[int, OptimizedSlip]":
    """
    Build optimized slips for every leg count from 2 to *max_size*.

    Parameters
    ----------
    candidates:  Pool of PPEdgeRecord objects (typically 20–30 recent picks).
    max_size:    Largest slip to attempt (2–6, capped at 6).

    Returns
    -------
    dict mapping leg-count → OptimizedSlip.
    Sizes where the optimizer could not fill ≥ 2 legs are omitted.
    """
    from engine.slip_optimizer import optimize_slip  # frozen — import at call time

    max_size = min(max(max_size, 2), 6)
    results: dict[int, OptimizedSlip] = {}

    for n in range(2, max_size + 1):
        try:
            slip = optimize_slip(candidates, n_legs=n)
        except Exception as exc:
            logger.warning("slip_builder: optimize_slip(%d) failed — %s", n, exc)
            continue

        if len(slip.legs) >= 2:
            results[n] = slip
            logger.debug(
                "slip_builder: %d-man slip built (%d legs, avg_edge=%.1f%%)",
                n, len(slip.legs), slip.avg_edge or 0.0,
            )
        else:
            logger.debug(
                "slip_builder: %d-man slip skipped (only %d legs returned)",
                n, len(slip.legs),
            )

    return results
