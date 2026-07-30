"""
End-to-end pipeline test for the Sharp Money +EV Detection Bot.

Covers every stage from raw odds inputs through to database persistence:

  AnalysisEngine
    ├─ VigRemover  (vig removal math)
    ├─ EVCalculator (EV + Kelly)
    ├─ SteamDetector (movement scoring)
    └─ AIConfidenceScorer (multi-signal confidence)

  Alert formatting
    ├─ format_ev_alert    (all required fields present in HTML)
    └─ format_steam_alert (all required fields present in HTML)

  Risk factors
    ├─ compute_ev_risk_factors
    └─ compute_steam_risk_factors

  AlertDelivery (full pipeline)
    ├─ filter: below-threshold alert is blocked
    ├─ send:   above-threshold alert reaches Telegram mock
    ├─ log:    EVRecord / SteamRecord written to real SQLite
    └─ dedup:  second identical alert is suppressed
"""

from __future__ import annotations

import sys
import os

# Make bot/ importable regardless of where pytest is invoked from.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from database import Database
from engine import AnalysisEngine
from engine.analysis import (
    VigRemover,
    EVCalculator,
    SteamDetector,
    AIConfidenceScorer,
)
from models import (
    AlertType,
    EVOpportunity,
    MarketType,
    OddsLine,
    OddsMovement,
    Recommendation,
    Sport,
    SteamAlert,
)
from alerts import (
    AlertDelivery,
    DeliveryResult,
    RiskFactor,
    compute_ev_risk_factors,
    compute_steam_risk_factors,
    format_ev_alert,
    format_steam_alert,
    identify_sharp_books,
)


# ── Test fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    """Fresh in-memory SQLite database for each test."""
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def engine():
    return AnalysisEngine()


@pytest.fixture
def mock_bot():
    """Telegram Bot mock that captures sent messages."""
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    return bot


# ── High-confidence scenario constants ────────────────────────────────────────
#
# NFL moneyline:  Chiefs (away) @ Raiders (home)
#
# Offered by soft book FanDuel: Chiefs at +155 (implied 39.22%)
# Best available opposite:       Raiders at +115 (implied 46.51%)
# Synthetic cross-book total:    85.73%  → negative synthetic vig → +EV
# Fair probability (Chiefs):     39.22 / 85.73 = 45.75%
# EV at +155:                    (0.4575 × 2.55 − 1) × 100 = +16.67%
# Kelly fraction:                (1.55 × 0.4575 − 0.5425) / 1.55 ≈ 10.7%
#
# Steam move:  Chiefs line moved from −130 to −155 at 5 books simultaneously
# Steam score:  change ≥ 20 → 40 pts | 5 books → 30 pts  = 70 / 100

OFFERED_ODDS   = +155   # FanDuel's Chiefs line
OPP_BEST_ODDS  = +115   # best available Raiders line
OPENING_ODDS   = -130   # Chiefs line before steam
CURRENT_ODDS   = -155   # Chiefs line after steam

SPORT     = Sport.NFL
MKT_TYPE  = MarketType.MONEYLINE
EVENT     = "Kansas City Chiefs @ Las Vegas Raiders"
SELECTION = "Kansas City Chiefs"
BOOK      = "FanDuel"

STEAM_BOOKS = ["Pinnacle", "Circa Sports", "DraftKings", "FanDuel", "BetMGM"]


# ── 1. VigRemover ─────────────────────────────────────────────────────────────

class TestVigRemover:
    def test_american_to_implied_negative_odds(self):
        # -110 → 110/210 = 52.38%
        p = VigRemover.american_to_implied(-110)
        assert abs(p - 0.5238) < 0.0001

    def test_american_to_implied_positive_odds(self):
        # +150 → 100/250 = 40.0%
        p = VigRemover.american_to_implied(+150)
        assert abs(p - 0.4000) < 0.0001

    def test_fair_probs_sum_to_one(self):
        fp_a, fp_b = VigRemover.fair_probability_multiplicative(-110, -110)
        assert abs(fp_a + fp_b - 1.0) < 1e-9
        assert abs(fp_a - 0.5) < 1e-9  # symmetric market

    def test_vig_percentage_standard_market(self):
        # -110 / -110 market → vig ≈ 4.76%
        vig = VigRemover.vig_percentage(-110, -110)
        assert abs(vig - 4.7619) < 0.001

    def test_build_fair_odds_side_a(self):
        fair = VigRemover.build_fair_odds("Chiefs", OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        assert fair.selection == "Chiefs"
        assert 0.44 < fair.fair_probability < 0.48  # ~45.75%
        assert fair.vig_percentage < 0              # synthetic market has negative vig

    def test_fair_american_odds_roundtrip(self):
        for p in (0.3, 0.45, 0.55, 0.7):
            american = VigRemover.fair_american_odds(p)
            back = VigRemover.american_to_implied(american)
            assert abs(back - p) < 0.05  # within 5pp after rounding to nearest 5


# ── 2. EVCalculator ────────────────────────────────────────────────────────────

class TestEVCalculator:
    def test_positive_ev_scenario(self):
        # fair_prob > implied_prob → positive EV
        fair = VigRemover.build_fair_odds(SELECTION, OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        ev_result = EVCalculator.build_ev_result(SELECTION, fair, OFFERED_ODDS)
        assert ev_result.ev_percentage > 10.0, (
            f"Expected strong +EV but got {ev_result.ev_percentage:.2f}%"
        )

    def test_negative_ev_standard_vig_market(self):
        # standard -110/-110 market: both sides are -EV for the bettor
        fair = VigRemover.build_fair_odds("Home", -110, -110, is_side_a=True)
        ev_result = EVCalculator.build_ev_result("Home", fair, -110)
        assert ev_result.ev_percentage < 0

    def test_kelly_fraction_positive_ev(self):
        fair = VigRemover.build_fair_odds(SELECTION, OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        ev_result = EVCalculator.build_ev_result(SELECTION, fair, OFFERED_ODDS)
        assert ev_result.kelly_fraction > 0
        assert ev_result.half_kelly == round(ev_result.kelly_fraction / 2, 4)

    def test_zero_kelly_when_negative_ev(self):
        fair = VigRemover.build_fair_odds("Home", -110, -110, is_side_a=True)
        ev_result = EVCalculator.build_ev_result("Home", fair, -110)
        assert ev_result.kelly_fraction == 0.0

    def test_edge_sign_matches_ev_sign(self):
        fair = VigRemover.build_fair_odds(SELECTION, OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        ev_result = EVCalculator.build_ev_result(SELECTION, fair, OFFERED_ODDS)
        assert (ev_result.edge > 0) == (ev_result.ev_percentage > 0)


# ── 3. SteamDetector ──────────────────────────────────────────────────────────

class TestSteamDetector:
    def _make_movement(self, opening: int, current: int, line: float | None = None):
        opening_line = OddsLine(
            sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION, american_odds=opening, line=line,
        )
        current_line = OddsLine(
            sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION, american_odds=current, line=line,
        )
        return OddsMovement(opening=opening_line, current=current_line)

    def test_large_move_many_books_high_score(self):
        mv = self._make_movement(OPENING_ODDS, CURRENT_ODDS)  # change = -25
        score = SteamDetector.score_movement(mv, STEAM_BOOKS)  # 5 books
        # 40 pts (change≥20) + 30 pts (5 books) = 70
        assert score == 70

    def test_small_move_single_book_low_score(self):
        mv = self._make_movement(-110, -112)  # change = -2
        score = SteamDetector.score_movement(mv, ["DraftKings"])
        assert score < 20

    def test_line_change_adds_to_score(self):
        # opening line=3.0, current line=4.5  → line_change = +1.5 → +10 pts
        opening = OddsLine(sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
                           event=EVENT, selection=SELECTION, american_odds=-110, line=3.0)
        current = OddsLine(sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
                           event=EVENT, selection=SELECTION, american_odds=-130, line=4.5)
        mv_with_line = OddsMovement(opening=opening, current=current)
        mv_no_line   = self._make_movement(-110, -130)   # line=None → no line pts
        score_with = SteamDetector.score_movement(mv_with_line, ["A", "B", "C"])
        score_no   = SteamDetector.score_movement(mv_no_line,   ["A", "B", "C"])
        assert score_with > score_no

    def test_score_capped_at_100(self):
        mv = self._make_movement(-100, -200, line=5.0)  # huge change + big line
        score = SteamDetector.score_movement(mv, ["A", "B", "C", "D", "E", "F"])
        assert score <= 100

    def test_build_steam_alert_direction(self):
        mv = self._make_movement(OPENING_ODDS, CURRENT_ODDS)
        alert = SteamDetector.build_steam_alert(mv, SPORT, MKT_TYPE, EVENT, SELECTION, STEAM_BOOKS)
        assert alert.steam_direction == "DOWN"   # CURRENT < OPENING
        assert alert.opening_odds == OPENING_ODDS
        assert alert.current_odds == CURRENT_ODDS
        assert alert.books_moved == STEAM_BOOKS


# ── 4. AIConfidenceScorer ─────────────────────────────────────────────────────
#
# Score breakdown (100-pt ceiling, 5 live signals):
#   EV Edge              0–25 pts
#   Steam Score          0–25 pts   (steam × 0.25)
#   Sharp Book Presence  0–20 pts
#   Line Shopping Gap    0–20 pts
#   Market Tightness     0–10 pts   (vig_pct ≤ 0 → 10, ≤ 3 → 7, ≤ 6 → 3)
#
# Star bands (from confidence score):
#   90–100 → 5★   75–89 → 4★   60–74 → 3★   40–59 → 2★   <40 → 1★

class TestAIConfidenceScorer:
    def test_three_star_range(self):
        # EV(25) + steam70(17) + gap40(20) + vig=10(0) = 62 → 3★ band (60-74)
        score = AIConfidenceScorer.score(
            ev_percentage=17.0, steam_score=70,
            line_shopping_gap=40, vig_pct=10.0,
        )
        assert 60 <= score <= 74, f"Expected 3★ band (60-74), got {score}"

    def test_four_star_range_reachable(self):
        # EV(25) + steam60(15) + 2sharp(14) + gap20(20) + vig0(10) = 84 → 4★ (75-89)
        score = AIConfidenceScorer.score(
            ev_percentage=8.0, steam_score=60,
            sharp_book_count=2, line_shopping_gap=20, vig_pct=0.0,
        )
        assert 75 <= score <= 89, f"Expected 4★ band (75-89), got {score}"

    def test_five_star_range_reachable(self):
        # Elite: EV(25) + steam100(25) + 3sharp(20) + gap40(20) + vig-5(10) = 100 → 5★
        score = AIConfidenceScorer.score(
            ev_percentage=17.0, steam_score=100,
            sharp_book_count=3, line_shopping_gap=40, vig_pct=-5.0,
        )
        assert score == 100, f"Elite conditions should yield 100, got {score}"

    def test_zero_ev_zero_steam_low_confidence(self):
        score = AIConfidenceScorer.score(ev_percentage=0.0, steam_score=0, line_shopping_gap=0)
        assert score == 0

    def test_score_capped_at_100(self):
        # All signals pegged to max should land at exactly 100 (not exceed it).
        score = AIConfidenceScorer.score(
            ev_percentage=50.0, steam_score=400,
            sharp_book_count=10, line_shopping_gap=200, vig_pct=-100.0,
        )
        assert score == 100

    def test_marginal_ev_gives_some_score(self):
        # Isolated EV signal only (no steam, no sharp, high vig → 0 pts from other signals).
        score = AIConfidenceScorer.score(
            ev_percentage=1.5, steam_score=0,
            sharp_book_count=0, line_shopping_gap=0, vig_pct=10.0,
        )
        assert score == 5  # 0 < ev < 3 → 5 pts

    def test_sharp_books_add_correct_points(self):
        base = AIConfidenceScorer.score(
            ev_percentage=5.0, steam_score=0,
            sharp_book_count=0, line_shopping_gap=0, vig_pct=10.0,
        )
        one   = AIConfidenceScorer.score(
            ev_percentage=5.0, steam_score=0,
            sharp_book_count=1, line_shopping_gap=0, vig_pct=10.0,
        )
        two   = AIConfidenceScorer.score(
            ev_percentage=5.0, steam_score=0,
            sharp_book_count=2, line_shopping_gap=0, vig_pct=10.0,
        )
        three = AIConfidenceScorer.score(
            ev_percentage=5.0, steam_score=0,
            sharp_book_count=3, line_shopping_gap=0, vig_pct=10.0,
        )
        assert base < one < two < three
        assert one   - base  == 8   # 1 sharp  → +8 pts
        assert two   - base  == 14  # 2 sharps → +14 pts
        assert three - base  == 20  # 3 sharps → +20 pts

    def test_vig_quality_tiers(self):
        # vig ≤ 0 → 10 pts
        neg  = AIConfidenceScorer.score(ev_percentage=0.0, steam_score=0, vig_pct=-1.0)
        zero = AIConfidenceScorer.score(ev_percentage=0.0, steam_score=0, vig_pct=0.0)
        low  = AIConfidenceScorer.score(ev_percentage=0.0, steam_score=0, vig_pct=2.0)
        mid  = AIConfidenceScorer.score(ev_percentage=0.0, steam_score=0, vig_pct=5.0)
        high = AIConfidenceScorer.score(ev_percentage=0.0, steam_score=0, vig_pct=10.0)
        assert neg  == 10
        assert zero == 10
        assert low  == 7
        assert mid  == 3
        assert high == 0


# ── 5. AnalysisEngine (full pipeline) ─────────────────────────────────────────

class TestAnalysisEngine:
    def test_analyze_line_returns_ev_opportunity(self, engine):
        opp = engine.analyze_line(
            sport=SPORT,
            market_type=MKT_TYPE,
            event=EVENT,
            selection=SELECTION,
            player=None,
            line=None,
            side_a_odds=OFFERED_ODDS,
            side_b_odds=OPP_BEST_ODDS,
            is_side_a=True,
            best_book=BOOK,
        )
        assert isinstance(opp, EVOpportunity)

    def test_analyze_line_positive_ev(self, engine):
        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
        )
        assert opp.expected_value > 10.0, (
            f"Expected strong +EV, got {opp.expected_value:.2f}%"
        )

    def test_analyze_line_with_steam(self, engine):
        opening = OddsLine(sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
                           event=EVENT, selection=SELECTION, american_odds=OPENING_ODDS)
        current = OddsLine(sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
                           event=EVENT, selection=SELECTION, american_odds=CURRENT_ODDS)
        movement = OddsMovement(opening=opening, current=current)

        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=movement, books_moved=STEAM_BOOKS,
        )
        assert opp.steam_score > 0
        assert opp.steam_alert is not None
        assert opp.steam_alert.steam_score == opp.steam_score

    def test_analyze_line_recommendation_and_stars(self, engine):
        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
        )
        assert opp.recommendation in list(Recommendation)
        assert 1 <= opp.stars <= 5

    def test_analyze_line_reason_codes_populated(self, engine):
        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
        )
        assert len(opp.reason_codes) > 0

    def test_analyze_line_negative_ev_fade(self, engine):
        # Standard vig market — should be FADE or PASS
        opp = engine.analyze_line(
            sport=Sport.NBA, market_type=MarketType.SPREAD, event="Lakers vs Celtics",
            selection="Lakers -4.5", player=None, line=-4.5,
            side_a_odds=-110, side_b_odds=-110,
            is_side_a=True, best_book="DraftKings",
        )
        assert opp.expected_value < 0
        assert opp.recommendation in (Recommendation.FADE, Recommendation.PASS)


# ── 6. Risk factor computation ────────────────────────────────────────────────

class TestRiskFactors:
    def _make_ev_opp(self, *, ev=16.67, steam=70, confidence=62,
                    vig=None, odds=OFFERED_ODDS):
        fair = VigRemover.build_fair_odds(SELECTION, OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        ev_result = EVCalculator.build_ev_result(SELECTION, fair, OFFERED_ODDS)
        return EVOpportunity(
            ev_result=ev_result,
            steam_alert=None,
            sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, player=None, line=None,
            best_odds=odds, best_book=BOOK,
            fair_probability=fair.fair_probability,
            expected_value=ev,
            steam_score=steam,
            ai_confidence=confidence,
            recommendation=Recommendation.STRONG_BET,
            stars=5,
            reason_codes=["High EV (+16.7%)"],
        )

    def _make_steam_alert(self, *, books=None, opening=-130, current=-155):
        return SteamAlert(
            alert_type=AlertType.STEAM,
            sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=opening, current_odds=current,
            steam_score=70, steam_direction="DOWN",
            books_moved=books or STEAM_BOOKS,
        )

    def test_no_risk_for_clean_opportunity(self):
        opp = self._make_ev_opp(ev=16.67, steam=70, confidence=80)
        factors = compute_ev_risk_factors(opp)
        # Healthy EV + good steam + high confidence → no HIGH risks
        high_risks = [f for f in factors if f.level == "HIGH"]
        assert len(high_risks) == 0

    def test_no_steam_adds_medium_risk(self):
        opp = self._make_ev_opp(ev=16.67, steam=0, confidence=80)
        factors = compute_ev_risk_factors(opp)
        descs = [f.description for f in factors]
        assert any("steam" in d.lower() for d in descs)

    def test_single_book_steam_is_high_risk(self):
        alert = self._make_steam_alert(books=["DraftKings"])
        factors = compute_steam_risk_factors(alert)
        high = [f for f in factors if f.level == "HIGH"]
        assert len(high) > 0
        assert any("single" in f.description.lower() for f in high)

    def test_no_sharp_books_is_medium_risk(self):
        alert = self._make_steam_alert(books=["DraftKings", "FanDuel", "BetMGM"])
        factors = compute_steam_risk_factors(alert)
        medium = [f for f in factors if f.level == "MEDIUM"]
        assert any("sharp" in f.description.lower() for f in medium)

    def test_sharp_books_present_no_sharp_risk(self):
        alert = self._make_steam_alert(books=["Pinnacle", "Circa Sports", "DraftKings"])
        factors = compute_steam_risk_factors(alert)
        assert not any("sharp" in f.description.lower() and f.level == "MEDIUM"
                       for f in factors)

    def test_risk_factor_icons(self):
        for level, expected_icon in [("HIGH", "🔴"), ("MEDIUM", "🟡"), ("LOW", "🔵")]:
            rf = RiskFactor(level=level, description="test")
            assert rf.icon == expected_icon


# ── 7. Alert formatting ───────────────────────────────────────────────────────

class TestAlertFormatting:
    """Verify all required fields appear in the formatted Telegram HTML."""

    def _full_opp(self, engine_fixture) -> EVOpportunity:
        opening = OddsLine(sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
                           event=EVENT, selection=SELECTION, american_odds=OPENING_ODDS)
        current = OddsLine(sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
                           event=EVENT, selection=SELECTION, american_odds=CURRENT_ODDS)
        movement = OddsMovement(opening=opening, current=current)
        return engine_fixture.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player="Patrick Mahomes", line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=movement, books_moved=STEAM_BOOKS,
        )

    def test_ev_alert_contains_alert_type(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "EV" in msg or "OPPORTUNITY" in msg

    def test_ev_alert_contains_sport(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "NFL" in msg

    def test_ev_alert_contains_event(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Raiders" in msg

    def test_ev_alert_contains_player(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Patrick Mahomes" in msg

    def test_ev_alert_contains_sportsbook(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert BOOK in msg

    def test_ev_alert_contains_odds(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "+155" in msg

    def test_ev_alert_contains_fair_probability(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        # Fair probability shown as XX.X%
        assert "%" in msg
        assert "Fair Prob" in msg

    def test_ev_alert_contains_ev_percentage(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Expected Value" in msg
        assert "%" in msg

    def test_ev_alert_contains_steam_score(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Steam Score" in msg
        assert "/100" in msg

    def test_ev_alert_contains_books_moving(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        # At least one book name from STEAM_BOOKS should appear
        assert any(b in msg for b in STEAM_BOOKS)

    def test_ev_alert_contains_sharp_books(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Sharp" in msg or "Pinnacle" in msg

    def test_ev_alert_contains_ai_confidence(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "AI Confidence" in msg

    def test_ev_alert_contains_star_rating(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Rating" in msg
        assert "★" in msg

    def test_ev_alert_contains_risk_factors(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Risk" in msg

    def test_ev_alert_contains_market_type(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        assert "Moneyline" in msg

    def test_ev_alert_is_valid_html(self, engine):
        msg = format_ev_alert(self._full_opp(engine))
        # Must have balanced <b>...</b> tags (Telegram HTML)
        assert msg.count("<b>") == msg.count("</b>")
        assert msg.count("<i>") == msg.count("</i>")
        assert msg.count("<code>") == msg.count("</code>")

    def test_steam_alert_contains_required_fields(self):
        alert = SteamAlert(
            alert_type=AlertType.STEAM, sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=OPENING_ODDS, current_odds=CURRENT_ODDS,
            steam_score=70, steam_direction="DOWN",
            books_moved=STEAM_BOOKS,
        )
        msg = format_steam_alert(alert)

        assert "SHARP MONEY ALERT" in msg             # alert type
        assert "NFL" in msg                           # sport / league
        assert "Raiders" in msg                       # event
        assert "Moneyline" in msg                     # market
        assert SELECTION in msg                       # selection
        assert str(OPENING_ODDS) in msg               # opening odds
        assert str(CURRENT_ODDS) in msg               # current odds
        assert "Steam Score" in msg                   # steam score
        assert "70" in msg
        assert any(b in msg for b in STEAM_BOOKS)     # books moving
        assert "Pinnacle" in msg                      # sharp book
        assert "Risk" in msg                          # risk factors
        assert "DOWN" in msg or "FALLING" in msg      # direction

    def test_steam_alert_score_bar_present(self):
        alert = SteamAlert(
            alert_type=AlertType.STEAM, sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=OPENING_ODDS, current_odds=CURRENT_ODDS,
            steam_score=70, steam_direction="DOWN", books_moved=STEAM_BOOKS,
        )
        msg = format_steam_alert(alert)
        assert "█" in msg  # visual score bar


# ── 8. Sharp book identification ──────────────────────────────────────────────

class TestSharpBooks:
    def test_identifies_known_sharp_books(self):
        books = ["Pinnacle", "FanDuel", "Circa Sports", "BetMGM"]
        sharp = identify_sharp_books(books)
        assert "Pinnacle" in sharp
        assert "Circa Sports" in sharp
        assert "FanDuel" not in sharp
        assert "BetMGM" not in sharp

    def test_returns_empty_for_all_soft(self):
        assert identify_sharp_books(["DraftKings", "FanDuel", "BetMGM"]) == []

    def test_returns_empty_for_empty_input(self):
        assert identify_sharp_books([]) == []


# ── 9. AlertDelivery — filtered (below threshold) ─────────────────────────────

class TestAlertDeliveryFiltering:
    def _low_ev_opp(self) -> EVOpportunity:
        """An opportunity well below the default MIN_EV_THRESHOLD (3.0%)."""
        fair = VigRemover.build_fair_odds("Home", -110, -110, is_side_a=True)
        ev_result = EVCalculator.build_ev_result("Home", fair, -110)
        return EVOpportunity(
            ev_result=ev_result, steam_alert=None,
            sport=Sport.NBA, market_type=MarketType.SPREAD,
            event="Lakers vs Celtics", player=None, line=-4.5,
            best_odds=-110, best_book="DraftKings",
            fair_probability=0.5, expected_value=-2.3,
            steam_score=0, ai_confidence=30,
            recommendation=Recommendation.FADE, stars=1,
            reason_codes=["Negative EV"],
        )

    async def test_below_ev_threshold_not_sent(self, db, mock_bot):
        delivery = AlertDelivery(db, mock_bot, [12345], min_ev=3.0, min_confidence=0)
        result = await delivery.deliver_ev(self._low_ev_opp())
        assert result.filtered
        assert "EV" in result.filtered_reason
        mock_bot.send_message.assert_not_called()

    async def test_below_confidence_threshold_not_sent(self, db, mock_bot):
        fair = VigRemover.build_fair_odds(SELECTION, OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        ev_result = EVCalculator.build_ev_result(SELECTION, fair, OFFERED_ODDS)
        opp = EVOpportunity(
            ev_result=ev_result, steam_alert=None,
            sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, player=None, line=None,
            best_odds=OFFERED_ODDS, best_book=BOOK,
            fair_probability=fair.fair_probability,
            expected_value=15.0,   # high EV
            steam_score=0, ai_confidence=20,  # but low confidence
            recommendation=Recommendation.BET, stars=3,
            reason_codes=["High EV"],
        )
        delivery = AlertDelivery(db, mock_bot, [12345], min_ev=3.0, min_confidence=50)
        result = await delivery.deliver_ev(opp)
        assert result.filtered
        assert "confidence" in result.filtered_reason.lower()
        mock_bot.send_message.assert_not_called()

    async def test_below_steam_threshold_not_sent(self, db, mock_bot):
        alert = SteamAlert(
            alert_type=AlertType.STEAM, sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=-110, current_odds=-115,
            steam_score=20, steam_direction="DOWN",
            books_moved=["DraftKings"],
        )
        delivery = AlertDelivery(db, mock_bot, [12345], min_steam=70)
        result = await delivery.deliver_steam(alert)
        assert result.filtered
        assert "20" in result.filtered_reason
        mock_bot.send_message.assert_not_called()


# ── 10. AlertDelivery — full happy-path end-to-end ────────────────────────────

class TestAlertDeliveryEndToEnd:
    """
    Full pipeline test using a real in-memory database.

    Flow:
      build opportunity → AlertDelivery → mock Telegram → SQLite EVRecord
    """

    def _high_conf_opp(self, engine_fixture: AnalysisEngine) -> EVOpportunity:
        opening = OddsLine(
            sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION, american_odds=OPENING_ODDS,
        )
        current = OddsLine(
            sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION, american_odds=CURRENT_ODDS,
        )
        return engine_fixture.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=OddsMovement(opening=opening, current=current),
            books_moved=STEAM_BOOKS,
        )

    async def test_ev_alert_sends_to_telegram(self, db, mock_bot, engine):
        opp = self._high_conf_opp(engine)
        delivery = AlertDelivery(
            db, mock_bot, [11111, 22222],
            min_ev=3.0, min_confidence=0,  # confidence=0 ensures we test the send path
        )
        result = await delivery.deliver_ev(opp)

        assert not result.filtered, f"Should not be filtered: {result}"
        assert result.sent
        assert result.recipients_sent == 2
        assert result.recipients_failed == 0
        assert mock_bot.send_message.call_count == 2

    async def test_ev_alert_message_sent_as_html(self, db, mock_bot, engine):
        opp = self._high_conf_opp(engine)
        delivery = AlertDelivery(db, mock_bot, [11111], min_ev=3.0, min_confidence=0)
        await delivery.deliver_ev(opp)

        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["parse_mode"] == "HTML"
        assert "<b>" in call_kwargs["text"]
        assert "EV" in call_kwargs["text"]

    async def test_ev_alert_stored_in_database(self, db, mock_bot, engine):
        opp = self._high_conf_opp(engine)
        assert await db.count_ev_records() == 0

        delivery = AlertDelivery(db, mock_bot, [11111], min_ev=3.0, min_confidence=0)
        await delivery.deliver_ev(opp)

        records = await db.get_recent_ev(limit=5)
        assert len(records) == 1
        rec = records[0]
        assert rec.event == EVENT
        assert rec.selection == SELECTION
        assert rec.best_book == BOOK
        assert rec.alert_sent is True
        assert rec.expected_value > 10.0

    async def test_ev_alert_db_record_has_all_fields(self, db, mock_bot, engine):
        opp = self._high_conf_opp(engine)
        delivery = AlertDelivery(db, mock_bot, [11111], min_ev=3.0, min_confidence=0)
        await delivery.deliver_ev(opp)

        rec = (await db.get_recent_ev(limit=1))[0]
        assert rec.sport == "NFL"
        assert rec.market_type == "Moneyline"
        assert rec.fair_probability > 0
        assert rec.steam_score > 0
        assert rec.ai_confidence > 0
        assert rec.recommendation != ""
        assert rec.stars >= 1
        assert rec.reason_codes != ""

    async def test_steam_alert_sends_and_stores(self, db, mock_bot):
        alert = SteamAlert(
            alert_type=AlertType.STEAM, sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=OPENING_ODDS, current_odds=CURRENT_ODDS,
            steam_score=70, steam_direction="DOWN",
            books_moved=STEAM_BOOKS,
            notes="5 books moved simultaneously",
        )
        delivery = AlertDelivery(db, mock_bot, [11111], min_steam=60)
        result = await delivery.deliver_steam(alert)

        assert not result.filtered
        assert result.sent
        mock_bot.send_message.assert_called_once()

        steam_records = await db.get_recent_steam(limit=5)
        assert len(steam_records) == 1
        rec = steam_records[0]
        assert rec.event == EVENT
        assert rec.steam_score == 70
        assert rec.alert_sent is True
        assert "Pinnacle" in rec.books_moved

    async def test_steam_alert_message_contains_required_fields(self, db, mock_bot):
        alert = SteamAlert(
            alert_type=AlertType.STEAM, sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=OPENING_ODDS, current_odds=CURRENT_ODDS,
            steam_score=70, steam_direction="DOWN",
            books_moved=STEAM_BOOKS,
        )
        delivery = AlertDelivery(db, mock_bot, [11111], min_steam=60)
        await delivery.deliver_steam(alert)

        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "NFL" in text
        assert "Raiders" in text       # event
        assert "Moneyline" in text     # market
        assert SELECTION in text       # selection
        assert "70" in text            # steam score
        assert "Pinnacle" in text      # sharp book
        assert "Risk" in text          # risk section


# ── 11. AlertDelivery — deduplication ────────────────────────────────────────

class TestAlertDeliveryDeduplication:
    def _opp(self):
        fair = VigRemover.build_fair_odds(SELECTION, OFFERED_ODDS, OPP_BEST_ODDS, is_side_a=True)
        ev_result = EVCalculator.build_ev_result(SELECTION, fair, OFFERED_ODDS)
        return EVOpportunity(
            ev_result=ev_result, steam_alert=None,
            sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, player=None, line=None,
            best_odds=OFFERED_ODDS, best_book=BOOK,
            fair_probability=fair.fair_probability,
            expected_value=16.0,
            steam_score=0, ai_confidence=80,
            recommendation=Recommendation.STRONG_BET, stars=5,
            reason_codes=["High EV (+16%)"],
        )

    async def test_second_ev_alert_is_deduped(self, db, mock_bot):
        delivery = AlertDelivery(
            db, mock_bot, [11111],
            min_ev=3.0, min_confidence=0,
            ev_dedup_window=3600,
        )
        # First delivery: should send
        r1 = await delivery.deliver_ev(self._opp())
        assert r1.sent

        # Second delivery (same event/selection within dedup window): should be blocked
        r2 = await delivery.deliver_ev(self._opp())
        assert r2.deduped
        assert not r2.sent
        assert mock_bot.send_message.call_count == 1  # still only one real send

    async def test_second_steam_alert_is_deduped(self, db, mock_bot):
        alert = SteamAlert(
            alert_type=AlertType.STEAM, sport=SPORT, market_type=MKT_TYPE,
            event=EVENT, selection=SELECTION,
            opening_odds=OPENING_ODDS, current_odds=CURRENT_ODDS,
            steam_score=70, steam_direction="DOWN",
            books_moved=STEAM_BOOKS,
        )
        delivery = AlertDelivery(
            db, mock_bot, [11111],
            min_steam=60, steam_dedup_window=3600,
        )
        r1 = await delivery.deliver_steam(alert)
        assert r1.sent

        r2 = await delivery.deliver_steam(alert)
        assert r2.deduped
        assert mock_bot.send_message.call_count == 1

    async def test_different_events_both_send(self, db, mock_bot):
        """Two alerts for different events should each send independently."""
        delivery = AlertDelivery(
            db, mock_bot, [11111],
            min_ev=3.0, min_confidence=0, ev_dedup_window=3600,
        )
        opp_a = self._opp()
        opp_b = self._opp()
        object.__setattr__(opp_b, "event", "New England Patriots @ Buffalo Bills")
        # Override the nested selection reference via a fresh EVOpportunity copy
        from dataclasses import replace
        from engine.analysis import EVCalculator, VigRemover
        fair2 = VigRemover.build_fair_odds("Patriots", OFFERED_ODDS, OPP_BEST_ODDS, True)
        ev2   = EVCalculator.build_ev_result("Patriots", fair2, OFFERED_ODDS)
        opp_b2 = EVOpportunity(
            ev_result=ev2, steam_alert=None,
            sport=SPORT, market_type=MKT_TYPE,
            event="New England Patriots @ Buffalo Bills",
            player=None, line=None,
            best_odds=OFFERED_ODDS, best_book=BOOK,
            fair_probability=fair2.fair_probability,
            expected_value=16.0, steam_score=0, ai_confidence=80,
            recommendation=Recommendation.STRONG_BET, stars=5,
            reason_codes=["High EV"],
        )
        r1 = await delivery.deliver_ev(opp_a)
        r2 = await delivery.deliver_ev(opp_b2)
        assert r1.sent
        assert r2.sent
        assert mock_bot.send_message.call_count == 2


# ── 12. Confidence recalibration regression suite ─────────────────────────────
#
# Explicitly verifies the five guarantees introduced by the calibration:
#
#   A. Confidence score can reach 100
#   B. 5-star alerts are achievable through the full analyze_line pipeline
#   C. Sharp books raise the confidence score
#   D. Soft-only books yield a lower score than the same scenario with sharp books
#   E. Existing alert delivery tests still pass  (confirmed by 69/69 baseline run)

# ── Fixtures shared by this class ─────────────────────────────────────────────

# All five books are in config.sharp_books
ELITE_SHARP_BOOKS = ["Pinnacle", "Circa Sports", "Bookmaker.eu", "Heritage", "BetOnline"]

# Five well-known soft books — none appear in config.sharp_books
SOFT_BOOKS        = ["DraftKings", "FanDuel", "BetMGM", "PointsBet", "Caesars"]

# OddsMovement that produces steam_score = 80:
#   • odds_change = -25  (≥ 20  → 40 pts)
#   • 5 books            (≥ 5   → 30 pts)
#   • line_change = 0.5  (≥ 0.5 →  10 pts)
#   Total: 80 / 100
def _elite_movement() -> OddsMovement:
    opening = OddsLine(
        sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
        event=EVENT, selection=SELECTION,
        american_odds=OPENING_ODDS, line=3.0,
    )
    current = OddsLine(
        sportsbook="Pinnacle", sport=SPORT, market_type=MKT_TYPE,
        event=EVENT, selection=SELECTION,
        american_odds=CURRENT_ODDS, line=3.5,
    )
    return OddsMovement(opening=opening, current=current)


class TestConfidenceRecalibration:
    """
    Regression suite for the recalibrated AIConfidenceScorer.

    Each test maps to one explicit guarantee from the calibration spec.
    """

    # ── A. Confidence score can reach 100 ─────────────────────────────────────

    def test_confidence_score_reaches_100(self):
        """
        With all five live signals at their maximum, the scorer must return 100.

        Signal breakdown:
          EV ≥ 10%            → 25 pts
          steam = 100         → 25 pts  (100 × 0.25)
          sharp_book_count ≥ 3→ 20 pts
          line_shopping ≥ 20  → 20 pts
          vig ≤ 0             → 10 pts
          ─────────────────────────────
          Total               = 100 pts
        """
        score = AIConfidenceScorer.score(
            ev_percentage=17.0,
            steam_score=100,
            sharp_book_count=3,
            line_shopping_gap=40,
            vig_pct=-5.0,
        )
        assert score == 100, f"All signals at max must yield 100, got {score}"

    # ── B. 5-star alerts are achievable through the full analyze_line pipeline ─

    def test_five_star_alert_via_analyze_line(self, engine):
        """
        End-to-end: analyze_line with elite inputs must produce stars=5
        and ai_confidence ≥ 90.

        Scenario:
          Offered:  Chiefs +155 (FanDuel)     — strong cross-book +EV
          Opposite: Raiders +115 (best avail) — synthetic negative-vig market
          Steam:    -25 odds change, 0.5 line change, 5 all-sharp books → steam=80

        Expected ai_confidence:
          EV=16.67% ≥ 10  → 25 pts
          steam=80 × 0.25 → 20 pts
          5 sharp books   → 20 pts
          gap=40 ≥ 20     → 20 pts
          vig ≈ −14% ≤ 0  → 10 pts
          ─────────────────────────
          Total           = 95 pts  → 5★
        """
        opp = engine.analyze_line(
            sport=SPORT,
            market_type=MKT_TYPE,
            event=EVENT,
            selection=SELECTION,
            player=None,
            line=None,
            side_a_odds=OFFERED_ODDS,   # +155
            side_b_odds=OPP_BEST_ODDS,  # +115
            is_side_a=True,
            best_book=BOOK,
            movement=_elite_movement(),
            books_moved=ELITE_SHARP_BOOKS,
        )
        assert opp.stars == 5, (
            f"Elite inputs must produce 5★ but got {opp.stars}★ "
            f"(ai_confidence={opp.ai_confidence})"
        )
        assert opp.ai_confidence >= 90, (
            f"Elite inputs must score ≥ 90 confidence, got {opp.ai_confidence}"
        )
        assert opp.recommendation == Recommendation.STRONG_BET, (
            f"5★ alert must be STRONG_BET, got {opp.recommendation}"
        )

    def test_five_star_alert_formatted_shows_five_stars(self, engine):
        """The formatted Telegram message for a 5★ alert must display five stars."""
        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=_elite_movement(), books_moved=ELITE_SHARP_BOOKS,
        )
        assert opp.stars == 5  # pre-condition
        msg = format_ev_alert(opp)
        assert "★★★★★" in msg, "5★ alert HTML must contain ★★★★★"

    # ── C. Sharp books raise the confidence score ──────────────────────────────

    def test_sharp_books_raise_analyze_line_confidence(self, engine):
        """
        Swapping soft books for an equal number of sharp books in books_moved
        must increase ai_confidence by exactly 20 pts (the sharp-book-presence
        signal maxes at 20 for ≥ 3 sharp books).
        """
        mv = _elite_movement()

        soft_opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=mv, books_moved=SOFT_BOOKS,   # 0 sharp books
        )
        sharp_opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=mv, books_moved=ELITE_SHARP_BOOKS,  # 5 sharp books
        )
        assert sharp_opp.ai_confidence > soft_opp.ai_confidence, (
            f"Sharp books ({sharp_opp.ai_confidence}) must outscore soft books "
            f"({soft_opp.ai_confidence})"
        )
        diff = sharp_opp.ai_confidence - soft_opp.ai_confidence
        assert diff == 20, (
            f"≥3 sharp books add exactly 20 pts; got diff={diff} "
            f"(soft={soft_opp.ai_confidence}, sharp={sharp_opp.ai_confidence})"
        )

    # ── D. Soft-only books yield a lower score than sharp books ───────────────

    def test_soft_only_books_reduce_score_vs_sharp(self, engine):
        """
        A scenario where all books that moved are soft (DraftKings, FanDuel,
        BetMGM, PointsBet, Caesars) must produce a lower confidence score
        than the identical scenario with sharp books (Pinnacle, Circa, etc.).

        This confirms that the sharp-book-presence signal flows correctly from
        books_moved through analyze_line into ai_confidence.
        """
        mv = _elite_movement()

        soft_conf = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=mv, books_moved=SOFT_BOOKS,
        ).ai_confidence

        sharp_conf = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=mv, books_moved=ELITE_SHARP_BOOKS,
        ).ai_confidence

        assert soft_conf < sharp_conf, (
            f"Soft-only books ({soft_conf}) should score lower than sharp books ({sharp_conf})"
        )

    def test_soft_only_books_score_is_still_positive(self, engine):
        """
        A soft-book move with strong EV must still register confidence > 0 —
        soft books reduce quality but do not zero out the other signals.
        """
        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=_elite_movement(), books_moved=SOFT_BOOKS,
        )
        assert opp.ai_confidence > 0
        assert opp.expected_value > 10.0  # EV signal still fires regardless

    # ── E. Alert delivery still works after recalibration ─────────────────────

    async def test_five_star_alert_delivers_end_to_end(self, db, mock_bot, engine):
        """
        A 5★ opportunity produced by analyze_line must pass through AlertDelivery
        and arrive at Telegram as a properly formatted HTML message.
        """
        opp = engine.analyze_line(
            sport=SPORT, market_type=MKT_TYPE, event=EVENT,
            selection=SELECTION, player=None, line=None,
            side_a_odds=OFFERED_ODDS, side_b_odds=OPP_BEST_ODDS,
            is_side_a=True, best_book=BOOK,
            movement=_elite_movement(), books_moved=ELITE_SHARP_BOOKS,
        )
        assert opp.stars == 5  # pre-condition

        delivery = AlertDelivery(db, mock_bot, [99999], min_ev=3.0, min_confidence=0)
        result = await delivery.deliver_ev(opp)

        assert not result.filtered, f"5★ alert must not be filtered: {result}"
        assert result.sent
        mock_bot.send_message.assert_called_once()

        # Message should carry the 5-star rating
        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "★★★★★" in text, "Delivered message must display ★★★★★"

        # DB record must reflect the elite confidence
        records = await db.get_recent_ev(limit=1)
        assert records[0].ai_confidence >= 90
        assert records[0].stars == 5
