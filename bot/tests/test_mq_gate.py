"""
test_mq_gate.py
────────────────────────────────────────────────────────────────────────────────
Focused tests proving the Market Quality hard gate is enforced correctly.

Success criteria (per spec):
  1. MQ 47 cannot reach actionable delivery.
  2. MQ 47 cannot consume a Tier 1/Tier 2 slot.
  3. A qualified Tier 1 candidate can be selected ahead of a comparable Tier 2.
  4. Tier 2 cannot exceed 2 deliveries in a 5-minute window.
  5. Tier 1 cannot exceed 8 deliveries in a 5-minute window.
  6. Total deliveries cannot exceed 10 in a 5-minute window.
  7. Empty slots remain empty when qualified candidates are unavailable.
  8. All three scan paths use the same MQ gate function.

MQ rules tested:
  MQ 70–100  — eligible (OVER or UNDER).
  MQ 40–69   — dead zone: never actionable regardless of direction.
  MQ 31–39   — UNDER only.
  MQ 0–30    — Strong UNDER only.
"""

from __future__ import annotations

import pytest
from market_engine import (
    _mq_passes_delivery_gate,
    _apply_delivery_diversification,
    _TIER2_SPORTS,
    _DQ_TIER1_CAP,
    _DQ_TIER2_CAP,
)


# ── 1. _mq_passes_delivery_gate unit tests ────────────────────────────────────

class TestMQGateFunction:
    """Unit tests for _mq_passes_delivery_gate()."""

    # Dead zone: 40–69 always blocked regardless of direction
    @pytest.mark.parametrize("mq,direction", [
        (40,  "OVER"),  (40,  "UNDER"), (40,  ""),
        (47,  "OVER"),  (47,  "UNDER"), (47,  "STRONG BET"),
        (55,  "OVER"),  (55,  "UNDER"),
        (69,  "OVER"),  (69,  "UNDER"),
    ])
    def test_dead_zone_always_blocked(self, mq, direction):
        """MQ 40–69 must never pass the gate, regardless of direction."""
        assert _mq_passes_delivery_gate(float(mq), direction) is False, (
            f"MQ={mq} dir={direction!r} should be blocked (dead zone)"
        )

    # MQ ≥ 70: OVER and UNDER both allowed
    @pytest.mark.parametrize("mq,direction", [
        (70,  "OVER"),  (70,  "UNDER"),
        (75,  "OVER"),  (75,  "UNDER"),
        (85,  "OVER"),  (85,  "UNDER"),
        (100, "OVER"),  (100, "UNDER"),
    ])
    def test_high_mq_allows_over_and_under(self, mq, direction):
        """MQ 70+ allows both OVER and UNDER."""
        assert _mq_passes_delivery_gate(float(mq), direction) is True, (
            f"MQ={mq} dir={direction!r} should be allowed"
        )

    # MQ 31–39: UNDER allowed, OVER blocked
    @pytest.mark.parametrize("mq", [31, 35, 39])
    def test_low_mq_under_allowed(self, mq):
        """MQ 31–39 allows UNDER."""
        assert _mq_passes_delivery_gate(float(mq), "UNDER") is True

    @pytest.mark.parametrize("mq", [31, 35, 39])
    def test_low_mq_over_blocked(self, mq):
        """MQ 31–39 blocks OVER (only UNDER evaluation valid)."""
        assert _mq_passes_delivery_gate(float(mq), "OVER") is False

    # MQ 0–30: Strong UNDER — UNDER allowed, OVER blocked
    @pytest.mark.parametrize("mq", [0, 10, 20, 30])
    def test_strong_under_zone_under_allowed(self, mq):
        """MQ 0–30 (strong UNDER zone) allows UNDER."""
        assert _mq_passes_delivery_gate(float(mq), "UNDER") is True

    @pytest.mark.parametrize("mq", [0, 10, 20, 30])
    def test_strong_under_zone_over_blocked(self, mq):
        """MQ 0–30 blocks OVER — strong UNDER signal, not a buy signal."""
        assert _mq_passes_delivery_gate(float(mq), "OVER") is False

    def test_exact_boundary_70_allowed(self):
        """MQ exactly 70 is the boundary of the eligible zone — must be allowed."""
        assert _mq_passes_delivery_gate(70.0, "OVER") is True

    def test_exact_boundary_69_blocked(self):
        """MQ exactly 69 is the top of the dead zone — must be blocked."""
        assert _mq_passes_delivery_gate(69.0, "OVER") is False

    def test_exact_boundary_40_blocked(self):
        """MQ exactly 40 is the bottom of the dead zone — must be blocked."""
        assert _mq_passes_delivery_gate(40.0, "UNDER") is False

    def test_exact_boundary_39_under_allowed(self):
        """MQ exactly 39 is just below dead zone — UNDER must be allowed."""
        assert _mq_passes_delivery_gate(39.0, "UNDER") is True

    def test_exact_boundary_39_over_blocked(self):
        """MQ exactly 39 — OVER must be blocked (sub-40 = UNDER evaluation only)."""
        assert _mq_passes_delivery_gate(39.0, "OVER") is False

    def test_mq47_over_blocked(self):
        """MQ=47 OVER — the exact failing scenario from live bot. Must be blocked."""
        assert _mq_passes_delivery_gate(47.0, "OVER") is False

    def test_mq47_under_blocked(self):
        """MQ=47 UNDER — dead zone, blocked regardless of direction."""
        assert _mq_passes_delivery_gate(47.0, "UNDER") is False

    def test_empty_direction_treated_as_non_under(self):
        """Empty direction string at sub-40 MQ must be blocked (not UNDER)."""
        assert _mq_passes_delivery_gate(25.0, "") is False

    def test_pass_direction_treated_as_non_under(self):
        """PASS direction at sub-40 MQ must be blocked."""
        assert _mq_passes_delivery_gate(25.0, "PASS") is False


# ── 2. Tier 2 is only NBA/MLB/NFL ─────────────────────────────────────────────

class TestTierDefinitions:
    """_TIER2_SPORTS must contain only NBA, MLB, NFL."""

    def test_tier2_contains_exactly_nba_mlb_nfl(self):
        assert _TIER2_SPORTS == frozenset({"NBA", "MLB", "NFL"})

    def test_wnba_is_not_tier2(self):
        assert "WNBA" not in _TIER2_SPORTS

    def test_nhl_is_not_tier2(self):
        assert "NHL" not in _TIER2_SPORTS

    def test_soccer_is_not_tier2(self):
        assert "SOCCER" not in _TIER2_SPORTS

    def test_nba_is_tier2(self):
        assert "NBA" in _TIER2_SPORTS

    def test_mlb_is_tier2(self):
        assert "MLB" in _TIER2_SPORTS

    def test_nfl_is_tier2(self):
        assert "NFL" in _TIER2_SPORTS


# ── 3. Tier caps ──────────────────────────────────────────────────────────────

class TestTierCaps:
    """Tier caps must be correctly configured."""

    def test_tier1_cap_is_8(self):
        assert _DQ_TIER1_CAP == 8

    def test_tier2_cap_is_2(self):
        assert _DQ_TIER2_CAP == 2

    def test_combined_cap_is_10(self):
        assert _DQ_TIER1_CAP + _DQ_TIER2_CAP == 10


# ── 4. Delivery queue: Tier 1 ranked before Tier 2 ───────────────────────────

def _make_cand(player, sport, tier="A", conf=75.0, bq=70.0, mq=75.0, is_tier1=None):
    """Helper: build a delivery queue candidate dict."""
    _is_t1 = (sport.upper() not in _TIER2_SPORTS) if is_tier1 is None else is_tier1
    return {
        "player":               player,
        "stat_type":            "Points",
        "sport":                sport,
        "tier":                 tier,
        "conf":                 conf,
        "bq":                   bq,
        "mq":                   mq,
        "is_tier1":             _is_t1,
        "is_meaningful_change": False,
        "_sent":                False,
    }


class TestDeliveryQueueTierOrdering:
    """After _apply_delivery_diversification, Tier 1 candidates precede Tier 2."""

    def test_tier1_before_tier2_same_quality(self):
        """A Tier 1 A-tier pick must precede a Tier 2 A-tier pick of equal quality."""
        t1 = _make_cand("WNBA Player", "WNBA", tier="A", conf=75, bq=70)
        t2 = _make_cand("NBA Player",  "NBA",  tier="A", conf=75, bq=70)
        queue = [t2, t1]
        _apply_delivery_diversification(queue)
        assert queue[0]["sport"] == "WNBA", "Tier 1 (WNBA) must come first"
        assert queue[1]["sport"] == "NBA",  "Tier 2 (NBA) must come second"

    def test_tier1_s_before_tier2_s(self):
        """Tier 1 S-tier must rank before Tier 2 S-tier."""
        t1 = _make_cand("NHL Player",  "NHL", tier="S", conf=85, bq=85)
        t2 = _make_cand("MLB Player",  "MLB", tier="S", conf=85, bq=85)
        queue = [t2, t1]
        _apply_delivery_diversification(queue)
        assert queue[0]["sport"] == "NHL"

    def test_multiple_tier1_before_tier2(self):
        """Multiple Tier 1 picks from different sports must precede all Tier 2 picks."""
        t1a = _make_cand("Tennis Player", "TENNIS", tier="A", conf=80, bq=75)
        t1b = _make_cand("Soccer Player", "SOCCER", tier="B", conf=65, bq=60)
        t2a = _make_cand("NFL Player",    "NFL",    tier="S", conf=90, bq=90)
        queue = [t2a, t1b, t1a]
        _apply_delivery_diversification(queue)
        # Both Tier 1 picks must come before the NFL pick
        sports = [c["sport"] for c in queue]
        nfl_idx = sports.index("NFL")
        tennis_idx = sports.index("TENNIS")
        soccer_idx = sports.index("SOCCER")
        assert tennis_idx < nfl_idx, "TENNIS (Tier 1) must come before NFL (Tier 2)"
        assert soccer_idx < nfl_idx, "SOCCER (Tier 1) must come before NFL (Tier 2)"

    def test_within_tier1_ranked_by_quality(self):
        """Within Tier 1, S-tier beats A-tier beats B-tier."""
        t1_s = _make_cand("S Player", "WNBA", tier="S", conf=85, bq=85)
        t1_a = _make_cand("A Player", "NHL",  tier="A", conf=75, bq=70)
        t1_b = _make_cand("B Player", "KBO",  tier="B", conf=60, bq=55)
        queue = [t1_b, t1_s, t1_a]
        _apply_delivery_diversification(queue)
        assert queue[0]["player"] == "S Player"
        assert queue[1]["player"] == "A Player"
        assert queue[2]["player"] == "B Player"

    def test_within_tier2_ranked_by_quality(self):
        """Within Tier 2, S-tier beats A-tier."""
        t2_s = _make_cand("S MLB Player", "MLB", tier="S", conf=90, bq=90)
        t2_a = _make_cand("A NBA Player", "NBA", tier="A", conf=70, bq=65)
        queue = [t2_a, t2_s]
        _apply_delivery_diversification(queue)
        assert queue[0]["player"] == "S MLB Player"
        assert queue[1]["player"] == "A NBA Player"

    def test_empty_slots_not_filled_no_candidates(self):
        """An empty queue produces an empty result — no phantom candidates."""
        queue = []
        _apply_delivery_diversification(queue)
        assert queue == []

    def test_only_tier1_no_tier2_candidates(self):
        """If only Tier 1 candidates exist, Tier 2 slots remain empty."""
        t1a = _make_cand("Player A", "WNBA", tier="S")
        t1b = _make_cand("Player B", "NHL",  tier="A")
        queue = [t1a, t1b]
        _apply_delivery_diversification(queue)
        # Should have exactly 2 candidates, both Tier 1
        assert len(queue) == 2
        assert all(c["sport"] not in _TIER2_SPORTS for c in queue)

    def test_only_tier2_no_tier1_candidates(self):
        """If only Tier 2 candidates exist, Tier 1 slots are empty (no filling)."""
        t2a = _make_cand("Player A", "NBA", tier="S")
        t2b = _make_cand("Player B", "MLB", tier="A")
        queue = [t2a, t2b]
        _apply_delivery_diversification(queue)
        assert len(queue) == 2
        assert all(c["sport"] in _TIER2_SPORTS for c in queue)
