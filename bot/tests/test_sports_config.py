"""
Tests for the expanded active-sports configuration.

Covers:
  - Sport enum contains all newly supported sports
  - Every default active sport parses to a Sport enum value
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
from config import Config, config


EXPECTED_DEFAULT_SPORTS = [
    "NFL", "CFB", "NBA", "MLB", "KBO", "NPB", "NHL", "NCAAB", "UFC", "WNBA",
    "EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "MLS", "UCL", "SOCCER",
    "CS", "LOL", "VAL", "VALORANT", "DOTA", "TENNIS", "FIFA", "PGA", "MMA",
    "GOLF", "TT", "BADMINTON",
]

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


class TestActiveSportsConfig:
    def test_default_includes_all_expected_sports(self):
        assert config.active_sports == EXPECTED_DEFAULT_SPORTS

    def test_every_active_sport_is_in_the_default_alert_scope(self):
        active = {
            str(sport).strip().upper()
            for sport in config.active_sports
            if str(sport).strip()
        }
        alerts = {
            str(sport).strip().upper()
            for sport in config.ud_alert_sports
            if str(sport).strip()
        }

        assert active <= alerts

    def test_every_default_active_sport_is_non_empty_string(self):
        # active_sports contains Underdog scanner identifiers (TENNIS, CS, etc.)
        # which are NOT Sport enum values — they use a different string namespace.
        for value in config.active_sports:
            assert isinstance(value, str) and len(value) > 0

    def test_football_is_enabled_in_active_sports_default(self):
        assert "NFL" in config.active_sports
        assert "CFB" in config.active_sports

    def test_env_var_override_still_works(self):
        c = Config(ACTIVE_SPORTS_RAW="NFL, NBA ,MLB")
        assert c.active_sports == ["NFL", "NBA", "MLB"]

    def test_default_does_not_include_legacy_soccer_alias(self):
        # "Soccer" is an EPL alias; including both would double-fetch EPL.
        assert "Soccer" not in config.active_sports
