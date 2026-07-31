"""
Tests for providers/tennis_stats.py

Covers:
  - _parse_score: various score string formats
  - _parse_sets: set counting from score strings
  - _tourney_date_to_iso: YYYYMMDD → YYYY-MM-DD
  - _TENNIS_STAT_MAP: all spec-required stat types are mapped
  - TennisStatsProvider._extract_result: winner and loser extraction
  - TennisStatsProvider._fetch_filtered_csv: player name matching
  - TennisStatsProvider.fetch_results: CSV load + filtering pipeline
  - Retirement / walkover scores are skipped
  - HTTP errors return []
  - Decision engine integration: tennis prop with real data qualifies
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import asyncio
import io
import csv
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from providers.tennis_stats import (
    TennisStatsProvider,
    _TENNIS_STAT_MAP,
    _parse_score,
    _parse_sets,
    _tourney_date_to_iso,
)
from providers.player_stats import RawGameResult
from engine.player_results import WindowStats, PlayerHitRates
from engine.ud_bet_decision import make_ud_bet_decision


# ── _parse_score ──────────────────────────────────────────────────────────────

class TestParseScore:
    def test_straight_sets(self):
        result = _parse_score("6-3 7-5")
        assert result == (13, 8)   # 6+7=13, 3+5=8

    def test_three_sets(self):
        result = _parse_score("6-3 4-6 7-5")
        assert result == (17, 14)  # 6+4+7=17, 3+6+5=14

    def test_tiebreak_suffix_stripped(self):
        result = _parse_score("6-3 7-6(4)")
        assert result == (13, 9)   # tiebreak "(4)" stripped; 6+7=13, 3+6=9

    def test_retirement_returns_none(self):
        assert _parse_score("6-3 3-1 RET") is None
        assert _parse_score("6-3 W/O") is None

    def test_empty_string_returns_none(self):
        assert _parse_score("") is None

    def test_walkover_returns_none(self):
        assert _parse_score("Walkover") is None

    def test_bagel_set(self):
        result = _parse_score("6-0 6-0")
        assert result == (12, 0)

    def test_malformed_score_returns_none(self):
        assert _parse_score("6:3 7:5") is None   # colon separator not supported


# ── _parse_sets ───────────────────────────────────────────────────────────────

class TestParseSets:
    def test_straight_sets_winner(self):
        result = _parse_sets("6-3 7-5")
        assert result == (2, 0)

    def test_three_set_match(self):
        result = _parse_sets("6-3 4-6 7-5")
        assert result == (2, 1)

    def test_retirement_returns_none(self):
        assert _parse_sets("6-3 3-1 RET") is None

    def test_walkover_returns_none(self):
        assert _parse_sets("W/O") is None

    def test_grand_slam_five_sets(self):
        result = _parse_sets("7-6 4-6 6-3 3-6 7-5")
        assert result == (3, 2)


# ── _tourney_date_to_iso ──────────────────────────────────────────────────────

class TestTourneyDateToIso:
    def test_valid_date(self):
        assert _tourney_date_to_iso("20260701") == "2026-07-01"

    def test_invalid_format_returns_none(self):
        assert _tourney_date_to_iso("2026-07-01") is None

    def test_empty_returns_none(self):
        assert _tourney_date_to_iso("") is None

    def test_garbage_returns_none(self):
        assert _tourney_date_to_iso("abc") is None


# ── _TENNIS_STAT_MAP completeness ────────────────────────────────────────────

class TestTennisStatMap:
    REQUIRED = [
        "aces",
        "double faults",
        "first serves in",
        "total games",
        "games won",
        "sets won",
        "sets",
        "service points",
    ]

    def test_all_required_types_mapped(self):
        for stat in self.REQUIRED:
            assert stat in _TENNIS_STAT_MAP, f"Missing tennis stat: {stat!r}"


# ── _extract_result ───────────────────────────────────────────────────────────

def _make_row(**kw) -> dict:
    defaults = {
        "winner_name": "Novak Djokovic",
        "loser_name":  "Carlos Alcaraz",
        "tourney_date": "20260701",
        "score": "6-3 7-5",
        "surface": "Grass",
        "w_ace": "10",
        "l_ace": "7",
        "w_df": "2",
        "l_df": "4",
        "w_1stIn": "45",
        "l_1stIn": "38",
        "w_svpt": "68",
        "l_svpt": "72",
    }
    defaults.update(kw)
    return defaults


class TestExtractResult:
    def test_winner_aces(self):
        row    = _make_row()
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "aces", ("w_ace", "l_ace")
        )
        assert result is not None
        assert result.actual_value == 10.0
        assert result.player_name  == "Novak Djokovic"
        assert result.stat_type    == "aces"
        assert result.opponent     == "Carlos Alcaraz"
        assert result.game_date    == "2026-07-01"
        assert result.source       == "sackmann_csv"
        assert result.sport        == "TENNIS"

    def test_loser_aces(self):
        row    = _make_row()
        result = TennisStatsProvider._extract_result(
            row, "Carlos Alcaraz", "aces", ("w_ace", "l_ace")
        )
        assert result is not None
        assert result.actual_value == 7.0
        assert result.opponent     == "Novak Djokovic"

    def test_winner_total_games(self):
        row    = _make_row(score="6-3 7-5")
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "total games", ("_games_w", "_games_l")
        )
        assert result is not None
        assert result.actual_value == 13.0   # 6+7=13

    def test_loser_total_games(self):
        row    = _make_row(score="6-3 7-5")
        result = TennisStatsProvider._extract_result(
            row, "Carlos Alcaraz", "total games", ("_games_w", "_games_l")
        )
        assert result is not None
        assert result.actual_value == 8.0    # 3+5=8

    def test_winner_sets_won(self):
        row    = _make_row(score="6-3 4-6 7-5")
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "sets won", ("_sets_w", "_sets_l")
        )
        assert result is not None
        assert result.actual_value == 2.0

    def test_loser_sets_won(self):
        row    = _make_row(score="6-3 4-6 7-5")
        result = TennisStatsProvider._extract_result(
            row, "Carlos Alcaraz", "sets won", ("_sets_w", "_sets_l")
        )
        assert result is not None
        assert result.actual_value == 1.0

    def test_retirement_score_returns_none(self):
        row    = _make_row(score="6-3 3-1 RET")
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "sets won", ("_sets_w", "_sets_l")
        )
        assert result is None

    def test_missing_ace_column_returns_none(self):
        row = _make_row()
        del row["w_ace"]
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "aces", ("w_ace", "l_ace")
        )
        assert result is None

    def test_empty_ace_value_returns_none(self):
        row    = _make_row(w_ace="")
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "aces", ("w_ace", "l_ace")
        )
        assert result is None

    def test_double_faults_winner(self):
        row    = _make_row()
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "double faults", ("w_df", "l_df")
        )
        assert result is not None
        assert result.actual_value == 2.0

    def test_first_serves_in_winner(self):
        row    = _make_row()
        result = TennisStatsProvider._extract_result(
            row, "Novak Djokovic", "first serves in", ("w_1stIn", "l_1stIn")
        )
        assert result is not None
        assert result.actual_value == 45.0


# ── CSV filtering ─────────────────────────────────────────────────────────────

def _build_csv_text(rows: list[dict]) -> str:
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class TestFetchFilteredCsv:
    @pytest.mark.asyncio
    async def test_filters_to_player_rows(self):
        rows = [
            _make_row(winner_name="Novak Djokovic",  loser_name="Carlos Alcaraz"),
            _make_row(winner_name="Rafael Nadal",    loser_name="Roger Federer"),
            _make_row(winner_name="Carlos Alcaraz",  loser_name="Jannik Sinner"),
        ]
        csv_text = _build_csv_text(rows)

        provider = TennisStatsProvider()
        provider._csv_cache["http://fake"] = csv_text

        result_rows = await provider._fetch_filtered_csv("http://fake", "Carlos Alcaraz")
        assert len(result_rows) == 2

    @pytest.mark.asyncio
    async def test_empty_on_http_error(self):
        resp = MagicMock()
        resp.status = 404
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        provider = TennisStatsProvider()
        with patch("providers.tennis_stats.aiohttp.ClientSession", return_value=session):
            result_rows = await provider._fetch_filtered_csv("http://fake/csv", "Djokovic")
        assert result_rows == []


# ── fetch_results end-to-end (mocked CSV) ────────────────────────────────────

_SAMPLE_CSV_ROWS = [
    _make_row(
        winner_name="Novak Djokovic", loser_name="Carlos Alcaraz",
        tourney_date="20260625", score="6-3 7-5",
        w_ace="12", l_ace="8", w_df="1", l_df="3",
    ),
    _make_row(
        winner_name="Alexander Zverev", loser_name="Novak Djokovic",
        tourney_date="20260618", score="6-4 3-6 7-5",
        w_ace="9", l_ace="14", w_df="4", l_df="2",
    ),
    _make_row(
        winner_name="Novak Djokovic", loser_name="Jannik Sinner",
        tourney_date="20260610", score="7-5 6-3",
        w_ace="15", l_ace="6", w_df="2", l_df="5",
    ),
]


class TestFetchResultsEndToEnd:
    @pytest.mark.asyncio
    async def test_aces_for_djokovic(self):
        csv_text = _build_csv_text(_SAMPLE_CSV_ROWS)

        provider = TennisStatsProvider()

        async def _fake_get_text(url):
            return csv_text

        with patch.object(provider, "_get_text", side_effect=_fake_get_text):
            results = await provider.fetch_results(
                "Novak Djokovic", "TENNIS", "aces"
            )

        assert len(results) == 3
        assert all(r.sport == "TENNIS" for r in results)
        assert all(r.stat_type == "aces" for r in results)
        assert all(r.source == "sackmann_csv" for r in results)

        # Match 1: Djokovic won → w_ace=12
        # Match 2: Djokovic lost → l_ace=14
        # Match 3: Djokovic won → w_ace=15
        ace_vals = sorted(r.actual_value for r in results)
        assert ace_vals == [12.0, 14.0, 15.0]

    @pytest.mark.asyncio
    async def test_total_games_for_djokovic(self):
        csv_text = _build_csv_text(_SAMPLE_CSV_ROWS)
        provider = TennisStatsProvider()

        async def _fake_get_text(url):
            return csv_text

        with patch.object(provider, "_get_text", side_effect=_fake_get_text):
            results = await provider.fetch_results(
                "Novak Djokovic", "TENNIS", "total games"
            )

        assert len(results) == 3
        vals = sorted(r.actual_value for r in results)
        # Match 1 (win): 6+7=13, Match 2 (loss): 3+5=8(wrong set order, check)
        # "6-4 3-6 7-5" — Djokovic lost, so loser_games = 4+6+5=15, winner=6+3+7=16
        assert 13.0 in vals   # match 1 win: 6+7=13
        assert 15.0 in vals   # match 2 loss: 4+6+5=15
        assert 13.0 in [r.actual_value for r in results]

    @pytest.mark.asyncio
    async def test_unknown_stat_type_returns_empty(self):
        provider = TennisStatsProvider()
        results  = await provider.fetch_results(
            "Novak Djokovic", "TENNIS", "drop shot winners"
        )
        assert results == []


# ── Decision engine integration ───────────────────────────────────────────────

class TestTennisDecisionEngineIntegration:
    """
    With real historical tennis data, the decision engine should produce
    OVER or UNDER — not PASS.
    """

    def _make_window(self, games: int, hit_rate: float, avg: float = 8.0) -> WindowStats:
        oc = round(games * hit_rate)
        uc = games - oc
        return WindowStats(games=games, over_count=oc, under_count=uc,
                           hit_rate=hit_rate, average=avg)

    def test_tennis_ace_prop_qualifies(self):
        hit_rates = PlayerHitRates(
            player_name  = "Novak Djokovic",
            stat_type    = "aces",
            current_line = 9.5,
            l5           = self._make_window(5,  0.80, avg=12.4),
            l10          = self._make_window(10, 0.70, avg=11.8),
            l20          = self._make_window(20, 0.65, avg=11.2),
            l30          = None,
            season       = None,
            h2h          = None,
            has_real_data = True,
            total_games  = 20,
        )

        score = MagicMock()
        score.tier                = "A"
        score.consistency         = 10
        score.historical_activity = 15
        score.n_history           = 20
        score.stars               = 4

        validation = MagicMock()
        validation.has_supporting_data = True
        validation.avg_line            = 9.5
        validation.min_line_seen       = 9.0
        validation.l5_rate             = 0.80
        validation.l10_rate            = 0.70
        validation.l20_rate            = 0.65
        validation.l30_rate            = None
        validation.rate_at_or_below    = 0.20

        decision = make_ud_bet_decision(
            score=score,
            validation=validation,
            hit_rates=hit_rates,
            current_line=9.5,
        )

        assert decision is not None
        assert decision.recommendation in ("OVER", "UNDER"), (
            "Tennis prop with real history should qualify, not PASS"
        )
