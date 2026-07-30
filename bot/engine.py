"""
Analysis Engine — Sharp Money +EV Detection Platform.

Responsibilities:
  • Remove sportsbook vig to calculate fair probabilities
  • Calculate expected value (+EV) for any line
  • Detect steam / sharp money moves from odds movement
  • Score AI confidence based on multiple signals
  • Compare sportsbook lines with PrizePicks props (placeholder)
  • CLV tracking (placeholder — populated post-event)

All public methods are async-ready. Placeholder stubs are clearly marked
so future integrations (live odds APIs, ML models) can drop in cleanly.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from models import (
    AlertType,
    EVOpportunity,
    EVResult,
    FairOdds,
    MarketType,
    OddsLine,
    OddsMovement,
    Recommendation,
    Sport,
    SteamAlert,
)
from config import config

logger = logging.getLogger(__name__)


# ── Vig removal ───────────────────────────────────────────────────────────────

class VigRemover:
    """Remove sportsbook vig using the multiplicative / Shin method."""

    @staticmethod
    def american_to_implied(odds: int) -> float:
        if odds < 0:
            return abs(odds) / (abs(odds) + 100)
        return 100 / (odds + 100)

    @staticmethod
    def fair_probability_multiplicative(side_a_odds: int, side_b_odds: int) -> tuple[float, float]:
        """
        Remove vig multiplicatively (standard approach).
        Returns (fair_prob_a, fair_prob_b) that sum to exactly 1.0.
        """
        p_a = VigRemover.american_to_implied(side_a_odds)
        p_b = VigRemover.american_to_implied(side_b_odds)
        total = p_a + p_b
        return p_a / total, p_b / total

    @staticmethod
    def vig_percentage(side_a_odds: int, side_b_odds: int) -> float:
        """Return the vig as a percentage of the total market width."""
        p_a = VigRemover.american_to_implied(side_a_odds)
        p_b = VigRemover.american_to_implied(side_b_odds)
        total = p_a + p_b
        return round((total - 1.0) * 100, 4)

    @staticmethod
    def fair_american_odds(fair_probability: float) -> int:
        """Convert a fair probability to American odds (rounded to nearest 5)."""
        if fair_probability <= 0 or fair_probability >= 1:
            raise ValueError("Probability must be between 0 and 1 (exclusive).")
        if fair_probability >= 0.5:
            raw = -(fair_probability / (1 - fair_probability)) * 100
        else:
            raw = ((1 - fair_probability) / fair_probability) * 100
        # Round to nearest 5 for cleanliness
        return int(round(raw / 5) * 5)

    @staticmethod
    def build_fair_odds(selection: str, side_a_odds: int, side_b_odds: int, is_side_a: bool) -> FairOdds:
        fp_a, fp_b = VigRemover.fair_probability_multiplicative(side_a_odds, side_b_odds)
        fair_prob = fp_a if is_side_a else fp_b
        vig = VigRemover.vig_percentage(side_a_odds, side_b_odds)
        p_a = VigRemover.american_to_implied(side_a_odds)
        p_b = VigRemover.american_to_implied(side_b_odds)
        return FairOdds(
            selection=selection,
            fair_probability=round(fair_prob, 4),
            fair_american_odds=VigRemover.fair_american_odds(fair_prob),
            vig_percentage=vig,
            market_width=round((p_a + p_b) * 100, 4),
        )


# ── EV Calculator ─────────────────────────────────────────────────────────────

class EVCalculator:
    """Calculate expected value and Kelly Criterion stake sizing."""

    @staticmethod
    def expected_value(fair_probability: float, offered_american_odds: int) -> float:
        """
        EV% = (fair_prob * decimal_odds - 1) * 100
        Positive value = +EV opportunity.
        """
        if offered_american_odds < 0:
            decimal = 1 + (100 / abs(offered_american_odds))
        else:
            decimal = 1 + (offered_american_odds / 100)
        ev = (fair_probability * decimal - 1) * 100
        return round(ev, 2)

    @staticmethod
    def kelly_fraction(fair_probability: float, offered_american_odds: int) -> float:
        """Full Kelly Criterion — fraction of bankroll to stake."""
        if offered_american_odds < 0:
            decimal = 1 + (100 / abs(offered_american_odds))
        else:
            decimal = 1 + (offered_american_odds / 100)
        b = decimal - 1  # net profit per unit staked
        q = 1 - fair_probability
        kelly = (b * fair_probability - q) / b
        return max(0.0, round(kelly, 4))

    @staticmethod
    def build_ev_result(selection: str, fair_odds: FairOdds, offered_american_odds: int) -> EVResult:
        ev = EVCalculator.expected_value(fair_odds.fair_probability, offered_american_odds)
        kelly = EVCalculator.kelly_fraction(fair_odds.fair_probability, offered_american_odds)
        return EVResult(
            selection=selection,
            fair_odds=fair_odds,
            offered_american_odds=offered_american_odds,
            ev_percentage=ev,
            edge=round(fair_odds.fair_probability - VigRemover.american_to_implied(offered_american_odds), 4),
            kelly_fraction=kelly,
            half_kelly=round(kelly / 2, 4),
        )


# ── Steam Detector ─────────────────────────────────────────────────────────────

class SteamDetector:
    """
    Detect steam moves from odds movement data.

    Steam score heuristic (0–100):
      • Rapid line movement in a short window       +40 pts max
      • Multiple books moving simultaneously        +30 pts max
      • Movement against public betting %           +20 pts max (placeholder)
      • Reverse line movement signal                +10 pts max (placeholder)
    """

    @staticmethod
    def score_movement(movement: OddsMovement, books_moved: list[str]) -> int:
        score = 0

        # 1. Magnitude of odds change
        change = abs(movement.odds_change)
        if change >= 20:
            score += 40
        elif change >= 15:
            score += 30
        elif change >= 10:
            score += 20
        elif change >= 5:
            score += 10

        # 2. Number of sportsbooks that moved
        num_books = len(books_moved)
        if num_books >= 5:
            score += 30
        elif num_books >= 3:
            score += 20
        elif num_books >= 2:
            score += 10

        # 3. Line movement (spread/total)
        if movement.line_change is not None:
            lc = abs(movement.line_change)
            if lc >= 2.0:
                score += 20
            elif lc >= 1.0:
                score += 10
            elif lc >= 0.5:
                score += 5

        # 4. Placeholder: public % vs movement contra (future ML signal)
        # score += _public_contra_signal(movement)

        return min(score, 100)

    @staticmethod
    def build_steam_alert(
        movement: OddsMovement,
        sport: Sport,
        market_type: MarketType,
        event: str,
        selection: str,
        books_moved: list[str],
    ) -> SteamAlert:
        score = SteamDetector.score_movement(movement, books_moved)
        direction = "DOWN" if movement.odds_change < 0 else "UP"
        return SteamAlert(
            alert_type=AlertType.STEAM,
            sport=sport,
            market_type=market_type,
            event=event,
            selection=selection,
            opening_odds=movement.opening.american_odds,
            current_odds=movement.current.american_odds,
            steam_score=score,
            steam_direction=direction,
            books_moved=books_moved,
            timestamp=datetime.utcnow(),
        )


# ── AI Confidence Scorer ───────────────────────────────────────────────────────

class AIConfidenceScorer:
    """
    Multi-signal confidence scorer (0–100).

    Signals:
      • EV magnitude                     (25 pts max)
      • Steam score                      (25 pts max)
      • Line shopping efficiency         (20 pts max)
      • Historical accuracy (placeholder)(20 pts max)
      • Market liquidity (placeholder)   (10 pts max)
    """

    @staticmethod
    def score(ev_percentage: float, steam_score: int, line_shopping_gap: int = 0) -> int:
        score = 0

        # EV signal
        if ev_percentage >= 10:
            score += 25
        elif ev_percentage >= 7:
            score += 20
        elif ev_percentage >= 5:
            score += 15
        elif ev_percentage >= 3:
            score += 10
        elif ev_percentage > 0:
            score += 5

        # Steam signal
        score += int(steam_score * 0.25)

        # Line shopping gap (difference between best and worst available odds)
        if line_shopping_gap >= 20:
            score += 20
        elif line_shopping_gap >= 10:
            score += 12
        elif line_shopping_gap >= 5:
            score += 6

        # Historical accuracy placeholder (future ML model hook)
        # score += _historical_model_signal(...)

        # Market liquidity placeholder
        # score += _liquidity_signal(...)

        return min(score, 100)


# ── Recommendation Engine ─────────────────────────────────────────────────────

def _to_recommendation(ev: float, confidence: int, steam: int) -> tuple[Recommendation, int]:
    """Return (Recommendation, stars 1-5)."""
    composite = ev * 0.5 + confidence * 0.3 + steam * 0.2

    if composite >= 70 and ev >= 8:
        return Recommendation.STRONG_BET, 5
    elif composite >= 55 and ev >= 5:
        return Recommendation.STRONG_BET, 4
    elif composite >= 40 and ev >= 3:
        return Recommendation.BET, 3
    elif composite >= 25 and ev > 0:
        return Recommendation.LEAN, 2
    elif ev < 0:
        return Recommendation.FADE, 1
    else:
        return Recommendation.PASS, 1


def _reason_codes(
    ev: float,
    steam_score: int,
    ai_confidence: int,
    line_change: Optional[float],
    books_moved: list[str],
) -> list[str]:
    codes: list[str] = []
    if ev >= 5:
        codes.append(f"High EV ({ev:+.1f}%)")
    elif ev > 0:
        codes.append(f"Positive EV ({ev:+.1f}%)")
    if steam_score >= 80:
        codes.append("Strong steam detected")
    elif steam_score >= 60:
        codes.append("Moderate steam detected")
    if len(books_moved) >= 3:
        codes.append(f"Multi-book movement ({len(books_moved)} books)")
    if line_change and abs(line_change) >= 1.0:
        codes.append(f"Line moved {line_change:+.1f}")
    if ai_confidence >= 80:
        codes.append("High AI confidence")
    if not codes:
        codes.append("Marginal edge — monitor")
    return codes


# ── Main Analysis Engine ───────────────────────────────────────────────────────

class AnalysisEngine:
    """
    Orchestrates vig removal, EV calculation, steam detection, and confidence
    scoring into a single EVOpportunity output.
    """

    def __init__(self) -> None:
        self._vig = VigRemover()
        self._ev = EVCalculator()
        self._steam = SteamDetector()
        self._ai = AIConfidenceScorer()

    def analyze_line(
        self,
        *,
        sport: Sport,
        market_type: MarketType,
        event: str,
        selection: str,
        player: Optional[str],
        line: Optional[float],
        side_a_odds: int,
        side_b_odds: int,
        is_side_a: bool,
        best_book: str,
        movement: Optional[OddsMovement] = None,
        books_moved: Optional[list[str]] = None,
    ) -> EVOpportunity:
        """
        Full analysis pipeline.

        Returns an EVOpportunity with EV, steam score, AI confidence,
        recommendation, and reason codes.
        """
        books_moved = books_moved or []
        fair = self._vig.build_fair_odds(selection, side_a_odds, side_b_odds, is_side_a)
        offered_odds = side_a_odds if is_side_a else side_b_odds
        ev_result = self._ev.build_ev_result(selection, fair, offered_odds)

        steam_alert: Optional[SteamAlert] = None
        steam_score = 0
        line_change: Optional[float] = None
        if movement:
            steam_alert = self._steam.build_steam_alert(
                movement, sport, market_type, event, selection, books_moved
            )
            steam_score = steam_alert.steam_score
            line_change = movement.line_change

        ai_conf = self._ai.score(
            ev_result.ev_percentage,
            steam_score,
            line_shopping_gap=abs(side_a_odds - side_b_odds),
        )

        recommendation, stars = _to_recommendation(
            ev_result.ev_percentage, ai_conf, steam_score
        )
        reason_codes = _reason_codes(
            ev_result.ev_percentage, steam_score, ai_conf, line_change, books_moved
        )

        return EVOpportunity(
            ev_result=ev_result,
            steam_alert=steam_alert,
            sport=sport,
            market_type=market_type,
            event=event,
            player=player,
            line=line,
            best_odds=offered_odds,
            best_book=best_book,
            fair_probability=fair.fair_probability,
            expected_value=ev_result.ev_percentage,
            steam_score=steam_score,
            ai_confidence=ai_conf,
            recommendation=recommendation,
            stars=stars,
            reason_codes=reason_codes,
        )

    # ── Placeholder stubs for future integrations ──────────────────────────

    async def fetch_live_odds(self, sport: Sport) -> list[OddsLine]:
        """
        PLACEHOLDER: Fetch live odds from sportsbook APIs.
        TODO: Integrate with The Odds API, Pinnacle API, etc.
        """
        logger.debug("fetch_live_odds called for %s (not yet implemented)", sport)
        return []

    async def fetch_prizepicks_lines(self, sport: Sport) -> list[OddsLine]:
        """
        PLACEHOLDER: Fetch PrizePicks player prop lines.
        TODO: Integrate with PrizePicks unofficial API / scraper.
        """
        logger.debug("fetch_prizepicks_lines called for %s (not yet implemented)", sport)
        return []

    async def run_ml_model(self, opportunity: EVOpportunity) -> int:
        """
        PLACEHOLDER: Run ML model to refine AI confidence score.
        TODO: Load trained model (sklearn / pytorch) and return confidence 0–100.
        """
        logger.debug("run_ml_model called (not yet implemented)")
        return opportunity.ai_confidence

    async def compute_clv(self, ev_record_id: int, closing_odds: int) -> float:
        """
        PLACEHOLDER: Compute Closing Line Value post-event.
        TODO: Fetch closing odds from DB / API and compare to bet odds.
        """
        logger.debug("compute_clv called (not yet implemented)")
        return 0.0


# Singleton engine instance
engine = AnalysisEngine()
