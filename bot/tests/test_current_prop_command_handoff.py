"""Regression coverage for current cross-sport /picks and /slip rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from commands import (
    _has_current_prop_data,
    _select_directional_provider_rows,
)


def _current_prop(**overrides):
    values = {
        "provider": "Underdog",
        "player_name": "CS Player",
        "sport": "CS",
        "stat_type": "Kills on Maps 1+2",
        "external_id": "source-1",
        "game_id": "event-1",
        "line_value": 29.5,
        "fetched_at": datetime.utcnow(),
        "game_time": datetime.utcnow() + timedelta(hours=3),
        "removed": False,
        "score_tier": "B",
        "score_total": 54.0,
        "market_quality_score": None,
        "bet_recommendation": None,
        "bet_confidence": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_directional_row_does_not_require_confidence_for_refresh():
    prop = _current_prop()

    assert _has_current_prop_data(
        prop,
        require_direction=False,
        require_scoring=False,
    )
    assert _has_current_prop_data(
        prop,
        require_direction=False,
    )


def test_current_scored_row_does_not_require_optional_market_quality():
    prop = _current_prop(
        bet_recommendation="OVER",
        bet_confidence=54,
    )

    assert _has_current_prop_data(prop)


def test_zero_market_quality_still_blocks_current_row():
    prop = _current_prop(
        bet_recommendation="OVER",
        bet_confidence=54,
        market_quality_score=0,
    )

    assert not _has_current_prop_data(prop, require_scoring=True)


def test_directional_selection_uses_fresh_snapshot_map_for_cross_sport_row():
    prop = _current_prop()
    selected = _select_directional_provider_rows(
        [prop],
        [],
        {
            ("CS Player", "CS", "Kills on Maps 1+2", "source-1"): (
                "OVER",
                54,
            )
        },
        limit=10,
    )

    assert selected == [("Underdog", prop)]