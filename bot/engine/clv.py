"""
engine/clv.py — Closing Line Value (CLV) tracking engine.

CLV measures whether your bet price was better than the closing line —
the market's final consensus before game start. Positive CLV means you
got value before the market corrected; it's the primary long-run edge
metric for sharp bettors.

CLV% formula (American odds):
    fair_prob_at_bet  = vig-removed probability at the price you got
    fair_prob_at_close = vig-removed probability at the closing price
    CLV% = (fair_prob_at_bet / fair_prob_at_close - 1) * 100

When closing odds are unavailable, we fall back to the raw American-odds
shift as a proxy:
    CLV_proxy = bet_price - closing_price  (positive = you beat the close)

Public API
----------
    compute_clv(bet_odds, closing_odds, counterpart_bet_odds, counterpart_close_odds)
        → CLVResult

    build_clv_opportunity(snapshot, consensus_result)
        → CLVOpportunity or None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Math helpers (no engine imports to keep this module self-contained) ────────

def _implied(odds: int) -> float:
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _fair_prob(my_odds: int, opp_odds: int) -> float:
    """Multiplicative vig removal — fair probability for my side."""
    p_me  = _implied(my_odds)
    p_opp = _implied(opp_odds)
    total = p_me + p_opp
    if total <= 0:
        return 0.5
    return p_me / total


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class CLVResult:
    """
    Closing Line Value for a single bet / alerted opportunity.

    Positive clv_pct means you beat the closing line — strong indicator
    of long-run edge. Negative means the market moved against you after
    your bet.
    """
    selection:       str
    bet_odds:        int        # American odds at time of alert/bet
    closing_odds:    int        # American odds at market close
    clv_pct:         float      # % CLV (positive = beat the close)
    clv_proxy:       int        # raw odds shift (bet - close), quick metric
    fair_prob_bet:   float      # de-vigged probability at bet odds
    fair_prob_close: float      # de-vigged probability at close odds
    computed_at:     datetime   = field(default_factory=datetime.utcnow)
    counterpart_bet_odds:   Optional[int] = None   # opposing side at bet time
    counterpart_close_odds: Optional[int] = None   # opposing side at close time
    notes:           str = ""

    @property
    def beat_close(self) -> bool:
        return self.clv_pct > 0

    @property
    def clv_grade(self) -> str:
        """Human-readable CLV quality tier."""
        if self.clv_pct >= 5:
            return "Excellent"
        if self.clv_pct >= 2:
            return "Strong"
        if self.clv_pct >= 0:
            return "Neutral"
        if self.clv_pct >= -3:
            return "Weak"
        return "Bad"

    @property
    def clv_emoji(self) -> str:
        if self.clv_pct >= 5:
            return "🔥"
        if self.clv_pct >= 2:
            return "✅"
        if self.clv_pct >= 0:
            return "⚪"
        if self.clv_pct >= -3:
            return "🟡"
        return "❌"

    def summary(self) -> str:
        sign = "+" if self.clv_pct >= 0 else ""
        proxy_sign = "+" if self.clv_proxy >= 0 else ""
        return (
            f"{self.selection} | "
            f"CLV {sign}{self.clv_pct:.2f}% ({self.clv_grade}) | "
            f"bet={self._fmt(self.bet_odds)} close={self._fmt(self.closing_odds)} | "
            f"Δodds={proxy_sign}{self.clv_proxy}"
        )

    @staticmethod
    def _fmt(odds: int) -> str:
        return f"+{odds}" if odds > 0 else str(odds)


@dataclass
class CLVOpportunity:
    """
    An actionable CLV opportunity: current price is better than projected close.

    Triggered when the market is moving and the current offered price is
    better than the projected closing price (based on consensus movement).
    """
    event:           str
    selection:       str
    current_odds:    int
    projected_close: int        # estimated closing odds from consensus trend
    clv_lead:        int        # current_odds - projected_close (positive = value)
    sport:           str = ""
    market_type:     str = ""
    sportsbook:      str = ""
    books_count:     int = 0    # number of books in consensus
    detected_at:     datetime   = field(default_factory=datetime.utcnow)

    @property
    def is_actionable(self) -> bool:
        """True when current odds are meaningfully better than projected close."""
        return self.clv_lead >= 5  # at least 5 cents ahead of projected close


# ── Core computation ──────────────────────────────────────────────────────────

def compute_clv(
    bet_odds: int,
    closing_odds: int,
    counterpart_bet_odds: Optional[int] = None,
    counterpart_close_odds: Optional[int] = None,
    *,
    selection: str = "Selection",
    notes: str = "",
) -> CLVResult:
    """
    Compute Closing Line Value for one selection.

    When counterpart odds are provided, uses de-vigged (fair) probabilities
    for a more accurate CLV measurement. When unavailable, falls back to
    implied probability without vig removal.

    Parameters
    ----------
    bet_odds
        American odds at which the bet was made / alert was sent.
    closing_odds
        American odds at market close (game start).
    counterpart_bet_odds
        American odds for the opposing side at bet time (for vig removal).
    counterpart_close_odds
        American odds for the opposing side at close (for vig removal).
    selection
        Display name for the bet.
    notes
        Any context notes.

    Returns
    -------
    CLVResult with clv_pct > 0 when the bet beat the closing line.
    """
    # Fair probabilities (use vig removal when counterpart odds available)
    if counterpart_bet_odds is not None and counterpart_close_odds is not None:
        fp_bet   = _fair_prob(bet_odds,     counterpart_bet_odds)
        fp_close = _fair_prob(closing_odds, counterpart_close_odds)
    else:
        # Fall back to raw implied (includes vig, but better than nothing)
        fp_bet   = _implied(bet_odds)
        fp_close = _implied(closing_odds)

    # CLV%: how much better was your price vs. the closing fair probability.
    #
    # As a bettor you want LOWER implied probability than close (less juice).
    # -110 implied ≈ 52.4%; -130 implied ≈ 56.5%.
    # If you bet -110 and it closed -130 the market got MORE confident after
    # you bet → positive CLV (you were right, you got in early).
    #
    # Formula: (fp_close / fp_bet - 1) × 100
    #   fp_close > fp_bet → market tightened → you beat the close (+)
    #   fp_close < fp_bet → market loosened → you missed the close (-)
    if fp_bet > 0:
        clv_pct = round((fp_close / fp_bet - 1.0) * 100, 4)
    else:
        clv_pct = 0.0

    # Raw odds proxy (quick scan metric)
    clv_proxy = bet_odds - closing_odds

    return CLVResult(
        selection              = selection,
        bet_odds               = bet_odds,
        closing_odds           = closing_odds,
        clv_pct                = clv_pct,
        clv_proxy              = clv_proxy,
        fair_prob_bet          = round(fp_bet,   4),
        fair_prob_close        = round(fp_close, 4),
        counterpart_bet_odds   = counterpart_bet_odds,
        counterpart_close_odds = counterpart_close_odds,
        notes                  = notes,
    )


def build_clv_opportunity(
    current_snapshot: "MarketSnapshot",  # noqa: F821
    consensus_snapshots: list["MarketSnapshot"],  # noqa: F821
    *,
    min_books: int = 3,
    min_lead: int = 5,
) -> Optional[CLVOpportunity]:
    """
    Determine whether the current price is ahead of the projected closing line.

    Uses the consensus price (median of all other books) as the projected close.
    Returns None when there aren't enough books or the lead is below threshold.

    Parameters
    ----------
    current_snapshot
        The snapshot whose price we are evaluating.
    consensus_snapshots
        All snapshots for the same market (including the current one).
    min_books
        Minimum total books for a reliable projected close estimate.
    min_lead
        Minimum American-odds lead over projected close to flag as opportunity.
    """
    # Import here to avoid circular dependency
    import statistics as _stats

    other_odds = [
        s.odds for s in consensus_snapshots
        if s.sportsbook != current_snapshot.sportsbook
        and s.odds != 0
        and not s.is_pickem
    ]

    if len(other_odds) < max(1, min_books - 1):
        return None

    projected_close = int(_stats.median(other_odds))
    clv_lead = current_snapshot.odds - projected_close

    if clv_lead < min_lead:
        return None

    return CLVOpportunity(
        event           = current_snapshot.event,
        selection       = current_snapshot.selection,
        current_odds    = current_snapshot.odds,
        projected_close = projected_close,
        clv_lead        = clv_lead,
        sport           = current_snapshot.sport,
        market_type     = current_snapshot.market_type,
        sportsbook      = current_snapshot.sportsbook,
        books_count     = len(consensus_snapshots),
    )
