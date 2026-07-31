"""
providers/prop_provider.py — Provider-agnostic player prop integration layer.

Defines the normalized PlayerProp dataclass and PropProviderBase ABC so any
pick'em / prop source (PrizePicks, Underdog, future providers) can be plugged
in without changing the comparison or alert engines.

Architecture
------------
    PropProviderBase        Abstract base class — implement to add a new source.
    PlayerProp              Normalized prop line (provider-agnostic dataclass).
    PropComparison          Result of comparing a prop to sportsbook fair odds.
    PropComparisonEngine    Accepts normalized props from any provider, produces
                            PropComparison objects for alerting/storage.

Usage
-----
    # 1.  Implement a provider (PrizePicks will slot in here when DataDome is
    #     bypassed; for now Underdog's connector is the reference implementation)
    class MyProvider(PropProviderBase):
        provider_name = "MySource"
        sport_keys    = ["MLB", "NBA"]

        async def fetch_props(self) -> list[PlayerProp]:
            ...  # fetch, normalize, return

    # 2.  Compare against sportsbook odds
    engine = PropComparisonEngine(min_edge_pct=3.0)
    result = engine.compare(prop, sb_line=28.5, sb_over_odds=-115,
                             sb_under_odds=-105, sportsbook="DraftKings")
    if result and result.has_edge:
        alert(result)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Normalized prop model ─────────────────────────────────────────────────────

@dataclass
class PlayerProp:
    """
    Normalized player prop line from any pick'em / daily-fantasy provider.

    Designed to be provider-agnostic: PrizePicks, Underdog, or any future
    source feeds data through this common schema.  The rest of the pipeline
    never needs to know which provider originated a prop.
    """
    provider:    str            # "PrizePicks" | "Underdog" | "Manual" | ...
    sport:       str            # normalized sport key ("MLB", "NBA", etc.)
    player_name: str
    team:        str
    stat_type:   str            # normalized stat label ("Hits", "Points", etc.)
    line_value:  float          # the pick'em line threshold
    game_time:   Optional[datetime] = None
    external_id: str = ""       # provider's own identifier (for deduplication)
    game_id:     str = ""       # game/event identifier from the provider
    fetched_at:  datetime = field(default_factory=datetime.utcnow)

    # ── Identity helpers ──────────────────────────────────────────────────────

    @property
    def prop_key(self) -> tuple[str, str, str, str]:
        """
        Unique deduplication key: (provider, player_name, sport, stat_type).

        Two PlayerProp objects with the same key represent the same real-world
        prop line (possibly at different times / line values).
        """
        return (self.provider, self.player_name, self.sport, self.stat_type)

    def normalized_stat(self) -> str:
        """
        Lowercase, whitespace-stripped stat label for comparison against
        sportsbook market strings.
        """
        return self.stat_type.lower().strip()

    def __repr__(self) -> str:
        return (
            f"<PlayerProp {self.provider}·{self.player_name}"
            f" {self.stat_type} {self.line_value:g} [{self.sport}]>"
        )


# ── Comparison result ─────────────────────────────────────────────────────────

@dataclass
class PropComparison:
    """
    Result of comparing a normalized PlayerProp against sportsbook fair odds.

    Positive ``edge_over`` means the fair probability of the over occurring is
    higher than 50 % (the pick'em break-even), so the over is +EV.
    The same logic applies to ``edge_under``.
    """
    prop:            PlayerProp
    sportsbook:      str
    sb_line:         float        # sportsbook line for the same stat
    sb_over_odds:    int          # American odds for over
    sb_under_odds:   int          # American odds for under
    fair_prob_over:  float        # de-vigged probability that over hits
    fair_prob_under: float        # de-vigged probability that under hits
    edge_over:       float        # (fair_prob_over  - 0.5) × 100  (+EV = positive)
    edge_under:      float        # (fair_prob_under - 0.5) × 100
    best_side:       str          # "OVER" | "UNDER"
    best_edge:       float        # max(edge_over, edge_under) in % points
    detected_at:     datetime = field(default_factory=datetime.utcnow)

    @property
    def has_edge(self) -> bool:
        """True when the best side has a positive EV edge over 50/50."""
        return self.best_edge > 0

    @property
    def line_diff(self) -> float:
        """Provider line minus sportsbook line (positive = provider is higher)."""
        return self.prop.line_value - self.sb_line

    @property
    def provider(self) -> str:
        return self.prop.provider

    @property
    def player_name(self) -> str:
        return self.prop.player_name

    @property
    def sport(self) -> str:
        return self.prop.sport

    @property
    def stat_type(self) -> str:
        return self.prop.stat_type

    def __repr__(self) -> str:
        sign = "+" if self.best_edge >= 0 else ""
        return (
            f"<PropComparison {self.player_name} {self.stat_type}"
            f" {self.best_side} {sign}{self.best_edge:.2f}%"
            f" [{self.provider} vs {self.sportsbook}]>"
        )


# ── Provider abstract base class ─────────────────────────────────────────────

class PropProviderBase(ABC):
    """
    Abstract base class for all player prop data providers.

    Subclass this to add a new source.  The comparison engine and dashboard
    accept any provider that satisfies this interface — no other changes needed.

    PrizePicks stub
    ---------------
    PrizePicks is currently protected by DataDome and cannot be integrated
    directly.  When that changes, implement PrizePicksProvider here:

        class PrizePicksProvider(PropProviderBase):
            provider_name = "PrizePicks"
            sport_keys    = ["MLB", "NBA", "NFL", "NHL"]

            async def fetch_props(self) -> list[PlayerProp]:
                # call the PP API, normalize each projection to PlayerProp
                ...
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable name — used in logs, DB records, and alert messages."""
        ...

    @property
    @abstractmethod
    def sport_keys(self) -> list[str]:
        """Sport keys this provider covers (e.g. ["MLB", "NBA"])."""
        ...

    @abstractmethod
    async def fetch_props(self) -> list[PlayerProp]:
        """
        Fetch current prop lines from the provider.

        Must return a list of normalized PlayerProp objects.
        Must return an empty list (and log) on any API failure — never raise.
        """
        ...

    def normalize_stat(self, raw_stat: str) -> str:
        """
        Map a provider-specific stat label to a shared canonical key.

        Override in subclasses to translate proprietary strings.
        Default implementation: lowercase + strip.
        """
        return raw_stat.lower().strip()

    def is_available(self) -> bool:
        """
        Return True when the provider is reachable and ready to fetch.

        Default: always True.  Override to add API-key or connectivity checks
        (e.g. PrizePicks would return False until DataDome is resolved).
        """
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} provider={self.provider_name!r}>"


# ── Comparison engine ─────────────────────────────────────────────────────────

class PropComparisonEngine:
    """
    Compare normalized PlayerProp lines to sportsbook fair odds.

    Provider-agnostic: works on any PlayerProp, regardless of source.
    Does NOT call external APIs — it operates on pre-fetched props and
    sportsbook odds that the caller supplies.

    Edge definition
    ---------------
    Pick'em props pay even money (1:1), so the break-even fair probability is
    exactly 50 %.  If the de-vigged sportsbook fair probability for the over is
    55 %, the edge is +5 percentage points — strong +EV.

        edge_over  = (fair_prob_over  − 0.5) × 100
        edge_under = (fair_prob_under − 0.5) × 100

    Line-gap rule
    -------------
    When the provider line differs from the sportsbook line, the direction of
    the mismatch determines which side is advantaged:

        provider_line > sb_line  →  UNDER is +EV (prop is too high)
        provider_line < sb_line  →  OVER  is +EV (prop is too low)
        provider_line == sb_line →  use fair-prob edge to pick best side

    Usage
    -----
        engine = PropComparisonEngine(min_edge_pct=3.0)

        comparison = engine.compare(
            prop,
            sb_line=28.5, sb_over_odds=-115, sb_under_odds=-105,
            sportsbook="DraftKings",
        )
        if comparison and comparison.best_edge >= engine.min_edge_pct:
            alert(comparison)
    """

    def __init__(self, *, min_edge_pct: float = 2.0) -> None:
        """
        Parameters
        ----------
        min_edge_pct
            Minimum edge percentage to consider a comparison meaningful.
            Used by ``filter_edges()``.  Does NOT affect ``compare()`` itself.
        """
        self.min_edge_pct = min_edge_pct

    # ── Core comparison ───────────────────────────────────────────────────────

    def compare(
        self,
        prop:          PlayerProp,
        sb_line:       float,
        sb_over_odds:  int,
        sb_under_odds: int,
        sportsbook:    str,
    ) -> Optional[PropComparison]:
        """
        Compare one PlayerProp to sportsbook odds and return a PropComparison.

        Returns None when either set of odds is 0 (market unavailable).

        Parameters
        ----------
        prop            Normalized provider prop.
        sb_line         Sportsbook line value for the same stat (can differ).
        sb_over_odds    American odds for the over at this sportsbook.
        sb_under_odds   American odds for the under at this sportsbook.
        sportsbook      Name of the sportsbook for display purposes.
        """
        if sb_over_odds == 0 or sb_under_odds == 0:
            return None

        fp_over, fp_under = self._fair_probs(sb_over_odds, sb_under_odds)

        # Edge: how much does fair probability exceed break-even (50 %)?
        edge_over  = (fp_over  - 0.5) * 100
        edge_under = (fp_under - 0.5) * 100

        # Direction from line-gap rule
        tol = 1e-6
        if prop.line_value > sb_line + tol:
            # Provider line higher → UNDER is the edge side
            best_side = "UNDER"
            best_edge = edge_under
        elif prop.line_value < sb_line - tol:
            # Provider line lower → OVER is the edge side
            best_side = "OVER"
            best_edge = edge_over
        else:
            # Lines match — pick whichever fair prob exceeds 50 % more
            if edge_over >= edge_under:
                best_side, best_edge = "OVER", edge_over
            else:
                best_side, best_edge = "UNDER", edge_under

        return PropComparison(
            prop            = prop,
            sportsbook      = sportsbook,
            sb_line         = sb_line,
            sb_over_odds    = sb_over_odds,
            sb_under_odds   = sb_under_odds,
            fair_prob_over  = round(fp_over,  4),
            fair_prob_under = round(fp_under, 4),
            edge_over       = round(edge_over,  4),
            edge_under      = round(edge_under, 4),
            best_side       = best_side,
            best_edge       = round(best_edge, 4),
        )

    # ── Batch helpers ─────────────────────────────────────────────────────────

    def compare_many(
        self,
        props:         list[PlayerProp],
        sb_line:       float,
        sb_over_odds:  int,
        sb_under_odds: int,
        sportsbook:    str,
    ) -> list[PropComparison]:
        """
        Compare a list of PlayerProp objects to the same sportsbook odds.

        Returns all valid (non-None) PropComparison objects.
        """
        results = []
        for prop in props:
            c = self.compare(prop, sb_line, sb_over_odds, sb_under_odds, sportsbook)
            if c is not None:
                results.append(c)
        return results

    def filter_edges(
        self,
        comparisons: list[PropComparison],
    ) -> list[PropComparison]:
        """
        Return only comparisons where ``best_edge ≥ min_edge_pct``.

        Sorted best-edge-first.
        """
        filtered = [c for c in comparisons if c.best_edge >= self.min_edge_pct]
        return sorted(filtered, key=lambda c: c.best_edge, reverse=True)

    # ── Math helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _fair_probs(over_odds: int, under_odds: int) -> tuple[float, float]:
        """
        Multiplicative vig removal → fair probability for over and under.

        Returns (fair_prob_over, fair_prob_under) where both sum to 1.0.
        Falls back to (0.5, 0.5) when the total implied probability is ≤ 0
        (malformed odds guard).
        """
        def implied(o: int) -> float:
            return abs(o) / (abs(o) + 100.0) if o < 0 else 100.0 / (o + 100.0)

        p_over  = implied(over_odds)
        p_under = implied(under_odds)
        total   = p_over + p_under
        if total <= 0:
            return 0.5, 0.5
        return p_over / total, p_under / total
