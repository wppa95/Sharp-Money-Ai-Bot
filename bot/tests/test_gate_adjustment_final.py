"""
Regression tests for the authoritative prop delivery gate spec:

  TIER 2 — MLB + NFL
  ─────────────────────────────────────────────────────────
  ACTIONABLE:  S-tier + OVER only
  WATCHLIST:   A/B/C tiers (any direction); S-tier UNDER

  TIER 1 — All other active sports
  ─────────────────────────────────────────────────────────
  ACTIONABLE:  S or A tier, both OVER and UNDER
  WATCHLIST:   B or C tier

  Three paths covered: new-prop, line-change, standing.
  Non-strict sports (NBA, CS, WNBA, …) must NOT be affected by the strict gate.

Spec:
  MLB/NFL S  + OVER   → ALLOW (actionable)
  MLB/NFL S  + UNDER  → BLOCK (Tier 2: UNDER always watchlist)
  MLB/NFL A  + OVER   → BLOCK (Tier 2: A is watchlist)
  MLB/NFL A  + UNDER  → BLOCK (Tier 2: A is watchlist + UNDER blocked)
  MLB/NFL B/C         → BLOCK (watchlist only)
  Other sports S/A    → ALLOW (both directions)
  Other sports B/C    → BLOCK (watchlist only)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


# ── helpers ────────────────────────────────────────────────────────────────────

def _gate_allows(sport: str, tier: str, direction: str = "OVER") -> bool:
    """
    Replicate the Tier 2 MLB/NFL strict gate:
      1. Tier gate: strict sports must pass ud_mlb_alert_tiers (default {"S"})
      2. Direction gate: strict sports UNDER is always blocked (Tier 2 = OVER only)
    Returns True iff NOT blocked by either gate.
    """
    config = me.config
    sport_up = sport.upper()

    if sport_up in config.ud_strict_alert_sports:
        # Tier gate: must be in ud_mlb_alert_tiers
        if tier not in config.ud_mlb_alert_tiers:
            return False
        # Direction gate: Tier 2 = OVER only, UNDER is always watchlist
        if direction == "UNDER":
            return False

    return True


# ── 1. Config values ───────────────────────────────────────────────────────────

class TestConfigValues:
    """Verify the gate thresholds stored in config."""

    def test_strict_sports_are_mlb_and_nfl_only(self):
        """Only MLB and NFL are in ud_strict_alert_sports."""
        assert me.config.ud_strict_alert_sports == frozenset({"MLB", "NFL"})

    def test_mlb_alert_tiers_default_s_only(self):
        """Default UD_MLB_MIN_TIER=S → ud_mlb_alert_tiers contains S only."""
        tiers = me.config.ud_mlb_alert_tiers
        assert "S" in tiers
        assert "A" not in tiers, "A must NOT be in default ud_mlb_alert_tiers (Tier 2: S+OVER only)"
        assert "B" not in tiers
        assert "C" not in tiers

    def test_mlb_min_tier_default_s(self):
        """UD_MLB_MIN_TIER default must be 'S'."""
        assert me.config.UD_MLB_MIN_TIER == "S"

    def test_nba_not_in_strict_sports(self):
        assert "NBA" not in me.config.ud_strict_alert_sports

    def test_cs_not_in_strict_sports(self):
        assert "CS" not in me.config.ud_strict_alert_sports

    def test_wnba_not_in_strict_sports(self):
        assert "WNBA" not in me.config.ud_strict_alert_sports


# ── 2. MLB gate logic ──────────────────────────────────────────────────────────

class TestMLBGate:
    """MLB: S+OVER actionable; S+UNDER watchlist; A/B/C watchlist."""

    def test_mlb_s_over_allowed(self):
        """S-tier OVER is the ONLY actionable MLB pick."""
        assert _gate_allows("MLB", "S", "OVER") is True

    def test_mlb_s_under_blocked(self):
        """S-tier UNDER is watchlist — Tier 2 is OVER only."""
        assert _gate_allows("MLB", "S", "UNDER") is False

    def test_mlb_a_over_blocked(self):
        """A-tier MLB is watchlist only (not actionable)."""
        assert _gate_allows("MLB", "A", "OVER") is False

    def test_mlb_a_under_blocked(self):
        """A-tier + UNDER is doubly blocked (tier + direction)."""
        assert _gate_allows("MLB", "A", "UNDER") is False

    def test_mlb_b_over_blocked(self):
        assert _gate_allows("MLB", "B", "OVER") is False

    def test_mlb_b_under_blocked(self):
        assert _gate_allows("MLB", "B", "UNDER") is False

    def test_mlb_c_over_blocked(self):
        assert _gate_allows("MLB", "C", "OVER") is False


# ── 3. NFL gate logic ──────────────────────────────────────────────────────────

class TestNFLGate:
    """NFL: same S+OVER requirement as MLB."""

    def test_nfl_s_over_allowed(self):
        assert _gate_allows("NFL", "S", "OVER") is True

    def test_nfl_s_under_blocked(self):
        """S-tier UNDER blocked — Tier 2 is OVER only."""
        assert _gate_allows("NFL", "S", "UNDER") is False

    def test_nfl_a_blocked(self):
        assert _gate_allows("NFL", "A", "OVER") is False

    def test_nfl_b_blocked(self):
        assert _gate_allows("NFL", "B", "OVER") is False

    def test_nfl_c_blocked(self):
        assert _gate_allows("NFL", "C", "OVER") is False


# ── 4. Non-strict sports — Tier 1 ─────────────────────────────────────────────

class TestTier1SportsUnaffected:
    """NBA, CS, WNBA, etc. S/A OVER and UNDER must all be allowed."""

    def test_nba_s_over_allowed(self):
        assert _gate_allows("NBA", "S", "OVER") is True

    def test_nba_s_under_allowed(self):
        assert _gate_allows("NBA", "S", "UNDER") is True

    def test_nba_a_over_allowed(self):
        assert _gate_allows("NBA", "A", "OVER") is True

    def test_nba_a_under_allowed(self):
        assert _gate_allows("NBA", "A", "UNDER") is True

    def test_cs_s_over_allowed(self):
        assert _gate_allows("CS", "S", "OVER") is True

    def test_cs_a_under_allowed(self):
        assert _gate_allows("CS", "A", "UNDER") is True

    def test_wnba_s_under_allowed(self):
        assert _gate_allows("WNBA", "S", "UNDER") is True

    def test_lol_a_under_allowed(self):
        assert _gate_allows("LOL", "A", "UNDER") is True

    def test_tennis_a_over_allowed(self):
        assert _gate_allows("TENNIS", "A", "OVER") is True

    def test_soccer_b_not_affected_by_mlb_gate(self):
        """B-tier non-strict sport passes the MLB/NFL gate (still hits other gates)."""
        assert _gate_allows("SOCCER", "B", "OVER") is True


# ── 5. Source code: gates present in all three paths ──────────────────────────

class TestAllPathsHaveGate:
    """
    Verify the strict-tier AND direction gates appear in all delivery paths
    inside market_engine.py via source-code inspection.
    BQ gate removed per spec — decision_tier enforces quality.
    """

    @pytest.fixture(scope="class")
    def src(self) -> str:
        import inspect
        return inspect.getsource(me)

    def test_strict_tier_gate_new_prop(self, src):
        assert "sport_tier_gate [new]" in src

    def test_strict_tier_gate_standing(self, src):
        assert "sport_tier_gate [standing]" in src

    def test_lc_strict_tier_ok_var(self, src):
        assert "_lc_strict_tier_ok" in src

    def test_strict_tier_blocked_rejection_label(self, src):
        assert "strict_tier_blocked" in src

    def test_mlb_under_gate_new_path(self, src):
        """mlb_under_gate [new] must block MLB/NFL UNDER in new-prop path."""
        assert "mlb_under_gate [new]" in src, (
            "mlb_under_gate [new] missing — MLB/NFL UNDER not blocked in new-prop path"
        )

    def test_mlb_under_gate_standing_path(self, src):
        """mlb_under_gate [standing] must block MLB/NFL UNDER in standing path."""
        assert "mlb_under_gate [standing]" in src, (
            "mlb_under_gate [standing] missing — MLB/NFL UNDER not blocked in standing path"
        )

    def test_lc_path_under_blocked(self, src):
        """_lc_strict_tier_ok must contain UNDER direction check for Tier 2."""
        idx = src.find("_lc_strict_tier_ok")
        assert idx != -1, "_lc_strict_tier_ok not found"
        snippet = src[idx: idx + 500]
        assert "recommendation != \"UNDER\"" in snippet, (
            "_lc_strict_tier_ok does not block UNDER for Tier 2 — add direction check"
        )

    def test_bq_gate_removed_from_new_prop(self, src):
        assert "bq_gate [new]" not in src

    def test_bq_gate_removed_from_lc_path(self, src):
        assert "bq_gate [lc]" not in src

    def test_bq_gate_removed_from_standing_path(self, src):
        assert "bq_gate [standing]" not in src

    def test_np_95_dir_ok_present(self, src):
        """_np_95_dir_ok must gate MLB/NFL UNDER out of 95+ override path."""
        assert "_np_95_dir_ok" in src, "_np_95_dir_ok missing — Tier 2 UNDER may fire via 95+ override"

    def test_lc_95_dir_ok_present(self, src):
        assert "_lc_95_dir_ok" in src, "_lc_95_dir_ok missing — Tier 2 UNDER may fire via lc 95+ override"

    def test_sp_95_dir_ok_present(self, src):
        assert "_sp_95_dir_ok" in src, "_sp_95_dir_ok missing — Tier 2 UNDER may fire via standing 95+ override"

    def test_fast_resume_allows_lc_delivery(self, src):
        idx = src.find("is_qualified = (")
        assert idx != -1, "is_qualified gate not found"
        snippet = src[idx: idx + 500]
        assert "_fast_resume" in snippet


# ── 6. Deduplication still intact ─────────────────────────────────────────────

class TestDeduplicationIntact:
    """Confirm dedup helpers from bug #118 are still in place."""

    def test_dedup_helpers_importable(self):
        from market_engine import _is_prop_deduped, _record_prop_alerted
        assert callable(_is_prop_deduped)
        assert callable(_record_prop_alerted)

    def test_dedup_uses_dict_not_set(self):
        """Dedup dict stores (timestamp, line) so meaningful moves re-alert."""
        import inspect
        from market_engine import _prop_market_alerted
        # The dedup store must be a dict, not a set
        assert isinstance(_prop_market_alerted, dict), (
            "_prop_market_alerted must be a dict with (ts, line) values — not a set"
        )
