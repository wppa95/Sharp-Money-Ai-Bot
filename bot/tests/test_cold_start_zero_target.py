"""Regression checks for the controlled zero-prop cold-start experiment."""

from pathlib import Path


ENGINE_SOURCE = (
    Path(__file__).resolve().parents[1] / "market_engine.py"
).read_text()


def test_cold_start_rescan_target_is_zero():
    assert "_COLD_START_RESCAN_TARGET: int = 0" in ENGINE_SOURCE


def test_cold_start_scoring_requires_positive_target():
    assert (
        "and _COLD_START_RESCAN_TARGET > 0"
        in ENGINE_SOURCE
    )


def test_new_prop_branch_remains_before_cold_start_branch():
    new_branch = ENGINE_SOURCE.index("if is_new_prop:")
    cold_branch = ENGINE_SOURCE.index("and _COLD_START_RESCAN_TARGET > 0")
    assert new_branch < cold_branch


def test_line_change_scoring_path_is_not_cold_start_gated():
    line_change = ENGINE_SOURCE.index(
        "if not is_removed and line_changed and prev_line is not None:"
    )
    cold_branch = ENGINE_SOURCE.index("and _COLD_START_RESCAN_TARGET > 0")
    assert line_change < cold_branch