"""
Tests for bot/providers/prop_provider.py

Covers:
  - PlayerProp dataclass (fields, prop_key, normalized_stat)
  - PropProviderBase ABC (cannot instantiate, must implement abstract methods)
  - PropComparisonEngine._fair_probs (vig removal math)
  - PropComparisonEngine.compare (direction logic, edge calculation, None guards)
  - PropComparisonEngine.compare_many (batch comparison)
  - PropComparisonEngine.filter_edges (threshold filtering)
  - PropComparison properties (has_edge, line_diff, provider, player_name, etc.)
"""

from __future__ import annotations

import pytest
from datetime import datetime
from typing import Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.prop_provider import (
    PlayerProp,
    PropComparison,
    PropProviderBase,
    PropComparisonEngine,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_prop(
    *,
    provider: str = "TestProvider",
    sport: str = "MLB",
    player_name: str = "Mike Trout",
    team: str = "LAA",
    stat_type: str = "Hits",
    line_value: float = 1.5,
    game_time: Optional[datetime] = None,
    external_id: str = "ext-001",
    game_id: str = "game-001",
) -> PlayerProp:
    return PlayerProp(
        provider=provider,
        sport=sport,
        player_name=player_name,
        team=team,
        stat_type=stat_type,
        line_value=line_value,
        game_time=game_time,
        external_id=external_id,
        game_id=game_id,
    )


# ── PlayerProp tests ──────────────────────────────────────────────────────────

class TestPlayerProp:
    def test_basic_fields(self):
        prop = make_prop()
        assert prop.provider == "TestProvider"
        assert prop.sport == "MLB"
        assert prop.player_name == "Mike Trout"
        assert prop.team == "LAA"
        assert prop.stat_type == "Hits"
        assert prop.line_value == 1.5
        assert prop.external_id == "ext-001"

    def test_prop_key_uniqueness(self):
        p1 = make_prop(provider="PP", sport="NBA", player_name="LeBron", stat_type="Points")
        p2 = make_prop(provider="PP", sport="NBA", player_name="LeBron", stat_type="Points")
        p3 = make_prop(provider="PP", sport="NBA", player_name="LeBron", stat_type="Assists")
        assert p1.prop_key == p2.prop_key
        assert p1.prop_key != p3.prop_key

    def test_prop_key_contains_provider(self):
        p1 = make_prop(provider="PrizePicks", player_name="Test", sport="MLB", stat_type="Hits")
        p2 = make_prop(provider="Underdog",   player_name="Test", sport="MLB", stat_type="Hits")
        assert p1.prop_key != p2.prop_key

    def test_prop_key_tuple_structure(self):
        prop = make_prop(provider="PP", player_name="X", sport="NFL", stat_type="Y")
        key = prop.prop_key
        assert isinstance(key, tuple)
        assert len(key) == 4
        assert key[0] == "PP"
        assert key[1] == "X"
        assert key[2] == "NFL"
        assert key[3] == "Y"

    def test_normalized_stat_lowercase(self):
        prop = make_prop(stat_type="Rushing Yards")
        assert prop.normalized_stat() == "rushing yards"

    def test_normalized_stat_strips_whitespace(self):
        prop = make_prop(stat_type="  Points  ")
        assert prop.normalized_stat() == "points"

    def test_fetched_at_auto_set(self):
        before = datetime.utcnow()
        prop = make_prop()
        assert prop.fetched_at >= before

    def test_game_time_optional(self):
        prop = make_prop(game_time=None)
        assert prop.game_time is None

    def test_game_time_stored(self):
        dt = datetime(2026, 8, 1, 19, 0)
        prop = make_prop(game_time=dt)
        assert prop.game_time == dt

    def test_repr_contains_key_info(self):
        prop = make_prop(player_name="Trout", stat_type="Hits")
        r = repr(prop)
        assert "Trout" in r
        assert "Hits" in r

    def test_different_sports_different_keys(self):
        p1 = make_prop(sport="MLB")
        p2 = make_prop(sport="NBA")
        assert p1.prop_key != p2.prop_key


# ── PropProviderBase ABC tests ────────────────────────────────────────────────

class TestPropProviderBase:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PropProviderBase()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_provider_name(self):
        class NoName(PropProviderBase):
            @property
            def sport_keys(self) -> list[str]:
                return []
            async def fetch_props(self):
                return []

        with pytest.raises(TypeError):
            NoName()

    def test_concrete_subclass_must_implement_fetch_props(self):
        class NoFetch(PropProviderBase):
            @property
            def provider_name(self) -> str:
                return "X"
            @property
            def sport_keys(self) -> list[str]:
                return []

        with pytest.raises(TypeError):
            NoFetch()

    def test_valid_subclass_instantiates(self):
        class Valid(PropProviderBase):
            @property
            def provider_name(self) -> str:
                return "TestValid"
            @property
            def sport_keys(self) -> list[str]:
                return ["MLB", "NBA"]
            async def fetch_props(self):
                return []

        p = Valid()
        assert p.provider_name == "TestValid"
        assert p.sport_keys == ["MLB", "NBA"]
        assert p.is_available() is True   # default

    def test_normalize_stat_default(self):
        class P(PropProviderBase):
            @property
            def provider_name(self): return "X"
            @property
            def sport_keys(self): return []
            async def fetch_props(self): return []

        p = P()
        assert p.normalize_stat("Rushing Yards") == "rushing yards"
        assert p.normalize_stat("  Hits  ") == "hits"

    def test_is_available_default_true(self):
        class P(PropProviderBase):
            @property
            def provider_name(self): return "X"
            @property
            def sport_keys(self): return []
            async def fetch_props(self): return []

        assert P().is_available() is True

    def test_repr_shows_class_and_name(self):
        class MyProv(PropProviderBase):
            @property
            def provider_name(self): return "MyProv"
            @property
            def sport_keys(self): return []
            async def fetch_props(self): return []

        r = repr(MyProv())
        assert "MyProv" in r


# ── PropComparisonEngine._fair_probs tests ────────────────────────────────────

class TestFairProbs:
    """Unit tests for the vig-removal math."""

    def _fp(self, over, under):
        return PropComparisonEngine._fair_probs(over, under)

    def test_symmetric_market(self):
        fp_o, fp_u = self._fp(-110, -110)
        assert abs(fp_o - 0.5) < 0.01
        assert abs(fp_u - 0.5) < 0.01
        assert abs(fp_o + fp_u - 1.0) < 1e-10

    def test_favourite_gets_higher_prob(self):
        # -130 is the favourite, +110 is the dog
        fp_o, fp_u = self._fp(-130, 110)
        assert fp_o > fp_u

    def test_sums_to_one(self):
        fp_o, fp_u = self._fp(-115, -105)
        assert abs(fp_o + fp_u - 1.0) < 1e-10

    def test_positive_over_odds(self):
        fp_o, fp_u = self._fp(110, -130)
        assert fp_u > fp_o
        assert abs(fp_o + fp_u - 1.0) < 1e-10

    def test_zero_odds_fallback(self):
        fp_o, fp_u = self._fp(0, 0)
        assert fp_o == 0.5
        assert fp_u == 0.5

    def test_no_vig_market(self):
        # +100 / +100 → each 50 % → fair 50/50
        fp_o, fp_u = self._fp(100, 100)
        assert abs(fp_o - 0.5) < 0.01
        assert abs(fp_u - 0.5) < 0.01


# ── PropComparisonEngine.compare tests ───────────────────────────────────────

class TestPropComparisonEngineCompare:
    def setup_method(self):
        self.engine = PropComparisonEngine(min_edge_pct=3.0)

    def _compare(self, prop_line, sb_line, over_odds=-110, under_odds=-110):
        prop = make_prop(line_value=prop_line)
        return self.engine.compare(
            prop, sb_line=sb_line,
            sb_over_odds=over_odds, sb_under_odds=under_odds,
            sportsbook="DraftKings",
        )

    def test_returns_none_when_over_odds_zero(self):
        prop = make_prop(line_value=1.5)
        result = self.engine.compare(prop, 1.5, sb_over_odds=0, sb_under_odds=-110, sportsbook="DK")
        assert result is None

    def test_returns_none_when_under_odds_zero(self):
        prop = make_prop(line_value=1.5)
        result = self.engine.compare(prop, 1.5, sb_over_odds=-110, sb_under_odds=0, sportsbook="DK")
        assert result is None

    def test_returns_comparison_for_valid_input(self):
        result = self._compare(prop_line=1.5, sb_line=1.5)
        assert result is not None
        assert isinstance(result, PropComparison)

    def test_direction_provider_higher_sb_line_under(self):
        """Provider line > sportsbook line → UNDER is the edge side."""
        result = self._compare(prop_line=2.5, sb_line=1.5)
        assert result is not None
        assert result.best_side == "UNDER"

    def test_direction_provider_lower_sb_line_over(self):
        """Provider line < sportsbook line → OVER is the edge side."""
        result = self._compare(prop_line=0.5, sb_line=1.5)
        assert result is not None
        assert result.best_side == "OVER"

    def test_equal_lines_uses_fair_prob(self):
        """When lines match, the side with higher fair prob wins."""
        # -110/-110 → fair 50/50 → edge_over == edge_under → OVER chosen (tie-break)
        result = self._compare(prop_line=1.5, sb_line=1.5, over_odds=-110, under_odds=-110)
        assert result is not None
        assert result.best_side in ("OVER", "UNDER")

    def test_equal_lines_favourite_over(self):
        """When lines equal but over is -130, over has higher fair prob → OVER."""
        result = self._compare(prop_line=1.5, sb_line=1.5, over_odds=-130, under_odds=110)
        assert result is not None
        assert result.best_side == "OVER"

    def test_edge_positive_for_under_when_provider_higher(self):
        # over=+120, under=-140 → sportsbook thinks UNDER is likely (fp_under > 0.5)
        # provider line > sb_line → best_side = UNDER
        # → edge_under = (fp_under - 0.5) * 100 > 0
        result = self._compare(prop_line=2.5, sb_line=1.5, over_odds=120, under_odds=-140)
        assert result is not None
        assert result.best_side == "UNDER"
        assert result.edge_under > 0
        assert result.best_edge == result.edge_under

    def test_fair_probs_sum_to_one(self):
        result = self._compare(prop_line=1.5, sb_line=1.5)
        assert result is not None
        assert abs(result.fair_prob_over + result.fair_prob_under - 1.0) < 1e-9

    def test_line_diff_positive_when_provider_higher(self):
        result = self._compare(prop_line=2.5, sb_line=1.5)
        assert result is not None
        assert result.line_diff == pytest.approx(1.0)

    def test_line_diff_negative_when_provider_lower(self):
        result = self._compare(prop_line=0.5, sb_line=1.5)
        assert result is not None
        assert result.line_diff == pytest.approx(-1.0)

    def test_has_edge_when_best_edge_positive(self):
        # over=+120, under=-140 → sportsbook thinks UNDER is likely (fp_under > 0.5)
        # provider line > sb_line → best_side = UNDER, best_edge = edge_under > 0
        result = self._compare(prop_line=2.5, sb_line=1.5, over_odds=120, under_odds=-140)
        assert result is not None
        assert result.has_edge is True

    def test_sportsbook_stored(self):
        prop = make_prop(line_value=1.5)
        result = self.engine.compare(prop, 1.5, -110, -110, "FanDuel")
        assert result is not None
        assert result.sportsbook == "FanDuel"

    def test_passthrough_properties(self):
        prop = make_prop(provider="PP", player_name="Shohei", sport="MLB", stat_type="Hits")
        result = self.engine.compare(prop, 1.5, -110, -110, "DK")
        assert result is not None
        assert result.provider == "PP"
        assert result.player_name == "Shohei"
        assert result.sport == "MLB"
        assert result.stat_type == "Hits"

    def test_detected_at_set(self):
        before = datetime.utcnow()
        result = self._compare(prop_line=1.5, sb_line=1.5)
        assert result is not None
        assert result.detected_at >= before

    def test_rounded_edge_values(self):
        result = self._compare(prop_line=1.5, sb_line=1.5)
        assert result is not None
        # Values should be rounded to 4 decimal places
        assert round(result.edge_over, 4) == result.edge_over
        assert round(result.edge_under, 4) == result.edge_under


# ── PropComparisonEngine.compare_many tests ───────────────────────────────────

class TestCompareManyAndFilter:
    def setup_method(self):
        self.engine = PropComparisonEngine(min_edge_pct=2.0)

    def test_compare_many_empty_list(self):
        results = self.engine.compare_many([], 1.5, -110, -110, "DK")
        assert results == []

    def test_compare_many_all_valid(self):
        props = [make_prop(line_value=v) for v in [0.5, 1.5, 2.5]]
        results = self.engine.compare_many(props, 1.5, -110, -110, "DK")
        assert len(results) == 3

    def test_compare_many_skips_zero_odds(self):
        props = [make_prop(line_value=1.5)]
        results = self.engine.compare_many(props, 1.5, 0, -110, "DK")
        assert results == []

    def test_filter_edges_removes_below_threshold(self):
        # prop_line == sb_line → edge ≈ 0 for -110/-110 → below threshold
        prop = make_prop(line_value=1.5)
        c = self.engine.compare(prop, 1.5, -110, -110, "DK")
        assert c is not None
        filtered = self.engine.filter_edges([c])
        # Edge for equal lines = (0.5 - 0.5)*100 = 0 < min_edge_pct=2.0
        assert filtered == []

    def test_filter_edges_keeps_above_threshold(self):
        # over=+110, under=-130 → sportsbook thinks UNDER is likely (fp_under > 0.5)
        # provider line > sb_line → best_side = UNDER, edge_under > 0 → above 0.1%
        engine = PropComparisonEngine(min_edge_pct=0.1)
        prop = make_prop(line_value=2.5)
        c = engine.compare(prop, 1.5, sb_over_odds=110, sb_under_odds=-130, sportsbook="DK")
        assert c is not None
        assert c.edge_under > 0.1, f"edge_under={c.edge_under}"
        filtered = engine.filter_edges([c])
        assert len(filtered) == 1

    def test_filter_edges_sorted_best_first(self):
        engine = PropComparisonEngine(min_edge_pct=0.0)
        props = [make_prop(line_value=v) for v in [0.5, 10.5, 5.5]]
        comparisons = [
            engine.compare(p, 1.5, -130, 110, "DK")
            for p in props
        ]
        comparisons = [c for c in comparisons if c is not None]
        filtered = engine.filter_edges(comparisons)
        # Should be sorted best_edge descending
        edges = [c.best_edge for c in filtered]
        assert edges == sorted(edges, reverse=True)


# ── PropComparison repr tests ─────────────────────────────────────────────────

class TestPropComparisonRepr:
    def test_repr_contains_player_name(self):
        prop = make_prop(player_name="Shohei Ohtani", stat_type="Strikeouts")
        engine = PropComparisonEngine()
        c = engine.compare(prop, 8.5, -115, -105, "DK")
        assert c is not None
        r = repr(c)
        assert "Shohei Ohtani" in r
        assert "Strikeouts" in r
