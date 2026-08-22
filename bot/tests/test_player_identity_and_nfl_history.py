"""Focused tests: Underdog null-name normalization + NFL history collection."""
from __future__ import annotations

import pytest

from connectors.underdog import UnderdogConnector
from player_history_job import _SUPPORTED_HISTORY_SPORTS, _TIER2_SPORTS


def _minimal_payload(*, first_name, last_name, sport_id="LOL", display_stat="Kills"):
    """Build the smallest Underdog v1 payload that exercises name parsing."""
    return {
        "players": [
            {
                "id": "p1",
                "first_name": first_name,
                "last_name": last_name,
                "team_id": "t1",
                "sport_id": sport_id,
            }
        ],
        "appearances": [
            {"id": "a1", "player_id": "p1", "match_id": 1}
        ],
        "games": [],
        "over_under_lines": [
            {
                "id": "line1",
                "stat_value": 10.5,
                "over_under": {
                    "appearance_stat": {
                        "appearance_id": "a1",
                        "display_stat": display_stat,
                    }
                },
            }
        ],
    }


class TestPlayerNameNormalization:
    def setup_method(self):
        self.conn = UnderdogConnector.__new__(UnderdogConnector)

    def test_null_first_name_only(self):
        payload = _minimal_payload(first_name=None, last_name="Rahel")
        props = self.conn._parse(payload)
        assert len(props) == 1
        assert props[0].player_name == "Rahel"
        assert "None" not in props[0].player_name

    def test_null_last_name_only(self):
        payload = _minimal_payload(first_name="Elijah", last_name=None)
        props = self.conn._parse(payload)
        assert len(props) == 1
        assert props[0].player_name == "Elijah"
        assert "None" not in props[0].player_name

    def test_both_null(self):
        payload = _minimal_payload(first_name=None, last_name=None)
        props = self.conn._parse(payload)
        assert len(props) == 1
        assert props[0].player_name == "Unknown Player"

    def test_legitimate_name_unchanged(self):
        payload = _minimal_payload(
            first_name="Elijah",
            last_name="Sarratt",
            sport_id="NFL",
            display_stat="Receiving Yards",
        )
        props = self.conn._parse(payload)
        assert len(props) == 1
        assert props[0].player_name == "Elijah Sarratt"

    def test_no_none_prefix_from_string_none(self):
        payload = _minimal_payload(first_name="None", last_name="Rahel")
        props = self.conn._parse(payload)
        assert len(props) == 1
        assert props[0].player_name == "Rahel"
        assert not props[0].player_name.startswith("None ")


class TestNFLHistoryCollection:
    def test_nfl_not_in_tier2_skip(self):
        assert "NFL" not in _TIER2_SPORTS

    def test_mlb_nba_still_skipped(self):
        assert "MLB" in _TIER2_SPORTS
        assert "NBA" in _TIER2_SPORTS

    def test_nfl_in_supported_history_sports(self):
        assert "NFL" in _SUPPORTED_HISTORY_SPORTS

    def test_receiving_yards_stat_map_exists(self):
        from providers.player_stats import _NFL_STAT_MAP
        assert "receiving yards" in _NFL_STAT_MAP
        category, labels = _NFL_STAT_MAP["receiving yards"]
        assert category == "receiving"
        assert "YDS" in labels

    def test_player_stats_routes_nfl(self):
        import inspect
        from providers.player_stats import PlayerStatsProvider
        src = inspect.getsource(PlayerStatsProvider.fetch_results)
        assert 'sport_upper == "NFL"' in src
