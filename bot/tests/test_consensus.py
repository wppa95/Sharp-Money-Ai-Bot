"""
Tests for the cross-book consensus engine and CLV module.

Covers:
  ConsensusEngine:
    - compute_consensus groups by market key
    - consensus_odds is median of all books
    - outlier detection at threshold
    - find_inefficiencies returns only positive-deviation books
    - build_multi_book_steam_inputs filters no-movement books
    - min_books filter excludes thin markets

  CLV:
    - compute_clv positive (beat the close)
    - compute_clv negative (missed the close)
    - compute_clv with counterpart odds (de-vigged)
    - compute_clv without counterpart odds (raw implied)
    - build_clv_opportunity returns None below threshold
    - build_clv_opportunity returns CLVOpportunity above threshold
    - CLVResult.clv_grade tiers
    - CLVOpportunity.is_actionable
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from datetime import datetime

from connectors.base import MarketSnapshot
from engine.consensus import (
    compute_consensus,
    find_inefficiencies,
    build_multi_book_steam_inputs,
    ConsensusResult,
    MarketInefficiency,
)
from engine.clv import (
    compute_clv,
    build_clv_opportunity,
    CLVResult,
    CLVOpportunity,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_snap(
    sportsbook: str,
    odds: int,
    selection: str = "Kansas City Chiefs",
    event: str = "Chiefs @ Raiders",
    market_type: str = "Moneyline",
    sport: str = "NFL",
    line: float | None = None,
    opening_odds: int | None = None,
    is_pickem: bool = False,
) -> MarketSnapshot:
    return MarketSnapshot(
        sportsbook   = sportsbook,
        sport        = sport,
        league       = sport,
        event        = event,
        market_type  = market_type,
        selection    = selection,
        odds         = odds,
        line         = line,
        opening_odds = opening_odds,
        external_id  = "ud-test-001",
        is_pickem    = is_pickem,
    )


# ── Consensus engine ──────────────────────────────────────────────────────────

class TestComputeConsensus:
    def _three_book_snaps(self) -> list[MarketSnapshot]:
        return [
            make_snap("DraftKings", -110),
            make_snap("FanDuel",    -115),
            make_snap("BetMGM",     -112),
        ]

    def test_groups_by_market_key(self):
        snaps = self._three_book_snaps()
        results = compute_consensus(snaps)
        assert len(results) == 1   # all same market key
        cr = results[0]
        assert cr.event       == "Chiefs @ Raiders"
        assert cr.selection   == "Kansas City Chiefs"
        assert cr.market_type == "Moneyline"
        assert cr.sport       == "NFL"

    def test_consensus_odds_is_median(self):
        # Odds: -110, -115, -112 → sorted: -115, -112, -110 → median = -112
        snaps = self._three_book_snaps()
        results = compute_consensus(snaps)
        assert results[0].consensus_odds == -112

    def test_book_count(self):
        snaps = self._three_book_snaps()
        results = compute_consensus(snaps)
        assert results[0].book_count == 3

    def test_books_list_contains_all(self):
        snaps = self._three_book_snaps()
        results = compute_consensus(snaps)
        books = set(results[0].books)
        assert "DraftKings" in books
        assert "FanDuel"    in books
        assert "BetMGM"     in books

    def test_min_odds_and_max_odds(self):
        snaps = self._three_book_snaps()
        results = compute_consensus(snaps)
        cr = results[0]
        assert cr.min_odds == -115
        assert cr.max_odds == -110

    def test_odds_range(self):
        snaps = self._three_book_snaps()
        cr = compute_consensus(snaps)[0]
        assert cr.odds_range == 5  # -110 - (-115)

    def test_min_books_filter_excludes_thin_markets(self):
        # Only 1 book — should be excluded with min_books=2
        snaps = [make_snap("Pinnacle", -110)]
        results = compute_consensus(snaps, min_books=2)
        assert results == []

    def test_two_books_meets_min_books_default(self):
        snaps = [make_snap("Pinnacle", -110), make_snap("Circa", -112)]
        results = compute_consensus(snaps, min_books=2)
        assert len(results) == 1

    def test_different_markets_produce_separate_results(self):
        snaps = [
            make_snap("DK", -110, selection="Chiefs"),
            make_snap("FD", -110, selection="Chiefs"),
            make_snap("DK", +140, selection="Raiders"),
            make_snap("FD", +145, selection="Raiders"),
        ]
        results = compute_consensus(snaps)
        assert len(results) == 2

    def test_pickem_snapshots_excluded(self):
        snaps = [
            make_snap("DraftKings", -110),
            make_snap("FanDuel",    -115),
            make_snap("Underdog",     0,  is_pickem=True),
        ]
        results = compute_consensus(snaps)
        # Only 2 sportsbook snaps → still 1 result (min_books=2 met)
        assert len(results) == 1
        assert results[0].book_count == 2  # Underdog not counted

    def test_no_outlier_when_all_agree(self):
        snaps = [make_snap("DK", -110), make_snap("FD", -110), make_snap("MGM", -110)]
        cr = compute_consensus(snaps)[0]
        assert len(cr.outliers) == 0

    def test_outlier_detected_above_threshold(self):
        # consensus = -112, outlier at -100 (deviation +12 > threshold 10)
        snaps = [
            make_snap("DraftKings", -110),
            make_snap("FanDuel",    -115),
            make_snap("BetMGM",     -112),
            make_snap("Circa",      -100),   # outlier: +12 above consensus
        ]
        results = compute_consensus(snaps, outlier_threshold=10)
        cr = results[0]
        assert len(cr.outliers) >= 1
        circa_outlier = next(o for o in cr.outliers if o.sportsbook == "Circa")
        assert circa_outlier.is_value
        assert circa_outlier.deviation > 0

    def test_has_inefficiency_property(self):
        snaps = [
            make_snap("DraftKings", -110),
            make_snap("FanDuel",    -115),
            make_snap("BetMGM",     -112),
            make_snap("Circa",      -98),    # clear outlier
        ]
        cr = compute_consensus(snaps, outlier_threshold=10)[0]
        assert cr.has_inefficiency is True

    def test_consensus_line_when_lines_present(self):
        snaps = [
            make_snap("DK", -110, market_type="Spread", line=-3.5),
            make_snap("FD", -110, market_type="Spread", line=-3.0),
            make_snap("MGM",-110, market_type="Spread", line=-3.5),
        ]
        results = compute_consensus(snaps)
        cr = results[0]
        assert cr.consensus_line is not None
        # Median of [-3.5, -3.0, -3.5] = -3.5
        assert cr.consensus_line == pytest.approx(-3.5)


class TestFindInefficiencies:
    def test_returns_value_outliers_only(self):
        # DraftKings at -100 is +12 from consensus (-112)
        # FanDuel at -124 is -12 from consensus — stale/slow book
        snaps = [
            make_snap("DraftKings", -100),   # +12 deviation (value)
            make_snap("FanDuel",    -115),
            make_snap("BetMGM",     -112),
            make_snap("Circa",      -124),   # -12 deviation (stale)
        ]
        ineficiencies = find_inefficiencies(snaps, outlier_threshold=10, value_only=True)
        books = {i.sportsbook for i in ineficiencies}
        assert "DraftKings" in books
        assert "Circa" not in books   # negative deviation excluded with value_only=True

    def test_returns_all_outliers_when_not_value_only(self):
        snaps = [
            make_snap("DraftKings", -100),
            make_snap("FanDuel",    -115),
            make_snap("BetMGM",     -112),
            make_snap("Circa",      -124),
        ]
        all_ineficiencies = find_inefficiencies(snaps, outlier_threshold=10, value_only=False)
        books = {i.sportsbook for i in all_ineficiencies}
        assert "DraftKings" in books
        assert "Circa"      in books

    def test_no_inefficiencies_when_all_agree(self):
        snaps = [make_snap("DK", -110), make_snap("FD", -110), make_snap("MGM", -110)]
        ineff = find_inefficiencies(snaps)
        assert ineff == []

    def test_deviation_sign_positive_for_value(self):
        snaps = [
            make_snap("ValueBook", -100),   # offered -100, consensus ~-113
            make_snap("B2", -115),
            make_snap("B3", -112),
            make_snap("B4", -115),
        ]
        ineff = find_inefficiencies(snaps, outlier_threshold=10)
        for i in ineff:
            assert i.deviation > 0   # all returned outliers are positive deviation


class TestBuildMultiBookSteamInputs:
    def test_no_movement_excluded(self):
        # No movement (opening == current)
        snaps = [
            make_snap("DK", -110, opening_odds=-110),
            make_snap("FD", -110, opening_odds=-110),
        ]
        result = build_multi_book_steam_inputs(snaps)
        assert len(result) == 0

    def test_one_book_moving_excluded(self):
        snaps = [
            make_snap("DK", -115, opening_odds=-110),   # moved
            make_snap("FD", -110, opening_odds=-110),   # flat
        ]
        result = build_multi_book_steam_inputs(snaps)
        # Need ≥2 moving books; only 1 moved → excluded
        assert len(result) == 0

    def test_two_books_moving_included(self):
        snaps = [
            make_snap("DK",  -115, opening_odds=-110),
            make_snap("FD",  -117, opening_odds=-110),
        ]
        result = build_multi_book_steam_inputs(snaps)
        assert len(result) == 1
        key = list(result.keys())[0]
        book_data = result[key]
        assert len(book_data) == 2

    def test_no_opening_odds_excluded(self):
        # opening_odds is None → skip
        snaps = [
            make_snap("DK", -115, opening_odds=None),
            make_snap("FD", -117, opening_odds=None),
        ]
        result = build_multi_book_steam_inputs(snaps)
        assert len(result) == 0

    def test_pickem_snapshots_excluded(self):
        snaps = [
            make_snap("DK",      -115, opening_odds=-110),
            make_snap("FD",      -117, opening_odds=-110),
            make_snap("Underdog",   0, opening_odds=None, is_pickem=True),
        ]
        result = build_multi_book_steam_inputs(snaps)
        assert len(result) == 1


# ── CLV engine ────────────────────────────────────────────────────────────────

class TestComputeCLV:
    def test_positive_clv_beat_close(self):
        # Bet at -110, closed at -130 → you got better odds
        result = compute_clv(-110, -130, selection="Chiefs ML")
        assert result.beat_close is True
        assert result.clv_pct > 0

    def test_negative_clv_missed_close(self):
        # Bet at -130, closed at -110 → market moved against you
        result = compute_clv(-130, -110, selection="Chiefs ML")
        assert result.beat_close is False
        assert result.clv_pct < 0

    def test_clv_zero_at_same_odds(self):
        result = compute_clv(-110, -110, selection="Chiefs ML")
        assert result.clv_pct == pytest.approx(0.0, abs=0.01)

    def test_clv_proxy_positive_when_better_odds(self):
        # bet_odds (-110) > closing_odds (-130)  → positive proxy
        result = compute_clv(-110, -130)
        assert result.clv_proxy == 20   # -110 - (-130) = +20

    def test_clv_proxy_negative_when_worse_odds(self):
        result = compute_clv(-130, -110)
        assert result.clv_proxy == -20

    def test_with_counterpart_odds(self):
        # Full de-vigged CLV
        result = compute_clv(
            bet_odds=+150, closing_odds=+135,
            counterpart_bet_odds=-180, counterpart_close_odds=-165,
            selection="Dog ML",
        )
        assert result.counterpart_bet_odds   == -180
        assert result.counterpart_close_odds == -165
        assert result.fair_prob_bet   > 0
        assert result.fair_prob_close > 0

    def test_clv_grade_excellent(self):
        # Large positive CLV → Excellent
        result = compute_clv(-100, -140, selection="X")   # bet at -100, closed -140
        assert result.clv_grade in ("Excellent", "Strong")

    def test_clv_grade_bad(self):
        result = compute_clv(-140, -100, selection="X")   # bet at -140, closed -100
        assert result.clv_grade in ("Bad", "Weak")

    def test_clv_grade_neutral(self):
        result = compute_clv(-110, -110, selection="X")
        assert result.clv_grade == "Neutral"

    def test_clv_emoji_beat(self):
        result = compute_clv(-100, -130, selection="X")
        assert result.clv_emoji in ("🔥", "✅", "⚪")

    def test_clv_emoji_missed(self):
        result = compute_clv(-130, -100, selection="X")
        assert result.clv_emoji in ("🟡", "❌")

    def test_summary_contains_selection(self):
        result = compute_clv(-110, -125, selection="My Bet")
        assert "My Bet" in result.summary()

    def test_result_fields_populated(self):
        result = compute_clv(-110, -130, selection="Test")
        assert result.selection     == "Test"
        assert result.bet_odds      == -110
        assert result.closing_odds  == -130
        assert isinstance(result.computed_at, datetime)


class TestBuildCLVOpportunity:
    def _snap(self, book: str, odds: int) -> MarketSnapshot:
        return make_snap(book, odds)

    def test_returns_opportunity_when_lead_sufficient(self):
        current  = self._snap("ValueBook", -100)
        consensus = [
            self._snap("ValueBook", -100),
            self._snap("DK",        -115),
            self._snap("FD",        -115),
        ]
        opp = build_clv_opportunity(current, consensus, min_books=2, min_lead=5)
        assert opp is not None
        assert opp.clv_lead > 0
        assert opp.sportsbook == "ValueBook"

    def test_returns_none_below_min_lead(self):
        # Current -110, others all -112 → lead = 2 < 5 (default)
        current   = self._snap("DK", -110)
        consensus = [
            self._snap("DK",  -110),
            self._snap("FD",  -112),
            self._snap("MGM", -112),
        ]
        opp = build_clv_opportunity(current, consensus, min_books=2, min_lead=5)
        assert opp is None

    def test_returns_none_insufficient_books(self):
        current   = self._snap("DK", -100)
        consensus = [self._snap("DK", -100)]   # no other books
        opp = build_clv_opportunity(current, consensus, min_books=3, min_lead=5)
        assert opp is None

    def test_opportunity_fields(self):
        current = self._snap("ValueBook", -95)
        consensus = [
            self._snap("ValueBook", -95),
            self._snap("DK",        -115),
            self._snap("FD",        -115),
            self._snap("MGM",       -115),
        ]
        opp = build_clv_opportunity(current, consensus, min_books=2, min_lead=5)
        assert opp is not None
        assert opp.event           == "Chiefs @ Raiders"
        assert opp.selection       == "Kansas City Chiefs"
        assert opp.current_odds    == -95
        assert opp.projected_close == -115
        assert opp.clv_lead        == 20
        assert opp.books_count     == 4

    def test_is_actionable_above_threshold(self):
        current = self._snap("VB", -90)
        consensus = [
            self._snap("VB", -90),
            self._snap("DK", -115),
            self._snap("FD", -115),
        ]
        opp = build_clv_opportunity(current, consensus, min_books=2, min_lead=5)
        assert opp is not None
        assert opp.is_actionable is True

    def test_is_not_actionable_when_lead_small(self):
        # Lead of 2 is actionable by CLVOpportunity standard (≥5 check in build_clv_opportunity)
        # but if it somehow gets through, is_actionable uses its own threshold
        opp = CLVOpportunity(
            event="Test", selection="Sel", current_odds=-110,
            projected_close=-112, clv_lead=2,
        )
        assert opp.is_actionable is False   # 2 < 5
