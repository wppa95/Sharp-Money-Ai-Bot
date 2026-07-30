"""
engine/ranking.py — AI Decision & Ranking Engine.

Combines all market intelligence signals (via compute_confidence) with
historical learning (win rate, CLV, market performance) to produce a
final TAKE / PASS decision for each detected opportunity.

Scoring
-------
    Base score  (0–100)   = compute_confidence() output
    Historical  (±10 pts) = win_rate_adj + clv_adj + market_adj
    Final score           = base + historical_adj  (clamped 0–100)

Tiers
-----
    S Tier   95–100   Take immediately — elite signal confluence
    A Tier   85–94    Take — strong multi-signal confirmation
    B Tier   75–84    Consider — solid signal, watch for line movement
    Pass      < 75    Skip — insufficient edge or confirmation

Decision
--------
    TAKE  tier ≥ B  AND  no HIGH-severity confidence warnings
    PASS  everything else

Historical learning is optional.  When fewer than MIN_SAMPLE_SIZE
resolved bets exist for a dimension, the adjustment for that dimension
is 0 — the score relies entirely on live market signals.

ML readiness
------------
    _apply_ml_override() is a documented stub.  Swap its body for a
    trained model's prediction to upgrade the adjustment range beyond ±10.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from .confidence import (
    ConfidenceResult,
    ConfidenceTier,
    RiskWarning,
    SupportingFactor,
    compute_confidence,
)

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_SAMPLE_SIZE = 5   # require ≥ this many resolved bets before using history


# ── Tier / decision types ─────────────────────────────────────────────────────

class RankingTier(str, enum.Enum):
    S    = "S"     # 95–100
    A    = "A"     # 85–94
    B    = "B"     # 75–84
    PASS = "PASS"  # < 75

    @classmethod
    def from_score(cls, score: int) -> "RankingTier":
        if score >= 95:
            return cls.S
        if score >= 85:
            return cls.A
        if score >= 75:
            return cls.B
        return cls.PASS

    @property
    def label(self) -> str:
        return {
            RankingTier.S:    "S Tier",
            RankingTier.A:    "A Tier",
            RankingTier.B:    "B Tier",
            RankingTier.PASS: "Pass",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            RankingTier.S:    "🔴",   # red = hottest signal
            RankingTier.A:    "🟢",
            RankingTier.B:    "🟡",
            RankingTier.PASS: "⚪",
        }[self]

    @property
    def should_act(self) -> bool:
        return self != RankingTier.PASS


class RankingDecision(str, enum.Enum):
    TAKE = "TAKE"
    PASS = "PASS"

    @property
    def emoji(self) -> str:
        return "✅" if self == RankingDecision.TAKE else "⛔"


# ── Historical stats (input) ──────────────────────────────────────────────────

@dataclass
class HistoricalStats:
    """
    Aggregated performance stats for a dimension (overall / sport / market).

    Computed from resolved EVRecord rows (result = WIN / LOSS / PUSH).
    Pass None (or leave defaults) when there is no historical data yet.
    """
    sample_size: int    = 0     # number of completed bets counted
    win_rate:    float  = 0.5   # 0.0–1.0
    avg_clv:     float  = 0.0   # average CLV %  (positive = consistently beat close)
    roi:         float  = 0.0   # average ROI %  (avg EV realized)

    @property
    def has_signal(self) -> bool:
        """True when sample size meets the minimum threshold."""
        return self.sample_size >= MIN_SAMPLE_SIZE


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class HistoricalBreakdown:
    """Per-dimension historical adjustments used in final ranking."""
    overall_adj:  int = 0   # ±5 from overall win rate
    clv_adj:      int = 0   # ±3 from historical CLV
    market_adj:   int = 0   # ±2 from market-type performance
    ml_adj:       int = 0   # ±0 until ML model is trained

    @property
    def total(self) -> int:
        return self.overall_adj + self.clv_adj + self.market_adj + self.ml_adj


@dataclass
class RankingResult:
    """
    Complete ranking decision for one detected market opportunity.
    Produced by compute_ranking().
    """
    # ── Core output ───────────────────────────────────────────────────────────
    score:    int              # 0–100 (clamped)
    tier:     RankingTier
    decision: RankingDecision

    # ── Signal breakdown ──────────────────────────────────────────────────────
    confidence_result:    ConfidenceResult    # full 9-signal confidence detail
    historical_breakdown: HistoricalBreakdown
    key_factors:          list[str]           # top factors driving the decision
    warning_flags:        list[str]           # flags suppressing a TAKE

    # ── Input stats (stored for auditability / ML training) ───────────────────
    overall_history: Optional[HistoricalStats]
    market_history:  Optional[HistoricalStats]
    sport_history:   Optional[HistoricalStats]

    # ── Display helpers ───────────────────────────────────────────────────────

    @property
    def tier_display(self) -> str:
        return f"{self.tier.emoji} {self.tier.label}"

    @property
    def decision_display(self) -> str:
        return f"{self.decision.emoji} {self.decision.value}"

    @property
    def base_score(self) -> int:
        return self.score - self.historical_breakdown.total

    @property
    def historical_used(self) -> bool:
        return self.historical_breakdown.total != 0

    def to_telegram_block(self) -> str:
        """
        Short inline block suitable for appending to EV / steam alerts.
        Uses compact format so it doesn't overwhelm the existing alert.
        """
        factor_lines = (
            "\n".join(f"  • {f}" for f in self.key_factors[:3])
            or "  • (none)"
        )
        warn_lines = (
            "\n".join(f"  ⚠️ {w}" for w in self.warning_flags)
            if self.warning_flags else ""
        )

        hist_line = ""
        if self.overall_history and self.overall_history.has_signal:
            h = self.overall_history
            hist_line = (
                f"\n  📈 History:    "
                f"Win {h.win_rate * 100:.0f}%  "
                f"· CLV {h.avg_clv:+.1f}%  "
                f"· n={h.sample_size}"
            )

        clv_adj_line = ""
        bd = self.historical_breakdown
        if bd.total != 0:
            sign = f"{bd.total:+d}"   # e.g. "+8" or "-5"
            clv_adj_line = f"  ↕ History adj: <code>{sign} pts</code>\n"

        block = "\n".join(filter(None, [
            "─" * 30,
            f"🎰 <b>AI Decision — {self.tier_display}</b>",
            f"  Score:      <code>{self.score}/100</code>",
            f"  Decision:   {self.decision_display}",
            clv_adj_line.rstrip(),
            "",
            "  <b>Key Factors:</b>",
            factor_lines,
        ]))

        if hist_line:
            block += hist_line
        if warn_lines:
            block += "\n" + warn_lines

        return block

    def to_console(self) -> str:
        return (
            f"[Ranking] {self.tier.label:<8}  score={self.score:>3}/100  "
            f"decision={self.decision.value:<5}  "
            f"hist_adj={self.historical_breakdown.total:+d}  "
            f"factors={self.key_factors}"
        )


# ── Historical scorer ─────────────────────────────────────────────────────────

class _HistoricalScorer:
    """
    Deterministic rule-based historical adjustment.

    Adjustments are intentionally small (±10 max) so live signals always
    dominate — history is a confirmation layer, not a replacement.

    ML upgrade path
    ---------------
    _apply_ml_override() returns 0.  Replace its body with:

        features = [overall_win_rate, avg_clv, roi, sample_size,
                    market_win_rate, base_confidence_score]
        return int(round(model.predict([features])[0]))   # expected: -10 to +10
    """

    # ── Win rate adjustment (±5 pts) ──────────────────────────────────────────
    @staticmethod
    def _win_rate_adj(history: Optional[HistoricalStats]) -> int:
        if history is None or not history.has_signal:
            return 0
        wr = history.win_rate
        if wr >= 0.60:  return  5
        if wr >= 0.55:  return  3
        if wr >= 0.52:  return  1
        if wr >= 0.48:  return  0
        if wr >= 0.45:  return -2
        if wr >= 0.40:  return -4
        return -5

    # ── CLV adjustment (±3 pts) ───────────────────────────────────────────────
    @staticmethod
    def _clv_adj(history: Optional[HistoricalStats]) -> int:
        if history is None or not history.has_signal:
            return 0
        clv = history.avg_clv
        if clv >= 3.0:  return  3
        if clv >= 1.5:  return  2
        if clv >= 0.0:  return  1
        if clv >= -2.0: return  0
        if clv >= -4.0: return -1
        if clv >= -6.0: return -2
        return -3

    # ── Market-type adjustment (±2 pts) ───────────────────────────────────────
    @staticmethod
    def _market_adj(market_history: Optional[HistoricalStats]) -> int:
        if market_history is None or not market_history.has_signal:
            return 0
        wr = market_history.win_rate
        if wr >= 0.58:  return  2
        if wr >= 0.54:  return  1
        if wr >= 0.46:  return  0
        if wr >= 0.42:  return -1
        return -2

    # ── ML stub (±0 pts until model is trained) ───────────────────────────────
    @staticmethod
    def _apply_ml_override(
        overall: Optional[HistoricalStats],
        market:  Optional[HistoricalStats],
        base_confidence_score: int,
    ) -> int:
        """
        PLACEHOLDER — ML historical adjustment.

        Replace this body when a trained model is available:

            features = [
                overall.win_rate if overall else 0.5,
                overall.avg_clv  if overall else 0.0,
                overall.sample_size if overall else 0,
                market.win_rate if market else 0.5,
                base_confidence_score,
            ]
            return int(round(model.predict([features])[0]))  # expected: -10 to +10

        Until then returns 0.
        """
        return 0

    def compute(
        self,
        confidence_result: ConfidenceResult,
        overall_history:   Optional[HistoricalStats],
        market_history:    Optional[HistoricalStats],
        sport_history:     Optional[HistoricalStats],
    ) -> tuple[int, HistoricalBreakdown]:
        """Return (final_score, breakdown)."""
        base = confidence_result.confidence_score

        overall_adj = self._win_rate_adj(overall_history)
        clv_adj     = self._clv_adj(overall_history)
        market_adj  = self._market_adj(market_history)
        ml_adj      = self._apply_ml_override(overall_history, market_history, base)

        breakdown = HistoricalBreakdown(
            overall_adj=overall_adj,
            clv_adj=clv_adj,
            market_adj=market_adj,
            ml_adj=ml_adj,
        )

        final = min(max(base + breakdown.total, 0), 100)
        return final, breakdown


_hist_scorer = _HistoricalScorer()


# ── Key factor extraction ─────────────────────────────────────────────────────

def _extract_key_factors(
    confidence_result: ConfidenceResult,
    historical_breakdown: HistoricalBreakdown,
    overall_history: Optional[HistoricalStats],
) -> list[str]:
    """
    Derive the 3 strongest positive factors driving this ranking decision.

    Ordered: historical boosts first, then live market signals.
    """
    factors: list[str] = []

    # Historical boosts (if active)
    if historical_breakdown.overall_adj > 0 and overall_history:
        factors.append(
            f"Proven win rate ({overall_history.win_rate * 100:.0f}% over "
            f"{overall_history.sample_size} bets)"
        )
    if historical_breakdown.clv_adj > 0 and overall_history:
        factors.append(
            f"Positive CLV history (avg +{overall_history.avg_clv:.1f}%)"
        )
    if historical_breakdown.market_adj > 0:
        factors.append("Strong market-type historical performance")

    # Live signal factors from confidence engine
    _FACTOR_LABELS: dict[SupportingFactor, str] = {
        SupportingFactor.SHARP_BOOK_CONFIRMATION: "Sharp book(s) confirmed the move",
        SupportingFactor.MULTI_BOOK_CONSENSUS:    "Multi-book consensus movement",
        SupportingFactor.STRONG_EV_EDGE:          f"Strong EV edge ({confidence_result.ev_edge_pct:+.1f}%)",
        SupportingFactor.RAPID_MOVEMENT:          f"Rapid line movement ({confidence_result.movement_speed:.1f} pts/min)",
        SupportingFactor.HIGH_LIQUIDITY:          "High-liquidity market — signal is hard to fake",
        SupportingFactor.EARLY_MARKET_SIGNAL:     "Early market signal — sharp bettors move early",
        SupportingFactor.HIGH_MARKET_AGREEMENT:   f"Market agreement ({confidence_result.market_agreement * 100:.0f}%)",
        SupportingFactor.SIGNIFICANT_STEAM:       f"Significant steam score ({confidence_result.steam_score}/100)",
    }
    for sf in confidence_result.supporting_factors:
        label = _FACTOR_LABELS.get(sf, sf.value)
        if label not in factors:
            factors.append(label)

    return factors[:5]   # cap at 5 for display


def _extract_warning_flags(
    confidence_result: ConfidenceResult,
    historical_breakdown: HistoricalBreakdown,
    overall_history: Optional[HistoricalStats],
) -> list[str]:
    """Warning flags that suppressed a TAKE decision or reduce conviction."""
    flags: list[str] = []

    # High-severity confidence warnings
    for w in confidence_result.high_severity_warnings:
        flags.append(w.description)

    # Moderate risk warnings worth surfacing
    moderate = {
        RiskWarning.WEAK_STEAM,
        RiskWarning.THIN_EDGE,
        RiskWarning.LATE_MOVEMENT,
        RiskWarning.LOW_MARKET_AGREEMENT,
    }
    for w in confidence_result.risk_warnings:
        if w in moderate and w.description not in flags:
            flags.append(w.description)

    # Historical penalties
    if historical_breakdown.overall_adj < -2 and overall_history:
        flags.append(
            f"Below-average win rate ({overall_history.win_rate * 100:.0f}% over "
            f"{overall_history.sample_size} bets)"
        )
    if historical_breakdown.clv_adj < -1 and overall_history:
        flags.append(
            f"Negative CLV history (avg {overall_history.avg_clv:+.1f}%)"
        )

    return flags[:4]   # cap at 4


# ── Public API ────────────────────────────────────────────────────────────────

def compute_ranking(
    *,
    steam_score:      int,
    ev_edge_pct:      float,
    fair_probability: float,
    n_books_moving:   int,
    sharp_book_count: int,
    market_agreement: float,
    movement_speed:   float,
    liquidity_score:  int,
    minutes_to_game:  Optional[float] = None,
    overall_history:  Optional[HistoricalStats] = None,
    market_history:   Optional[HistoricalStats] = None,
    sport_history:    Optional[HistoricalStats] = None,
) -> RankingResult:
    """
    Compute the full AI ranking decision for one market opportunity.

    All live-signal parameters are keyword-only and mirror the signature
    of compute_confidence() exactly.  The three Optional[HistoricalStats]
    parameters are populated from the database (pass None to skip historical
    learning for a dimension).

    Parameters
    ----------
    steam_score         Steam engine output (0–100).
    ev_edge_pct         Edge % above break-even (e.g. 4.5 = 4.5%).
    fair_probability    De-vigged fair probability for the evaluated side.
    n_books_moving      Number of sportsbooks that moved.
    sharp_book_count    Number of sharp-tier books that moved.
    market_agreement    Fraction of books in directional consensus (0–1).
    movement_speed      Line movement speed in American-odds pts per minute.
    liquidity_score     Market depth proxy (0–100).
    minutes_to_game     Minutes until event start.  None = unknown.
    overall_history     Aggregate performance across all resolved bets.
    market_history      Performance filtered to this specific market type.
    sport_history       Performance filtered to this specific sport.

    Returns
    -------
    RankingResult
    """
    # ── Step 1: Base confidence score ─────────────────────────────────────────
    confidence_result = compute_confidence(
        steam_score=steam_score,
        ev_edge_pct=ev_edge_pct,
        fair_probability=fair_probability,
        n_books_moving=n_books_moving,
        sharp_book_count=sharp_book_count,
        market_agreement=market_agreement,
        movement_speed=movement_speed,
        liquidity_score=liquidity_score,
        minutes_to_game=minutes_to_game,
    )

    # ── Step 2: Historical adjustment ─────────────────────────────────────────
    final_score, hist_breakdown = _hist_scorer.compute(
        confidence_result, overall_history, market_history, sport_history
    )

    # ── Step 3: Tier and decision ─────────────────────────────────────────────
    tier = RankingTier.from_score(final_score)

    # TAKE requires tier ≥ B AND no HIGH-severity warnings
    has_high_warn = bool(confidence_result.high_severity_warnings)
    decision = (
        RankingDecision.TAKE
        if tier.should_act and not has_high_warn
        else RankingDecision.PASS
    )

    # ── Step 4: Human-readable explanation ────────────────────────────────────
    key_factors   = _extract_key_factors(confidence_result, hist_breakdown, overall_history)
    warning_flags = _extract_warning_flags(confidence_result, hist_breakdown, overall_history)

    return RankingResult(
        score=final_score,
        tier=tier,
        decision=decision,
        confidence_result=confidence_result,
        historical_breakdown=hist_breakdown,
        key_factors=key_factors,
        warning_flags=warning_flags,
        overall_history=overall_history,
        market_history=market_history,
        sport_history=sport_history,
    )
