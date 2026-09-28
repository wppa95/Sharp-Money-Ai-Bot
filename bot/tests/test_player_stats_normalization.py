"""Regression tests for live Underdog MLB stat-name normalization."""

from __future__ import annotations

from providers.player_stats import (
    PlayerStatsProvider,
    _parse_date,
    _MLB_HITTING_MAP,
    _MLB_PITCHING_MAP,
    _MLB_PITCHING_STATS,
)


def test_parse_date_preserves_date_only_and_naive_values():
    assert _parse_date("2026-08-01") == "2026-08-01"
    assert _parse_date("2026-08-01T23:30:00") == "2026-08-01"


def test_parse_date_normalizes_explicit_offset_to_utc_date():
    assert _parse_date("2026-08-02T00:30:00+02:00") == "2026-08-01"
    assert _parse_date("2026-08-01T23:30:00-04:00") == "2026-08-02"


def test_parse_date_keeps_none_and_invalid_contract():
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None


def test_batter_walks_maps_to_mlb_base_on_balls():
    assert _MLB_HITTING_MAP["batter walks"] == "baseOnBalls"


def test_pitching_outs_maps_to_mlb_outs_conversion():
    assert _MLB_PITCHING_MAP["pitching outs"] == "_outs"
    assert "pitching outs" in _MLB_PITCHING_STATS
    assert PlayerStatsProvider._mlb_extract(
        {"inningsPitched": "5.2"}, "_outs", "pitching outs"
    ) == 17.0


def test_basketball_threes_alias_maps_to_three_pointers():
    from providers.player_stats import _NBA_STAT_MAP

    assert _NBA_STAT_MAP["threes"] == ["3PM"]