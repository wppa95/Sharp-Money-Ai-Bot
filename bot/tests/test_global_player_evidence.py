"""Global player-history evidence: routing, identity, collector capability."""
from __future__ import annotations

import inspect
import pytest

from connectors.underdog import UnderdogConnector
from engine.identity import normalize_player_name
from player_history_job import _SUPPORTED_HISTORY_SPORTS
from providers.player_stats import PlayerStatsProvider, _ESPN_ROUTE, _NFL_STAT_MAP


_PROVIDER_SPORTS = frozenset({
    "MLB", "NBA", "NFL", "WNBA", "NHL",
    "NCAAF", "CFB", "NCAAB", "MLS",
    "TENNIS", "CS", "DOTA", "SOCCER",
})

_UNSUPPORTED_SPORTS = frozenset({
    "LOL", "VAL", "VALORANT", "PGA", "GOLF", "MMA", "BOXING",
    "TT", "BADMINTON", "FIFA", "CRICKET", "RUGBY", "AFL", "AFLW",
    "KBO", "NPB", "CFL",
})


def _minimal_payload(*, first_name, last_name, sport_id="NFL", display_stat="Receiving Yards"):
    return {
        "players": [{
            "id": "p1",
            "first_name": first_name,
            "last_name": last_name,
            "team_id": "t1",
            "sport_id": sport_id,
        }],
        "appearances": [{"id": "a1", "player_id": "p1", "match_id": 1}],
        "games": [],
        "over_under_lines": [{
            "id": "line1",
            "stat_value": 10.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "a1",
                    "display_stat": display_stat,
                }
            },
        }],
    }


class TestIdentityGlobal:
    def setup_method(self):
        self.conn = UnderdogConnector.__new__(UnderdogConnector)

    def test_null_first_name(self):
        props = self.conn._parse(_minimal_payload(first_name=None, last_name="Rahel", sport_id="LOL"))
        assert props[0].player_name == "Rahel"

    def test_null_last_name(self):
        props = self.conn._parse(_minimal_payload(first_name="Elijah", last_name=None))
        assert props[0].player_name == "Elijah"

    def test_both_null(self):
        props = self.conn._parse(_minimal_payload(first_name=None, last_name=None))
        assert props[0].player_name == "Unknown Player"

    def test_one_word_name(self):
        props = self.conn._parse(_minimal_payload(first_name=None, last_name="Neymar", sport_id="SOCCER"))
        assert props[0].player_name == "Neymar"

    def test_legitimate_two_part(self):
        props = self.conn._parse(_minimal_payload(first_name="Elijah", last_name="Sarratt"))
        assert props[0].player_name == "Elijah Sarratt"

    def test_normalize_strips_none_prefix(self):
        assert normalize_player_name("None Rahel") == "rahel"
        assert normalize_player_name("Elijah Sarratt") == "elijah_sarratt"


class TestCollectorCapability:
    def test_all_provider_sports_are_collected(self):
        for sport in _PROVIDER_SPORTS:
            assert sport in _SUPPORTED_HISTORY_SPORTS, f"{sport} missing from collector"

    def test_unsupported_sports_not_collected(self):
        for sport in _UNSUPPORTED_SPORTS:
            assert sport not in _SUPPORTED_HISTORY_SPORTS, f"{sport} should not be collected"

    def test_no_tier2_exclusion_remains(self):
        import player_history_job as phj
        assert not hasattr(phj, "_TIER2_SPORTS") or not getattr(phj, "_TIER2_SPORTS", None)


class TestProviderRouting:
    def test_espn_route_covers_major_sports(self):
        for sport in ("NBA", "WNBA", "NFL", "NCAAF", "CFB", "NCAAB", "MLS"):
            assert sport in _ESPN_ROUTE

    def test_fetch_results_routes_provider_sports(self):
        src = inspect.getsource(PlayerStatsProvider.fetch_results)
        for token in ('"MLB"', '"NFL"', '"NHL"', '"SOCCER"', '"CS"', '"DOTA"', '"TENNIS"'):
            assert token in src

    def test_nfl_receiving_yards_mapped(self):
        assert "receiving yards" in _NFL_STAT_MAP
        cat, labels = _NFL_STAT_MAP["receiving yards"]
        assert cat == "receiving" and "YDS" in labels

    def test_nfl_rushing_and_passing_mapped(self):
        assert "rushing yards" in _NFL_STAT_MAP
        assert "passing yards" in _NFL_STAT_MAP or "pass yards" in _NFL_STAT_MAP

    @pytest.mark.asyncio
    async def test_unsupported_sport_returns_empty(self):
        provider = PlayerStatsProvider()
        results = await provider.fetch_results("Some Player", "LOL", "Kills")
        assert results == []
