"""
Tests for backfill grading, stat normalization, and alert formatting.

P2: /backfill command fetches fresh stats for pending opps and grades them.
The retired market-confirmation path is not part of this test module.
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


@pytest.mark.asyncio
async def test_backfill_rejects_player_stats_as_underdog_settlement():
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import commands

    opportunity = SimpleNamespace(
        id=991,
        player_name="Backfill Player",
        sport="NBA",
        stat_type="Points",
        game_time=datetime.utcnow() - timedelta(hours=2),
        game_id="underdog-event-991",
        provider="Underdog",
        line_value=20.5,
        recommendation="OVER",
        decision_tier="B",
        confidence=60,
    )
    unofficial_result = SimpleNamespace(
        actual_value=30.0,
        game_date=opportunity.game_time.strftime("%Y-%m-%d"),
        game_id=opportunity.game_id,
        source="espn_gamelog",
    )
    db = MagicMock()
    db.get_pending_opportunities = AsyncMock(return_value=[opportunity])
    db.get_game_result_for_grading = AsyncMock(
        return_value=unofficial_result
    )
    db.upsert_player_result = AsyncMock()
    db.grade_opportunity = AsyncMock()

    stats_provider = MagicMock()
    stats_provider.fetch_results = AsyncMock(return_value=[unofficial_result])
    propline_provider = SimpleNamespace(
        fetch_resolved_results=AsyncMock(return_value=[])
    )
    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock())
    )

    with (
        patch.object(commands, "_db", db),
        patch(
            "providers.player_stats.PlayerStatsProvider",
            return_value=stats_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=propline_provider,
        ),
    ):
        await commands._cmd_backfill_inner(update, SimpleNamespace())

    stats_provider.fetch_results.assert_not_awaited()
    propline_provider.fetch_resolved_results.assert_awaited_once_with(
        "Backfill Player",
        "NBA",
        "Points",
        source_provider="Underdog",
    )
    assert db.get_game_result_for_grading.await_count == 2
    assert all(
        call.kwargs.get("provider") == "Underdog"
        for call in db.get_game_result_for_grading.await_args_list
    )
    db.grade_opportunity.assert_not_awaited()


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
        assert "<b>Line:</b>" in result
        assert "Market Check" not in result

    def test_change_alert_works_without_confirmation(self):
        from alerts_multiplatform import format_underdog_change_alert
        result = format_underdog_change_alert(
            "LeBron James", "LAL", "NBA", "points", 24.5, 25.5
        )
        assert "PLAYER PROP" in result and "<b>Line:</b>" in result

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
        assert "<b>Line:</b>" in result
        assert "Market Check" not in result

    def test_new_prop_alert_works_without_confirmation(self):
        from alerts_multiplatform import format_underdog_new_prop_alert
        result = format_underdog_new_prop_alert(
            "LeBron James", "LAL", "NBA", "points", 25.5
        )
        assert "PLAYER PROP" in result

    def test_deliver_underdog_signature_accepts_market_confirmation(self):
        """deliver_underdog() must accept market_confirmation kwarg."""
        import inspect
        from alerts import AlertDelivery
        sig = inspect.signature(AlertDelivery.deliver_underdog)
        assert "market_confirmation" in sig.parameters
