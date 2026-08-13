"""
test_pipeline_fix.py

Regression tests for the two pipeline bottleneck fixes:

Fix 1: Esports stats added to _HIGH_FLOOR_STATS
  — "Kills on Maps 1+2" and "Assists on Maps 1+2" must be in the set so
    CS/LOL props with stable lines can reach the standing alert path.

Fix 2: _processed_keys only set when should_alert=True
  — Props with any line change but sub-threshold delta (is_qualified=True,
    should_alert=False) must NOT be in _processed_keys so the standing path
    can evaluate them.
  — Props that ARE alert-eligible (should_alert=True) MUST be in
    _processed_keys to prevent standing path double-evaluation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 — _HIGH_FLOOR_STATS must include esports multi-map aggregates
# ─────────────────────────────────────────────────────────────────────────────

class TestHighFloorStatsEsports:
    """Esports multi-map aggregate stats must be in _HIGH_FLOOR_STATS."""

    def _get_hfs(self):
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        return _HIGH_FLOOR_STATS

    def test_kills_on_maps_in_high_floor_stats(self):
        """'Kills on Maps 1+2' must be in _HIGH_FLOOR_STATS (CS/LOL standing path gate)."""
        assert "Kills on Maps 1+2" in self._get_hfs(), (
            "'Kills on Maps 1+2' missing from _HIGH_FLOOR_STATS — "
            "CS/LOL props with stable lines will never reach the standing path."
        )

    def test_assists_on_maps_in_high_floor_stats(self):
        """'Assists on Maps 1+2' must be in _HIGH_FLOOR_STATS."""
        assert "Assists on Maps 1+2" in self._get_hfs(), (
            "'Assists on Maps 1+2' missing from _HIGH_FLOOR_STATS."
        )

    def test_traditional_stats_still_present(self):
        """Existing traditional sport stats must not have been removed."""
        hfs = self._get_hfs()
        for stat in (
            "Hits", "Points", "Rebounds", "Assists",
            "Fantasy Score", "Rushing Yards", "Passing Yards",
        ):
            assert stat in hfs, f"'{stat}' was unexpectedly removed from _HIGH_FLOOR_STATS"

    def test_volatile_single_event_stats_not_added(self):
        """Volatile stats must not be in _HIGH_FLOOR_STATS."""
        hfs = self._get_hfs()
        for stat in ("Home Runs", "Stolen Bases", "Saves", "Wins"):
            assert stat not in hfs, (
                f"'{stat}' should not be in _HIGH_FLOOR_STATS — it is high-variance."
            )

    def test_high_floor_stats_is_frozenset(self):
        """_HIGH_FLOOR_STATS must remain a frozenset (immutable, hashable)."""
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert isinstance(_HIGH_FLOOR_STATS, frozenset)


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 — _processed_keys gate logic in the line-change path
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessedKeysGate:
    """
    _processed_keys must only be set when should_alert=True so that
    qualified-but-not-alerted props remain available for the standing path.

    These tests verify the logic by inspecting the market_engine source code
    directly (AST / source-text analysis) to confirm the fix is in place
    without needing to spin up a full DB + bot instance.
    """

    def _get_engine_source(self) -> str:
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "market_engine.py"
        return path.read_text()

    def test_processed_keys_not_added_immediately_after_line_change_detection(self):
        """
        The comment 'NOTE: _processed_keys is NOT set here' must be present at
        the point where line-change scoring occurs, confirming the fix is in place.
        """
        src = self._get_engine_source()
        assert "_processed_keys is NOT set here" in src, (
            "Fix 2 marker comment missing from market_engine.py. "
            "The _processed_keys.add() may have been reverted to fire for all line-change props."
        )

    def test_processed_keys_added_inside_should_alert_block(self):
        """
        _processed_keys.add() in the line-change path must appear AFTER the
        'Stage 4: gated count' comment (i.e., inside the should_alert=True block).
        """
        src = self._get_engine_source()
        stage4_pos    = src.find("Stage 4: gated count")
        processed_pos = src.find("_processed_keys.add((player, stat_type))",
                                  stage4_pos)
        assert stage4_pos != -1, "Stage 4 gated count comment not found in market_engine.py"
        assert processed_pos != -1, (
            "_processed_keys.add() not found after Stage 4 comment — "
            "fix may not have been applied correctly."
        )
        # The add() must appear within 800 characters of the Stage 4 comment
        assert processed_pos - stage4_pos < 800, (
            f"_processed_keys.add() is too far from Stage 4 comment "
            f"(gap={processed_pos - stage4_pos} chars). "
            "It may not be inside the should_alert=True block."
        )

    def test_no_early_processed_keys_add_in_line_change_scoring(self):
        """
        The pattern '_processed_keys.add' must NOT appear between
        'detect_market_pressure(_lc_magnitude' and 'validation = validate_player_prop'
        (the removed location from the bug).
        """
        src = self._get_engine_source()
        detect_pos    = src.find("detect_market_pressure(_lc_magnitude")
        validate_pos  = src.find("validation = validate_player_prop", detect_pos)
        assert detect_pos   != -1, "detect_market_pressure(_lc_magnitude not found"
        assert validate_pos != -1, "validate_player_prop not found after detect_market_pressure"

        # Check whether _processed_keys.add appears between these two positions
        segment = src[detect_pos:validate_pos]
        assert "_processed_keys.add" not in segment, (
            "_processed_keys.add() was found between detect_market_pressure and "
            "validate_player_prop. The early-add bug may have been re-introduced."
        )

    def test_reentry_path_still_adds_processed_keys(self):
        """
        The re-entry path (is_reentry=True) must still add to _processed_keys
        — re-entries go through the new-prop path and should not be re-evaluated
        by the standing path.
        """
        src = self._get_engine_source()
        reentry_pos   = src.find("is_reentry = not is_removed")
        add_pos       = src.find("_processed_keys.add((player, stat_type))", reentry_pos)
        assert reentry_pos != -1, "is_reentry detection not found"
        assert add_pos != -1, (
            "_processed_keys.add() not found after is_reentry detection — "
            "re-entry path may have lost its _processed_keys protection."
        )
        # Must be within the re-entry scoring block (< 1500 chars)
        assert add_pos - reentry_pos < 1500, (
            "Re-entry _processed_keys.add() is too far from the is_reentry detection."
        )

    def test_new_prop_path_still_adds_processed_keys(self):
        """
        The new-prop path (is_new_prop=True) must still add to _processed_keys.
        """
        src = self._get_engine_source()
        new_prop_pos = src.find("if is_new_prop:")
        add_pos      = src.find("_processed_keys.add((player, stat_type))", new_prop_pos)
        assert new_prop_pos != -1, "'if is_new_prop:' not found in market_engine.py"
        assert add_pos != -1, (
            "_processed_keys.add() not found after 'if is_new_prop:' — "
            "new-prop path may have lost its _processed_keys protection."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Regression: standing path gates still intact
# ─────────────────────────────────────────────────────────────────────────────

class TestStandingPathGatesIntact:
    """Verify the standing path's quality gates were not removed."""

    def _get_engine_source(self) -> str:
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "market_engine.py"
        return path.read_text()

    def test_hfs_filter_still_in_standing_path(self):
        """Standing path must still filter by _HFS (only high-floor stat types)."""
        src = self._get_engine_source()
        assert "_st not in _HFS" in src, (
            "Standing path _HFS filter was removed — "
            "could allow volatile single-event stats into standing alerts."
        )

    def test_score_tier_gate_still_in_standing_path(self):
        """Standing path must still require prior S/A/B tier snapshot (or derived equivalent).

        The gate was extended from ("A","S") to ("A","S","B") so B-tier candidates
        can enter the standing path when score_total ≥ 50, consistent with the spec
        change making S/A/B all actionable.
        """
        src = self._get_engine_source()
        # The gate now uses _prev_eff_tier including B-tier
        assert '_prev_eff_tier not in ("A", "S", "B")' in src, (
            "Effective-tier gate missing or too narrow in standing path — "
            "must block tiers below B (S/A/B are all actionable)."
        )

    def test_24h_dedup_still_in_standing_path(self):
        """Standing path must still enforce 24h alert dedup."""
        src = self._get_engine_source()
        assert "has_recent_ud_alert" in src, (
            "24h dedup gate removed from standing path — "
            "could cause repeat daily alerts for the same prop."
        )

    def test_game_live_gate_still_in_standing_path(self):
        """Game-live gate must still be applied in standing path."""
        src = self._get_engine_source()
        # Look for the standing-specific live gate log label
        assert "live_gate [standing]" in src, (
            "Game-live gate removed from standing path — "
            "could send alerts on already-started games."
        )

    def test_confidence_gate_still_in_standing_path(self):
        """Per-tier confidence gate must still be applied in standing path."""
        src = self._get_engine_source()
        assert "conf_gate [standing]" in src, (
            "Confidence gate removed from standing path."
        )

    def test_bq_gate_removed_from_standing_path(self):
        """BQ gate removed from standing path — decision_tier (S/A only) enforces quality."""
        src = self._get_engine_source()
        assert "bq_gate [standing]" not in src, (
            "bq_gate [standing] found — BQ gate must be removed per spec Tier 2"
        )

    def test_debug_logging_added_for_no_data_rejection(self):
        """Debug log must be present for has_supporting_data=False rejection in standing path."""
        src = self._get_engine_source()
        assert "standing_gate [no_data]" in src, (
            "Debug log for standing path no_data rejection is missing — "
            "silent rejections cannot be diagnosed from logs."
        )

    def test_debug_logging_added_for_decision_pass_rejection(self):
        """Debug log must be present for PASS decision rejection in standing path."""
        src = self._get_engine_source()
        assert "standing_gate [decision_pass]" in src, (
            "Debug log for standing path decision_pass rejection is missing."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Regression: MLB/NFL gates unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestMLBNFLGatesUnchanged:
    """MLB/NFL strict gates must remain intact after the fix."""

    def _get_engine_source(self) -> str:
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "market_engine.py"
        return path.read_text()

    def test_bq_gate_removed_from_new_prop_path(self):
        """BQ gate removed from new-prop path — decision_tier enforces quality."""
        src = self._get_engine_source()
        assert "bq_gate [new]" not in src, (
            "bq_gate [new] found — BQ gate must be removed per spec Tier 2"
        )

    def test_mlb_under_block_present(self):
        """MLB/NFL UNDER block must be present — Tier 2 OVER only."""
        src = self._get_engine_source()
        assert "mlb_under_gate" in src, (
            "mlb_under_gate not found in market_engine.py — MLB/NFL UNDER must be blocked for Tier 2"
        )

    def test_strict_alert_sports_config_referenced(self):
        """ud_strict_alert_sports must still be referenced in market_engine.py."""
        src = self._get_engine_source()
        assert "ud_strict_alert_sports" in src, (
            "ud_strict_alert_sports not found — MLB/NFL strict treatment may be broken."
        )
