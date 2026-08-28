from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from player_history_job import _SUPPORTED_HISTORY_SPORTS, _fair_cycle_targets


def test_fair_cycle_targets_round_robins_all_sports():
    targets = [
        ("nfl-1", "NFL", "passing yards"),
        ("nfl-2", "NFL", "passing yards"),
        ("nba-1", "NBA", "points"),
        ("mlb-1", "MLB", "hits"),
    ]
    selected = _fair_cycle_targets(targets, 3)
    assert [target[1] for target in selected] == ["MLB", "NBA", "NFL"]


def test_fair_cycle_targets_preserves_all_when_under_limit():
    targets = [("nba-1", "NBA", "points"), ("wnba-1", "WNBA", "assists")]
    assert _fair_cycle_targets(targets, 100) == [
        ("nba-1", "NBA", "points"),
        ("wnba-1", "WNBA", "assists"),
    ]


def test_fifa_uses_existing_soccer_history_path():
    assert "FIFA" in _SUPPORTED_HISTORY_SPORTS
