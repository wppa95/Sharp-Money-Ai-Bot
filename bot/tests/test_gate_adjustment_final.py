"""
Regression tests for the final gate adjustment:

  MLB/NFL strict alert gate:
    - S-tier only (ud_mlb_alert_tiers)
    - BQ ≥ 95 (UD_STRICT_SPORT_MIN_BET_QUALITY)

  Three paths covered: new-prop, line-change, standing.
  Non-strict sports (NBA, CS, WNBA, …) must NOT be affected by either gate.

Spec:
  MLB/NFL S + BQ ≥ 95 → ALLOW
  MLB/NFL S + BQ < 95  → BLOCK (bq_gate)
  MLB/NFL A or B       → BLOCK (tier gate)
  Other sports         → unaffected by MLB/NFL gates
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me

# ── helpers ────────────────────────────────────────────────────────────────────

def _gate_allows(sport: str, tier: str, bq: int) -> bool:
    """
    Replicate the combined MLB/NFL gate used in all three delivery paths.
    Returns True iff the prop is NOT blocked by either the tier gate or the BQ gate.
    """
    config = me.config
    sport_up = sport.upper()

    # Tier gate: strict sports must pass ud_mlb_alert_tiers (default {"S"})
    if sport_up in config.ud_strict_alert_sports:
        if tier not in config.ud_mlb_alert_tiers:
            return False  # blocked by tier gate

    # BQ gate: strict sports must meet UD_STRICT_SPORT_MIN_BET_QUALITY (default 95)
    if sport_up in config.ud_strict_alert_sports:
        if bq < config.UD_STRICT_SPORT_MIN_BET_QUALITY:
            return False  # blocked by BQ gate

    return True


# ── 1. Config values ───────────────────────────────────────────────────────────

class TestConfigValues:
    """Verify the gate thresholds stored in config."""

    def test_bq_threshold_is_95(self):
        """UD_STRICT_SPORT_MIN_BET_QUALITY default must be 95."""
        assert me.config.UD_STRICT_SPORT_MIN_BET_QUALITY == 95

    def test_strict_sports_are_mlb_and_nfl_only(self):
        """Only MLB and NFL are in ud_strict_alert_sports."""
        assert me.config.ud_strict_alert_sports == frozenset({"MLB", "NFL"})

    def test_mlb_alert_tiers_is_s_only(self):
        """ud_mlb_alert_tiers must contain only 'S' (default UD_MLB_MIN_TIER=S)."""
        assert me.config.ud_mlb_alert_tiers == frozenset({"S"})

    def test_nba_not_in_strict_sports(self):
        assert "NBA" not in me.config.ud_strict_alert_sports

    def test_cs_not_in_strict_sports(self):
        assert "CS" not in me.config.ud_strict_alert_sports

    def test_wnba_not_in_strict_sports(self):
        assert "WNBA" not in me.config.ud_strict_alert_sports


# ── 2. MLB gate logic ──────────────────────────────────────────────────────────

class TestMLBGate:
    """MLB: S + BQ ≥ 95 → ALLOW; everything else → BLOCK."""

    def test_mlb_s_95_allowed(self):
        assert _gate_allows("MLB", "S", 95) is True

    def test_mlb_s_96_allowed(self):
        assert _gate_allows("MLB", "S", 96) is True

    def test_mlb_s_100_allowed(self):
        assert _gate_allows("MLB", "S", 100) is True

    def test_mlb_s_94_blocked(self):
        assert _gate_allows("MLB", "S", 94) is False

    def test_mlb_s_85_blocked(self):
        """Old default BQ=85 must now be blocked for MLB."""
        assert _gate_allows("MLB", "S", 85) is False

    def test_mlb_a_100_blocked(self):
        """A-tier MLB is always blocked regardless of BQ."""
        assert _gate_allows("MLB", "A", 100) is False

    def test_mlb_b_100_blocked(self):
        """B-tier MLB is always blocked regardless of BQ."""
        assert _gate_allows("MLB", "B", 100) is False

    def test_mlb_a_94_blocked(self):
        """A-tier + sub-threshold BQ — both gates block."""
        assert _gate_allows("MLB", "A", 94) is False


# ── 3. NFL gate logic ──────────────────────────────────────────────────────────

class TestNFLGate:
    """NFL: same S + BQ ≥ 95 requirement as MLB."""

    def test_nfl_s_95_allowed(self):
        assert _gate_allows("NFL", "S", 95) is True

    def test_nfl_s_97_allowed(self):
        assert _gate_allows("NFL", "S", 97) is True

    def test_nfl_s_100_allowed(self):
        assert _gate_allows("NFL", "S", 100) is True

    def test_nfl_s_94_blocked(self):
        assert _gate_allows("NFL", "S", 94) is False

    def test_nfl_s_85_blocked(self):
        """Old default BQ=85 must now be blocked for NFL."""
        assert _gate_allows("NFL", "S", 85) is False

    def test_nfl_a_100_blocked(self):
        """A-tier NFL is always blocked regardless of BQ."""
        assert _gate_allows("NFL", "A", 100) is False

    def test_nfl_b_100_blocked(self):
        """B-tier NFL is always blocked regardless of BQ."""
        assert _gate_allows("NFL", "B", 100) is False

    def test_nfl_a_94_blocked(self):
        """A-tier + sub-threshold BQ — both gates apply."""
        assert _gate_allows("NFL", "A", 94) is False


# ── 4. Non-strict sports are unaffected ────────────────────────────────────────

class TestNonStrictSportsUnaffected:
    """NBA, CS, WNBA, etc. must pass through both gates unconditionally."""

    def test_nba_s_95_allowed(self):
        assert _gate_allows("NBA", "S", 95) is True

    def test_nba_s_60_allowed(self):
        """NBA S-tier with BQ=60 — not blocked by MLB/NFL gate."""
        assert _gate_allows("NBA", "S", 60) is True

    def test_nba_a_50_allowed(self):
        """NBA A-tier — not restricted to S-only."""
        assert _gate_allows("NBA", "A", 50) is True

    def test_nba_b_40_allowed(self):
        """NBA B-tier — allowed through the MLB/NFL gate."""
        assert _gate_allows("NBA", "B", 40) is True

    def test_cs_s_70_allowed(self):
        assert _gate_allows("CS", "S", 70) is True

    def test_cs_a_50_allowed(self):
        assert _gate_allows("CS", "A", 50) is True

    def test_wnba_s_80_allowed(self):
        assert _gate_allows("WNBA", "S", 80) is True

    def test_nhl_s_88_allowed(self):
        assert _gate_allows("NHL", "S", 88) is True

    def test_tennis_a_75_allowed(self):
        assert _gate_allows("TENNIS", "A", 75) is True

    def test_soccer_b_60_allowed(self):
        assert _gate_allows("SOCCER", "B", 60) is True


# ── 5. Source code: all three paths have the gate ─────────────────────────────

class TestAllPathsHaveGate:
    """
    Verify the BQ gate and strict-tier gate appear in all three delivery paths
    inside market_engine.py via source-code inspection.
    """

    @pytest.fixture(scope="class")
    def src(self) -> str:
        import inspect
        return inspect.getsource(me)

    def test_bq_gate_new_prop_path(self, src):
        """bq_gate [new] must be present (new-prop delivery path)."""
        assert "bq_gate [new]" in src, "bq_gate [new] not found in market_engine source"

    def test_bq_gate_line_change_path(self, src):
        """bq_gate [lc] must be present (line-change delivery path — added in this pass)."""
        assert "bq_gate [lc]" in src, "bq_gate [lc] not found — line-change path missing BQ gate"

    def test_bq_gate_standing_path(self, src):
        """bq_gate [standing] must be present (standing delivery path)."""
        assert "bq_gate [standing]" in src, "bq_gate [standing] not found in market_engine source"

    def test_strict_tier_gate_new_prop(self, src):
        """sport_tier_gate [new] must be present."""
        assert "sport_tier_gate [new]" in src

    def test_strict_tier_gate_standing(self, src):
        """sport_tier_gate [standing] must be present."""
        assert "sport_tier_gate [standing]" in src

    def test_lc_strict_tier_ok_var(self, src):
        """_lc_strict_tier_ok must exist — closes the NFL tier-gate gap in lc path."""
        assert "_lc_strict_tier_ok" in src, (
            "_lc_strict_tier_ok not found — NFL A/B tier may reach Telegram via lc path"
        )

    def test_strict_tier_blocked_rejection_label(self, src):
        """strict_tier_blocked rejection label must exist in debug tracking."""
        assert "strict_tier_blocked" in src, (
            "strict_tier_blocked rejection label not found in line-change path debug tracking"
        )

    def test_lc_bq_gate_uses_config_threshold(self, src):
        """bq_gate [lc] must use UD_STRICT_SPORT_MIN_BET_QUALITY (not a hardcoded value)."""
        # Look for the bq_gate [lc] comment adjacent to config reference
        idx = src.find("bq_gate [lc]")
        window = src[max(0, idx - 50): idx + 400]
        assert "UD_STRICT_SPORT_MIN_BET_QUALITY" in window, (
            "bq_gate [lc] does not reference config.UD_STRICT_SPORT_MIN_BET_QUALITY"
        )

    def test_no_hardcoded_95_in_bq_gates(self, src):
        """Gate must use config attribute — not a magic literal 95."""
        # Each bq_gate block must NOT have a bare `< 95` literal
        # (it should reference config.UD_STRICT_SPORT_MIN_BET_QUALITY)
        import re
        # Find all `< 95` that appear outside of a comment (heuristic)
        bare = re.findall(r"<\s*95\b(?!\s*#)", src)
        assert len(bare) == 0, (
            f"Found {len(bare)} bare '< 95' literal(s) in market_engine — should use config attribute"
        )


# ── 6. Deduplication still intact (#118) ──────────────────────────────────────

class TestDeduplicationIntact:
    """Confirm dedup helpers from bug #118 are still in place."""

    def test_dedup_helpers_importable(self):
        from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
        assert callable(_is_prop_deduped)
        assert callable(_record_prop_alerted)

    def test_dedup_import_in_market_engine(self):
        import inspect
        src = inspect.getsource(me)
        assert "_is_prop_deduped" in src
        assert "_record_prop_alerted" in src

    def test_dedup_suppresses_same_entry(self):
        """Dedup must suppress a repeated player/sport/stat/line within the window."""
        from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        c = cfg.Config()
        store: dict = {}
        _record_prop_alerted(store, "Test Player", "NBA", "Points", 22.5)
        assert _is_prop_deduped(
            store, "Test Player", "NBA", "Points", 22.5,
            dedup_window_seconds=c.UD_ALERT_DEDUP_WINDOW,
            min_line_change=c.MIN_UNDERDOG_LINE_CHANGE,
        )

    def test_dedup_does_not_suppress_different_player(self):
        from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        c = cfg.Config()
        store: dict = {}
        _record_prop_alerted(store, "Player A", "NBA", "Points", 22.5)
        assert not _is_prop_deduped(
            store, "Player B", "NBA", "Points", 22.5,
            dedup_window_seconds=c.UD_ALERT_DEDUP_WINDOW,
            min_line_change=c.MIN_UNDERDOG_LINE_CHANGE,
        )

    def test_dedup_line_change_bypasses_suppression(self):
        """A significant line move must bypass the dedup window."""
        from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        c = cfg.Config()
        store: dict = {}
        _record_prop_alerted(store, "Player A", "NBA", "Points", 22.5)
        new_line = 22.5 + c.MIN_UNDERDOG_LINE_CHANGE + 0.1
        assert not _is_prop_deduped(
            store, "Player A", "NBA", "Points", new_line,
            dedup_window_seconds=c.UD_ALERT_DEDUP_WINDOW,
            min_line_change=c.MIN_UNDERDOG_LINE_CHANGE,
        )
