"""
Regression tests for the pre-delivery Underdog line freshness guard.

ROOT CAUSE (documented): Within a single underdog_job scan cycle, scoring and
delivery use the same snap object, so the candidate line always equals the latest
known scan line.  The observed stale-alert scenario (alert: 57.5, Underdog app:
59.5) is caused by Underdog API propagation lag — the unofficial endpoint serves
cached data that may not yet reflect a market-maker move visible in Underdog's app.

These tests verify:
  1–6   : _ud_line_fresh() unit behaviour
  10–14 : guard is present in all 5 delivery paths (source inspection)
  15    : _format_95_priority_alert includes "verify" disclaimer
  20    : dedup still works (unchanged)
  30–32 : config finding — MIN_AI_CONFIDENCE vs UD_MIN_CONF_A are separate gates
"""

import inspect
import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 1–6  Unit tests for _ud_line_fresh()
# ═══════════════════════════════════════════════════════════════════════════════

class TestUdLineFresh:
    """_ud_line_fresh() must enforce the freshness invariant correctly."""

    def _fn(self):
        from market_engine import _ud_line_fresh
        return _ud_line_fresh

    def test_01_matching_line_is_fresh(self):
        """1. Scored 57.5 / current 57.5 → allowed."""
        fn = self._fn()
        scan_map = {("Kyren Williams", "Rush Yards"): 57.5}
        assert fn(57.5, "Kyren Williams", "Rush Yards", scan_map) is True

    def test_02_stale_lower_line_blocked(self):
        """2. Scored 57.5 / current 59.5 → blocked (API propagation lag scenario)."""
        fn = self._fn()
        scan_map = {("Kyren Williams", "Rush Yards"): 59.5}
        assert fn(57.5, "Kyren Williams", "Rush Yards", scan_map) is False

    def test_03_stale_higher_line_blocked(self):
        """3. Scored 59.5 / current 57.5 → blocked."""
        fn = self._fn()
        scan_map = {("Kyren Williams", "Rush Yards"): 57.5}
        assert fn(59.5, "Kyren Williams", "Rush Yards", scan_map) is False

    def test_04_float_noise_within_tolerance(self):
        """4. Floating-point noise < 0.01 is NOT treated as a line move."""
        fn = self._fn()
        scan_map = {("Player A", "Points"): 24.500001}
        assert fn(24.5, "Player A", "Points", scan_map) is True

    def test_05_unknown_player_allowed_through(self):
        """5. Player not in scan map → allowed (no stale evidence)."""
        fn = self._fn()
        assert fn(57.5, "Unknown Player", "Yards", {}) is True

    def test_06_half_step_line_move_blocked(self):
        """6. A 0.5-step line move (discrete increment) is blocked."""
        fn = self._fn()
        scan_map = {("Player B", "Points"): 24.5}
        assert fn(24.0, "Player B", "Points", scan_map) is False

    def test_07_full_point_move_blocked(self):
        """7. A 1.0-point line move is blocked."""
        fn = self._fn()
        scan_map = {("Player C", "Assists"): 6.5}
        assert fn(5.5, "Player C", "Assists", scan_map) is False

    def test_08_exact_match_different_player_not_confused(self):
        """8. Players are keyed separately — no cross-player confusion."""
        fn = self._fn()
        scan_map = {
            ("Player D", "Points"): 20.5,
            ("Player E", "Points"): 25.5,
        }
        assert fn(20.5, "Player D", "Points", scan_map) is True
        assert fn(25.5, "Player E", "Points", scan_map) is True
        # Player D's line against Player E's map entry → blocked
        assert fn(20.5, "Player E", "Points", scan_map) is False


# ═══════════════════════════════════════════════════════════════════════════════
# 10–14  Guard is present in all 5 delivery paths (source inspection)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFreshnessGuardPresent:
    """All 5 delivery paths must call _ud_line_fresh before sending."""

    def _src(self):
        import market_engine
        return inspect.getsource(market_engine.underdog_job)

    def test_10_guard_function_exported(self):
        """_ud_line_fresh must be importable from market_engine."""
        from market_engine import _ud_line_fresh
        assert callable(_ud_line_fresh)

    def test_11_scan_line_map_built_each_cycle(self):
        """_current_scan_line_map must be built inside underdog_job."""
        src = self._src()
        assert "_current_scan_line_map" in src, (
            "_current_scan_line_map not found in underdog_job — freshness map missing"
        )

    def test_12_np_95_override_path_removed(self):
        """The separate NP 95+ priority override path has been removed per spec.
        _np_95_fresh was a guard for the broadcast_alert path; it's gone."""
        src = self._src()
        assert "_np_95_fresh" not in src, (
            "_np_95_fresh re-added to underdog_job — the 95+ override NP path is removed"
        )

    def test_13_guard_in_np_normal_path(self):
        """NP normal delivery must check freshness and set _np_bet_ready=False on fail."""
        src = self._src()
        assert "_ud_line_fresh(line_val, player, stat_type" in src, (
            "_ud_line_fresh not found in NP normal path of underdog_job"
        )

    def test_14_lc_95_override_path_removed(self):
        """The separate LC 95+ priority override path has been removed per spec."""
        src = self._src()
        assert "_lc_95_fresh" not in src, (
            "_lc_95_fresh re-added to underdog_job — the 95+ override LC path is removed"
        )

    def test_15_sp_95_override_path_removed(self):
        """The separate SP 95+ priority override path has been removed per spec."""
        src = self._src()
        assert "_sp_95_fresh" not in src, (
            "_sp_95_fresh re-added to underdog_job — the 95+ override SP path is removed"
        )

    def test_16_guard_in_sp_normal_path(self):
        """SP normal delivery must check freshness before deliver_underdog."""
        src = self._src()
        assert "_ud_line_fresh(_line_val, _sp, _st" in src, (
            "_ud_line_fresh not found in SP normal path of underdog_job"
        )

    def test_17_95_override_not_called_in_underdog_job(self):
        """_format_95_priority_alert must NOT be called inside underdog_job.
        The 95+ override paths have been removed; it is now dead code only."""
        import market_engine
        src = inspect.getsource(market_engine.underdog_job)
        assert "_format_95_priority_alert(" not in src, (
            "_format_95_priority_alert is called inside underdog_job — "
            "the 95+ override paths are removed per spec"
        )

    def test_18_freshness_guard_blocks_np_bet_ready(self):
        """NP normal: failing freshness guard must set _np_bet_ready = False."""
        src = self._src()
        # The pattern 'not _ud_line_fresh' must appear before the _np_bet_ready = False assignment
        guard_idx   = src.find("not _ud_line_fresh(line_val, player, stat_type")
        blocked_idx = src.find("_np_bet_ready = False", guard_idx)
        assert guard_idx != -1, "NP freshness guard not found"
        assert blocked_idx != -1, (
            "_np_bet_ready = False not found after freshness guard in NP normal path"
        )

    def test_19_sp_normal_freshness_uses_continue(self):
        """SP normal: failing freshness guard must use continue to skip delivery."""
        src = self._src()
        # _ud_line_fresh(_line_val, _sp, _st ...) followed by continue within 3000 chars
        # (includes the logger.warning() block between the guard and the continue)
        guard_idx    = src.find("_ud_line_fresh(_line_val, _sp, _st")
        continue_idx = src.find("continue", guard_idx)
        assert guard_idx != -1, "SP normal freshness guard not found"
        assert continue_idx != -1 and continue_idx - guard_idx < 3000, (
            "SP normal freshness guard must use `continue` to skip delivery"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 20  Dedup still works (unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDedup:
    """Dedup must be unaffected by the freshness guard addition."""

    def test_20_exact_duplicate_still_deduped(self):
        """Identical prop within the dedup window is still suppressed."""
        from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        store = {}
        _record_prop_alerted(store, "Player X", "NFL", "Rush Yards", 57.5)
        assert _is_prop_deduped(
            store, "Player X", "NFL", "Rush Yards", 57.5,
            dedup_window_seconds=int(cfg.config.UD_ALERT_DEDUP_WINDOW),
            min_line_change=cfg.config.MIN_UNDERDOG_LINE_CHANGE,
        ), "Dedup must still work after freshness guard addition"

    def test_21_significant_line_move_bypasses_dedup(self):
        """A meaningful line move (> MIN_UNDERDOG_LINE_CHANGE) bypasses dedup."""
        from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        store = {}
        _record_prop_alerted(store, "Player Y", "NBA", "Points", 24.5)
        big_move = 24.5 + cfg.config.MIN_UNDERDOG_LINE_CHANGE + 0.1
        assert not _is_prop_deduped(
            store, "Player Y", "NBA", "Points", big_move,
            dedup_window_seconds=int(cfg.config.UD_ALERT_DEDUP_WINDOW),
            min_line_change=cfg.config.MIN_UNDERDOG_LINE_CHANGE,
        ), "Significant line move must not be deduped"


# ═══════════════════════════════════════════════════════════════════════════════
# 30–32  Config finding: MIN_AI_CONFIDENCE is separate from A-tier delivery gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigGates:
    """
    Issue #2 verification: MIN_AI_CONFIDENCE (60) is the global scoring baseline;
    UD_MIN_CONF_A (70) is the actual A-tier delivery gate.  They are intentionally
    separate and the display of 60 does NOT weaken the V3.5 A-tier gate.
    """

    def test_30_min_ai_confidence_is_60(self):
        """MIN_AI_CONFIDENCE default is 60 — the global scoring baseline."""
        import config as cfg
        assert cfg.config.MIN_AI_CONFIDENCE == 60

    def test_31_ud_min_conf_a_is_75(self):
        """UD_MIN_CONF_A default is 75 — the actual A-tier delivery gate."""
        import config as cfg
        assert cfg.config.UD_MIN_CONF_A == 75

    def test_32_a_tier_gate_exceeds_global_baseline(self):
        """A-tier delivery gate (75) is strictly higher than global baseline (60)."""
        import config as cfg
        assert cfg.config.UD_MIN_CONF_A > cfg.config.MIN_AI_CONFIDENCE, (
            "A-tier delivery gate must exceed the global scoring baseline — "
            "60 is display-only; 75 is the real actionable floor"
        )

    def test_33_non_strict_a_tier_also_75(self):
        """UD_NON_STRICT_MIN_CONF_A is also 75 — same actionable floor for all sports."""
        import config as cfg
        assert cfg.config.UD_NON_STRICT_MIN_CONF_A == 75

    def test_34_min_conf_for_sport_returns_75_for_nba_a(self):
        """min_conf_for_sport_tier('NBA', 'A') returns 75 (NBA is Tier 2 — strict floor)."""
        import config as cfg
        result = cfg.config.min_conf_for_sport_tier("NBA", "A")
        assert result == 75, f"Expected 75 for NBA/A, got {result}"

    def test_35_min_conf_for_sport_returns_85_for_s(self):
        """min_conf_for_sport_tier('NBA', 'S') returns 85."""
        import config as cfg
        result = cfg.config.min_conf_for_sport_tier("NBA", "S")
        assert result == 85, f"Expected 85 for NBA/S, got {result}"
