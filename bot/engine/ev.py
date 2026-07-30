"""
engine/ev.py — Expected Value calculation module.

All probability inputs come exclusively from engine.fair_probability.
No odds are fabricated; callers pass real market lines.

Public API
----------
    compute_ev(market_odds, counterpart_odds, method, label)  → EVResult
    compute_ev_from_market(fair_market, outcome_label, market_odds)  → EVResult
    compute_ev_batch(lines)  → list[EVResult]

EV Rating tiers (ev_rating property)
--------------------------------------
    EXCEPTIONAL  EV ≥ 10 %
    STRONG       EV ≥  6 %
    GOOD         EV ≥  3 %
    MARGINAL     EV ≥  1 %
    NEUTRAL      EV ≥  0 %
    NEGATIVE     EV <  0 %

Confidence flags (ConfidenceFlag enum)
---------------------------------------
    HIGH_VIG         Market overround > 8 % — fair prob estimate less reliable
    ASYMMETRIC_VIG   One side carries > 70 % of the total vig burden
    THIN_EDGE        Edge < 1 % — within noise; don't over-index on this
    STRONG_EDGE      Edge ≥ 5 % — high-conviction signal
    DEEP_POSITIVE    EV ≥ 8 % — rare; double-check the line before acting
    LINE_DISCREPANCY Fair odds differ from market odds by ≥ 20 cents American

Telegram formatting
-------------------
    EVResult.to_telegram()  → HTML-formatted string ready for send_message()
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Sequence

from engine.fair_probability import (
    FairMarket,
    FairProbabilityMethod,
    FairProbabilityResult,
    american_to_decimal,
    american_to_implied,
    compute_fair_market,
    compute_fair_probability,
    implied_to_american,
)


# ── EV Rating tiers ────────────────────────────────────────────────────────────

class EVRating(str, enum.Enum):
    EXCEPTIONAL = "EXCEPTIONAL"   # ≥ 10 %
    STRONG      = "STRONG"        # ≥  6 %
    GOOD        = "GOOD"          # ≥  3 %
    MARGINAL    = "MARGINAL"      # ≥  1 %
    NEUTRAL     = "NEUTRAL"       # ≥  0 %
    NEGATIVE    = "NEGATIVE"      # <  0 %

    @classmethod
    def from_ev(cls, ev_pct: float) -> "EVRating":
        if ev_pct >= 10:
            return cls.EXCEPTIONAL
        if ev_pct >= 6:
            return cls.STRONG
        if ev_pct >= 3:
            return cls.GOOD
        if ev_pct >= 1:
            return cls.MARGINAL
        if ev_pct >= 0:
            return cls.NEUTRAL
        return cls.NEGATIVE

    @property
    def emoji(self) -> str:
        return {
            EVRating.EXCEPTIONAL: "🔥",
            EVRating.STRONG:      "💰",
            EVRating.GOOD:        "✅",
            EVRating.MARGINAL:    "🔵",
            EVRating.NEUTRAL:     "⚪",
            EVRating.NEGATIVE:    "❌",
        }[self]

    @property
    def is_positive(self) -> bool:
        return self not in (EVRating.NEUTRAL, EVRating.NEGATIVE)


# ── Confidence flags ───────────────────────────────────────────────────────────

class ConfidenceFlag(str, enum.Enum):
    HIGH_VIG         = "HIGH_VIG"           # Market overround > 8 %
    ASYMMETRIC_VIG   = "ASYMMETRIC_VIG"     # One side carries > 70 % of vig burden
    THIN_EDGE        = "THIN_EDGE"          # |edge| < 1 %
    STRONG_EDGE      = "STRONG_EDGE"        # edge ≥ 5 %
    DEEP_POSITIVE    = "DEEP_POSITIVE"      # EV ≥ 8 %
    LINE_DISCREPANCY = "LINE_DISCREPANCY"   # |fair_odds - market_odds| ≥ 20

    @property
    def description(self) -> str:
        return {
            ConfidenceFlag.HIGH_VIG:
                "Market overround > 8% — fair probability estimate less reliable",
            ConfidenceFlag.ASYMMETRIC_VIG:
                "Vig is concentrated on one side — check line carefully",
            ConfidenceFlag.THIN_EDGE:
                "Edge < 1% — within statistical noise; monitor before acting",
            ConfidenceFlag.STRONG_EDGE:
                "Edge ≥ 5% — high-conviction signal",
            ConfidenceFlag.DEEP_POSITIVE:
                "EV ≥ 8% — exceptional; verify the line is current",
            ConfidenceFlag.LINE_DISCREPANCY:
                "Fair odds differ from market odds by ≥ 20 cents — significant mispricing",
        }[self]


def _assess_confidence(
    ev_pct: float,
    edge: float,
    vig_pct: float,
    fair_american: int,
    market_american: int,
    implied_probs: list[float],
) -> list[ConfidenceFlag]:
    flags: list[ConfidenceFlag] = []

    if vig_pct > 8.0:
        flags.append(ConfidenceFlag.HIGH_VIG)

    # Asymmetric vig: check if one side carries more than 70% of the total overround
    if len(implied_probs) == 2:
        total = sum(implied_probs)
        excess = total - 1.0
        if excess > 0:
            share_0 = (implied_probs[0] - implied_probs[0] / total) / excess
            if share_0 > 0.70 or share_0 < 0.30:
                flags.append(ConfidenceFlag.ASYMMETRIC_VIG)

    if abs(edge) < 0.01:
        flags.append(ConfidenceFlag.THIN_EDGE)

    if edge >= 0.05:
        flags.append(ConfidenceFlag.STRONG_EDGE)

    if ev_pct >= 8.0:
        flags.append(ConfidenceFlag.DEEP_POSITIVE)

    if abs(fair_american - market_american) >= 20:
        flags.append(ConfidenceFlag.LINE_DISCREPANCY)

    return flags


# ── Kelly Criterion ────────────────────────────────────────────────────────────

def kelly_fraction(fair_probability: float, market_american_odds: int) -> float:
    """
    Full Kelly Criterion stake fraction.
    Returns 0.0 for negative-EV bets (never bet into negative edge).

    f* = (b*p - q) / b
    where b = net profit per unit, p = fair probability, q = 1 - p
    """
    decimal = american_to_decimal(market_american_odds)
    b = decimal - 1.0
    q = 1.0 - fair_probability
    k = (b * fair_probability - q) / b
    return max(0.0, round(k, 6))


# ── Core EV calculations ───────────────────────────────────────────────────────

def break_even_probability(market_american_odds: int) -> float:
    """
    Minimum win rate needed to break even at these odds (raw implied probability).
    This is just american_to_implied — a vig-inclusive measure of what the
    book needs you to win to avoid losing money long-term.
    """
    return american_to_implied(market_american_odds)


def expected_value_pct(fair_probability: float, market_american_odds: int) -> float:
    """
    EV% = (fair_probability × decimal_odds − 1) × 100

    Positive  → edge over the market (bet is +EV)
    Zero      → fair price, no edge
    Negative  → market is overpriced (bet is −EV)
    """
    decimal = american_to_decimal(market_american_odds)
    return round((fair_probability * decimal - 1.0) * 100, 6)


def edge_pct(fair_probability: float, market_american_odds: int) -> float:
    """
    Raw probability edge = fair_prob − break_even_prob.

    Positive means the market is offering better-than-fair odds.
    Expressed as a fraction (0.05 = 5 percentage points of edge).
    """
    return round(fair_probability - break_even_probability(market_american_odds), 6)


def fair_vs_market_diff(fair_american: int, market_american: int) -> int:
    """
    American-odds difference between the fair line and the offered line.
    Positive → fair line is more favourable than market (you're getting value).
    Negative → market is offering worse than fair odds (you're paying vig).
    """
    return fair_american - market_american


# ── Structured result ──────────────────────────────────────────────────────────

@dataclass
class EVResult:
    """
    Complete EV analysis for one side of a market.

    Constructed by compute_ev() or compute_ev_from_market().
    All monetary fields use American odds; probabilities are 0–1 floats.
    """
    # ── Identification ─────────────────────────────────────────────────────
    label: str                          # e.g. "Over", "Chiefs -3"
    market_american_odds: int           # odds offered by the book

    # ── Fair probability (from engine.fair_probability) ────────────────────
    fair_probability: float             # de-vigged probability (0–1)
    fair_american_odds: int             # fair odds equivalent
    vig_pct: float                      # market overround %
    devig_method: FairProbabilityMethod

    # ── EV metrics ─────────────────────────────────────────────────────────
    break_even_prob: float              # raw implied probability at market odds
    ev_percentage: float                # EV% (positive = +EV)
    edge: float                         # fair_prob − break_even_prob
    fair_vs_market: int                 # fair_american − market_american (cents)

    # ── Kelly sizing ───────────────────────────────────────────────────────
    kelly_full: float                   # full Kelly fraction
    kelly_half: float                   # half Kelly (conservative default)
    kelly_quarter: float                # quarter Kelly (very conservative)

    # ── Rating and flags ───────────────────────────────────────────────────
    ev_rating: EVRating
    confidence_flags: list[ConfidenceFlag] = field(default_factory=list)

    # ── Derived display helpers ────────────────────────────────────────────

    @property
    def ev_sign(self) -> str:
        return f"+{self.ev_percentage:.2f}%" if self.ev_percentage >= 0 else f"{self.ev_percentage:.2f}%"

    @property
    def edge_sign(self) -> str:
        return f"+{self.edge * 100:.2f}%" if self.edge >= 0 else f"{self.edge * 100:.2f}%"

    @property
    def fair_prob_pct(self) -> str:
        return f"{self.fair_probability * 100:.2f}%"

    @property
    def break_even_pct(self) -> str:
        return f"{self.break_even_prob * 100:.2f}%"

    @property
    def market_odds_fmt(self) -> str:
        return f"+{self.market_american_odds}" if self.market_american_odds > 0 else str(self.market_american_odds)

    @property
    def fair_odds_fmt(self) -> str:
        return f"+{self.fair_american_odds}" if self.fair_american_odds > 0 else str(self.fair_american_odds)

    @property
    def fair_vs_market_fmt(self) -> str:
        d = self.fair_vs_market
        return f"+{d}" if d > 0 else str(d)

    def to_dict(self) -> dict:
        return {
            "label":               self.label,
            "market_odds":         self.market_american_odds,
            "fair_probability":    round(self.fair_probability, 6),
            "break_even_prob":     round(self.break_even_prob, 6),
            "ev_percentage":       round(self.ev_percentage, 4),
            "edge":                round(self.edge, 6),
            "fair_american_odds":  self.fair_american_odds,
            "fair_vs_market":      self.fair_vs_market,
            "kelly_full":          self.kelly_full,
            "kelly_half":          self.kelly_half,
            "kelly_quarter":       self.kelly_quarter,
            "ev_rating":           self.ev_rating.value,
            "vig_pct":             self.vig_pct,
            "devig_method":        self.devig_method.value,
            "confidence_flags":    [f.value for f in self.confidence_flags],
        }

    # ── Telegram HTML alert ────────────────────────────────────────────────

    def to_telegram(self) -> str:
        """
        Return an HTML-formatted string ready for Telegram send_message()
        with parse_mode=ParseMode.HTML.
        """
        flag_lines = ""
        if self.confidence_flags:
            flag_lines = "\n\n<b>⚑ Confidence Flags:</b>\n" + "\n".join(
                f"  • <i>{f.value}</i> — {f.description}"
                for f in self.confidence_flags
            )

        kelly_block = (
            f"  Full:     <code>{self.kelly_full:.2%}</code>\n"
            f"  Half:     <code>{self.kelly_half:.2%}</code>\n"
            f"  Quarter:  <code>{self.kelly_quarter:.2%}</code>"
        )

        return (
            f"{self.ev_rating.emoji} <b>EV Analysis — {self.label}</b>\n"
            f"\n"
            f"<b>Market Odds:</b>   <code>{self.market_odds_fmt}</code>\n"
            f"<b>Fair Odds:</b>     <code>{self.fair_odds_fmt}</code>"
            f"  <i>(diff: {self.fair_vs_market_fmt})</i>\n"
            f"<b>Vig:</b>           <code>{self.vig_pct:.2f}%</code>"
            f"  <i>({self.devig_method.value})</i>\n"
            f"\n"
            f"<b>Break-even:</b>    <code>{self.break_even_pct}</code>\n"
            f"<b>Fair Prob:</b>     <code>{self.fair_prob_pct}</code>\n"
            f"<b>Edge:</b>          <code>{self.edge_sign}</code>\n"
            f"\n"
            f"<b>Expected Value:</b> <code>{self.ev_sign}</code>  "
            f"— {self.ev_rating.emoji} <b>{self.ev_rating.value}</b>\n"
            f"\n"
            f"<b>Kelly Sizing:</b>\n"
            f"{kelly_block}"
            f"{flag_lines}"
        )

    def to_console(self) -> str:
        """Plain-text summary for logging / terminal output."""
        flags = ", ".join(f.value for f in self.confidence_flags) or "none"
        return (
            f"[EVResult] {self.label}  |  "
            f"odds={self.market_odds_fmt}  fair={self.fair_odds_fmt}  "
            f"EV={self.ev_sign}  edge={self.edge_sign}  "
            f"rating={self.ev_rating.value}  flags=[{flags}]"
        )


# ── Factory functions ──────────────────────────────────────────────────────────

def compute_ev(
    market_odds: int,
    counterpart_odds: int,
    label: str = "Side A",
    counterpart_label: str = "Side B",
    method: FairProbabilityMethod = FairProbabilityMethod.MULTIPLICATIVE,
) -> EVResult:
    """
    Full EV analysis for one side of a two-outcome market.

    Parameters
    ----------
    market_odds         American odds for the side you are evaluating.
    counterpart_odds    American odds for the opposing side.
    label               Display name for the evaluated side.
    counterpart_label   Display name for the opposing side.
    method              De-vig method (default: MULTIPLICATIVE).

    Returns
    -------
    EVResult
    """
    fair_market = compute_fair_market(
        american_odds=[market_odds, counterpart_odds],
        labels=[label, counterpart_label],
        method=method,
    )
    return compute_ev_from_market(fair_market, label, market_odds)


def compute_ev_from_market(
    fair_market: FairMarket,
    outcome_label: str,
    market_american_odds: int,
) -> EVResult:
    """
    Build an EVResult from an already-computed FairMarket.

    Use this when you have a full FairMarket (e.g. from compute_fair_market())
    and want to evaluate EV for one specific outcome at given market odds.

    Parameters
    ----------
    fair_market             Pre-computed FairMarket object.
    outcome_label           Label of the outcome to evaluate (must match a
                            label in fair_market.outcomes).
    market_american_odds    The odds currently offered by the book for this side.
    """
    outcome: FairProbabilityResult | None = fair_market.get(outcome_label)
    if outcome is None:
        available = [o.label for o in fair_market.outcomes]
        raise ValueError(
            f"Label '{outcome_label}' not found in FairMarket. "
            f"Available: {available}"
        )

    fp = outcome.fair_probability
    be = break_even_probability(market_american_odds)
    ev = expected_value_pct(fp, market_american_odds)
    edge = edge_pct(fp, market_american_odds)
    fvm = fair_vs_market_diff(outcome.fair_american_odds, market_american_odds)
    kf = kelly_fraction(fp, market_american_odds)
    rating = EVRating.from_ev(ev)

    implied = [o.raw_implied for o in fair_market.outcomes]
    flags = _assess_confidence(ev, edge, fair_market.vig_pct, outcome.fair_american_odds, market_american_odds, implied)

    return EVResult(
        label=outcome_label,
        market_american_odds=market_american_odds,
        fair_probability=fp,
        fair_american_odds=outcome.fair_american_odds,
        vig_pct=fair_market.vig_pct,
        devig_method=fair_market.method,
        break_even_prob=be,
        ev_percentage=ev,
        edge=edge,
        fair_vs_market=fvm,
        kelly_full=kf,
        kelly_half=round(kf / 2, 6),
        kelly_quarter=round(kf / 4, 6),
        ev_rating=rating,
        confidence_flags=flags,
    )


def compute_ev_batch(
    lines: Sequence[dict],
    method: FairProbabilityMethod = FairProbabilityMethod.MULTIPLICATIVE,
) -> list[EVResult]:
    """
    Evaluate EV across multiple markets in one call.

    Each entry in ``lines`` must be a dict with keys:
        label             str   — display name for the evaluated side
        market_odds       int   — American odds for the evaluated side
        counterpart_odds  int   — American odds for the opposing side
        counterpart_label str   — (optional) display name for the opposing side
        method            str   — (optional) override de-vig method per line

    Returns a list of EVResult, one per entry, sorted by ev_percentage descending.

    Example
    -------
        results = compute_ev_batch([
            {"label": "Over",   "market_odds": -120, "counterpart_odds": +100},
            {"label": "Chiefs", "market_odds": -110, "counterpart_odds": -110},
        ])
    """
    results = []
    for entry in lines:
        m = FairProbabilityMethod(entry["method"]) if "method" in entry else method
        result = compute_ev(
            market_odds=entry["market_odds"],
            counterpart_odds=entry["counterpart_odds"],
            label=entry.get("label", "Side A"),
            counterpart_label=entry.get("counterpart_label", "Side B"),
            method=m,
        )
        results.append(result)
    return sorted(results, key=lambda r: r.ev_percentage, reverse=True)
