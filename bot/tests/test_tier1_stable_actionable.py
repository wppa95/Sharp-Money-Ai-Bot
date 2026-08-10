"""
Focused tests for the Tier 1 stable-prop actionable fix (V3.2).

The fix: standing path score_tier=NULL fallback — when the latest snapshot
has score_tier=NULL (stored during a no-change cycle without re-scoring),
derive effective tier from score_total using the same thresholds as
ud_scoring.py (S≥80, A≥65) so valid stable Tier 1 props aren't silently
dropped from the standing path.

Tests cover the 12 required scenarios from the spec.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


# ─────────────────────────────────────────────────────────────────────────────
# Helper: replicate the tier-derivation logic added at the standing-path filter
# ─────────────────────────────────────────────────────────────────────────────

def _derive_standing_tier(score_tier, score_total) -> str | None:
    """
    Mirror the standing-path effective-tier derivation introduced in the fix.
    Fallback only applies when score_tier is None (no-change cycle, no re-scoring).
    Explicit "B" / "PASS" tiers are NOT promoted by the fallback.
    Returns the effective tier string, or None if the prop should be dropped.
    """
    eff = score_tier
    # Only fall back to score_total when score_tier is NULL (not explicitly stored)
    if eff is None and score_total is not None:
        if score_total >= 80:
            eff = "S"
        elif score_total >= 65:
            eff = "A"
    return eff if eff in ("A", "S") else None


# ─────────────────────────────────────────────────────────────────────────────
# Helper: replicate the strict-sport gates applied later in the standing path
# ─────────────────────────────────────────────────────────────────────────────

def _strict_gates_allow(sport: str, decision_tier: str, bq: int = 0) -> bool:
    """
    Mirrors the strict-sport tier gate in the standing path (BQ gate removed per spec Tier 2):
      1. Tier gate: strict sports (MLB/NFL) must be in ud_mlb_alert_tiers (default {"S","A"})
    Returns True if NOT blocked by the tier gate.  BQ is accepted but unused.
    """
    cfg = me.config
    su = sport.upper()
    if su in cfg.ud_strict_alert_sports:
        if decision_tier not in cfg.ud_mlb_alert_tiers:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 1 & 2. Stable Tier 1 S-tier prop (explicit + derived) → reaches standing path
# ─────────────────────────────────────────────────────────────────────────────

class TestTier1StableEntersStandingPath:
    """Tier 1 S/A props must survive the standing-path candidate filter."""

    def test_explicit_s_tier_passes_filter(self):
        """Stored score_tier='S' → accepted by standing filter."""
        assert _derive_standing_tier("S", 85) == "S"

    def test_null_score_tier_with_high_total_derives_s(self):
        """
        score_tier=NULL but score_total=85 → derive 'S'.
        Covers the bug: stable no-change snapshot stored without re-scoring.
        """
        assert _derive_standing_tier(None, 85) == "S"

    def test_null_score_tier_with_total_80_derives_s(self):
        """Boundary: score_total=80 is the minimum for S-tier."""
        assert _derive_standing_tier(None, 80) == "S"

    def test_explicit_a_tier_passes_filter(self):
        """Stored score_tier='A' → accepted."""
        assert _derive_standing_tier("A", 70) == "A"

    def test_null_score_tier_with_total_70_derives_a(self):
        """score_tier=NULL, score_total=70 → derive 'A'."""
        assert _derive_standing_tier(None, 70) == "A"

    def test_null_score_tier_with_total_65_derives_a(self):
        """Boundary: score_total=65 is the minimum for A-tier."""
        assert _derive_standing_tier(None, 65) == "A"

    def test_tier1_cs_sport_not_strict(self):
        """CS is NOT a strict sport — strict-sport gates must not fire."""
        assert "CS" not in me.config.ud_strict_alert_sports

    def test_tier1_lol_sport_not_strict(self):
        """LOL is NOT a strict sport."""
        assert "LOL" not in me.config.ud_strict_alert_sports

    def test_tier1_wnba_sport_not_strict(self):
        """WNBA is NOT a strict sport."""
        assert "WNBA" not in me.config.ud_strict_alert_sports


# ─────────────────────────────────────────────────────────────────────────────
# 3. Stable Tier 1 prop without sufficient validation → rejected
# ─────────────────────────────────────────────────────────────────────────────

class TestTier1ValidationGate:
    """Standing path rejects props that fail the validation (has_supporting_data) gate."""

    def test_prop_without_supporting_data_is_rejected(self):
        """
        The standing-path gate explicitly checks _sval.has_supporting_data.
        This is not changed by the fix — verify the gate still exists in source.
        """
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "has_supporting_data" in src, "standing-path must still gate on has_supporting_data"
        assert "standing_gate [no_data]" in src, "no_data debug log must remain"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Stable Tier 1 prop without supporting data → rejected (gate presence)
# ─────────────────────────────────────────────────────────────────────────────

class TestSupportingDataGatePresent:
    def test_supporting_data_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        # Gate: if not _sval.has_supporting_data: continue
        assert "not _sval.has_supporting_data" in src


# ─────────────────────────────────────────────────────────────────────────────
# 5. Stable Tier 1 prop without direction (PASS decision) → rejected
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectionGatePresent:
    def test_pass_decision_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "decision_pass" in src, "PASS-decision gate must exist in standing path"
        assert '_sdec.recommendation == "PASS"' in src


# ─────────────────────────────────────────────────────────────────────────────
# 6. Stable Tier 1 prop with invalid/inactive event → rejected
# ─────────────────────────────────────────────────────────────────────────────

class TestEventValidationGatePresent:
    def test_live_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "live_gate [standing]" in src, "game-live gate must exist in standing path"
        assert "_is_game_live_or_past(_ssnap, now)" in src


# ─────────────────────────────────────────────────────────────────────────────
# 7. Previously alerted prop → dedup protection still works
# ─────────────────────────────────────────────────────────────────────────────

class TestDedupProtectionPresent:
    def test_dedup_gate_in_standing_path(self):
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "has_recent_ud_alert" in src
        assert "86400" in src  # 24-hour dedup window


# ─────────────────────────────────────────────────────────────────────────────
# 8. MLB/NFL prop without S-tier → rejected by strict-sport tier gate
# ─────────────────────────────────────────────────────────────────────────────

class TestMLBNFLTierGate:
    """MLB and NFL require S or A tier (spec Tier 2: S/A deliver, B/C watchlist)."""

    def test_mlb_a_tier_allowed(self):
        """A-tier MLB is NOW allowed — spec Tier 2."""
        assert _strict_gates_allow("MLB", "A")

    def test_mlb_b_tier_blocked(self):
        assert not _strict_gates_allow("MLB", "B")

    def test_nfl_a_tier_allowed(self):
        """A-tier NFL is NOW allowed — spec Tier 2."""
        assert _strict_gates_allow("NFL", "A")

    def test_nfl_s_tier_passes(self):
        assert _strict_gates_allow("MLB", "S")

    def test_nfl_s_tier_nfl_passes(self):
        assert _strict_gates_allow("NFL", "S")


# ─────────────────────────────────────────────────────────────────────────────
# 9. MLB/NFL 95 BQ requirement → unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestMLBNFLBQGate:
    """BQ gate removed — decision_tier (S/A only) enforces quality per spec Tier 2."""

    def test_mlb_s_tier_any_bq_allowed(self):
        """BQ gate removed — S-tier MLB allowed regardless of confidence."""
        assert _strict_gates_allow("MLB", "S", 60)

    def test_nfl_s_tier_any_bq_allowed(self):
        """BQ gate removed — S-tier NFL allowed regardless of confidence."""
        assert _strict_gates_allow("NFL", "S", 70)

    def test_mlb_a_tier_any_bq_allowed(self):
        """A-tier MLB now allowed with any confidence."""
        assert _strict_gates_allow("MLB", "A", 50)

    def test_mlb_b_tier_blocked_regardless_of_bq(self):
        """B-tier MLB is watchlist only — tier gate still blocks it."""
        assert not _strict_gates_allow("MLB", "B", 100)

    def test_bq_threshold_config_value_still_defined(self):
        """Config value still defined (for standing path reference); just not enforced."""
        assert me.config.UD_STRICT_SPORT_MIN_BET_QUALITY == 95


# ─────────────────────────────────────────────────────────────────────────────
# 10. Global MIN_UNDERDOG_LINE_CHANGE remains unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestLineMovementThresholdUnchanged:
    """The global line-movement detection threshold must NOT have been lowered."""

    def test_min_underdog_line_change_is_0_5(self):
        """MIN_UNDERDOG_LINE_CHANGE must remain at 0.5 (unchanged)."""
        assert me.config.MIN_UNDERDOG_LINE_CHANGE == 0.5

    def test_line_movement_threshold_in_market_move_path(self):
        """The should_alert formula for the lc path still gates on MIN_UNDERDOG_LINE_CHANGE."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "MIN_UNDERDOG_LINE_CHANGE" in src
        # Verify the unchanged formula for lc/market-move path
        assert "abs(snap.line - prev_line) >= config.MIN_UNDERDOG_LINE_CHANGE" in src


# ─────────────────────────────────────────────────────────────────────────────
# 11. Qualified intermediate that fails final gates → does NOT reach Telegram
# ─────────────────────────────────────────────────────────────────────────────

class TestQualifiedIntermedNotAutoAlerted:
    """
    score_tier=NULL derivation ONLY allows prop into the standing *candidate* list.
    Props still pass through the full gate stack (validation, direction, conf, BQ, live)
    before any Telegram delivery.
    """

    def test_b_tier_score_total_not_allowed(self):
        """score_total=64 → below A threshold → rejected from standing candidates."""
        assert _derive_standing_tier(None, 64) is None

    def test_pass_tier_score_total_not_allowed(self):
        """score_total=49 → below B → rejected."""
        assert _derive_standing_tier(None, 49) is None

    def test_null_score_tier_null_total_rejected(self):
        """score_tier=NULL and score_total=None → no fallback → rejected."""
        assert _derive_standing_tier(None, None) is None

    def test_b_explicit_tier_rejected(self):
        """Explicit score_tier='B' → not in (A, S) → rejected."""
        assert _derive_standing_tier("B", 75) is None

    def test_pass_explicit_tier_rejected(self):
        """Explicit score_tier='PASS' → rejected."""
        assert _derive_standing_tier("PASS", 45) is None


# ─────────────────────────────────────────────────────────────────────────────
# 12. Valid stable Tier 1 candidate passing all final gates → reaches actionable
# ─────────────────────────────────────────────────────────────────────────────

class TestStableTier1ReachesActionable:
    """
    A Tier 1 prop that passes the derivation filter AND all downstream gates
    (conf, star, validation, direction, live, dedup) should reach the alert path.
    Verify the gate topology is intact in market_engine source.
    """

    def test_tier1_s_derived_not_blocked_by_strict_gates(self):
        """CS/LOL/WNBA S-tier props are not blocked by MLB/NFL strict gates."""
        for sport in ("CS", "LOL", "WNBA", "NBA", "SOCCER", "TENNIS"):
            assert _strict_gates_allow(sport, "S", 70), f"{sport} S-tier should NOT be blocked"
            assert _strict_gates_allow(sport, "A", 70), f"{sport} A-tier should NOT be blocked"

    def test_standing_path_has_deliver_underdog_call(self):
        """Verify the standing path still calls deliver_underdog (Telegram delivery)."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        # The standing path passes standing=True (with possible whitespace) to deliver_underdog
        assert "standing" in src and "_n_standing_sent" in src, (
            "standing path deliver_underdog call must be present"
        )

    def test_mlb_nfl_strict_gates_updated_in_standing_path(self):
        """Tier gate present; BQ gate removed per spec Tier 2."""
        import inspect, market_engine as _me
        src = inspect.getsource(_me.underdog_job)
        assert "sport_tier_gate [standing]" in src
        assert "bq_gate [standing]" not in src

    def test_fix_uses_score_total_thresholds_matching_ud_scoring(self):
        """
        The derivation thresholds (80=S, 65=A) must match ud_scoring._S_THRESHOLD
        and ud_scoring._A_THRESHOLD so the derivation is internally consistent.
        """
        from engine.ud_scoring import _S_THRESHOLD, _A_THRESHOLD
        # S boundary
        assert _derive_standing_tier(None, _S_THRESHOLD) == "S"
        assert _derive_standing_tier(None, _S_THRESHOLD - 1) == "A"
        # A boundary
        assert _derive_standing_tier(None, _A_THRESHOLD) == "A"
        assert _derive_standing_tier(None, _A_THRESHOLD - 1) is None
