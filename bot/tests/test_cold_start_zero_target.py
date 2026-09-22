"""Regression checks that the retired cold-start framework stays removed."""

from pathlib import Path


ENGINE_SOURCE = (
    Path(__file__).resolve().parents[1] / "market_engine.py"
).read_text()


def test_cold_start_state_is_removed():
    assert "_COLD_START_RESCAN_TARGET" not in ENGINE_SOURCE
    assert "_cold_start_done" not in ENGINE_SOURCE


def test_cold_start_branch_is_removed():
    assert "is_cold_start" not in ENGINE_SOURCE


def test_uniform_scan_has_incremental_snapshot_batch():
    assert "_incremental_records.append(record)" in ENGINE_SOURCE


def test_line_change_scoring_is_not_startup_gated():
    line_change = ENGINE_SOURCE.index("line_move_threshold_met")
    gate_window = ENGINE_SOURCE[line_change:line_change + 500]
    assert "is_cold_start" not in gate_window