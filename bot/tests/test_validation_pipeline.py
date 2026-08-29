"""Regression tests for sport-neutral Underdog evaluation validation."""

from pathlib import Path

from engine.player_validator import validate_player_prop
from market_engine import _tier_delivery_gate


def test_first_seen_statistical_prop_is_validation_context_not_pipeline_block():
    """A valid first-seen market has no evidence yet, but is still evaluable."""
    validation = validate_player_prop(
        "Patrick Mahomes",
        "Season Receiving Yards",
        250.5,
        [],
    )

    assert validation.has_supporting_data is False
    assert "first appearance" in validation.reason

    source = (Path(__file__).parents[1] / "market_engine.py").read_text()
    first_new_prop = source.index("if is_new_prop:")
    next_new_prop = source.index("if is_new_prop:", first_new_prop + 1)
    new_prop_block = source[first_new_prop:next_new_prop]
    assert 'if score.tier != "PASS" or np_immediate:' not in new_prop_block
    assert "np_immediate = False" not in new_prop_block
    assert "validation_blocked" not in new_prop_block


def test_direction_and_evidence_gate_stays_sport_independent():
    for sport in ("NFL", "MLB", "NBA", "NHL", "WNBA", "TENNIS", "DOTA"):
        assert not _tier_delivery_gate(sport, "OVER", 0, 0, False, "PASS", confidence=0)
        assert _tier_delivery_gate(sport, "OVER", 0, 0, True, "PASS", confidence=0)
        assert _tier_delivery_gate(sport, "UNDER", 0, 0, True, "C", confidence=1)
        assert not _tier_delivery_gate(sport, "MONEYLINE", 100, 100, True, "S", confidence=100)