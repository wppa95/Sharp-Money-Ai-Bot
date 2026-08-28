"""Provider-independent sport safety policy."""

from __future__ import annotations

import re
from typing import Optional


_FOOTBALL_ALIASES = frozenset(
    {
        "AFL",
        "AFLW",
        "AMERICAN_FOOTBALL",
        "CANADIAN_FOOTBALL",
        "CFB",
        "CFL",
        "COLLEGE_FOOTBALL",
        "FCS",
        "FOOTBALL",
        "GRIDIRON",
        "NCAA",
        "NCAAF",
        "NFL",
        "UFL",
        "USFL",
        "XFL",
    }
)


def canonical_sport_key(sport: Optional[object]) -> str:
    """Normalize provider sport labels without conflating soccer and football."""
    raw = sport.value if hasattr(sport, "value") else str(sport or "")
    return re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")


def is_football_related_sport(sport: Optional[object]) -> bool:
    """Return True for gridiron/football competitions and common aliases."""
    key = canonical_sport_key(sport)
    if not key:
        return False
    return key in _FOOTBALL_ALIASES or "FOOTBALL" in key
