"""Provider-independent sport safety policy."""

from __future__ import annotations

import re
from typing import Optional


_FOOTBALL_ALIASES = frozenset(
    {
        "AFL",
        "AFLW",
        "AUSSIE_RULES",
        "AUSTRALIAN_RULES",
        "AMERICAN_FOOTBALL",
        "AMERICANFOOTBALL",
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
        "RUGBY",
        "UFL",
        "USFL",
        "XFL",
    }
)

_ALLOWED_FOOTBALL_ALIASES = frozenset(
    {
        "NFL",
        "FOOTBALL_NFL",
        "AMERICANFOOTBALL_NFL",
        "AMERICAN_FOOTBALL_NFL",
        "CFB",
        "NCAAF",
        "FOOTBALL_NCAAF",
        "AMERICANFOOTBALL_NCAAF",
        "AMERICAN_FOOTBALL_NCAAF",
    }
)


def canonical_sport_key(sport: Optional[object]) -> str:
    """Normalize provider sport labels without conflating soccer and football."""
    raw = sport.value if hasattr(sport, "value") else str(sport or "")
    key = re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")
    key = {
        "CS2": "CS", "CSGO": "CS", "CS_GO": "CS",
        "COUNTER_STRIKE": "CS", "COUNTER_STRIKE_2": "CS",
        "DOTA2": "DOTA", "DOTA_2": "DOTA",
        "LEAGUE_OF_LEGENDS": "LOL", "VALORANT": "VAL",
        "TABLE_TENNIS": "TABLETENNIS",
    }.get(key, key)
    # Keep aliases on one evidence/delivery identity.  Soccer is deliberately
    # not included: a provider's generic FOOTBALL label means gridiron, while
    # SOCCER/ASSOCIATION_FOOTBALL remains its own sport.
    if key in {"CFB", "NCAA", "COLLEGE_FOOTBALL", "FCS"} or key.endswith("_NCAAF"):
        return "NCAAF"
    if key in {"FOOTBALL", "GRIDIRON", "AMERICAN_FOOTBALL", "AMERICANFOOTBALL"}:
        return "NFL"
    if key == "BASEBALL":
        return "MLB"
    if key in {"ASSOCIATION_FOOTBALL", "FOOTBALL_SOCCER"}:
        return "SOCCER"
    return key


def player_sport_key(sport: Optional[object]) -> str:
    """Unify esports identity while preserving established football labels."""
    raw = sport.value if hasattr(sport, "value") else str(sport or "")
    canonical = canonical_sport_key(raw)
    return canonical if canonical in {"CS", "DOTA", "LOL", "VAL"} else raw.strip().upper()


def player_sport_aliases(sport: Optional[object]) -> tuple[str, ...]:
    """Read legacy aliases without changing or migrating stored evidence."""
    key = player_sport_key(sport)
    aliases = {
        "CS": ("CS", "CS2", "CSGO", "CS_GO", "COUNTER_STRIKE_2", "COUNTER-STRIKE 2"),
        "DOTA": ("DOTA", "DOTA2", "DOTA_2", "DOTA 2"),
        "LOL": ("LOL", "LEAGUE_OF_LEGENDS", "LEAGUE OF LEGENDS"),
        "VAL": ("VAL", "VALORANT"),
    }
    return aliases.get(key, (key,))


def is_football_related_sport(sport: Optional[object]) -> bool:
    """Return True for gridiron/football competitions and common aliases."""
    key = canonical_sport_key(sport)
    if not key:
        return False
    # Some feeds call association football simply "football"; never merge
    # those rows with NFL/CFB evidence or delivery identity.
    if key == "SOCCER":
        return False
    return (
        key in _FOOTBALL_ALIASES
        or "FOOTBALL" in key
        or key.startswith("RUGBY")
        or key.startswith("AUSSIE_RULES")
        or key.startswith("AUSTRALIAN_RULES")
        or key.endswith("_AFL")
        or key.endswith("_AFLW")
    )


def is_disallowed_football_sport(sport: Optional[object]) -> bool:
    """Return True for football-family sports outside NFL and CFB."""
    raw = sport.value if hasattr(sport, "value") else str(sport or "")
    raw_key = re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")
    if raw_key in _ALLOWED_FOOTBALL_ALIASES:
        return False
    return is_football_related_sport(sport)
