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
import math
from dataclasses import dataclass
from datetime import date
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

    hit_rate is over_count / games. Pushes are completed games, but are not
    hits for either direction; strict UNDER counts are stored independently.
    """
    games:     int
    over_count: int
    under_count: int
    hit_rate:  float   # over_count / games;  0.0–1.0
    average:   float   # mean actual_value across games in this window
    push_count: int = 0

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
    l15:    Optional[WindowStats] = None  # most recent 15 games

    # Evidence provenance.  Supplemental rows remain eligible for the existing
    # decision engine, but callers can distinguish them from primary-provider
    # game results without inspecting the input list.
    data_source: str = "primary"
    n_supplemental_games: int = 0
    supplemental_sources: tuple[str, ...] = ()
    # Recent opponent-specific windows. ``h2h`` remains the aggregate sample
    # for backward compatibility.
    h2h_l5: Optional[WindowStats] = None
    h2h_l10: Optional[WindowStats] = None
    h2h_l20: Optional[WindowStats] = None
    # Most-recent settled actuals, retained for evidence display only. These
    # values do not participate in direction or qualification calculations.
    recent_values: tuple[float, ...] = ()
    # Actual results for every available displayed sample window, newest first.
    # Keys include l5/l10/l15/l20/l30/season and the corresponding h2h windows.
    sample_values: tuple[tuple[str, tuple[float, ...]], ...] = ()


def directional_window_counts(window, direction: str) -> tuple[int, int, int] | None:
    """One directional numerator/denominator contract for all history views.

    Pushes remain completed samples but are never OVER or UNDER hits.
    Missing strict UNDER counts are unknown, never the complement of OVER.
    """
    if window is None or direction not in {"OVER", "UNDER"}:
        return None
    def value(name, short, default=None):
        if isinstance(window, dict):
            return window.get(name, window.get(short, default))
        return getattr(window, name, default)
    n = value("games", "n")
    hits = value("over_count" if direction == "OVER" else "under_count",
                 "o" if direction == "OVER" else "u")
    pushes = value("push_count", "p", 0)
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           for v in (n, hits, pushes)):
        return None
    if not all(math.isfinite(v) and int(v) == v for v in (n, hits, pushes)):
        return None
    if n <= 0 or hits < 0 or pushes < 0 or hits + pushes > n:
        return None
    return int(hits), int(n), int(pushes)


# ── Computation ───────────────────────────────────────────────────────────────

def compute_hit_rates(
    results: "list[PlayerGameResult]",
    current_line: float,
    opponent: Optional[str] = None,
    h2h_min_games: int = H2H_MIN_GAMES,
) -> PlayerHitRates:
    """
    Compute L5 / L10 / L15 / L20 / L30 / season / H2H statistics from DB records.

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
    # Remove invalid/DNP/non-final rows before slicing rolling windows. A
    # missing recent result must not hide an older valid completed game.
    valid_results = []
    for row in results:
        value = getattr(row, "actual_value", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        if getattr(row, "is_dnp", False) is True or getattr(row, "did_play", None) is False:
            continue
        state = str(getattr(row, "status", "") or getattr(row, "game_status", "") or "").lower()
        if state in {"dnp", "scheduled", "live", "in_progress", "cancelled", "postponed", "void"}:
            continue
        if not getattr(row, "game_date", None):
            continue
        valid_results.append(row)
    if not valid_results:
        return PlayerHitRates(
            player_name  = "",
            stat_type    = "",
            current_line = current_line,
            l5           = None,
            l10          = None,
            l15          = None,
            l20          = None,
            l30          = None,
            season       = None,
            h2h          = None,
            has_real_data = False,
            total_games  = 0,
        )

    # Sort newest first (already expected to be sorted, but enforce)
    sorted_results = sorted(
        valid_results,
        key=lambda r: r.game_date if isinstance(r.game_date, str) else r.game_date.isoformat(),
        reverse=True,
    )

    def _window_stats(subset: list, required_games: int) -> Optional[WindowStats]:
        # A rolling window is only valid when the complete requested sample is
        # available.  Never label 15 games as L20 or 16 games as L30.
        if len(subset) < required_games:
            return None
        values = [r.actual_value for r in subset if r.actual_value is not None]
        if len(values) < required_games:
            return None
        n   = len(values)
        # A push must never become an UNDER win by subtraction.
        oc  = sum(1 for v in values if v > current_line)
        uc  = sum(1 for v in values if v < current_line)
        avg = sum(values) / n
        return WindowStats(
            games       = n,
            over_count  = oc,
            under_count = uc,
            hit_rate    = oc / n,
            average     = round(avg, 2),
            push_count  = n - oc - uc,
        )

    def _window_values(
        subset: list,
        stats: Optional[WindowStats],
    ) -> tuple[float, ...]:
        if stats is None:
            return ()
        return tuple(
            float(row.actual_value)
            for row in subset
            if row.actual_value is not None
        )

    l5_stats  = _window_stats(sorted_results[:5], 5)
    l10_stats = _window_stats(sorted_results[:10], 10)
    l15_stats = _window_stats(sorted_results[:15], 15)
    l20_stats = _window_stats(sorted_results[:20], 20)
    l30_stats = _window_stats(sorted_results[:30], 30)
    season_stats = _window_stats(sorted_results, 1)

    # H2H — only compute when opponent known and sufficient matchups exist
    h2h_stats: Optional[WindowStats] = None
    h2h_l5_stats: Optional[WindowStats] = None
    h2h_l10_stats: Optional[WindowStats] = None
    h2h_l20_stats: Optional[WindowStats] = None
    if opponent:
        h2h_subset = [
            r for r in sorted_results
            if r.opponent is not None and _fuzzy_team_match(r.opponent, opponent)
        ]
        if len(h2h_subset) >= h2h_min_games:
            h2h_stats = _window_stats(h2h_subset, h2h_min_games)
        h2h_l5_stats = _window_stats(h2h_subset[:5], 5)
        h2h_l10_stats = _window_stats(h2h_subset[:10], 10)
        h2h_l20_stats = _window_stats(h2h_subset[:20], 20)

    sample_values = {
        "l5": _window_values(sorted_results[:5], l5_stats),
        "l10": _window_values(sorted_results[:10], l10_stats),
        "l15": _window_values(sorted_results[:15], l15_stats),
        "l20": _window_values(sorted_results[:20], l20_stats),
        "l30": _window_values(sorted_results[:30], l30_stats),
        "season": _window_values(sorted_results, season_stats),
        "h2h_l5": _window_values(
            h2h_subset[:5] if opponent else [], h2h_l5_stats
        ),
        "h2h_l10": _window_values(
            h2h_subset[:10] if opponent else [], h2h_l10_stats
        ),
        "h2h_l20": _window_values(
            h2h_subset[:20] if opponent else [], h2h_l20_stats
        ),
        "h2h": _window_values(
            h2h_subset if opponent else [], h2h_stats
        ),
    }

    # Determine player_name / stat_type from first record
    first = sorted_results[0]
    player_name = getattr(first, "player_name", "")
    stat_type   = getattr(first, "stat_type",   "")
    supplemental_rows = [
        row
        for row in sorted_results
        if str(getattr(row, "source", "") or "").lower().startswith("propline")
    ]
    supplemental_sources = tuple(
        sorted(
            {
                str(getattr(row, "source", "") or "").strip()
                for row in supplemental_rows
                if str(getattr(row, "source", "") or "").strip()
            }
        )
    )
    n_supplemental = len(supplemental_rows)
    if n_supplemental:
        data_source = (
            "propline"
            if n_supplemental == len(sorted_results)
            else "real+propline"
        )
    else:
        data_source = "primary"

    return PlayerHitRates(
        player_name   = player_name,
        stat_type     = stat_type,
        current_line  = current_line,
        l5            = l5_stats,
        l10           = l10_stats,
        l15           = l15_stats,
        l20           = l20_stats,
        l30           = l30_stats,
        season        = season_stats,
        h2h           = h2h_stats,
        has_real_data = True,
        total_games   = len(sorted_results),
        data_source   = data_source,
        n_supplemental_games = n_supplemental,
        supplemental_sources = supplemental_sources,
        h2h_l5       = h2h_l5_stats,
        h2h_l10      = h2h_l10_stats,
        h2h_l20      = h2h_l20_stats,
        recent_values=tuple(
            float(row.actual_value)
            for row in sorted_results[:5]
            if row.actual_value is not None
        ),
        sample_values=tuple(
            (window, values)
            for window, values in sample_values.items()
            if values
        ),
    )


# ── H2H team name fuzzy matching ─────────────────────────────────────────────

def _fuzzy_team_match(stored: str, query: str, threshold: float = 0.6) -> bool:
    """
    Return True if *stored* and *query* look like the same team.

    Handles abbreviation vs full name: "BOS" vs "Boston Red Sox".
    """
    s = stored.strip().lower()
    q = query.strip().lower()
    s = " ".join("state" if token.rstrip(".") == "st" else token for token in s.split())
    q = " ".join("state" if token.rstrip(".") == "st" else token for token in q.split())

    # Exact or abbreviation match
    if s == q:
        return True

    # Short city/team fragments and fuzzy similarity can join opponents such
    # as New York and New Jersey. Only unambiguous token-prefix abbreviations
    # remain supported; source aliases must otherwise resolve upstream.
    short, long = sorted((s, q), key=len)
    if len(short) >= 3 and len(short) <= 4:
        return any(token.startswith(short) for token in long.split())
    return False
