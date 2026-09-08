"""Regression tests for historical-average direction fallback."""

from __future__ import annotations

from unittest.mock import MagicMock

from engine.ud_bet_decision import UDBetDecision, make_ud_bet_decision


def _score(total: float = 75.0, tier: str = "A") -> MagicMock:
    score = MagicMock()
    score.total = total
    score.tier = tier
    return score


def _validation(avg_line: float | None = None) -> MagicMock:
    validation = MagicMock()
    validation.avg_line = avg_line
    validation.min_line_seen = None
    return validation


def test_missing_history_uses_historical_average_for_direction():
    result = make_ud_bet_decision(
        score=_score(98.0, "S"),
        validation=_validation(avg_line=20.0),
        current_line=15.0,
        hit_rates=None,
    )

    assert isinstance(result, UDBetDecision)
    assert result.recommendation == "OVER"
    assert result.decision_tier == "S"
    assert result.confidence == 95
    assert result.l5_games is None
    assert result.l5_hit_rate is None
    assert result.season_games is None
    assert result.avg_vs_line_pct == 0.25
    assert "historical average fallback" in result.reason.lower()


def test_missing_history_can_create_under_from_historical_average():
    result = make_ud_bet_decision(
        score=_score(70.0, "C"),
        validation=_validation(avg_line=8.0),
        current_line=9.0,
        hit_rates=[],
    )

    assert result.recommendation == "UNDER"
    assert result.l5_games is None
    assert result.l5_over is None
    assert result.l5_under is None
    assert result.l5_hit_rate is None
    assert result.l10_games is None
    assert result.season_games is None


def test_missing_history_uses_current_line_above_average_for_under():
    result = make_ud_bet_decision(
        score=_score(60.0, "S"),
        validation=_validation(avg_line=10.0),
        current_line=12.0,
        hit_rates=None,
    )

    assert result.recommendation == "UNDER"
    assert result.avg_vs_line_pct == -0.2
    assert "historical average fallback" in result.reason.lower()


def test_missing_history_direction_does_not_depend_on_score_tier():
    results = [
        make_ud_bet_decision(
            score=_score(80.0, tier),
            validation=_validation(avg_line=12.0),
            current_line=10.0,
            hit_rates=None,
        )
        for tier in ("S", "A", "B", "C", "PASS", "legacy")
    ]

    assert all(result.recommendation == "OVER" for result in results)