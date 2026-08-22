"""
Canonical Identity Layer — Sharp Money Bot Framework v3.0 Layer 1.

Provides stable, normalised identifiers for players, markets, and events
across all data providers.  All identity resolution flows through this module.

Rules
─────
• normalize_player_name() is the single source of truth for player key derivation.
• player_key() always includes sport to prevent cross-sport collisions.
• event_key() is order-independent (teams are sorted before joining).
• normalize_stat() delegates to engine.pp_reference for canonical stat names.

Nothing in this module touches the database, network, or any other engine module
except the one-way import of pp_reference.normalize_stat_for_pp.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Canonical identity dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CanonicalPlayer:
    """Stable identity for a player across providers."""

    key:          str        # "{sport_upper}:{normalized_name}"
    display_name: str        # best known display form (from source)
    sport:        str        # normalised sport string (uppercase)
    aliases:      frozenset  # known alternate spellings / abbreviations

    def matches(self, raw_name: str) -> bool:
        """Return True if *raw_name* resolves to this player's key component."""
        return normalize_player_name(raw_name) in {
            normalize_player_name(a) for a in self.aliases | {self.display_name}
        }


@dataclass(frozen=True)
class CanonicalMarket:
    """Stable identity for a prop market across providers."""

    stat_key:     str           # normalised stat identifier (e.g. "hits")
    stat_display: str           # human-readable name (e.g. "Hits")
    sport:        str           # normalised sport string (uppercase)
    line:         Optional[float]   # current line value; None = market-only identity
    provider:     str           # originating provider name


@dataclass(frozen=True)
class CanonicalEvent:
    """Stable identity for a game/match across providers."""

    key:       str           # "{sport}:{date}:{team_a}_vs_{team_b}" (sorted teams)
    sport:     str           # normalised sport string (uppercase)
    date_str:  str           # YYYY-MM-DD
    home_team: Optional[str]
    away_team: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation functions
# ─────────────────────────────────────────────────────────────────────────────

def normalize_player_name(raw: str) -> str:
    """Return a stable lowercase key for a player name.

    Steps
    -----
    1.  Decompose unicode → strip combining characters (accents, diacritics).
    2.  Lowercase and strip surrounding whitespace.
    3.  Remove all non-alphanumeric characters except spaces
        (dots, apostrophes, hyphens, commas are removed and their letters merged).
    4.  Collapse runs of whitespace to underscores.

    Examples::

        normalize_player_name("LeBron James")   → "lebron_james"
        normalize_player_name("Rondón, José")   → "rondon_jose"
        normalize_player_name("A.J. Brown")     → "aj_brown"
        normalize_player_name("D'Angelo Russell")→ "dangelo_russell"
    """
    if not raw:
        return ""
    # Strip residual "None " prefix from older Underdog null-name payloads
    if isinstance(raw, str) and raw.startswith("None "):
        raw = raw[5:].strip() or raw
    # 1. Strip accents via NFD decomposition
    decomposed    = unicodedata.normalize("NFD", raw)
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    # 2. Lowercase + strip outer whitespace
    lower = without_marks.lower().strip()
    # 3. Keep only a-z, 0-9, underscores, and spaces.
    #    Dots/apostrophes/hyphens/commas are removed (adjacent letters merge).
    #    Underscores are preserved so the function is idempotent on already-
    #    normalised keys (e.g. "lebron_james" stays "lebron_james" on a second pass).
    cleaned = re.sub(r"[^a-z0-9_ ]", "", lower)
    # 4. Collapse whitespace → underscore
    keyed = re.sub(r"\s+", "_", cleaned).strip("_")
    return keyed


def player_key(name: str, sport: str) -> str:
    """Produce a globally unique, stable player key.

    Format: ``"{SPORT_UPPER}:{normalized_name}"``

    Examples::

        player_key("Mike Trout", "MLB")   → "MLB:mike_trout"
        player_key("Ja Morant",  "NBA")   → "NBA:ja_morant"
    """
    return f"{sport.upper()}:{normalize_player_name(name)}"


def normalize_stat(raw: str) -> str:
    """Return the canonical snake_case stat key for *raw*.

    Delegates to ``engine.pp_reference.normalize_stat_for_pp`` for the
    lookup table, then converts the result to lowercase underscore form
    for use as a dict / DB key.

    Examples::

        normalize_stat("Hits")              → "hits"
        normalize_stat("Fantasy Points")    → "fantasy_points"
        normalize_stat("Kills on Maps 1+2") → "kills_on_maps_1+2"
    """
    from engine.pp_reference import normalize_stat_for_pp  # one-way, no circular risk
    canonical = normalize_stat_for_pp(raw)
    return canonical.lower().replace(" ", "_")


def event_key(sport: str, date_str: str, team_a: str, team_b: str) -> str:
    """Produce a stable event key independent of home/away team ordering.

    Teams are sorted lexicographically so the key is identical regardless
    of which team is home or away.

    Format: ``"{sport}:{date}:{team_a}_vs_{team_b}"``

    Examples::

        event_key("MLB", "2026-08-01", "Red Sox", "Yankees")
        → "mlb:2026-08-01:red_sox_vs_yankees"

        event_key("MLB", "2026-08-01", "Yankees", "Red Sox")
        → "mlb:2026-08-01:red_sox_vs_yankees"   # identical
    """
    teams = tuple(sorted([normalize_player_name(team_a), normalize_player_name(team_b)]))
    return f"{sport.lower()}:{date_str}:{teams[0]}_vs_{teams[1]}"
