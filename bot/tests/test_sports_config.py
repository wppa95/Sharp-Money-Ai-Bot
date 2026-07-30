"""
Tests for the expanded active-sports configuration.

Covers:
  - Sport enum contains all newly supported sports
  - Every default active sport parses to a Sport enum value
  - Every default active sport has an Odds API key in every mapping dict
    (analysis engine, DraftKings connector, FanDuel connector)
  - Legacy "Soccer" alias still maps to soccer_epl everywhere
  - ACTIVE_SPORTS env-var override behavior is preserved
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest

from models import Sport
from engine.analysis import _SPORT_TO_ODDS_API_KEY
from connectors.draftkings import _SPORT_KEYS as DK_SPORT_KEYS
from connectors.fanduel import _SPORT_KEYS as FD_SPORT_KEYS
from config import Config, config


EXPECTED_DEFAULT_SPORTS = [
    # Only sports whose alerts can currently be delivered (see alert_scope_filter.py).
    # Expand this list when delivery scope is widened for additional sports.
    "MLB",
]

# Verified Odds API sport keys (documented at the-odds-api.com)
EXPECTED_KEYS = {
    "NFL":        "americanfootball_nfl",
    "NBA":        "basketball_nba",
    "MLB":        "baseball_mlb",
    "WNBA":       "basketball_wnba",
    "NHL":        "icehockey_nhl",
    "NCAAF":      "americanfootball_ncaaf",
    "NCAAB":      "basketball_ncaab",
    "UFC":        "mma_mixed_martial_arts",
    "EPL":        "soccer_epl",
    "LaLiga":     "soccer_spain_la_liga",
    "SerieA":     "soccer_italy_serie_a",
    "Bundesliga": "soccer_germany_bundesliga",
    "Ligue1":     "soccer_france_ligue_one",
    "MLS":        "soccer_usa_mls",
    "UCL":        "soccer_uefa_champs_league",
}


class TestSportEnum:
    def test_all_default_sports_parse(self):
        for value in EXPECTED_DEFAULT_SPORTS:
            assert Sport(value).value == value

    def test_legacy_values_still_exist(self):
        assert Sport.SOCCER.value == "Soccer"
        assert Sport.OTHER.value == "Other"

    def test_unknown_sport_raises(self):
        with pytest.raises(ValueError):
            Sport("Cricket")


class TestAnalysisEngineMapping:
    def test_every_default_sport_has_odds_api_key(self):
        for value in EXPECTED_DEFAULT_SPORTS:
            sport = Sport(value)
            assert sport in _SPORT_TO_ODDS_API_KEY, f"missing key for {value}"

    def test_keys_match_verified_odds_api_keys(self):
        for value, key in EXPECTED_KEYS.items():
            assert _SPORT_TO_ODDS_API_KEY[Sport(value)] == key

    def test_legacy_soccer_alias_maps_to_epl(self):
        assert _SPORT_TO_ODDS_API_KEY[Sport.SOCCER] == "soccer_epl"


class TestConnectorMappings:
    @pytest.mark.parametrize("mapping", [DK_SPORT_KEYS, FD_SPORT_KEYS],
                             ids=["draftkings", "fanduel"])
    def test_every_default_sport_present(self, mapping):
        for value in EXPECTED_DEFAULT_SPORTS:
            assert value in mapping, f"missing {value}"

    @pytest.mark.parametrize("mapping", [DK_SPORT_KEYS, FD_SPORT_KEYS],
                             ids=["draftkings", "fanduel"])
    def test_keys_match_verified_odds_api_keys(self, mapping):
        for value, key in EXPECTED_KEYS.items():
            assert mapping[value] == key

    @pytest.mark.parametrize("mapping", [DK_SPORT_KEYS, FD_SPORT_KEYS],
                             ids=["draftkings", "fanduel"])
    def test_legacy_soccer_alias(self, mapping):
        assert mapping["Soccer"] == "soccer_epl"

    def test_connector_maps_consistent_with_engine_map(self):
        engine_by_value = {s.value: k for s, k in _SPORT_TO_ODDS_API_KEY.items()}
        for mapping in (DK_SPORT_KEYS, FD_SPORT_KEYS):
            for value, key in mapping.items():
                assert engine_by_value.get(value) == key, value


class TestActiveSportsConfig:
    def test_default_includes_all_expected_sports(self):
        assert config.active_sports == EXPECTED_DEFAULT_SPORTS

    def test_every_default_active_sport_is_valid_enum(self):
        for value in config.active_sports:
            Sport(value)  # must not raise

    def test_every_default_active_sport_has_mapping_everywhere(self):
        for value in config.active_sports:
            assert Sport(value) in _SPORT_TO_ODDS_API_KEY
            assert value in DK_SPORT_KEYS
            assert value in FD_SPORT_KEYS

    def test_env_var_override_still_works(self):
        c = Config(ACTIVE_SPORTS_RAW="NFL, NBA ,MLB")
        assert c.active_sports == ["NFL", "NBA", "MLB"]

    def test_default_does_not_include_legacy_soccer_alias(self):
        # "Soccer" is an EPL alias; including both would double-fetch EPL.
        assert "Soccer" not in config.active_sports
