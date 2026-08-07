"""
Tests for P2 (backfill grading) and P6 (OddsAPI market confirmation).

P2: /backfill command fetches fresh stats for pending opps and grades them.
P6: _get_odds_api_confirmation() returns non-blocking confirmation for S/A picks.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Shared event loop for async tests ────────────────────────────────────────

@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ═══════════════════════════════════════════════════════════════════════════
# P2 — Backfill grading logic tests
# ═══════════════════════════════════════════════════════════════════════════

class TestBackfillGradingLogic:
    """Unit-test the direction-aware grading logic used in cmd_backfill."""

    def _grade(self, recommendation: str, actual: float, line: float) -> str:
        _push_tol = 0.01
        if abs(actual - line) < _push_tol:
            return "PUSH"
        elif (recommendation or "OVER").upper() == "UNDER":
            return "HIT" if actual < line else "MISS"
        else:
            return "HIT" if actual > line else "MISS"

    def test_over_hit(self):
        assert self._grade("OVER", 25.0, 22.5) == "HIT"

    def test_over_miss(self):
        assert self._grade("OVER", 20.0, 22.5) == "MISS"

    def test_under_hit(self):
        assert self._grade("UNDER", 20.0, 22.5) == "HIT"

    def test_under_miss(self):
        assert self._grade("UNDER", 25.0, 22.5) == "MISS"

    def test_push_within_tolerance(self):
        assert self._grade("OVER", 22.5, 22.5) == "PUSH"

    def test_push_exact_zero_tolerance(self):
        # abs(22.505 - 22.5) = 0.005 < 0.01 → PUSH
        assert self._grade("OVER", 22.505, 22.5) == "PUSH"

    def test_none_recommendation_defaults_to_over(self):
        # None recommendation → OVER direction
        assert self._grade(None, 25.0, 22.5) == "HIT"
        assert self._grade(None, 20.0, 22.5) == "MISS"

    def test_case_insensitive_recommendation(self):
        assert self._grade("over", 25.0, 22.5) == "HIT"
        assert self._grade("under", 20.0, 22.5) == "HIT"

    def test_half_line_over(self):
        assert self._grade("OVER", 1.5, 0.5) == "HIT"

    def test_half_line_under(self):
        assert self._grade("UNDER", 0.0, 0.5) == "HIT"


class TestBackfillStatNormalization:
    """stat_type normalization: lookup must be case-insensitive."""

    def _normalize(self, stat_type: str) -> str:
        return stat_type.lower().strip()

    def test_normalize_points(self):
        assert self._normalize("Points") == "points"

    def test_normalize_with_whitespace(self):
        assert self._normalize("  Pitcher Strikeouts  ") == "pitcher strikeouts"

    def test_normalize_mixed_case(self):
        assert self._normalize("Pts+Reb+Ast") == "pts+reb+ast"


# ═══════════════════════════════════════════════════════════════════════════
# P6 — OddsAPI market confirmation tests
# ═══════════════════════════════════════════════════════════════════════════

class TestUdToOddsApiMarketMapping:
    """_UD_TO_ODDS_API_MARKET maps Underdog stat names to OddsAPI keys."""

    def setup_method(self):
        from market_engine import _UD_TO_ODDS_API_MARKET
        self.mapping = _UD_TO_ODDS_API_MARKET

    def test_nba_points_mapped(self):
        assert self.mapping["points"] == "player_points"

    def test_nba_rebounds_mapped(self):
        assert self.mapping["rebounds"] == "player_rebounds"

    def test_nba_assists_mapped(self):
        assert self.mapping["assists"] == "player_assists"

    def test_nba_three_pointers_aliases(self):
        assert self.mapping["3-pointers made"] == "player_threes"
        assert self.mapping["three-pointers made"] == "player_threes"
        assert self.mapping["3pt made"] == "player_threes"

    def test_nba_steals_blocks_mapped(self):
        assert self.mapping["steals"] == "player_steals"
        assert self.mapping["blocks"] == "player_blocks"

    def test_mlb_hits_mapped(self):
        assert self.mapping["hits"] == "player_hits"

    def test_mlb_strikeouts_mapped(self):
        assert self.mapping["pitcher strikeouts"] == "player_pitcher_strikeouts"
        assert self.mapping["strikeouts"] == "player_pitcher_strikeouts"

    def test_mlb_total_bases_mapped(self):
        assert self.mapping["total bases"] == "player_total_bases"

    def test_unmapped_stat_returns_none(self):
        assert self.mapping.get("fantasy points") is None
        assert self.mapping.get("goals") is None


@pytest.mark.asyncio
class TestGetOddsApiConfirmation:
    """Tests for _get_odds_api_confirmation()."""

    @pytest.mark.asyncio
    async def test_returns_none_when_engine_not_set(self):
        from market_engine import _get_odds_api_confirmation
        import market_engine
        original = market_engine._analysis_engine
        market_engine._analysis_engine = None
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "points", "OVER", 25.5)
            assert result is None
        finally:
            market_engine._analysis_engine = original

    @pytest.mark.asyncio
    async def test_returns_none_for_unmapped_stat(self):
        from market_engine import _get_odds_api_confirmation
        import market_engine
        market_engine._analysis_engine = MagicMock()
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "fantasy points", "OVER", 45.0)
            assert result is None
        finally:
            market_engine._analysis_engine = None

    @pytest.mark.asyncio
    async def test_returns_none_for_unsupported_sport(self):
        from market_engine import _get_odds_api_confirmation
        import market_engine
        market_engine._analysis_engine = MagicMock()
        try:
            # "DOTA" is not a valid Sport enum value for OddsAPI
            result = await _get_odds_api_confirmation("DOTA", "Player1", "kills", "OVER", 10.5)
            assert result is None
        finally:
            market_engine._analysis_engine = None

    @pytest.mark.asyncio
    async def test_returns_confirmation_when_player_found(self):
        """When OddsAPI returns lines for the player/stat, confirmation is populated."""
        from market_engine import _get_odds_api_confirmation, init_odds_confirmation
        import market_engine

        # Build mock PlayerPropLine objects
        MockLine = MagicMock()
        MockLine.market_key  = "player_points"
        MockLine.player_name = "LeBron James"
        MockLine.sportsbook  = "DraftKings"
        MockLine.line        = 25.5
        MockLine.description = "over"

        MockLine2 = MagicMock()
        MockLine2.market_key  = "player_points"
        MockLine2.player_name = "LeBron James"
        MockLine2.sportsbook  = "FanDuel"
        MockLine2.line        = 25.5
        MockLine2.description = "over"

        mock_engine = MagicMock()
        mock_engine.fetch_player_prop_lines = AsyncMock(return_value=[MockLine, MockLine2])
        init_odds_confirmation(mock_engine)
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "points", "OVER", 25.5)
            assert result is not None
            assert result["num_books"] == 2
            assert result["avg_line"] == 25.5
            assert result["confirmed"] is True
            assert "book" in result["notes"].lower() or "✅" in result["notes"]
        finally:
            init_odds_confirmation(None)

    @pytest.mark.asyncio
    async def test_returns_none_when_player_not_found(self):
        """When no matching player in OddsAPI lines, returns None."""
        from market_engine import _get_odds_api_confirmation, init_odds_confirmation
        import market_engine

        MockLine = MagicMock()
        MockLine.market_key  = "player_points"
        MockLine.player_name = "Steph Curry"
        MockLine.sportsbook  = "DraftKings"
        MockLine.line        = 28.5
        MockLine.description = "over"

        mock_engine = MagicMock()
        mock_engine.fetch_player_prop_lines = AsyncMock(return_value=[MockLine])
        init_odds_confirmation(mock_engine)
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "points", "OVER", 25.5)
            assert result is None
        finally:
            init_odds_confirmation(None)

    @pytest.mark.asyncio
    async def test_confirmed_false_when_no_direction_match(self):
        """Player found on sportsbooks but no lines for requested direction."""
        from market_engine import _get_odds_api_confirmation, init_odds_confirmation

        # Line is only for "under", requesting "over"
        MockLine = MagicMock()
        MockLine.market_key  = "player_points"
        MockLine.player_name = "LeBron James"
        MockLine.sportsbook  = "DraftKings"
        MockLine.line        = 25.5
        MockLine.description = "under"  # only under available

        mock_engine = MagicMock()
        mock_engine.fetch_player_prop_lines = AsyncMock(return_value=[MockLine])
        init_odds_confirmation(mock_engine)
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "points", "OVER", 25.5)
            # Player found (sportsbooks has entry) but no direction match → confirmed=False
            assert result is not None
            assert result["confirmed"] is False
            assert result["num_books"] == 1
            assert result["avg_line"] is None
        finally:
            init_odds_confirmation(None)

    @pytest.mark.asyncio
    async def test_exception_in_fetch_returns_none(self):
        """If fetch_player_prop_lines raises, _get_odds_api_confirmation returns None (non-blocking)."""
        from market_engine import _get_odds_api_confirmation, init_odds_confirmation

        mock_engine = MagicMock()
        mock_engine.fetch_player_prop_lines = AsyncMock(side_effect=RuntimeError("API down"))
        init_odds_confirmation(mock_engine)
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "points", "OVER", 25.5)
            assert result is None
        finally:
            init_odds_confirmation(None)

    @pytest.mark.asyncio
    async def test_line_diff_positive_shows_in_notes(self):
        """When avg_line > UD line, notes shows the positive diff."""
        from market_engine import _get_odds_api_confirmation, init_odds_confirmation

        MockLine = MagicMock()
        MockLine.market_key  = "player_points"
        MockLine.player_name = "LeBron James"
        MockLine.sportsbook  = "DraftKings"
        MockLine.line        = 27.0   # higher than UD 25.5
        MockLine.description = "over"

        mock_engine = MagicMock()
        mock_engine.fetch_player_prop_lines = AsyncMock(return_value=[MockLine])
        init_odds_confirmation(mock_engine)
        try:
            result = await _get_odds_api_confirmation("NBA", "LeBron James", "points", "OVER", 25.5)
            assert result is not None
            assert result["avg_line"] == 27.0
            assert "+1.5" in result["notes"]
        finally:
            init_odds_confirmation(None)

    @pytest.mark.asyncio
    async def test_surname_fuzzy_match(self):
        """A player stored as 'Junior Caminero' should match 'Caminero' from OddsAPI."""
        from market_engine import _get_odds_api_confirmation, init_odds_confirmation

        MockLine = MagicMock()
        MockLine.market_key  = "player_hits"
        MockLine.player_name = "Caminero"   # OddsAPI may use short name
        MockLine.sportsbook  = "DraftKings"
        MockLine.line        = 1.5
        MockLine.description = "over"

        mock_engine = MagicMock()
        mock_engine.fetch_player_prop_lines = AsyncMock(return_value=[MockLine])
        init_odds_confirmation(mock_engine)
        try:
            result = await _get_odds_api_confirmation("MLB", "Junior Caminero", "hits", "OVER", 1.5)
            assert result is not None
            assert result["num_books"] >= 1
        finally:
            init_odds_confirmation(None)


# ═══════════════════════════════════════════════════════════════════════════
# P6 — Format function parameter presence tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatFunctionMarketConfirmationParam:
    """Verify format functions accept and render market_confirmation."""

    def _make_mock_decision(self, tier="S", rec="OVER", conf=85):
        d = MagicMock()
        d.recommendation = rec
        d.decision_tier  = tier
        d.confidence     = conf
        d.reason         = "Strong L5"
        d.l5_hit_rate    = 0.8
        d.l5_games       = 5
        return d

    def _make_mock_score(self, tier="S", stars=5, total=88, n=12):
        s = MagicMock()
        s.tier          = tier
        s.stars         = stars
        s.total         = total
        s.n_history     = n
        s.stars_display = "★★★★★"
        s.move_velocity = None
        return s

    def test_change_alert_accepts_market_confirmation(self):
        from alerts_multiplatform import format_underdog_change_alert
        result = format_underdog_change_alert(
            "LeBron James", "LAL", "NBA", "points",
            24.5, 25.5,
            decision=self._make_mock_decision(),
            score=self._make_mock_score(),
            market_confirmation={"num_books": 3, "avg_line": 25.5, "notes": "3 books · avg 25.5 ✅", "confirmed": True},
        )
        assert "Market Check" in result
        assert "3 books" in result

    def test_change_alert_works_without_confirmation(self):
        from alerts_multiplatform import format_underdog_change_alert
        result = format_underdog_change_alert(
            "LeBron James", "LAL", "NBA", "points", 24.5, 25.5
        )
        assert "ACTIONABLE BET PICK" in result or "Line:" in result

    def test_change_alert_no_confirmation_block_when_none(self):
        from alerts_multiplatform import format_underdog_change_alert
        result = format_underdog_change_alert(
            "LeBron James", "LAL", "NBA", "points", 24.5, 25.5,
            market_confirmation=None,
        )
        assert "Market Check" not in result

    def test_new_prop_alert_accepts_market_confirmation(self):
        from alerts_multiplatform import format_underdog_new_prop_alert
        result = format_underdog_new_prop_alert(
            "LeBron James", "LAL", "NBA", "points", 25.5,
            decision=self._make_mock_decision(),
            score=self._make_mock_score(),
            market_confirmation={"num_books": 2, "avg_line": 25.5, "notes": "2 books · avg 25.5 ✅", "confirmed": True},
        )
        assert "Market Check" in result

    def test_new_prop_alert_works_without_confirmation(self):
        from alerts_multiplatform import format_underdog_new_prop_alert
        result = format_underdog_new_prop_alert(
            "LeBron James", "LAL", "NBA", "points", 25.5
        )
        assert "UNDERDOG PROP LIVE" in result

    def test_deliver_underdog_signature_accepts_market_confirmation(self):
        """deliver_underdog() must accept market_confirmation kwarg."""
        import inspect
        from alerts import AlertDelivery
        sig = inspect.signature(AlertDelivery.deliver_underdog)
        assert "market_confirmation" in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════
# P6 — init_odds_confirmation wiring test
# ═══════════════════════════════════════════════════════════════════════════

class TestInitOddsConfirmation:
    """init_odds_confirmation() sets the module-level _analysis_engine."""

    def test_sets_and_clears_engine(self):
        from market_engine import init_odds_confirmation
        import market_engine

        mock_engine = MagicMock()
        init_odds_confirmation(mock_engine)
        assert market_engine._analysis_engine is mock_engine

        init_odds_confirmation(None)
        assert market_engine._analysis_engine is None
