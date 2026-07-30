"""
engine/player_results.py — Player game-result hit-rate computation.

Converts raw per-game result records into per-window hit-rate statistics
(L5 / L10 / L20 / L30 / Season / H2H) that feed the betting decision engine.

No external I/O — purely deterministic computation from stored records.

Public API
──────────
  WindowStats          — immutable per-window statistics
  PlayerHitRates       — all windows for one player × stat × line combination
  compute_hit_rates()  — compute all windows from a list of PlayerGameResult rows
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import PlayerGameResult

logger = logging.getLogger(__name__)

# Minimum H2H appearances to compute the H2H window
H2H_MIN_GAMES: int = 3


# ── Per-window statistics ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowStats:
    """
    Summary statistics for one rolling window (L5, L10, season, etc.).

    hit_rate is over_count / games.  A push (actual == line) is counted as
    UNDER so that OVER hit-rates are conservative.
    """
    games:     int
    over_count: int
    under_count: int
    hit_rate:  float   # over_count / games;  0.0–1.0
    average:   float   # mean actual_value across games in this window

    def over_display(self) -> str:
        """e.g. '4/5 (80%)  avg 2.8'"""
        if self.games == 0:
            return "N/A"
        return (
            f"{self.over_count}/{self.games}"
            f" ({self.hit_rate:.0%})"
            f"  avg {self.average:.1f}"
        )


# ── Aggregate container ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlayerHitRates:
    """
    All rolling-window hit rates for a specific player × stat × line.

    ``has_real_data`` is True when at least one game result is available.
    ``total_games`` counts all stored results regardless of window.
    """
    player_name:  str
    stat_type:    str
    current_line: float

    l5:     Optional[WindowStats]   # most recent 5 games
    l10:    Optional[WindowStats]   # most recent 10 games
    l20:    Optional[WindowStats]   # most recent 20 games
    l30:    Optional[WindowStats]   # most recent 30 games
    season: Optional[WindowStats]   # all available records (full season)
    h2h:    Optional[WindowStats]   # vs specific opponent (None if unknown/few)

    has_real_data: bool
    total_games:   int


# ── Computation ───────────────────────────────────────────────────────────────

def compute_hit_rates(
    results: "list[PlayerGameResult]",
    current_line: float,
    opponent: Optional[str] = None,
    h2h_min_games: int = H2H_MIN_GAMES,
) -> PlayerHitRates:
    """
    Compute L5 / L10 / L20 / L30 / season / H2H statistics from DB records.

    Parameters
    ----------
    results:
        Records from ``Database.get_player_results()``, ordered most-recent-first.
        Each record must have ``actual_value``, ``game_date``, and ``opponent``
        attributes.
    current_line:
        Current prop line — used to classify each game as OVER / UNDER.
    opponent:
        Current game opponent abbreviation or name (e.g. "BOS", "Boston Red Sox").
        When provided and enough H2H games exist, an H2H window is computed.
    h2h_min_games:
        Minimum H2H appearances required to compute the H2H window.

    Returns
    -------
    PlayerHitRates with all available windows populated.
    """
    if not results:
        return PlayerHitRates(
            player_name  = "",
            stat_type    = "",
            current_line = current_line,
            l5           = None,
            l10          = None,
            l20          = None,
            l30          = None,
            season       = None,
            h2h          = None,
            has_real_data = False,
            total_games  = 0,
        )

    # Sort newest first (already expected to be sorted, but enforce)
    sorted_results = sorted(
        results,
        key=lambda r: r.game_date if isinstance(r.game_date, str) else r.game_date.isoformat(),
        reverse=True,
    )

    def _window_stats(subset: list) -> Optional[WindowStats]:
        if not subset:
            return None
        values = [r.actual_value for r in subset if r.actual_value is not None]
        if not values:
            return None
        n   = len(values)
        # Push (actual == line exactly) counted as UNDER
        oc  = sum(1 for v in values if v > current_line)
        uc  = n - oc
        avg = sum(values) / n
        return WindowStats(
            games       = n,
            over_count  = oc,
            under_count = uc,
            hit_rate    = oc / n,
            average     = round(avg, 2),
        )

    l5_stats  = _window_stats(sorted_results[:5])
    l10_stats = _window_stats(sorted_results[:10])
    l20_stats = _window_stats(sorted_results[:20])
    l30_stats = _window_stats(sorted_results[:30])
    season_stats = _window_stats(sorted_results)

    # H2H — only compute when opponent known and sufficient matchups exist
    h2h_stats: Optional[WindowStats] = None
    if opponent:
        h2h_subset = [
            r for r in sorted_results
            if r.opponent is not None and _fuzzy_team_match(r.opponent, opponent)
        ]
        if len(h2h_subset) >= h2h_min_games:
            h2h_stats = _window_stats(h2h_subset)

    # Determine player_name / stat_type from first record
    first = sorted_results[0]
    player_name = getattr(first, "player_name", "")
    stat_type   = getattr(first, "stat_type",   "")

    return PlayerHitRates(
        player_name   = player_name,
        stat_type     = stat_type,
        current_line  = current_line,
        l5            = l5_stats,
        l10           = l10_stats,
        l20           = l20_stats,
        l30           = l30_stats,
        season        = season_stats,
        h2h           = h2h_stats,
        has_real_data = True,
        total_games   = len(sorted_results),
    )


# ── H2H team name fuzzy matching ─────────────────────────────────────────────

def _fuzzy_team_match(stored: str, query: str, threshold: float = 0.6) -> bool:
    """
    Return True if *stored* and *query* look like the same team.

    Handles abbreviation vs full name: "BOS" vs "Boston Red Sox".
    """
    s = stored.strip().lower()
    q = query.strip().lower()

    # Exact or abbreviation match
    if s == q:
        return True

    # One is a substring of the other (e.g. "bos" in "boston red sox")
    if s in q or q in s:
        return True

    # Fuzzy ratio for partial-name matches
    ratio = SequenceMatcher(None, s, q).ratio()
    return ratio >= threshold
