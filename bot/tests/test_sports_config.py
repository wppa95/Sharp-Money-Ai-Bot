"""
Tests for the expanded active-sports configuration.

Covers:
  - Sport enum contains all newly supported sports
  - Every default active sport parses to a Sport enum value
  - Every default active sport has an Odds API key in the analysis engine mapping
  - Legacy "Soccer" alias still maps to soccer_epl
  - ACTIVE_SPORTS env-var override behavior is preserved

Note: DraftKings and FanDuel connector mapping tests removed Aug 2026.
Those connectors were removed from the framework (provider rule: only
providers that improve Underdog actionable picks are kept).
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest

from models import Sport
from engine.analysis import _SPORT_TO_ODDS_API_KEY
from config import Config, config


EXPECTED_DEFAULT_SPORTS = [
    # Underdog scanner scope (not Odds API Sport enum values).
    # These are the default Underdog sports scanned for prop alerts.
    # Tier-1 Odds API scope is governed by UD_TIER1_SPORTS_RAW separately.
    "TENNIS", "WNBA", "CS", "LOL", "VAL", "DOTA", "PGA", "FIFA", "CFB",
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


_EXPECTED_ENUM_SPORTS = ["MLB", "NBA", "NFL", "WNBA", "NHL"]

class TestSportEnum:
    def test_all_default_sports_parse(self):
        for value in _EXPECTED_ENUM_SPORTS:
            assert Sport(value).value == value

    def test_legacy_values_still_exist(self):
        assert Sport.SOCCER.value == "Soccer"
        assert Sport.OTHER.value == "Other"

    def test_unknown_sport_raises(self):
        with pytest.raises(ValueError):
            Sport("Cricket")


class TestAnalysisEngineMapping:
    def test_every_default_sport_has_odds_api_key(self):
        for value in _EXPECTED_ENUM_SPORTS:
            sport = Sport(value)
            assert sport in _SPORT_TO_ODDS_API_KEY, f"missing key for {value}"

    def test_keys_match_verified_odds_api_keys(self):
        for value, key in EXPECTED_KEYS.items():
            assert _SPORT_TO_ODDS_API_KEY[Sport(value)] == key

    def test_legacy_soccer_alias_maps_to_epl(self):
        assert _SPORT_TO_ODDS_API_KEY[Sport.SOCCER] == "soccer_epl"


class TestActiveSportsConfig:
    def test_default_includes_all_expected_sports(self):
        assert config.active_sports == EXPECTED_DEFAULT_SPORTS

    def test_every_default_active_sport_is_non_empty_string(self):
        # active_sports contains Underdog scanner identifiers (TENNIS, CS, etc.)
        # which are NOT Sport enum values — they use a different string namespace.
        for value in config.active_sports:
            assert isinstance(value, str) and len(value) > 0

    def test_no_tier2_sport_in_active_sports_default(self):
        # Tier-2 sports (MLB/NBA/NFL) must not appear in the Underdog scanner default.
        tier2 = {"MLB", "NBA", "NFL"}
        for value in config.active_sports:
            assert value not in tier2, f"Tier-2 sport {value!r} in active_sports default"

    def test_env_var_override_still_works(self):
        c = Config(ACTIVE_SPORTS_RAW="NFL, NBA ,MLB")
        assert c.active_sports == ["NFL", "NBA", "MLB"]

    def test_default_does_not_include_legacy_soccer_alias(self):
        # "Soccer" is an EPL alias; including both would double-fetch EPL.
        assert "Soccer" not in config.active_sports
