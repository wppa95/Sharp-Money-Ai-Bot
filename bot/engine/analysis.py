"""
analysis.py — AnalysisEngine orchestrator.

Wraps VigRemover, EVCalculator, SteamDetector, and AIConfidenceScorer into a
single entry-point used by main.py and commands.py.

Also owns fetch_live_odds() — the live data feed via The Odds API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp

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

# ── The Odds API — sport / market key mappings ────────────────────────────────

_SPORT_TO_ODDS_API_KEY: dict[Sport, str] = {
    Sport.NFL:    "americanfootball_nfl",
    Sport.NBA:    "basketball_nba",
    Sport.MLB:    "baseball_mlb",
    Sport.NHL:    "icehockey_nhl",
    Sport.NCAAF:  "americanfootball_ncaaf",
    Sport.NCAAB:  "basketball_ncaab",
    Sport.UFC:    "mma_mixed_martial_arts",
    Sport.WNBA:   "basketball_wnba",
    # Soccer — one Odds API key per league
    Sport.EPL:        "soccer_epl",
    Sport.LA_LIGA:    "soccer_spain_la_liga",
    Sport.SERIE_A:    "soccer_italy_serie_a",
    Sport.BUNDESLIGA: "soccer_germany_bundesliga",
    Sport.LIGUE_1:    "soccer_france_ligue_one",
    Sport.MLS:        "soccer_usa_mls",
    Sport.UCL:        "soccer_uefa_champs_league",
    Sport.SOCCER:     "soccer_epl",   # legacy alias — do not activate with EPL
}

_MARKET_KEY_TO_TYPE: dict[str, MarketType] = {
    "h2h":     MarketType.MONEYLINE,
    "spreads": MarketType.SPREAD,
    "totals":  MarketType.TOTAL,
}

# Player-prop markets to request per sport, keyed by the raw Odds API market
# name that also appears in prizepicks.PP_STAT_TO_ODDS_API.
# Priority 1 (default active): NBA, MLB.
# Priority 2 (enable via PLAYER_PROP_SPORTS env var): soccer leagues.
# NFL excluded — add Sport.NFL here only if re-enabled.
_SPORT_PLAYER_PROP_MARKETS: dict[Sport, str] = {
    Sport.NBA: (
        "player_points,player_rebounds,player_assists,"
        "player_threes,player_steals,player_blocks"
    ),
    Sport.MLB: "player_hits,player_pitcher_strikeouts,player_total_bases",
    # Soccer — Priority 2; only requested when sport is in PLAYER_PROP_SPORTS
    Sport.EPL:        "player_shots_on_target,player_goal_scorer_anytime",
    Sport.MLS:        "player_shots_on_target,player_goal_scorer_anytime",
    Sport.LA_LIGA:    "player_shots_on_target,player_goal_scorer_anytime",
    Sport.SERIE_A:    "player_shots_on_target,player_goal_scorer_anytime",
    Sport.BUNDESLIGA: "player_shots_on_target,player_goal_scorer_anytime",
    Sport.LIGUE_1:    "player_shots_on_target,player_goal_scorer_anytime",
    Sport.UCL:        "player_shots_on_target,player_goal_scorer_anytime",
}

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"


# ── Player-prop result dataclass ──────────────────────────────────────────────

@dataclass
class PlayerPropLine:
    """A single sportsbook player-prop outcome from The Odds API."""
    sportsbook:   str
    sport:        Sport
    market_key:   str            # raw Odds API key, e.g. "player_points"
    event:        str
    player_name:  str
    description:  str            # "Over" or "Under" (from outcome description)
    american_odds: int
    line:         Optional[float]
    event_start:  Optional[datetime]


# ── Vig removal ───────────────────────────────────────────────────────────────

class VigRemover:
    """Remove sportsbook vig using the multiplicative method."""

    @staticmethod
    def american_to_implied(odds: int) -> float:
        if odds < 0:
            return abs(odds) / (abs(odds) + 100)
        return 100 / (odds + 100)

    @staticmethod
    def fair_probability_multiplicative(
        side_a_odds: int, side_b_odds: int
    ) -> tuple[float, float]:
        p_a = VigRemover.american_to_implied(side_a_odds)
        p_b = VigRemover.american_to_implied(side_b_odds)
        total = p_a + p_b
        return p_a / total, p_b / total

    @staticmethod
    def vig_percentage(side_a_odds: int, side_b_odds: int) -> float:
        p_a = VigRemover.american_to_implied(side_a_odds)
        p_b = VigRemover.american_to_implied(side_b_odds)
        total = p_a + p_b
        return round((total - 1.0) * 100, 4)

    @staticmethod
    def fair_american_odds(fair_probability: float) -> int:
        if fair_probability <= 0 or fair_probability >= 1:
            raise ValueError("Probability must be between 0 and 1 (exclusive).")
        if fair_probability >= 0.5:
            raw = -(fair_probability / (1 - fair_probability)) * 100
        else:
            raw = ((1 - fair_probability) / fair_probability) * 100
        return int(round(raw / 5) * 5)

    @staticmethod
    def build_fair_odds(
        selection: str, side_a_odds: int, side_b_odds: int, is_side_a: bool
    ) -> FairOdds:
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
        if offered_american_odds < 0:
            decimal = 1 + (100 / abs(offered_american_odds))
        else:
            decimal = 1 + (offered_american_odds / 100)
        ev = (fair_probability * decimal - 1) * 100
        return round(ev, 2)

    @staticmethod
    def kelly_fraction(fair_probability: float, offered_american_odds: int) -> float:
        if offered_american_odds < 0:
            decimal = 1 + (100 / abs(offered_american_odds))
        else:
            decimal = 1 + (offered_american_odds / 100)
        b = decimal - 1
        q = 1 - fair_probability
        kelly = (b * fair_probability - q) / b
        return max(0.0, round(kelly, 4))

    @staticmethod
    def build_ev_result(
        selection: str, fair_odds: FairOdds, offered_american_odds: int
    ) -> EVResult:
        ev = EVCalculator.expected_value(fair_odds.fair_probability, offered_american_odds)
        kelly = EVCalculator.kelly_fraction(fair_odds.fair_probability, offered_american_odds)
        return EVResult(
            selection=selection,
            fair_odds=fair_odds,
            offered_american_odds=offered_american_odds,
            ev_percentage=ev,
            edge=round(
                fair_odds.fair_probability
                - VigRemover.american_to_implied(offered_american_odds),
                4,
            ),
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

        change = abs(movement.odds_change)
        if change >= 20:
            score += 40
        elif change >= 15:
            score += 30
        elif change >= 10:
            score += 20
        elif change >= 5:
            score += 10

        num_books = len(books_moved)
        if num_books >= 5:
            score += 30
        elif num_books >= 3:
            score += 20
        elif num_books >= 2:
            score += 10

        if movement.line_change is not None:
            lc = abs(movement.line_change)
            if lc >= 2.0:
                score += 20
            elif lc >= 1.0:
                score += 10
            elif lc >= 0.5:
                score += 5

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

    Five live signals (all computable from current data):

      Signal                  Max pts  Notes
      ─────────────────────────────────────────────────────────────────
      EV Edge                  25      magnitude of EV advantage
      Steam Score              25      cross-book movement intensity
      Sharp Book Presence      20      how many known-sharp books moved
      Line Shopping Efficiency 20      gap between offered and best opp. odds
      Market Tightness          10      vig quality — cross-book arb gives 10 pts

      Total live ceiling:     100

    Bands map to star ratings per spec:
      90–100 → 5★   (elite: all signals firing)
      75–89  → 4★   (strong)
      60–74  → 3★   (good)
      40–59  → 2★   (marginal)
      0–39   → 1★   (weak / noise)

    Future signals (add when data available):
      • Liquidity / volume (Odds API doesn't expose volume yet)
      • Timing proximity to game start
      • PrizePicks lag confirmation
    """

    @staticmethod
    def score(
        ev_percentage: float,
        steam_score: int,
        *,
        sharp_book_count: int = 0,
        line_shopping_gap: int = 0,
        vig_pct: float = 10.0,  # default >6 → 0 pts, forces explicit opt-in
    ) -> int:
        score = 0

        # ── 1. EV Edge (0–25 pts) ────────────────────────────────────────────
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

        # ── 2. Steam Score (0–25 pts) ────────────────────────────────────────
        score += int(steam_score * 0.25)

        # ── 3. Sharp Book Presence (0–20 pts) ────────────────────────────────
        # More sharp books validating the move = higher confidence in the signal.
        if sharp_book_count >= 3:
            score += 20
        elif sharp_book_count >= 2:
            score += 14
        elif sharp_book_count >= 1:
            score += 8

        # ── 4. Line Shopping Efficiency (0–20 pts) ───────────────────────────
        # How wide the spread is between our offered price and the opposing
        # best line.  Wide spread = strong cross-book mispricing.
        if line_shopping_gap >= 20:
            score += 20
        elif line_shopping_gap >= 10:
            score += 12
        elif line_shopping_gap >= 5:
            score += 6

        # ── 5. Market Tightness / Vig Quality (0–10 pts) ────────────────────
        # Negative vig means we're computing fair probability from a synthetic
        # best-of-market (cross-book arb scenario) — the most reliable pricing.
        # Low positive vig = sharp single-book reference.
        # High vig = soft book pricing, less reliable fair prob.
        if vig_pct <= 0:
            score += 10
        elif vig_pct <= 3:
            score += 7
        elif vig_pct <= 6:
            score += 3

        return min(score, 100)


# ── Recommendation Engine ─────────────────────────────────────────────────────

def _to_recommendation(ev: float, confidence: int, steam: int) -> tuple[Recommendation, int]:
    """
    Map EV + confidence → (Recommendation, stars).

    Stars come directly from the 0-100 confidence score per spec:
      90–100 → 5★   75–89 → 4★   60–74 → 3★   40–59 → 2★   <40 → 1★

    Recommendation requires both a minimum EV threshold AND a minimum
    confidence level — EV alone is not enough to bet aggressively.
    """
    # Stars: direct confidence-score bands (spec: 90-100=5★, 75-89=4★, 60-74=3★)
    if confidence >= 90:
        stars = 5
    elif confidence >= 75:
        stars = 4
    elif confidence >= 60:
        stars = 3
    elif confidence >= 40:
        stars = 2
    else:
        stars = 1

    # Recommendation: EV quality + confidence threshold
    if ev >= 8 and confidence >= 75:
        rec = Recommendation.STRONG_BET
    elif ev >= 5 and confidence >= 60:
        rec = Recommendation.BET
    elif ev >= 3 and confidence >= 40:
        rec = Recommendation.LEAN
    elif ev < 0:
        rec = Recommendation.FADE
    else:
        rec = Recommendation.PASS

    return rec, stars


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

    Also provides fetch_live_odds() which calls The Odds API to retrieve live
    sportsbook odds as a flat list of OddsLine objects.
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

        sharp_book_count = sum(
            1 for b in books_moved if b in config.sharp_books
        )
        ai_conf = self._ai.score(
            ev_result.ev_percentage,
            steam_score,
            sharp_book_count=sharp_book_count,
            line_shopping_gap=abs(side_a_odds - side_b_odds),
            vig_pct=fair.vig_percentage,
        )

        recommendation, stars = _to_recommendation(ev_result.ev_percentage, ai_conf, steam_score)
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

    # ── Live data feed ─────────────────────────────────────────────────────────

    async def fetch_live_odds(self, sport: Sport) -> list[OddsLine]:
        """
        Fetch live odds from The Odds API (https://the-odds-api.com).

        Returns a flat list of OddsLine objects — one per outcome per book per
        market. Markets fetched: h2h (moneyline), spreads, totals.
        Returns an empty list when the API key is missing or the request fails.
        """
        if not config.ODDS_API_KEY:
            logger.warning(
                "ODDS_API_KEY not configured; skipping live odds fetch for %s", sport
            )
            return []

        sport_key = _SPORT_TO_ODDS_API_KEY.get(sport)
        if not sport_key:
            logger.debug("No Odds API key mapping for sport %s", sport)
            return []

        # Route through the shared OddsApiCache so every call is:
        #   1. Budget-guarded (usage tracker blocks over-quota calls)
        #   2. Deduplicated (TTL cache — DK & FD share one fetch per sport)
        #   3. Health-monitored (quota headers recorded automatically)
        # This eliminates the direct aiohttp path that previously bypassed all
        # three controls and caused repeated 401-spam in the logs.
        try:
            from providers.odds_cache import get_odds_cache, OddsApiError
            cache = get_odds_cache()
            if cache is None:
                logger.warning(
                    "OddsApiCache not yet initialised; skipping live odds for %s", sport
                )
                return []
            data: list[dict] = await cache.get_or_fetch(
                sport_key,
                api_key=config.ODDS_API_KEY,
                markets="h2h,spreads,totals",
                regions="us",
                odds_format="american",
            )
        except OddsApiError as exc:
            logger.error("Odds API request failed for %s: %s", sport, exc)
            return []
        except Exception as exc:
            logger.exception("Unexpected error fetching odds for %s: %s", sport, exc)
            return []

        lines: list[OddsLine] = []
        for event in data:
            away = event.get("away_team", "Away")
            home = event.get("home_team", "Home")
            event_name = f"{away} @ {home}"

            commence_str = event.get("commence_time")
            try:
                event_start: Optional[datetime] = (
                    datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                    if commence_str
                    else None
                )
            except ValueError:
                event_start = None

            for bookmaker in event.get("bookmakers", []):
                book_title = bookmaker.get("title") or bookmaker.get("key", "Unknown")
                for market in bookmaker.get("markets", []):
                    mtype = _MARKET_KEY_TO_TYPE.get(market.get("key", ""))
                    if mtype is None:
                        continue
                    for outcome in market.get("outcomes", []):
                        try:
                            lines.append(
                                OddsLine(
                                    sportsbook=book_title,
                                    sport=sport,
                                    market_type=mtype,
                                    event=event_name,
                                    selection=outcome["name"],
                                    american_odds=int(outcome["price"]),
                                    line=outcome.get("point"),
                                    event_start=event_start,
                                )
                            )
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.debug("Skipping malformed outcome: %s", exc)

        logger.info("Fetched %d odds lines for %s", len(lines), sport)
        return lines

    async def fetch_player_prop_odds(self, sport: Sport) -> list[PlayerPropLine]:
        """
        Fetch player-prop odds from The Odds API for *sport*.

        Only runs for sports listed in _SPORT_PLAYER_PROP_MARKETS.  Returns a
        flat list of PlayerPropLine objects — one per outcome per book per
        market.  Results are budget-guarded and TTL-cached via OddsApiCache
        (same path as fetch_live_odds).

        These rows are stored in odds_records with market_type = the raw Odds
        API key (e.g. "player_points") so find_player_prop_odds() can match
        them against PrizePicks lines via PP_STAT_TO_ODDS_API.
        """
        if not config.ODDS_API_KEY:
            logger.warning(
                "ODDS_API_KEY not configured; skipping player prop fetch for %s", sport
            )
            return []

        markets = _SPORT_PLAYER_PROP_MARKETS.get(sport)
        if not markets:
            logger.debug("No player-prop market mapping for sport %s", sport)
            return []

        sport_key = _SPORT_TO_ODDS_API_KEY.get(sport)
        if not sport_key:
            logger.debug("No Odds API key mapping for sport %s", sport)
            return []

        try:
            from providers.odds_cache import get_odds_cache, OddsApiError
            cache = get_odds_cache()
            if cache is None:
                logger.warning(
                    "OddsApiCache not initialised; skipping player props for %s", sport
                )
                return []
            data: list[dict] = await cache.get_or_fetch(
                sport_key,
                api_key=config.ODDS_API_KEY,
                markets=markets,
                regions="us",
                odds_format="american",
            )
        except OddsApiError as exc:
            logger.error("Odds API player-prop request failed for %s: %s", sport, exc)
            return []
        except Exception as exc:
            logger.exception("Unexpected error fetching player props for %s: %s", sport, exc)
            return []

        lines: list[PlayerPropLine] = []
        for event in data:
            away = event.get("away_team", "Away")
            home = event.get("home_team", "Home")
            event_name = f"{away} @ {home}"

            commence_str = event.get("commence_time")
            try:
                event_start: Optional[datetime] = (
                    datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                    if commence_str
                    else None
                )
            except ValueError:
                event_start = None

            for bookmaker in event.get("bookmakers", []):
                book_title = bookmaker.get("title") or bookmaker.get("key", "Unknown")
                for market in bookmaker.get("markets", []):
                    market_key = market.get("key", "")
                    if not market_key.startswith("player_"):
                        continue
                    for outcome in market.get("outcomes", []):
                        try:
                            lines.append(
                                PlayerPropLine(
                                    sportsbook=book_title,
                                    sport=sport,
                                    market_key=market_key,
                                    event=event_name,
                                    player_name=outcome["name"],
                                    description=outcome.get("description", ""),
                                    american_odds=int(outcome["price"]),
                                    line=outcome.get("point"),
                                    event_start=event_start,
                                )
                            )
                        except (KeyError, TypeError, ValueError) as exc:
                            logger.debug("Skipping malformed player prop outcome: %s", exc)

        logger.info("Fetched %d player prop lines for %s", len(lines), sport)
        return lines

    # ── Future integration stubs ───────────────────────────────────────────────

    async def fetch_prizepicks_lines(self, sport: Sport) -> list[OddsLine]:
        """PLACEHOLDER: Fetch PrizePicks player prop lines."""
        logger.debug("fetch_prizepicks_lines called for %s (not yet implemented)", sport)
        return []

    async def run_ml_model(self, opportunity: EVOpportunity) -> int:
        """PLACEHOLDER: Run ML model to refine AI confidence score."""
        logger.debug("run_ml_model called (not yet implemented)")
        return opportunity.ai_confidence

    async def compute_clv(self, ev_record_id: int, closing_odds: int) -> float:
        """PLACEHOLDER: Compute Closing Line Value post-event."""
        logger.debug("compute_clv called (not yet implemented)")
        return 0.0
