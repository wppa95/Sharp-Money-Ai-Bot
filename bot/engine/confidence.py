"""
engine/confidence.py – Bet Quality Scoring Engine.

Grades the quality of a detected betting opportunity at the CURRENT line
using pure market-intelligence signals (EV, steam, sharp books, consensus,
timing, liquidity).

This module does NOT measure how confident the model is in its own projection.
That is the job of Projection Confidence (separate engine).

It answers one question only:
    "How good is this bet right now?"

Output is a 0–100 Bet Quality score + S/A/B/PASS-style tier.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


# ── Star / tier constants ──────────────────────────────────────────────────────

STAR_FILLED = "★"
STAR_EMPTY  = "☆"


class ConfidenceTier(str, enum.Enum):
                                    ELITE = "ELITE"           # 95-100
                                    VERY_HIGH = "VERY_HIGH"   # 85-94
                                    HIGH = "HIGH"             # 75-84
                                    MEDIUM = "MEDIUM"         # 65-74
                                    PASS = "PASS"             # < 65

                                    @classmethod
                                    def from_score(cls, score: int) -> "ConfidenceTier":
                                        if score >= 95:
                                            return cls.ELITE
                                        if score >= 85:
                                            return cls.VERY_HIGH
                                        if score >= 75:
                                            return cls.HIGH
                                        if score >= 65:
                                            return cls.MEDIUM
                                        return cls.PASS

                                    @property
                                    def stars(self) -> int:
                                        return {
                                            ConfidenceTier.ELITE: 5,
                                            ConfidenceTier.VERY_HIGH: 4,
                                            ConfidenceTier.HIGH: 3,
                                            ConfidenceTier.MEDIUM: 2,
                                            ConfidenceTier.PASS: 1,
                                        }[self]

                                    @property
                                    def star_display(self) -> str:
                                        n = self.stars
                                        return STAR_FILLED * n + STAR_EMPTY * (5 - n)

                                    @property
                                    def label(self) -> str:
                                        return {
                                            ConfidenceTier.ELITE: "Elite",
                                            ConfidenceTier.VERY_HIGH: "Very High",
                                            ConfidenceTier.HIGH: "High",
                                            ConfidenceTier.MEDIUM: "Medium",
                                            ConfidenceTier.PASS: "Pass",
                                        }[self]

                                    @property
                                    def emoji(self) -> str:
                                        return {
                                            ConfidenceTier.ELITE: "🟣",
                                            ConfidenceTier.VERY_HIGH: "🟢",
                                            ConfidenceTier.HIGH: "🟡",
                                            ConfidenceTier.MEDIUM: "🟠",
                                            ConfidenceTier.PASS: "🔴",
                                        }[self]

                                    @property
                                    def should_act(self) -> bool:
                                        """True for MEDIUM and above."""
                                        return self != ConfidenceTier.PASS


# ── Supporting factors ─────────────────────────────────────────────────────────

class SupportingFactor(str, enum.Enum):
    """Positive signals that increase confidence."""
    SHARP_BOOK_CONFIRMATION = "SHARP_BOOK_CONFIRMATION"
    MULTI_BOOK_CONSENSUS    = "MULTI_BOOK_CONSENSUS"
    STRONG_EV_EDGE          = "STRONG_EV_EDGE"
    RAPID_MOVEMENT          = "RAPID_MOVEMENT"
    HIGH_LIQUIDITY          = "HIGH_LIQUIDITY"
    EARLY_MARKET_SIGNAL     = "EARLY_MARKET_SIGNAL"
    HIGH_MARKET_AGREEMENT   = "HIGH_MARKET_AGREEMENT"
    SIGNIFICANT_STEAM       = "SIGNIFICANT_STEAM"

    @property
    def description(self) -> str:
        return {
            SupportingFactor.SHARP_BOOK_CONFIRMATION:
                "Sharp-tier book(s) (Pinnacle / Circa / Bookmaker) led the move",
            SupportingFactor.MULTI_BOOK_CONSENSUS:
                "3+ sportsbooks moving in the same direction",
            SupportingFactor.STRONG_EV_EDGE:
                "Edge ≥ 4% above break-even — statistically meaningful",
            SupportingFactor.RAPID_MOVEMENT:
                "Line moving at ≥ 1 pt/min — consistent with steam",
            SupportingFactor.HIGH_LIQUIDITY:
                "Deep market — movement is harder to fake and more informative",
            SupportingFactor.EARLY_MARKET_SIGNAL:
                "Detected > 6 hours before game — sharp bettors typically move early",
            SupportingFactor.HIGH_MARKET_AGREEMENT:
                "≥ 80% of sampled books moving in the same direction",
            SupportingFactor.SIGNIFICANT_STEAM:
                "Steam score ≥ 70 — strong coordinated line movement detected",
        }[self]


# ── Risk warnings ──────────────────────────────────────────────────────────────

class RiskWarning(str, enum.Enum):
    """Flags that reduce confidence or require caution."""
    LOW_LIQUIDITY         = "LOW_LIQUIDITY"
    GAME_IMMINENT         = "GAME_IMMINENT"
    LATE_MOVEMENT         = "LATE_MOVEMENT"
    THIN_EDGE             = "THIN_EDGE"
    SINGLE_BOOK           = "SINGLE_BOOK"
    WEAK_STEAM            = "WEAK_STEAM"
    LOW_MARKET_AGREEMENT  = "LOW_MARKET_AGREEMENT"
    NEAR_EVEN_PROBABILITY = "NEAR_EVEN_PROBABILITY"
    UNKNOWN_GAME_TIME     = "UNKNOWN_GAME_TIME"

    @property
    def description(self) -> str:
        return {
            RiskWarning.LOW_LIQUIDITY:
                "Thin market — line is easier to move; signal may be noise",
            RiskWarning.GAME_IMMINENT:
                "Game starts < 30 min — movement may reflect injury/news, not sharp money",
            RiskWarning.LATE_MOVEMENT:
                "Game starts < 2h — late steam is less reliable than early-week moves",
            RiskWarning.THIN_EDGE:
                "Edge < 2% — within normal variance; size down or pass",
            RiskWarning.SINGLE_BOOK:
                "Only one book triggered — insufficient consensus to confirm steam",
            RiskWarning.WEAK_STEAM:
                "Steam score < 50 — movement is present but below reliable threshold",
            RiskWarning.LOW_MARKET_AGREEMENT:
                "Books disagree on direction — conflicting signals reduce reliability",
            RiskWarning.NEAR_EVEN_PROBABILITY:
                "Fair probability near 50% — lower variance but also lower edge ceiling",
            RiskWarning.UNKNOWN_GAME_TIME:
                "Game time unknown — cannot assess timing quality of the signal",
        }[self]

    @property
    def severity(self) -> str:
        """HIGH / MEDIUM / LOW — for display sorting."""
        high = {RiskWarning.GAME_IMMINENT, RiskWarning.SINGLE_BOOK, RiskWarning.LOW_LIQUIDITY}
        low  = {RiskWarning.NEAR_EVEN_PROBABILITY, RiskWarning.UNKNOWN_GAME_TIME}
        if self in high:
            return "HIGH"
        if self in low:
            return "LOW"
        return "MEDIUM"


# ── Score components ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScoreBreakdown:
    """Per-component contribution to the overall confidence score (max 100)."""
    ev_edge:          int   # max 20
    steam_signal:     int   # max 18
    n_books_moving:   int   # max 12
    sharp_books:      int   # max 12
    market_agreement: int   # max 12
    movement_speed:   int   # max 10
    liquidity:        int   # max 8
    time_to_game:     int   # max 8
    ml_override:      int   # max 0 (stub — future use)

    @property
    def total(self) -> int:
        return (
            self.ev_edge + self.steam_signal + self.n_books_moving
            + self.sharp_books + self.market_agreement + self.movement_speed
            + self.liquidity + self.time_to_game + self.ml_override
        )

    @property
    def max_possible(self) -> int:
        return 20 + 18 + 12 + 12 + 12 + 10 + 8 + 8   # = 100

    def as_dict(self) -> dict[str, int]:
        return {
            "ev_edge":          self.ev_edge,
            "steam_signal":     self.steam_signal,
            "n_books_moving":   self.n_books_moving,
            "sharp_books":      self.sharp_books,
            "market_agreement": self.market_agreement,
            "movement_speed":   self.movement_speed,
            "liquidity":        self.liquidity,
            "time_to_game":     self.time_to_game,
            "ml_override":      self.ml_override,
            "total":            self.total,
        }


# ── Structured result ──────────────────────────────────────────────────────────

@dataclass
class ConfidenceResult:
    """
    Complete confidence assessment for one market signal.
    Produced by compute_confidence().
    """
    # ── Core output ────────────────────────────────────────────────────────
    confidence_score: int
    tier: ConfidenceTier
    star_rating: int

    # ── Evidence ───────────────────────────────────────────────────────────
    supporting_factors: list[SupportingFactor]
    risk_warnings: list[RiskWarning]
    score_breakdown: ScoreBreakdown

    # ── Raw inputs (stored for auditability / ML training) ─────────────────
    steam_score: int
    ev_edge_pct: float
    fair_probability: float
    n_books_moving: int
    sharp_book_count: int
    market_agreement: float
    movement_speed: float
    liquidity_score: int
    minutes_to_game: Optional[float]

    # ── Derived display helpers ────────────────────────────────────────────

    @property
    def star_display(self) -> str:
        return self.tier.star_display

    @property
    def tier_label(self) -> str:
        return f"{self.star_display} {self.tier.label}"

    @property
    def high_severity_warnings(self) -> list[RiskWarning]:
        return [w for w in self.risk_warnings if w.severity == "HIGH"]

    @property
    def is_actionable(self) -> bool:
        """True when tier ≥ MEDIUM and no HIGH-severity warnings present."""
        return self.tier.should_act and not self.high_severity_warnings

    def to_dict(self) -> dict:
        return {
            "confidence_score":   self.confidence_score,
            "tier":               self.tier.value,
            "star_rating":        self.star_rating,
            "star_display":       self.star_display,
            "supporting_factors": [f.value for f in self.supporting_factors],
            "risk_warnings":      [
                {"code": w.value, "severity": w.severity, "description": w.description}
                for w in self.risk_warnings
            ],
            "score_breakdown":    self.score_breakdown.as_dict(),
            "inputs": {
                "steam_score":       self.steam_score,
                "ev_edge_pct":       self.ev_edge_pct,
                "fair_probability":  self.fair_probability,
                "n_books_moving":    self.n_books_moving,
                "sharp_book_count":  self.sharp_book_count,
                "market_agreement":  self.market_agreement,
                "movement_speed":    self.movement_speed,
                "liquidity_score":   self.liquidity_score,
                "minutes_to_game":   self.minutes_to_game,
            },
        }

    def to_telegram(self) -> str:
        factors_block = "\n".join(
            f"  {STAR_FILLED} <i>{f.value}</i> — {f.description}"
            for f in self.supporting_factors
        ) or "  <i>None detected</i>"

        warnings_block = "\n".join(
            f"  ⚠️ <b>[{w.severity}]</b> <i>{w.value}</i> — {w.description}"
            for w in sorted(self.risk_warnings,
                            key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x.severity])
        ) or "  <i>None</i>"

        breakdown_lines = "\n".join(
            f"  {k:<22} <code>{v:>3}</code>"
            for k, v in self.score_breakdown.as_dict().items()
            if k != "total"
        )

        time_str = (
            f"{int(self.minutes_to_game // 60)}h {int(self.minutes_to_game % 60)}m"
            if self.minutes_to_game is not None else "Unknown"
        )

        return (
            f"{self.tier.emoji} <b>AI Confidence — {self.tier_label}</b>\n"
            f"\n"
            f"<b>Score:</b>        <code>{self.confidence_score}/100</code>\n"
            f"<b>Tier:</b>         {self.tier.emoji} {self.tier.label}\n"
            f"<b>Actionable:</b>   {'✅ YES' if self.is_actionable else '⛔ NO'}\n"
            f"\n"
            f"<b>📥 Signal Inputs</b>\n"
            f"  Steam score:      <code>{self.steam_score}/100</code>\n"
            f"  EV edge:          <code>{self.ev_edge_pct:+.2f}%</code>\n"
            f"  Fair probability: <code>{self.fair_probability * 100:.2f}%</code>\n"
            f"  Books moving:     <code>{self.n_books_moving}</code>"
            f"  (sharp: <code>{self.sharp_book_count}</code>)\n"
            f"  Market agreement: <code>{self.market_agreement * 100:.0f}%</code>\n"
            f"  Speed:            <code>{self.movement_speed:.2f} pts/min</code>\n"
            f"  Liquidity:        <code>{self.liquidity_score}/100</code>\n"
            f"  Time to game:     <code>{time_str}</code>\n"
            f"\n"
            f"<b>✅ Supporting Factors</b>\n"
            f"{factors_block}\n"
            f"\n"
            f"<b>⚠️ Risk Warnings</b>\n"
            f"{warnings_block}\n"
            f"\n"
            f"<b>📊 Score Breakdown</b>\n"
            f"{breakdown_lines}\n"
            f"  {'total':<22} <code>{self.score_breakdown.total:>3}</code>"
        )

    def to_console(self) -> str:
        flags = [f.value for f in self.supporting_factors]
        warns = [f"{w.severity}:{w.value}" for w in self.risk_warnings]
        return (
            f"[Confidence] {self.tier_label:<22}  score={self.confidence_score:>3}/100  "
            f"actionable={'Y' if self.is_actionable else 'N'}  "
            f"factors={flags}  warnings={warns}"
        )


# ── Internal scorer ────────────────────────────────────────────────────────────

class _ConfidenceScorer:
    """
    Deterministic rule-based scorer.

    Component weights (max 100 total):
        EV edge          20 pts  — mathematical edge size
        Steam signal     18 pts  — coordinated movement quality
        Books moving     12 pts  — breadth of market confirmation
        Sharp books      12 pts  — quality of participating books
        Market agreement 12 pts  — directional consensus
        Movement speed   10 pts  — urgency / velocity
        Liquidity         8 pts  — market depth proxy
        Time to game      8 pts  — signal timing quality
        ──────────────────────
        Total           100 pts

    ML upgrade path
    ---------------
    _score_ml_override() returns 0. Replace its body with:

        features = [steam_score, ev_edge_pct, fair_probability,
                    n_books_moving, sharp_book_count, market_agreement,
                    movement_speed, liquidity_score,
                    minutes_to_game if minutes_to_game is not None else -1]
        return int(round(model.predict([features])[0]))   # expected: -10 to +10
    """

    # ── Component: EV edge (max 20 pts) ───────────────────────────────────────
    @staticmethod
    def _score_ev_edge(ev_edge_pct: float) -> int:
        """
        Grades the raw edge above break-even.
        ≥ 10 %  → 20 pts  (rare; exceptional edge)
        ≥  8 %  → 18 pts
        ≥  7 %  → 17 pts
        ≥  6 %  → 15 pts
        ≥  5 %  → 13 pts
        ≥  4 %  → 11 pts
        ≥  3 %  →  9 pts  (meaningful threshold)
        ≥  2 %  →  6 pts
        ≥  1 %  →  3 pts
        >  0 %  →  1 pt
        ≤  0 %  →  0 pts  (negative / zero edge)
        """
        if ev_edge_pct >= 10:  return 20
        if ev_edge_pct >= 8:   return 18
        if ev_edge_pct >= 7:   return 17
        if ev_edge_pct >= 6:   return 15
        if ev_edge_pct >= 5:   return 13
        if ev_edge_pct >= 4:   return 11
        if ev_edge_pct >= 3:   return 9
        if ev_edge_pct >= 2:   return 6
        if ev_edge_pct >= 1:   return 3
        if ev_edge_pct > 0:    return 1
        return 0

    # ── Component: steam signal quality (max 18 pts) ──────────────────────────
    @staticmethod
    def _score_steam(steam_score: int) -> int:
        """
        Passes the steam engine's score through a calibrated curve.
        ≥ 90 → 18 pts  (critical steam)
        ≥ 80 → 17 pts  (strong steam)
        ≥ 70 → 15 pts
        ≥ 60 → 14 pts  (moderate steam)
        ≥ 50 → 11 pts
        ≥ 40 →  7 pts
        ≥ 30 →  3 pts
        < 30 →  0 pts  (below noise floor)
        """
        if steam_score >= 90:  return 18
        if steam_score >= 80:  return 17
        if steam_score >= 70:  return 15
        if steam_score >= 60:  return 14
        if steam_score >= 50:  return 11
        if steam_score >= 40:  return 7
        if steam_score >= 30:  return 3
        return 0

    # ── Component: books moving (max 12 pts) ──────────────────────────────────
    @staticmethod
    def _score_n_books(n_books_moving: int) -> int:
        """
        Breadth of market confirmation — how many books moved.
        ≥ 5 → 12 pts
        ≥ 4 → 10 pts
        ≥ 3 →  8 pts
        ≥ 2 →  6 pts
        ≥ 1 →  3 pts
           0 →  0 pts
        """
        if n_books_moving >= 5:  return 12
        if n_books_moving >= 4:  return 10
        if n_books_moving >= 3:  return 8
        if n_books_moving >= 2:  return 6
        if n_books_moving >= 1:  return 3
        return 0

    # ── Component: sharp book involvement (max 12 pts) ────────────────────────
    @staticmethod
    def _score_sharp_books(sharp_book_count: int) -> int:
        """
        ≥ 3 sharp books → 12 pts  (strongest possible signal)
           2             → 10 pts
           1             →  8 pts  (e.g. Pinnacle alone is meaningful)
           0             →  0 pts
        """
        if sharp_book_count >= 3:  return 12
        if sharp_book_count == 2:  return 10
        if sharp_book_count == 1:  return 8
        return 0

    # ── Component: market agreement (max 12 pts) ──────────────────────────────
    @staticmethod
    def _score_market_agreement(agreement: float) -> int:
        """
        Directional consensus across all sampled books (0.0–1.0).
        100% agreement → 12 pts
         ≥  90 %       → 11 pts
         ≥  80 %       → 10 pts  (strong consensus)
         ≥  70 %       →  7 pts
         ≥  60 %       →  4 pts
         ≥  50 %       →  2 pts
        < 50 %         →  0 pts  (books contradicting each other)
        """
        if agreement >= 1.00:   return 12
        if agreement >= 0.90:   return 11
        if agreement >= 0.80:   return 10
        if agreement >= 0.70:   return 7
        if agreement >= 0.60:   return 4
        if agreement >= 0.50:   return 2
        return 0

    # ── Component: movement speed (max 10 pts) ────────────────────────────────
    @staticmethod
    def _score_speed(movement_speed: float) -> int:
        """
        Speed in American-odds points per minute.
        ≥ 5.0 → 10 pts  (very fast — steam is live right now)
        ≥ 2.0 →  8 pts
        ≥ 1.0 →  7 pts  (active steam threshold)
        ≥ 0.5 →  5 pts
        ≥ 0.2 →  2 pts
        < 0.2 →  0 pts
        """
        if movement_speed >= 5.0:  return 10
        if movement_speed >= 2.0:  return 8
        if movement_speed >= 1.0:  return 7
        if movement_speed >= 0.5:  return 5
        if movement_speed >= 0.2:  return 2
        return 0

    # ── Component: liquidity (max 8 pts) ──────────────────────────────────────
    @staticmethod
    def _score_liquidity(liquidity_score: int) -> int:
        """
        Market depth proxy (0–100).
        High liquidity → harder to move → movement more informative.
        ≥ 80 → 8 pts
        ≥ 60 → 7 pts
        ≥ 40 → 5 pts
        ≥ 20 → 2 pts
        < 20 → 0 pts
        """
        if liquidity_score >= 80:  return 8
        if liquidity_score >= 60:  return 7
        if liquidity_score >= 40:  return 5
        if liquidity_score >= 20:  return 2
        return 0

    # ── Component: time to game (max 8 pts) ───────────────────────────────────
    @staticmethod
    def _score_time_to_game(minutes_to_game: Optional[float]) -> int:
        """
        Earlier signals from sharp bettors are more reliable.
        None (unknown)  → 3 pts  (neutral — can't assess)
        ≥ 48 h          → 8 pts  (early-week sharp move)
        ≥ 24 h          → 7 pts
        ≥ 12 h          → 6 pts
        ≥  6 h          → 6 pts
        ≥  2 h          → 3 pts
        ≥ 30 min        → 1 pt
        < 30 min        → 0 pts  (could be injury / news, not sharp money)
        """
        if minutes_to_game is None:
            return 3
        if minutes_to_game >= 60 * 48:   return 8
        if minutes_to_game >= 60 * 24:   return 7
        if minutes_to_game >= 60 * 12:   return 6
        if minutes_to_game >= 60 * 6:    return 6
        if minutes_to_game >= 60 * 2:    return 3
        if minutes_to_game >= 30:        return 1
        return 0

    # ── ML stub (max 0 pts until model is trained) ────────────────────────────
    @staticmethod
    def _score_ml_override(
        steam_score: int,
        ev_edge_pct: float,
        fair_probability: float,
        n_books_moving: int,
        sharp_book_count: int,
        market_agreement: float,
        movement_speed: float,
        liquidity_score: int,
        minutes_to_game: Optional[float],
    ) -> int:
        """
        PLACEHOLDER — Machine Learning confidence adjustment.

        Replace this body when a trained model is available:

            features = [steam_score, ev_edge_pct, fair_probability,
                        n_books_moving, sharp_book_count, market_agreement,
                        movement_speed, liquidity_score,
                        minutes_to_game if minutes_to_game is not None else -1]
            return int(round(model.predict([features])[0]))   # range: -10 to +10

        Until then returns 0 (no adjustment).
        """
        return 0

    # ── Supporting factors ────────────────────────────────────────────────────
    @staticmethod
    def _assess_factors(
        steam_score: int,
        ev_edge_pct: float,
        n_books_moving: int,
        sharp_book_count: int,
        market_agreement: float,
        movement_speed: float,
        liquidity_score: int,
        minutes_to_game: Optional[float],
    ) -> list[SupportingFactor]:
        factors: list[SupportingFactor] = []
        if sharp_book_count >= 1:
            factors.append(SupportingFactor.SHARP_BOOK_CONFIRMATION)
        if n_books_moving >= 3:
            factors.append(SupportingFactor.MULTI_BOOK_CONSENSUS)
        if ev_edge_pct >= 4.0:
            factors.append(SupportingFactor.STRONG_EV_EDGE)
        if movement_speed >= 1.0:
            factors.append(SupportingFactor.RAPID_MOVEMENT)
        if liquidity_score >= 60:
            factors.append(SupportingFactor.HIGH_LIQUIDITY)
        if minutes_to_game is not None and minutes_to_game >= 60 * 6:
            factors.append(SupportingFactor.EARLY_MARKET_SIGNAL)
        if market_agreement >= 0.80:
            factors.append(SupportingFactor.HIGH_MARKET_AGREEMENT)
        if steam_score >= 70:
            factors.append(SupportingFactor.SIGNIFICANT_STEAM)
        return factors

    # ── Risk warnings ─────────────────────────────────────────────────────────
    @staticmethod
    def _assess_warnings(
        steam_score: int,
        ev_edge_pct: float,
        fair_probability: float,
        n_books_moving: int,
        market_agreement: float,
        liquidity_score: int,
        minutes_to_game: Optional[float],
    ) -> list[RiskWarning]:
        warnings: list[RiskWarning] = []
        if liquidity_score < 30:
            warnings.append(RiskWarning.LOW_LIQUIDITY)
        if minutes_to_game is None:
            warnings.append(RiskWarning.UNKNOWN_GAME_TIME)
        elif minutes_to_game < 30:
            warnings.append(RiskWarning.GAME_IMMINENT)
        elif minutes_to_game < 120:
            warnings.append(RiskWarning.LATE_MOVEMENT)
        if ev_edge_pct < 2.0:
            warnings.append(RiskWarning.THIN_EDGE)
        if n_books_moving <= 1:
            warnings.append(RiskWarning.SINGLE_BOOK)
        if steam_score < 50:
            warnings.append(RiskWarning.WEAK_STEAM)
        if market_agreement < 0.60:
            warnings.append(RiskWarning.LOW_MARKET_AGREEMENT)
        if 0.47 <= fair_probability <= 0.53:
            warnings.append(RiskWarning.NEAR_EVEN_PROBABILITY)
        return warnings

    # ── Master scorer ─────────────────────────────────────────────────────────
    def compute(
        self,
        steam_score: int,
        ev_edge_pct: float,
        fair_probability: float,
        n_books_moving: int,
        sharp_book_count: int,
        market_agreement: float,
        movement_speed: float,
        liquidity_score: int,
        minutes_to_game: Optional[float],
    ) -> ConfidenceResult:

        s_ev     = self._score_ev_edge(ev_edge_pct)
        s_steam  = self._score_steam(steam_score)
        s_books  = self._score_n_books(n_books_moving)
        s_sharp  = self._score_sharp_books(sharp_book_count)
        s_agree  = self._score_market_agreement(market_agreement)
        s_speed  = self._score_speed(movement_speed)
        s_liq    = self._score_liquidity(liquidity_score)
        s_time   = self._score_time_to_game(minutes_to_game)
        s_ml     = self._score_ml_override(
            steam_score, ev_edge_pct, fair_probability, n_books_moving,
            sharp_book_count, market_agreement, movement_speed,
            liquidity_score, minutes_to_game,
        )

        breakdown = ScoreBreakdown(
            ev_edge=s_ev,
            steam_signal=s_steam,
            n_books_moving=s_books,
            sharp_books=s_sharp,
            market_agreement=s_agree,
            movement_speed=s_speed,
            liquidity=s_liq,
            time_to_game=s_time,
            ml_override=s_ml,
        )

        score = min(max(breakdown.total, 0), 100)
        tier  = ConfidenceTier.from_score(score)

        factors  = self._assess_factors(
            steam_score, ev_edge_pct, n_books_moving, sharp_book_count,
            market_agreement, movement_speed, liquidity_score, minutes_to_game,
        )
        warnings = self._assess_warnings(
            steam_score, ev_edge_pct, fair_probability, n_books_moving,
            market_agreement, liquidity_score, minutes_to_game,
        )

        return ConfidenceResult(
            confidence_score=score,
            tier=tier,
            star_rating=tier.stars,
            supporting_factors=factors,
            risk_warnings=warnings,
            score_breakdown=breakdown,
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


_scorer = _ConfidenceScorer()


# ── Public API ─────────────────────────────────────────────────────────────────

def compute_confidence(
    *,
    steam_score: int,
    ev_edge_pct: float,
    fair_probability: float,
    n_books_moving: int,
    sharp_book_count: int,
    market_agreement: float,
    movement_speed: float,
    liquidity_score: int,
    minutes_to_game: Optional[float] = None,
) -> ConfidenceResult:
    """
    Compute the AI confidence score for a detected market signal.

    All parameters are keyword-only to prevent positional mistakes.

    Parameters
    ----------
    steam_score         Steam engine output (0–100).
    ev_edge_pct         Edge % above break-even (e.g. 4.5 = 4.5%).
                        Negative values accepted (→ 0 pts, triggers THIN_EDGE).
    fair_probability    De-vigged fair probability for the evaluated side (0–1).
    n_books_moving      Number of sportsbooks that moved in the same direction.
    sharp_book_count    Number of sharp-tier books (Pinnacle / Circa / Bookmaker)
                        that moved.
    market_agreement    Fraction of sampled books in consensus (0.0–1.0).
    movement_speed      Line movement speed in American-odds points per minute.
    liquidity_score     Market depth proxy (0–100; 100 = deepest).
    minutes_to_game     Minutes until event start. None = unknown.

    Returns
    -------
    ConfidenceResult
    """
    return _scorer.compute(
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
