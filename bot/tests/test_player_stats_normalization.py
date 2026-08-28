"""Regression tests for live Underdog MLB stat-name normalization."""

from __future__ import annotations

from providers.player_stats import (
    PlayerStatsProvider,
    _MLB_HITTING_MAP,
    _MLB_PITCHING_MAP,
    _MLB_PITCHING_STATS,
)


def test_batter_walks_maps_to_mlb_base_on_balls():
    assert _MLB_HITTING_MAP["batter walks"] == "baseOnBalls"


def test_pitching_outs_maps_to_mlb_outs_conversion():
    assert _MLB_PITCHING_MAP["pitching outs"] == "_outs"
    assert "pitching outs" in _MLB_PITCHING_STATS
    assert PlayerStatsProvider._mlb_extract(
        {"inningsPitched": "5.2"}, "_outs", "pitching outs"
    ) == 17.0