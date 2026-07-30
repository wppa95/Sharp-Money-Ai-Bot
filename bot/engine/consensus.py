"""
engine/consensus.py — Cross-book consensus engine.

Groups MarketSnapshot objects by market key (sport, event, market_type,
selection) and computes:
  - Consensus line  (median of all book lines)
  - Consensus price (median of all American odds)
  - Market inefficiency flags (books deviating beyond threshold)

Pick'em snapshots (is_pickem=True) are never fed into this engine.
Only sportsbook moneyline / spread / total / player-prop snapshots.

Public API
----------
    result = compute_consensus(snapshots)      # list[ConsensusResult]
    outliers = find_inefficiencies(snapshots)  # list[MarketInefficiency]
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from connectors.base import MarketSnapshot


# ── Thresholds ────────────────────────────────────────────────────────────────

# A book's odds must deviate by this many American-odds cents from consensus
# to be flagged as an outlier (market inefficiency).
DEFAULT_OUTLIER_THRESHOLD_ODDS = 10   # e.g. consensus -110, book -100 → flagged

# Minimum books required to compute a meaningful consensus.
MIN_BOOKS_FOR_CONSENSUS = 2


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class ConsensusResult:
    """
    Cross-book consensus for one side of one market.

    Attributes
    ----------
    sport, event, market_type, selection
        Canonical market identifier.
    books
        All sportsbooks that have this market.
    consensus_odds
        Median American odds across all books.
    consensus_line
        Median spread/total value; None for pure moneylines.
    min_odds, max_odds
        Range of offered odds across books.
    book_count
        Number of books in this consensus group.
    computed_at
        When this consensus was computed.
    outliers
        Books deviating beyond the threshold — market inefficiencies.
    """
    sport:        str
    event:        str
    market_type:  str
    selection:    str
    books:        list[str]
    consensus_odds: int
    min_odds:     int
    max_odds:     int
    book_count:   int
    computed_at:  datetime = field(default_factory=datetime.utcnow)
    consensus_line: Optional[float] = None
    outliers:     list["MarketInefficiency"] = field(default_factory=list)

    @property
    def odds_range(self) -> int:
        """Total spread between best and worst odds across books."""
        return self.max_odds - self.min_odds

    @property
    def has_inefficiency(self) -> bool:
        return len(self.outliers) > 0

    def __repr__(self) -> str:
        consensus_str = f"+{self.consensus_odds}" if self.consensus_odds > 0 else str(self.consensus_odds)
        return (
            f"ConsensusResult({self.event!r}, {self.selection!r}, "
            f"consensus={consensus_str}, books={self.book_count})"
        )


@dataclass
class MarketInefficiency:
    """
    One book's odds deviate significantly from cross-book consensus.
    Positive deviation = book offers better odds than consensus (value for bettor).
    Negative deviation = book offers worse odds than consensus (book is stale/slow).
    """
    sportsbook:    str
    event:         str
    market_type:   str
    selection:     str
    offered_odds:  int
    consensus_odds: int
    deviation:     int       # offered_odds - consensus_odds (positive = value)
    sport:         str = ""
    detected_at:   datetime = field(default_factory=datetime.utcnow)

    @property
    def is_value(self) -> bool:
        """True when the book offers better-than-consensus odds."""
        return self.deviation > 0

    @property
    def is_stale(self) -> bool:
        """True when the book is offering worse odds than consensus."""
        return self.deviation < 0

    @property
    def abs_deviation(self) -> int:
        return abs(self.deviation)

    def __repr__(self) -> str:
        sign = "+" if self.deviation > 0 else ""
        return (
            f"MarketInefficiency({self.sportsbook!r}, {self.selection!r}, "
            f"dev={sign}{self.deviation})"
        )


# ── Core engine ───────────────────────────────────────────────────────────────

def compute_consensus(
    snapshots: list[MarketSnapshot],
    *,
    outlier_threshold: int = DEFAULT_OUTLIER_THRESHOLD_ODDS,
    min_books: int = MIN_BOOKS_FOR_CONSENSUS,
) -> list[ConsensusResult]:
    """
    Group snapshots by market key and compute cross-book consensus.

    Parameters
    ----------
    snapshots
        Flat list of MarketSnapshot objects (pick'em excluded automatically).
    outlier_threshold
        American-odds distance from consensus that flags a book as outlier.
    min_books
        Minimum number of books required to compute consensus for a market.

    Returns
    -------
    List of ConsensusResult, one per unique (sport, event, market_type, selection).
    Markets with fewer than ``min_books`` books are skipped.
    """
    # Exclude pick'em
    sb_snaps = [s for s in snapshots if not s.is_pickem and s.odds != 0]

    # Group by market key
    groups: dict[tuple, list[MarketSnapshot]] = {}
    for snap in sb_snaps:
        key = snap.market_key
        groups.setdefault(key, []).append(snap)

    results: list[ConsensusResult] = []
    now = datetime.utcnow()

    for (sport, event, market_type, selection), group in groups.items():
        if len(group) < min_books:
            continue

        odds_list  = [s.odds for s in group]
        lines_list = [s.line for s in group if s.line is not None]
        books      = [s.sportsbook for s in group]

        consensus_odds = int(statistics.median(odds_list))
        consensus_line = float(statistics.median(lines_list)) if lines_list else None

        # Detect outliers
        outliers: list[MarketInefficiency] = []
        for snap in group:
            deviation = snap.odds - consensus_odds
            if abs(deviation) >= outlier_threshold:
                outliers.append(MarketInefficiency(
                    sportsbook     = snap.sportsbook,
                    event          = event,
                    market_type    = market_type,
                    selection      = selection,
                    offered_odds   = snap.odds,
                    consensus_odds = consensus_odds,
                    deviation      = deviation,
                    sport          = sport,
                    detected_at    = now,
                ))

        results.append(ConsensusResult(
            sport          = sport,
            event          = event,
            market_type    = market_type,
            selection      = selection,
            books          = books,
            consensus_odds = consensus_odds,
            min_odds       = min(odds_list),
            max_odds       = max(odds_list),
            book_count     = len(group),
            computed_at    = now,
            consensus_line = consensus_line,
            outliers       = outliers,
        ))

    return results


def find_inefficiencies(
    snapshots: list[MarketSnapshot],
    *,
    outlier_threshold: int = DEFAULT_OUTLIER_THRESHOLD_ODDS,
    min_books: int = MIN_BOOKS_FOR_CONSENSUS,
    value_only: bool = True,
) -> list[MarketInefficiency]:
    """
    Convenience wrapper: run consensus and return only the outlier books.

    Parameters
    ----------
    value_only
        When True (default), return only positive-deviation outliers
        (books offering better-than-consensus odds — the actionable signals).
        When False, return all outliers including stale/slow books.
    """
    consensus_results = compute_consensus(
        snapshots,
        outlier_threshold=outlier_threshold,
        min_books=min_books,
    )
    inefficiencies: list[MarketInefficiency] = []
    for cr in consensus_results:
        for ineff in cr.outliers:
            if value_only and not ineff.is_value:
                continue
            inefficiencies.append(ineff)
    return inefficiencies


def build_multi_book_steam_inputs(
    snapshots: list[MarketSnapshot],
) -> dict[tuple, list[dict]]:
    """
    Prepare per-market snapshot groups for multi-book steam detection.

    Returns a dict mapping market key → list of book snapshots suitable
    for passing to engine.steam.compute_steam_simple() as ``book_snapshots``.

    Each entry has keys: sportsbook, open_odds, current_odds.
    Only includes books where opening_odds is set and there's actual movement.
    """
    sb_snaps = [s for s in snapshots if not s.is_pickem and s.odds != 0]

    groups: dict[tuple, list[MarketSnapshot]] = {}
    for snap in sb_snaps:
        groups.setdefault(snap.market_key, []).append(snap)

    result: dict[tuple, list[dict]] = {}
    for key, group in groups.items():
        book_data = []
        for snap in group:
            if snap.opening_odds is None:
                continue
            if snap.odds == snap.opening_odds:
                continue  # no movement
            book_data.append({
                "sportsbook":   snap.sportsbook,
                "open_odds":    snap.opening_odds,
                "current_odds": snap.odds,
            })
        if len(book_data) >= 2:   # need at least 2 books for steam signal
            result[key] = book_data

    return result
