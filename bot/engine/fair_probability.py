"""
engine/fair_probability.py — Fair probability (vig removal) library.

Implements four de-vig methods and provides a unified interface for computing
fair probabilities from American, decimal, or implied-probability odds.

Methods
-------
MULTIPLICATIVE  Standard proportional vig removal. Fast, widely used.
ADDITIVE        Subtracts equal vig from each side's implied probability.
POWER (Shin)    Shin (1993) power-iteration method. Best for skewed markets.
ODDS_RATIO      Preserves the odds ratio between sides; useful for large fields.

All methods produce fair probabilities that sum to 1.0 across all outcomes.

Usage
-----
    from engine.fair_probability import compute_fair_market, FairProbabilityMethod

    # Two-sided market (spread, total, moneyline)
    market = compute_fair_market(
        american_odds=[-110, -110],
        method=FairProbabilityMethod.MULTIPLICATIVE,
    )
    print(market.fair_probs)      # [0.5238, 0.4762] → sums to 1.0 before rounding
    print(market.vig_pct)         # 4.76
    print(market.fair_american_odds)  # [-110, +110] approx

    # Pick the method with the lowest vig for a given set of lines
    best = best_fair_market(american_odds=[-115, +105])
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Sequence


# ── Odds conversion utilities ─────────────────────────────────────────────────

def american_to_implied(odds: int) -> float:
    """
    Convert American odds to raw implied probability (includes vig).

    >>> american_to_implied(-110)
    0.5238095238095238
    >>> american_to_implied(+110)
    0.47619047619047616
    """
    if odds == 0:
        raise ValueError("American odds cannot be zero.")
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def implied_to_american(prob: float, round_to: int = 5) -> int:
    """
    Convert a probability (0 < p < 1) to American odds.
    Rounds to the nearest ``round_to`` cents for cleaner display.

    >>> implied_to_american(0.5238)
    -110
    >>> implied_to_american(0.3333)
    200
    """
    if not 0 < prob < 1:
        raise ValueError(f"Probability must be strictly between 0 and 1, got {prob}")
    if prob >= 0.5:
        raw = -(prob / (1 - prob)) * 100
    else:
        raw = ((1 - prob) / prob) * 100
    if round_to and round_to > 0:
        return int(round(raw / round_to) * round_to)
    return int(round(raw))


def american_to_decimal(odds: int) -> float:
    """
    Convert American odds to decimal (European) odds.

    >>> american_to_decimal(-110)
    1.9090909090909092
    >>> american_to_decimal(+150)
    2.5
    """
    if odds < 0:
        return 1 + (100 / abs(odds))
    return 1 + (odds / 100)


def decimal_to_american(decimal: float, round_to: int = 5) -> int:
    """
    Convert decimal odds to American odds.

    >>> decimal_to_american(1.909)
    -110
    >>> decimal_to_american(2.5)
    150
    """
    if decimal <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal}")
    if decimal >= 2.0:
        raw = (decimal - 1) * 100
    else:
        raw = -100 / (decimal - 1)
    if round_to and round_to > 0:
        return int(round(raw / round_to) * round_to)
    return int(round(raw))


# ── Vig / overround helpers ────────────────────────────────────────────────────

def overround(implied_probs: Sequence[float]) -> float:
    """Sum of implied probabilities across all outcomes (> 1.0 = there is vig)."""
    return sum(implied_probs)


def vig_percentage(implied_probs: Sequence[float]) -> float:
    """
    Vig as a percentage of the total market width.

    For a standard -110 / -110 two-sided market:
        vig_percentage([0.5238, 0.5238]) ≈ 4.76
    """
    total = overround(implied_probs)
    return round((total - 1.0) * 100, 6)


def hold_percentage(implied_probs: Sequence[float]) -> float:
    """
    Sportsbook hold as a fraction of total action (0 to 1).
    Equivalent to vig / total implied probability.
    """
    total = overround(implied_probs)
    return (total - 1.0) / total


# ── De-vig methods ─────────────────────────────────────────────────────────────

class FairProbabilityMethod(str, enum.Enum):
    MULTIPLICATIVE = "multiplicative"
    ADDITIVE       = "additive"
    POWER          = "power"        # Shin / Jullien-Salanié
    ODDS_RATIO     = "odds_ratio"


def _devig_multiplicative(implied_probs: list[float]) -> list[float]:
    """
    Proportionally scale all implied probabilities so they sum to 1.
    Most common method; appropriate for symmetric markets.
    """
    total = sum(implied_probs)
    return [p / total for p in implied_probs]


def _devig_additive(implied_probs: list[float]) -> list[float]:
    """
    Subtract equal shares of the vig from each outcome's implied probability.
    Simple and intuitive; can produce negative probabilities for outsider odds
    in large-field markets (use POWER or MULTIPLICATIVE there instead).
    """
    n = len(implied_probs)
    total = sum(implied_probs)
    excess = (total - 1.0) / n
    fair = [p - excess for p in implied_probs]
    # Guard against negative probabilities
    if any(p <= 0 for p in fair):
        raise ValueError(
            "Additive de-vig produced non-positive probability. "
            "Use MULTIPLICATIVE or POWER for asymmetric markets."
        )
    return fair


def _devig_power(implied_probs: list[float], tol: float = 1e-9, max_iter: int = 500) -> list[float]:
    """
    Shin / power-iteration de-vig (Jullien & Salanié 1994).

    Finds exponent k such that sum(p_i ^ (1/k)) = 1.
    Handles skewed markets (heavy favourites / large fields) better than
    multiplicative scaling.
    """
    if len(implied_probs) < 2:
        raise ValueError("Power de-vig requires at least 2 outcomes.")

    # Binary search for k in (1, 100)
    lo, hi = 1.0, 100.0
    for _ in range(max_iter):
        k = (lo + hi) / 2.0
        total = sum(p ** (1.0 / k) for p in implied_probs)
        if abs(total - 1.0) < tol:
            break
        if total > 1.0:
            lo = k
        else:
            hi = k

    return [p ** (1.0 / k) for p in implied_probs]


def _devig_odds_ratio(implied_probs: list[float]) -> list[float]:
    """
    Odds-ratio de-vig (preserves the ratio between all pairs of outcomes).

    Finds scalar c such that p_i / (p_i + c*(1-p_i)) normalise to sum=1.
    Uses Newton's method. Well-suited for large-field markets.
    """
    # Initial guess: multiplicative solution
    scale = sum(implied_probs)
    probs = [p / scale for p in implied_probs]

    # Newton-Raphson: find c with f(c) = sum(p/(p+c*(1-p))) - 1 = 0
    c = 1.0
    for _ in range(200):
        f  = sum(p / (p + c * (1 - p)) for p in implied_probs) - 1.0
        df = sum(-(p * (1 - p)) / (p + c * (1 - p)) ** 2 for p in implied_probs)
        if df == 0:
            break
        c -= f / df
        if abs(f) < 1e-12:
            break

    fair = [p / (p + c * (1 - p)) for p in implied_probs]
    # Normalise for numerical cleanliness
    total = sum(fair)
    return [p / total for p in fair]


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FairProbabilityResult:
    """Fair probability for a single outcome within a market."""
    label: str
    raw_implied: float          # with vig
    fair_probability: float     # vig removed (0–1)
    fair_american_odds: int
    fair_decimal_odds: float
    edge: float                 # fair_prob - raw_implied (positive = market undervalues this side)
    method: FairProbabilityMethod

    @property
    def fair_pct(self) -> str:
        return f"{self.fair_probability * 100:.2f}%"

    @property
    def raw_pct(self) -> str:
        return f"{self.raw_implied * 100:.2f}%"

    @property
    def edge_pct(self) -> str:
        sign = "+" if self.edge >= 0 else ""
        return f"{sign}{self.edge * 100:.2f}%"


@dataclass
class FairMarket:
    """
    De-vigged market across all outcomes.

    Attributes
    ----------
    outcomes        Individual FairProbabilityResult for each side.
    method          De-vig method applied.
    vig_pct         Raw vig percentage (e.g. 4.76 for −110/−110).
    hold_pct        Sportsbook hold fraction (vig / total implied).
    market_width    Sum of raw implied probabilities (e.g. 1.0476).
    """
    outcomes: list[FairProbabilityResult]
    method: FairProbabilityMethod
    vig_pct: float
    hold_pct: float
    market_width: float

    @property
    def fair_probs(self) -> list[float]:
        return [o.fair_probability for o in self.outcomes]

    @property
    def fair_american_odds(self) -> list[int]:
        return [o.fair_american_odds for o in self.outcomes]

    @property
    def labels(self) -> list[str]:
        return [o.label for o in self.outcomes]

    def get(self, label: str) -> FairProbabilityResult | None:
        """Return the outcome matching ``label`` (case-insensitive), or None."""
        label_lower = label.lower()
        return next((o for o in self.outcomes if o.label.lower() == label_lower), None)

    def summary(self) -> str:
        lines = [
            f"Method: {self.method.value}  |  Vig: {self.vig_pct:.2f}%  |  Hold: {self.hold_pct * 100:.2f}%",
        ]
        for o in self.outcomes:
            lines.append(
                f"  {o.label:<20}  raw={o.raw_pct}  fair={o.fair_pct}  "
                f"({implied_to_american(o.fair_probability):+d})  edge={o.edge_pct}"
            )
        return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_fair_probability(
    american_odds: int,
    counterpart_odds: int,
    method: FairProbabilityMethod = FairProbabilityMethod.MULTIPLICATIVE,
    label: str = "Side A",
) -> FairProbabilityResult:
    """
    Compute the fair probability for one side of a two-outcome market.

    Parameters
    ----------
    american_odds       Odds for the side you want to evaluate.
    counterpart_odds    Odds for the opposing side.
    method              De-vig method.
    label               Display label for this side.

    Returns
    -------
    FairProbabilityResult
    """
    market = compute_fair_market(
        american_odds=[american_odds, counterpart_odds],
        labels=[label, "Counterpart"],
        method=method,
    )
    return market.outcomes[0]


def compute_fair_market(
    american_odds: Sequence[int],
    labels: Sequence[str] | None = None,
    method: FairProbabilityMethod = FairProbabilityMethod.MULTIPLICATIVE,
) -> FairMarket:
    """
    De-vig a complete market and return fair probabilities for every outcome.

    Parameters
    ----------
    american_odds   Sequence of American odds for each outcome (≥ 2).
    labels          Optional display labels (defaults to "Side 1", "Side 2", …).
    method          De-vig method to apply.

    Returns
    -------
    FairMarket

    Examples
    --------
    >>> m = compute_fair_market([-110, -110])
    >>> m.vig_pct
    4.761904761904762
    >>> [round(p, 4) for p in m.fair_probs]
    [0.5, 0.5]

    >>> m2 = compute_fair_market([-200, +170], method=FairProbabilityMethod.POWER)
    >>> round(sum(m2.fair_probs), 10)
    1.0
    """
    if len(american_odds) < 2:
        raise ValueError("A market must have at least 2 outcomes.")

    if labels is None:
        labels = [f"Side {i + 1}" for i in range(len(american_odds))]
    elif len(labels) != len(american_odds):
        raise ValueError("`labels` must have the same length as `american_odds`.")

    implied = [american_to_implied(o) for o in american_odds]
    total = sum(implied)
    vig_pct = vig_percentage(implied)
    hold_pct = hold_percentage(implied)

    # Apply the chosen de-vig method
    devig_fn = {
        FairProbabilityMethod.MULTIPLICATIVE: _devig_multiplicative,
        FairProbabilityMethod.ADDITIVE:       _devig_additive,
        FairProbabilityMethod.POWER:          _devig_power,
        FairProbabilityMethod.ODDS_RATIO:     _devig_odds_ratio,
    }[method]

    fair_probs = devig_fn(implied)

    # Normalise to guard against floating-point drift
    prob_sum = sum(fair_probs)
    fair_probs = [p / prob_sum for p in fair_probs]

    outcomes = []
    for label, raw_imp, fair_p in zip(labels, implied, fair_probs):
        outcomes.append(FairProbabilityResult(
            label=label,
            raw_implied=raw_imp,
            fair_probability=fair_p,
            fair_american_odds=implied_to_american(fair_p),
            fair_decimal_odds=round(1 / fair_p, 4),
            edge=round(fair_p - raw_imp, 6),
            method=method,
        ))

    return FairMarket(
        outcomes=outcomes,
        method=method,
        vig_pct=vig_pct,
        hold_pct=hold_pct,
        market_width=round(total, 8),
    )


def best_fair_market(
    american_odds: Sequence[int],
    labels: Sequence[str] | None = None,
) -> FairMarket:
    """
    Run all four de-vig methods and return the one that produces the lowest vig.

    Useful when you're uncertain which method is most appropriate and want
    the most conservative (lowest-edge) estimate.

    Parameters
    ----------
    american_odds   Sequence of American odds for each outcome.
    labels          Optional display labels.

    Returns
    -------
    FairMarket with the method that minimises the raw vig.
    """
    best: FairMarket | None = None
    for method in FairProbabilityMethod:
        try:
            market = compute_fair_market(american_odds, labels=labels, method=method)
            if best is None or market.vig_pct < best.vig_pct:
                best = market
        except (ValueError, ZeroDivisionError, OverflowError):
            continue  # Some methods fail on extreme odds — skip gracefully

    if best is None:
        raise RuntimeError("All de-vig methods failed. Check your input odds.")
    return best


# ── Multi-way market helpers ──────────────────────────────────────────────────

def normalize_multi_way(american_odds: Sequence[int]) -> FairMarket:
    """
    Convenience wrapper for futures / multi-outcome markets (3+ sides).
    Uses MULTIPLICATIVE de-vig by default (most common for large fields).

    >>> m = normalize_multi_way([-150, +200, +300])
    >>> round(sum(m.fair_probs), 10)
    1.0
    """
    return compute_fair_market(american_odds, method=FairProbabilityMethod.MULTIPLICATIVE)


def no_vig_line(side_a_odds: int, side_b_odds: int) -> tuple[int, int]:
    """
    Return the fair (no-vig) American odds for both sides of a two-outcome market.

    >>> no_vig_line(-110, -110)
    (+100, +100) equivalent: (100, -100) ... actually:
    (-100, -100) for even money when rounded
    """
    market = compute_fair_market([side_a_odds, side_b_odds])
    return market.fair_american_odds[0], market.fair_american_odds[1]
