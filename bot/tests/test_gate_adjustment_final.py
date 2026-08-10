"""
Regression tests for the delivery gate spec (updated):

  MLB/NFL strict alert gate (Tier 2):
    - S-tier or A-tier ONLY  (UD_MLB_MIN_TIER=A default)
    - B/C = WATCHLIST, not delivered
    - BOTH OVER and UNDER are allowed
    - No separate BQ gate — decision_tier enforces quality

  Tier 1 (all other sports):
    - S or A delivered; B/C watchlist
    - Both directions allowed

  Three paths covered: new-prop, line-change, standing.
  Non-strict sports (NBA, CS, WNBA, …) must NOT be affected by the strict-sport gate.

Spec:
  MLB/NFL S or A  → ALLOW (both directions)
  MLB/NFL B or C  → BLOCK (tier gate)
  Other sports    → unaffected by MLB/NFL tier gate
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


# ── helpers ────────────────────────────────────────────────────────────────────

def _gate_allows(sport: str, tier: str) -> bool:
    """
    Replicate the Tier 2 MLB/NFL strict gate (tier only, no BQ gate).
    Returns True iff NOT blocked by the tier gate.
    """
    config = me.config
    sport_up = sport.upper()

    # Tier gate: strict sports must pass ud_mlb_alert_tiers (default {"S","A"})
    if sport_up in config.ud_strict_alert_sports:
        if tier not in config.ud_mlb_alert_tiers:
            return False  # blocked by tier gate

    return True


# ── 1. Config values ───────────────────────────────────────────────────────────

class TestConfigValues:
    """Verify the gate thresholds stored in config."""

    def test_strict_sports_are_mlb_and_nfl_only(self):
        """Only MLB and NFL are in ud_strict_alert_sports."""
        assert me.config.ud_strict_alert_sports == frozenset({"MLB", "NFL"})

    def test_mlb_alert_tiers_includes_s_and_a(self):
        """ud_mlb_alert_tiers must contain 'S' AND 'A' (default UD_MLB_MIN_TIER=A)."""
        tiers = me.config.ud_mlb_alert_tiers
        assert "S" in tiers, "S-tier must be in ud_mlb_alert_tiers"
        assert "A" in tiers, "A-tier must be in ud_mlb_alert_tiers (spec Tier 2)"

    def test_mlb_alert_tiers_excludes_b_and_c(self):
        """B and C must NOT be in ud_mlb_alert_tiers (watchlist only)."""
        tiers = me.config.ud_mlb_alert_tiers
        assert "B" not in tiers, "B-tier must remain watchlist for MLB"
        assert "C" not in tiers, "C-tier must remain watchlist for MLB"

    def test_nba_not_in_strict_sports(self):
        assert "NBA" not in me.config.ud_strict_alert_sports

    def test_cs_not_in_strict_sports(self):
        assert "CS" not in me.config.ud_strict_alert_sports

    def test_wnba_not_in_strict_sports(self):
        assert "WNBA" not in me.config.ud_strict_alert_sports


# ── 2. MLB gate logic ──────────────────────────────────────────────────────────

class TestMLBGate:
    """MLB: S or A → ALLOW; B/C → BLOCK. Both directions valid."""

    def test_mlb_s_allowed(self):
        assert _gate_allows("MLB", "S") is True

    def test_mlb_a_allowed(self):
        """A-tier MLB is NOW allowed (spec Tier 2: S or A)."""
        assert _gate_allows("MLB", "A") is True

    def test_mlb_b_blocked(self):
        """B-tier MLB is WATCHLIST only — not delivered."""
        assert _gate_allows("MLB", "B") is False

    def test_mlb_c_blocked(self):
        """C-tier MLB is WATCHLIST only — not delivered."""
        assert _gate_allows("MLB", "C") is False

    def test_mlb_under_not_blocked_by_gate_logic(self):
        """UNDER direction is not a gate condition — direction must not block delivery."""
        # The _gate_allows helper uses only tier; direction not a factor
        assert _gate_allows("MLB", "S") is True
        assert _gate_allows("MLB", "A") is True


# ── 3. NFL gate logic ──────────────────────────────────────────────────────────

class TestNFLGate:
    """NFL: same S/A tier requirement as MLB."""

    def test_nfl_s_allowed(self):
        assert _gate_allows("NFL", "S") is True

    def test_nfl_a_allowed(self):
        """A-tier NFL is NOW allowed (spec Tier 2: S or A)."""
        assert _gate_allows("NFL", "A") is True

    def test_nfl_b_blocked(self):
        """B-tier NFL is WATCHLIST only."""
        assert _gate_allows("NFL", "B") is False

    def test_nfl_c_blocked(self):
        """C-tier NFL is WATCHLIST only."""
        assert _gate_allows("NFL", "C") is False


# ── 4. Non-strict sports are unaffected ────────────────────────────────────────

class TestNonStrictSportsUnaffected:
    """NBA, CS, WNBA, etc. must pass through both gates unconditionally."""

    def test_nba_s_allowed(self):
        assert _gate_allows("NBA", "S") is True

    def test_nba_a_allowed(self):
        assert _gate_allows("NBA", "A") is True

    def test_nba_b_allowed(self):
        """NBA B-tier — not blocked by MLB/NFL gate."""
        assert _gate_allows("NBA", "B") is True

    def test_cs_s_allowed(self):
        assert _gate_allows("CS", "S") is True

    def test_cs_a_allowed(self):
        assert _gate_allows("CS", "A") is True

    def test_wnba_s_allowed(self):
        assert _gate_allows("WNBA", "S") is True

    def test_nhl_s_allowed(self):
        assert _gate_allows("NHL", "S") is True

    def test_tennis_a_allowed(self):
        assert _gate_allows("TENNIS", "A") is True

    def test_soccer_b_allowed(self):
        assert _gate_allows("SOCCER", "B") is True


# ── 5. Source code: all three paths have the tier gate ─────────────────────────

class TestAllPathsHaveGate:
    """
    Verify the strict-tier gate appears in all three delivery paths
    inside market_engine.py via source-code inspection.
    BQ gate removed per spec — decision_tier enforces quality.
    """

    @pytest.fixture(scope="class")
    def src(self) -> str:
        import inspect
        return inspect.getsource(me)

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

    def test_bq_gate_removed_from_new_prop(self, src):
        """BQ gate must NOT be enforced in the new-prop path (removed per spec)."""
        assert "bq_gate [new]" not in src, (
            "bq_gate [new] found — BQ gate was removed from new-prop path per spec Tier 2"
        )

    def test_bq_gate_removed_from_lc_path(self, src):
        """BQ gate must NOT be enforced in the line-change path."""
        assert "bq_gate [lc]" not in src, (
            "bq_gate [lc] found — BQ gate was removed from lc path per spec Tier 2"
        )

    def test_bq_gate_removed_from_standing_path(self, src):
        """BQ gate must NOT be enforced in the standing path."""
        assert "bq_gate [standing]" not in src, (
            "bq_gate [standing] found — BQ gate was removed from standing path per spec Tier 2"
        )

    def test_mlb_under_not_blocked_in_lc_path(self, src):
        """lc-path _lc_mlb_ok must NOT contain UNDER direction block."""
        idx = src.find("_lc_mlb_ok")
        assert idx != -1, "_lc_mlb_ok not found"
        snippet = src[idx: idx + 600]
        assert "recommendation != \"UNDER\"" not in snippet, (
            "_lc_mlb_ok still blocks UNDER — remove per spec Tier 2"
        )

    def test_mlb_under_not_blocked_in_standing_path(self, src):
        """Standing path must not have an MLB UNDER block."""
        assert "mlb_under_gate [standing]" not in src, (
            "mlb_under_gate [standing] found — UNDER block must be removed per spec Tier 2"
        )

    def test_fast_resume_allows_lc_delivery(self, src):
        """is_qualified must allow delivery when _fast_resume=True (first post-restart scan)."""
        assert "_fast_resume" in src, "_fast_resume flag not referenced in market_engine"
        # The is_qualified gate must include _fast_resume as an OR condition
        idx = src.find("is_qualified = (")
        assert idx != -1, "is_qualified gate not found"
        snippet = src[idx: idx + 500]
        assert "_fast_resume" in snippet, (
            "_fast_resume not in is_qualified gate — post-restart first scan blocks all lc delivery"
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
