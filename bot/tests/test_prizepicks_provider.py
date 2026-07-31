"""
Tests for bot/providers/prizepicks.py

Covers:
  - _normalize_stat(): known mappings and unknown passthrough
  - _parse_dt(): datetime / ISO string / None / invalid input
  - pp_line_to_player_prop(): PrizePicksLine → PlayerProp adapter
  - PrizePicksProvider: provider_name, sport_keys, is_available=False, fetch raises
  - PrizePicksManualProvider.from_dicts(): class-method constructor
  - PrizePicksManualProvider.fetch_props(): from dicts, from PrizePicksLine objects
  - PrizePicksManualProvider sport filter, is_available, __len__, normalize_stat
  - datetime parsing edge cases in fetch_props
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone
from typing import Any

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from providers.prizepicks import (
    PrizePicksProvider,
    PrizePicksManualProvider,
    _normalize_stat,
    _parse_dt,
    pp_line_to_player_prop,
)
from providers.prop_provider import PlayerProp


# ── Shared event loop (matches pattern in test_dashboard.py) ──────────────────

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


def _make_dict(
    *,
    external_id: str  = "pp-001",
    player_name: str  = "LeBron James",
    team:        str  = "LAL",
    sport:       str  = "NBA",
    stat_type:   str  = "Points",
    line_value:  float = 25.5,
    start_time:  Any  = "2026-08-01T19:00:00",
    game_id:     str  = "game-001",
) -> dict:
    return dict(
        external_id = external_id,
        player_name = player_name,
        team        = team,
        sport       = sport,
        stat_type   = stat_type,
        line_value  = line_value,
        start_time  = start_time,
        game_id     = game_id,
    )


def _make_pp_line(**kwargs):
    """Build a PrizePicksLine if the module is available, else skip."""
    try:
        from prizepicks import PrizePicksLine
    except ImportError:
        pytest.skip("bot/prizepicks.py not importable in this environment")

    defaults = dict(
        external_id = "pp-001",
        player_name = "Mike Trout",
        team        = "LAA",
        sport       = "MLB",
        league      = "MLB",
        stat_type   = "Hits",
        line_value  = 1.5,
        start_time  = datetime(2026, 8, 1, 19, 0),
        # PrizePicksLine has no game_id field — game_id is constructed by adapter
    )
    defaults.update(kwargs)
    return PrizePicksLine(**defaults)


# ── _normalize_stat tests ──────────────────────────────────────────────────────

class TestNormalizeStat:
    def test_points_short(self):
        assert _normalize_stat("pts") == "points"

    def test_points_full(self):
        assert _normalize_stat("points") == "points"

    def test_rebounds(self):
        assert _normalize_stat("reb") == "rebounds"

    def test_assists(self):
        assert _normalize_stat("ast") == "assists"

    def test_home_runs_short(self):
        assert _normalize_stat("hr") == "home runs"

    def test_strikeouts(self):
        assert _normalize_stat("strikeouts") == "strikeouts"

    def test_rushing_yards(self):
        assert _normalize_stat("rushing yards") == "rushing yards"

    def test_receiving_yards(self):
        assert _normalize_stat("receiving yards") == "receiving yards"

    def test_unknown_passthrough(self):
        assert _normalize_stat("xfactor_stat") == "xfactor_stat"

    def test_uppercase_normalized(self):
        assert _normalize_stat("PTS") == "points"

    def test_strips_whitespace(self):
        assert _normalize_stat("  hits  ") == "hits"

    def test_kills_esports(self):
        assert _normalize_stat("kills") == "kills"

    def test_shots(self):
        assert _normalize_stat("shots") == "shots on goal"


# ── _parse_dt tests ────────────────────────────────────────────────────────────

class TestParseDt:
    def test_none_returns_none(self):
        assert _parse_dt(None) is None

    def test_datetime_passthrough(self):
        dt = datetime(2026, 8, 1, 19, 0)
        assert _parse_dt(dt) is dt

    def test_iso_string_naive(self):
        result = _parse_dt("2026-08-01T19:00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.hour == 19

    def test_iso_string_with_z(self):
        result = _parse_dt("2026-08-01T19:00:00Z")
        assert isinstance(result, datetime)

    def test_invalid_string_returns_none(self):
        assert _parse_dt("not-a-date") is None

    def test_invalid_type_returns_none(self):
        assert _parse_dt(12345) is None

    def test_empty_string_returns_none(self):
        # fromisoformat("") raises ValueError → None
        assert _parse_dt("") is None


# ── pp_line_to_player_prop tests ──────────────────────────────────────────────

class TestPpLineToPlayerProp:
    def test_basic_conversion(self):
        ln = _make_pp_line()
        prop = pp_line_to_player_prop(ln)
        assert isinstance(prop, PlayerProp)
        assert prop.provider    == "PrizePicks"
        assert prop.player_name == "Mike Trout"
        assert prop.sport       == "MLB"
        assert prop.line_value  == 1.5

    def test_stat_type_normalized(self):
        ln = _make_pp_line(stat_type="Hits")
        prop = pp_line_to_player_prop(ln)
        assert prop.stat_type == "hits"

    def test_provider_always_prizepicks(self):
        ln = _make_pp_line()
        prop = pp_line_to_player_prop(ln)
        assert prop.provider == "PrizePicks"

    def test_external_id_preserved(self):
        ln = _make_pp_line(external_id="pp-xyz")
        prop = pp_line_to_player_prop(ln)
        assert prop.external_id == "pp-xyz"

    def test_game_time_preserved(self):
        dt = datetime(2026, 9, 1, 18, 30)
        ln = _make_pp_line(start_time=dt)
        prop = pp_line_to_player_prop(ln)
        assert prop.game_time == dt

    def test_fetched_at_is_recent(self):
        before = datetime.utcnow()
        ln = _make_pp_line()
        prop = pp_line_to_player_prop(ln)
        assert prop.fetched_at >= before

    def test_team_preserved(self):
        ln = _make_pp_line(team="LAA")
        prop = pp_line_to_player_prop(ln)
        assert prop.team == "LAA"


# ── PrizePicksProvider tests ──────────────────────────────────────────────────

class TestPrizePicksProvider:
    def setup_method(self):
        self.provider = PrizePicksProvider()

    def test_provider_name(self):
        assert self.provider.provider_name == "PrizePicks"

    def test_is_available_false(self):
        assert self.provider.is_available() is False

    def test_sport_keys_default_non_empty(self):
        keys = self.provider.sport_keys
        assert len(keys) > 0
        assert "MLB" in keys
        assert "NBA" in keys

    def test_sport_keys_custom_filter(self):
        p = PrizePicksProvider(sport_filter=["MLB", "NBA"])
        assert p.sport_keys == ["MLB", "NBA"]

    def test_fetch_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            _run(self.provider.fetch_props())

    def test_normalize_stat_delegates(self):
        assert self.provider.normalize_stat("pts") == "points"
        assert self.provider.normalize_stat("reb") == "rebounds"

    def test_repr_contains_provider_name(self):
        r = repr(self.provider)
        assert "PrizePicks" in r


# ── PrizePicksManualProvider tests ────────────────────────────────────────────

class TestPrizePicksManualProviderFromDicts:
    def test_from_dicts_constructor(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict()])
        assert len(p) == 1
        assert p.is_available() is True

    def test_empty_from_dicts_not_available(self):
        p = PrizePicksManualProvider.from_dicts([])
        assert p.is_available() is False

    def test_fetch_props_basic(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict()])
        props = _run(p.fetch_props())
        assert len(props) == 1
        prop = props[0]
        assert prop.provider    == "PrizePicks"
        assert prop.player_name == "LeBron James"
        assert prop.sport       == "NBA"
        assert prop.line_value  == 25.5

    def test_stat_type_normalized_in_fetch(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict(stat_type="pts")])
        props = _run(p.fetch_props())
        assert props[0].stat_type == "points"

    def test_sport_filter_applied(self):
        dicts = [
            _make_dict(sport="NBA"),
            _make_dict(sport="MLB", external_id="pp-002"),
        ]
        p = PrizePicksManualProvider.from_dicts(dicts, sport_filter=["NBA"])
        props = _run(p.fetch_props())
        assert len(props) == 1
        assert props[0].sport == "NBA"

    def test_multiple_records(self):
        dicts = [_make_dict(external_id=f"pp-{i}", player_name=f"Player {i}") for i in range(5)]
        p = PrizePicksManualProvider.from_dicts(dicts)
        props = _run(p.fetch_props())
        assert len(props) == 5

    def test_game_time_parsed_from_iso(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict(start_time="2026-09-01T20:00:00")])
        props = _run(p.fetch_props())
        assert props[0].game_time is not None
        assert props[0].game_time.year == 2026
        assert props[0].game_time.hour == 20

    def test_game_time_none_when_missing(self):
        d = _make_dict()
        del d["start_time"]
        p = PrizePicksManualProvider.from_dicts([d])
        props = _run(p.fetch_props())
        assert props[0].game_time is None

    def test_game_time_none_when_invalid(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict(start_time="NOT-A-DATE")])
        props = _run(p.fetch_props())
        assert props[0].game_time is None

    def test_game_time_from_datetime_object(self):
        dt = datetime(2026, 9, 1, 18, 0)
        p = PrizePicksManualProvider.from_dicts([_make_dict(start_time=dt)])
        props = _run(p.fetch_props())
        assert props[0].game_time == dt

    def test_invalid_line_value_defaults_to_zero(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict(line_value="bad")])
        props = _run(p.fetch_props())
        assert props[0].line_value == 0.0

    def test_external_id_stringified(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict(external_id=12345)])
        props = _run(p.fetch_props())
        assert props[0].external_id == "12345"

    def test_provider_name_is_prizepicks(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict()])
        assert p.provider_name == "PrizePicks"

    def test_sport_keys_default(self):
        p = PrizePicksManualProvider.from_dicts([])
        assert "MLB" in p.sport_keys
        assert "NBA" in p.sport_keys

    def test_sport_keys_custom(self):
        p = PrizePicksManualProvider.from_dicts([], sport_filter=["NFL"])
        assert p.sport_keys == ["NFL"]

    def test_len_dicts(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict() for _ in range(3)])
        assert len(p) == 3

    def test_normalize_stat_delegates(self):
        p = PrizePicksManualProvider.from_dicts([])
        assert p.normalize_stat("hits") == "hits"
        assert p.normalize_stat("pts")  == "points"

    def test_repr_shows_provider(self):
        p = PrizePicksManualProvider.from_dicts([_make_dict()])
        r = repr(p)
        assert "PrizePicks" in r


class TestPrizePicksManualProviderFromLines:
    """Tests using PrizePicksLine objects (skipped if prizepicks module unavailable)."""

    def test_from_pp_lines(self):
        ln = _make_pp_line()
        p = PrizePicksManualProvider(lines=[ln])
        assert len(p) == 1
        assert p.is_available() is True

    def test_fetch_from_pp_lines(self):
        ln = _make_pp_line(player_name="Shohei Ohtani", sport="MLB", line_value=7.5)
        p = PrizePicksManualProvider(lines=[ln])
        props = _run(p.fetch_props())
        assert len(props) == 1
        assert props[0].player_name == "Shohei Ohtani"
        assert props[0].line_value  == 7.5

    def test_sport_filter_on_lines(self):
        lines = [
            _make_pp_line(sport="MLB"),
            _make_pp_line(sport="NBA", stat_type="Points"),
        ]
        p = PrizePicksManualProvider(lines=lines, sport_filter=["MLB"])
        props = _run(p.fetch_props())
        assert len(props) == 1
        assert props[0].sport == "MLB"

    def test_len_lines_plus_dicts(self):
        ln = _make_pp_line()
        p = PrizePicksManualProvider(lines=[ln], raw_dicts=[_make_dict(sport="NBA")])
        assert len(p) == 2

    def test_fetch_from_both_sources(self):
        ln = _make_pp_line(sport="MLB")
        p = PrizePicksManualProvider(lines=[ln], raw_dicts=[_make_dict(sport="NBA")])
        props = _run(p.fetch_props())
        sports = {pr.sport for pr in props}
        assert "MLB" in sports
        assert "NBA" in sports
