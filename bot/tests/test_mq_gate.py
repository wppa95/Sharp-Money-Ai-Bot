"""
test_mq_gate.py
────────────────────────────────────────────────────────────────────────────────
Tests for the canonical _tier_delivery_gate and _is_tier2_sport functions.

Covers spec test items 1–39:
  Sport classification (items 1–20)
  Tier 1 delivery rules (items 21–28)
  Tier 2 delivery rules (items 29–34)
  Priority and rate-limit structure (items 35–39)
"""

from __future__ import annotations
import pytest
from market_engine import (
    _tier_delivery_gate,
    _is_tier2_sport,
    _TIER2_SPORTS,
    _cand_priority,
    _apply_delivery_diversification,
)


# ── Sport classification (spec items 1–20) ─────────────────────────────────────

class TestSportClassification:
    """_is_tier2_sport and _TIER2_SPORTS classify every sport correctly."""

    # Spec items 1–3: Tier 2 sports
    def test_nba_is_tier2(self):        assert _is_tier2_sport("NBA")    is True
    def test_mlb_is_tier2(self):        assert _is_tier2_sport("MLB")    is True
    def test_nfl_is_tier2(self):        assert _is_tier2_sport("NFL")    is True

    # Spec items 4–19: Tier 1 sports
    def test_wnba_is_tier1(self):       assert _is_tier2_sport("WNBA")        is False
    def test_nhl_is_tier1(self):        assert _is_tier2_sport("NHL")         is False
    def test_tennis_is_tier1(self):     assert _is_tier2_sport("TENNIS")      is False
    def test_soccer_is_tier1(self):     assert _is_tier2_sport("SOCCER")      is False
    def test_fifa_is_tier1(self):       assert _is_tier2_sport("FIFA")        is False
    def test_cs2_is_tier1(self):        assert _is_tier2_sport("CS2")         is False
    def test_dota2_is_tier1(self):      assert _is_tier2_sport("DOTA2")       is False
    def test_dota_is_tier1(self):       assert _is_tier2_sport("DOTA")        is False
    def test_lol_is_tier1(self):        assert _is_tier2_sport("LOL")         is False
    def test_val_is_tier1(self):        assert _is_tier2_sport("VAL")         is False
    def test_valorant_is_tier1(self):   assert _is_tier2_sport("VALORANT")    is False
    def test_mma_is_tier1(self):        assert _is_tier2_sport("MMA")         is False
    def test_badminton_is_tier1(self):  assert _is_tier2_sport("BADMINTON")   is False
    def test_table_tennis_is_tier1(self): assert _is_tier2_sport("TABLE TENNIS") is False
    def test_racing_is_tier1(self):     assert _is_tier2_sport("RACING")      is False
    def test_cfb_is_tier1(self):        assert _is_tier2_sport("CFB")         is False
    def test_cfl_is_tier1(self):        assert _is_tier2_sport("CFL")         is False
    def test_kbo_is_tier1(self):        assert _is_tier2_sport("KBO")         is False
    def test_npb_is_tier1(self):        assert _is_tier2_sport("NPB")         is False

    # Spec item 20: any supported sport other than NBA/MLB/NFL → Tier 1
    def test_unknown_sport_is_tier1(self):    assert _is_tier2_sport("UNKNOWN")    is False
    def test_esports_is_tier1(self):          assert _is_tier2_sport("ESPORTS")    is False
    def test_cricket_is_tier1(self):          assert _is_tier2_sport("CRICKET")    is False
    def test_empty_sport_is_tier1(self):      assert _is_tier2_sport("")           is False
    def test_none_sport_is_tier1(self):       assert _is_tier2_sport(None)         is False

    # Case-insensitivity — sport strings come from various APIs with mixed case.
    def test_nba_lowercase_is_tier2(self):    assert _is_tier2_sport("nba")    is True
    def test_mlb_lowercase_is_tier2(self):    assert _is_tier2_sport("mlb")    is True
    def test_nfl_lowercase_is_tier2(self):    assert _is_tier2_sport("nfl")    is True
    def test_wnba_lowercase_is_tier1(self):   assert _is_tier2_sport("wnba")   is False
    def test_nhl_lowercase_is_tier1(self):    assert _is_tier2_sport("nhl")    is False

    def test_tier2_set_contains_exactly_three_sports(self):
        assert _TIER2_SPORTS == frozenset({"NBA", "MLB", "NFL"})


# ── Tier 1 delivery rules (spec items 21–28) ──────────────────────────────────

class TestTier1DeliveryRules:
    """
    Tier 1 = every sport except NBA/MLB/NFL.
    Requires valid OVER/UNDER direction.
    BQ and MQ are NOT standalone blockers.
    """

    # Spec item 21: Tier 1 + valid OVER + strong analysis → allowed
    def test_tier1_over_strong_analysis_allowed(self):
        assert _tier_delivery_gate("WNBA",  "OVER",  bq_score=68, mq_score=42) is True
        assert _tier_delivery_gate("TENNIS","OVER",  bq_score=61, mq_score=38) is True
        assert _tier_delivery_gate("NHL",   "OVER",  bq_score=72, mq_score=55) is True

    # Spec item 22: Tier 1 + valid UNDER + strong analysis → allowed
    def test_tier1_under_strong_analysis_allowed(self):
        assert _tier_delivery_gate("TENNIS","UNDER", bq_score=61, mq_score=38) is True
        assert _tier_delivery_gate("DOTA2", "UNDER", bq_score=50, mq_score=31) is True
        assert _tier_delivery_gate("CS2",   "UNDER", bq_score=72, mq_score=55) is True

    # Spec item 23: Tier 1 BQ below 75 does NOT automatically block
    def test_tier1_bq_below_75_does_not_block(self):
        for bq in [0, 10, 30, 50, 60, 70, 74]:
            assert _tier_delivery_gate("NHL", "OVER", bq_score=bq, mq_score=80) is True, (
                f"Tier 1 BQ={bq} should not block"
            )

    # Spec item 24: Tier 1 MQ below 75 does NOT automatically block
    def test_tier1_mq_below_75_does_not_block(self):
        for mq in [0, 10, 30, 50, 60, 70, 74]:
            assert _tier_delivery_gate("WNBA", "OVER", bq_score=80, mq_score=mq) is True, (
                f"Tier 1 MQ={mq} should not block"
            )

    # Spec item 25: Tier 1 MQ below 40 does NOT automatically block
    def test_tier1_mq_below_40_does_not_block(self):
        for mq in [0, 5, 10, 20, 30, 39]:
            assert _tier_delivery_gate("TENNIS", "OVER",  bq_score=80, mq_score=mq) is True
            assert _tier_delivery_gate("TENNIS", "UNDER", bq_score=80, mq_score=mq) is True

    # Spec item 26: Tier 1 MQ 40–69 does NOT automatically block
    def test_tier1_mq_dead_zone_does_not_block(self):
        for mq in [40, 47, 55, 60, 69]:
            assert _tier_delivery_gate("CS2",  "OVER",  bq_score=80, mq_score=mq) is True, (
                f"Tier 1 MQ={mq} (dead zone) should NOT block"
            )
            assert _tier_delivery_gate("CS2",  "UNDER", bq_score=80, mq_score=mq) is True

    # Spec item 27: Tier 1 with no direction → blocked
    def test_tier1_no_direction_blocked(self):
        assert _tier_delivery_gate("WNBA", "PASS",  bq_score=90, mq_score=90) is False
        assert _tier_delivery_gate("NHL",  "",       bq_score=90, mq_score=90) is False
        assert _tier_delivery_gate("NHL",  "STRONG BET", bq_score=90, mq_score=90) is False

    # Spec item 28: Tier 1 without actionable analysis → blocked (no direction)
    # The gate checks direction as the proxy for "actionable" (PASS = no actionable direction).
    def test_tier1_pass_direction_blocked(self):
        assert _tier_delivery_gate("MMA", "PASS", bq_score=100, mq_score=100) is False

    # Spec examples from the document
    def test_spec_example_wnba_over_bq68_mq42(self):
        """WNBA | OVER | BQ:68 | MQ:42 | Strong → ✅ ALLOW"""
        assert _tier_delivery_gate("WNBA", "OVER", bq_score=68, mq_score=42) is True

    def test_spec_example_tennis_under_bq61_mq38(self):
        """Tennis | UNDER | BQ:61 | MQ:38 | Strong → ✅ ALLOW"""
        assert _tier_delivery_gate("TENNIS", "UNDER", bq_score=61, mq_score=38) is True

    def test_spec_example_cs2_over_bq72_mq55(self):
        """CS2 | OVER | BQ:72 | MQ:55 | Strong → ✅ ALLOW"""
        assert _tier_delivery_gate("CS2", "OVER", bq_score=72, mq_score=55) is True

    def test_spec_example_dota2_under_bq50_mq31(self):
        """Dota 2 | UNDER | BQ:50 | MQ:31 | Strong → ✅ ALLOW"""
        assert _tier_delivery_gate("DOTA2", "UNDER", bq_score=50, mq_score=31) is True

    def test_spec_example_nhl_no_direction_blocked(self):
        """NHL | No direction | BQ:90 | MQ:90 → ❌ BLOCK"""
        assert _tier_delivery_gate("NHL", "", bq_score=90, mq_score=90) is False


# ── Tier 2 delivery rules (spec items 29–34) ──────────────────────────────────

class TestTier2DeliveryRules:
    """
    Tier 2 = ONLY NBA, MLB, NFL.
    Requires BQ ≥ 75 AND MQ ≥ 75 AND valid OVER/UNDER direction.
    """

    # Spec item 29: NBA BQ 75 + MQ 75 + direction → allowed
    def test_nba_bq75_mq75_over_allowed(self):
        assert _tier_delivery_gate("NBA", "OVER", bq_score=75, mq_score=75) is True

    def test_nba_bq75_mq75_under_allowed(self):
        assert _tier_delivery_gate("NBA", "UNDER", bq_score=75, mq_score=75) is True

    # Spec item 30: MLB BQ 75 + MQ 75 + direction → allowed
    def test_mlb_bq75_mq75_over_allowed(self):
        assert _tier_delivery_gate("MLB", "OVER", bq_score=75, mq_score=75) is True

    def test_mlb_bq80_mq80_over_allowed(self):
        """MLB | OVER | BQ:80 | MQ:80 → ✅ ALLOW"""
        assert _tier_delivery_gate("MLB", "OVER", bq_score=80, mq_score=80) is True

    # Spec item 31: NFL BQ 75 + MQ 75 + direction → allowed
    def test_nfl_bq75_mq75_over_allowed(self):
        assert _tier_delivery_gate("NFL", "OVER", bq_score=75, mq_score=75) is True

    # Spec item 32: BQ 74 → blocked
    def test_bq74_blocked(self):
        """NFL | OVER | BQ:74 | MQ:90 → ❌ BLOCK"""
        assert _tier_delivery_gate("NFL", "OVER",  bq_score=74, mq_score=90) is False
        assert _tier_delivery_gate("NBA", "UNDER", bq_score=74, mq_score=90) is False
        assert _tier_delivery_gate("MLB", "OVER",  bq_score=74, mq_score=90) is False

    # Spec item 33: MQ 74 → blocked
    def test_mq74_blocked(self):
        """MLB | UNDER | BQ:90 | MQ:74 → ❌ BLOCK"""
        assert _tier_delivery_gate("MLB", "UNDER", bq_score=90, mq_score=74) is False
        assert _tier_delivery_gate("NBA", "OVER",  bq_score=90, mq_score=74) is False
        assert _tier_delivery_gate("NFL", "OVER",  bq_score=95, mq_score=70) is False

    # Spec item 34: Missing direction → blocked
    def test_tier2_no_direction_blocked(self):
        assert _tier_delivery_gate("NBA", "",      bq_score=90, mq_score=90) is False
        assert _tier_delivery_gate("MLB", "PASS",  bq_score=90, mq_score=90) is False
        assert _tier_delivery_gate("NFL", "STRONG BET", bq_score=90, mq_score=90) is False

    # Spec examples from the document
    def test_spec_example_mlb_under_mq74_blocked(self):
        """MLB | UNDER | BQ:90 | MQ:74 → ❌ BLOCK — MQ below 75"""
        assert _tier_delivery_gate("MLB", "UNDER", bq_score=90, mq_score=74) is False

    def test_spec_example_nfl_over_bq74_blocked(self):
        """NFL | OVER | BQ:74 | MQ:90 → ❌ BLOCK — BQ below 75"""
        assert _tier_delivery_gate("NFL", "OVER", bq_score=74, mq_score=90) is False

    def test_spec_example_nba_under_bq75_mq75_allowed(self):
        """NBA | UNDER | BQ:75 | MQ:75 → ✅ ALLOW"""
        assert _tier_delivery_gate("NBA", "UNDER", bq_score=75, mq_score=75) is True

    def test_spec_example_nfl_over_bq95_mq70_blocked(self):
        """NFL | OVER | BQ:95 | MQ:70 → ❌ BLOCK"""
        assert _tier_delivery_gate("NFL", "OVER", bq_score=95, mq_score=70) is False

    def test_tier2_bq_and_mq_both_required(self):
        """Both BQ AND MQ must be ≥ 75; one alone is insufficient."""
        # BQ good, MQ bad → blocked
        assert _tier_delivery_gate("NBA", "OVER", bq_score=80, mq_score=74) is False
        # MQ good, BQ bad → blocked
        assert _tier_delivery_gate("NBA", "OVER", bq_score=74, mq_score=80) is False
        # Both good → allowed
        assert _tier_delivery_gate("NBA", "OVER", bq_score=80, mq_score=80) is True

    def test_tier2_boundary_values(self):
        """Boundary: exactly 75 on both passes; 74 on either fails."""
        assert _tier_delivery_gate("MLB", "OVER", bq_score=75, mq_score=75) is True
        assert _tier_delivery_gate("MLB", "OVER", bq_score=74.9, mq_score=75) is False
        assert _tier_delivery_gate("MLB", "OVER", bq_score=75, mq_score=74.9) is False

    def test_tier2_high_scores_allowed(self):
        for bq in [75, 80, 90, 100]:
            for mq in [75, 80, 90, 100]:
                assert _tier_delivery_gate("NFL", "OVER", bq_score=bq, mq_score=mq) is True


# ── Priority and rate-limit structure (spec items 35–39) ──────────────────────

class TestPriorityAndRateLimit:
    """
    Spec items 35–39:
    35. Tier 1 ranks ahead of Tier 2.
    36. 10 total Telegram alerts / 5 minutes remains.
    37. No 8/2 allocation remains.
    38. No Tier 1 slot counter remains.
    39. No Tier 2 slot counter remains.
    """

    # Spec item 35: Tier 1 ranks ahead of Tier 2
    def test_tier1_ranks_higher_than_tier2_same_score(self):
        """is_tier1=True adds 500 priority points; Tier 1 should outscore Tier 2."""
        t1 = {"tier": "A", "conf": 70, "bq": 70, "mq": 70, "is_tier1": True, "is_meaningful_change": False}
        t2 = {"tier": "A", "conf": 70, "bq": 70, "mq": 70, "is_tier1": False, "is_meaningful_change": False}
        assert _cand_priority(t1) > _cand_priority(t2)

    def test_tier1_outranks_tier2_after_diversification(self):
        """After _apply_delivery_diversification, Tier 1 candidates precede Tier 2."""
        t1a = {"sport": "WNBA", "stat_type": "Points", "tier": "A", "conf": 70, "bq": 70, "mq": 70,
               "is_tier1": True, "is_meaningful_change": False}
        t1b = {"sport": "NHL",  "stat_type": "Goals",  "tier": "A", "conf": 70, "bq": 70, "mq": 70,
               "is_tier1": True, "is_meaningful_change": False}
        t2a = {"sport": "NBA",  "stat_type": "Points", "tier": "A", "conf": 70, "bq": 70, "mq": 70,
               "is_tier1": False, "is_meaningful_change": False}
        t2b = {"sport": "MLB",  "stat_type": "Hits",   "tier": "A", "conf": 70, "bq": 70, "mq": 70,
               "is_tier1": False, "is_meaningful_change": False}
        queue = [t2a, t2b, t1a, t1b]
        result = _apply_delivery_diversification(queue)
        # First two in result must be Tier 1.
        assert result[0].get("is_tier1") is True
        assert result[1].get("is_tier1") is True

    # Spec item 37: No 8/2 allocation remains
    def test_no_dq_tier1_cap_constant(self):
        """_DQ_TIER1_CAP must not exist in market_engine."""
        import market_engine
        assert not hasattr(market_engine, "_DQ_TIER1_CAP"), (
            "_DQ_TIER1_CAP was removed but still exists"
        )

    def test_no_dq_tier2_cap_constant(self):
        """_DQ_TIER2_CAP must not exist in market_engine."""
        import market_engine
        assert not hasattr(market_engine, "_DQ_TIER2_CAP"), (
            "_DQ_TIER2_CAP was removed but still exists"
        )

    # Spec items 38–39: No tier-specific slot counters
    def test_no_tier1_slot_counter(self):
        """TG_TIER1_MAX_PER_WINDOW env var must not drive a hard cap constant."""
        import market_engine
        assert not hasattr(market_engine, "_DQ_TIER1_CAP")

    def test_no_tier2_slot_counter(self):
        """TG_TIER2_MAX_PER_WINDOW env var must not drive a hard cap constant."""
        import market_engine
        assert not hasattr(market_engine, "_DQ_TIER2_CAP")

    def test_old_mq_gate_function_removed(self):
        """_mq_passes_delivery_gate (old dead-zone gate) must not exist."""
        import market_engine
        assert not hasattr(market_engine, "_mq_passes_delivery_gate"), (
            "_mq_passes_delivery_gate still exists — should have been replaced by _tier_delivery_gate"
        )

    def test_canonical_gate_function_exists(self):
        """_tier_delivery_gate must exist and be callable."""
        from market_engine import _tier_delivery_gate
        assert callable(_tier_delivery_gate)

    def test_canonical_helper_exists(self):
        """_is_tier2_sport must exist and be callable."""
        from market_engine import _is_tier2_sport
        assert callable(_is_tier2_sport)

    # Spec item 36: 10 total / 5 minutes — rate limiter still active
    def test_rate_limiter_module_exists(self):
        """TelegramRateLimiter provides the total-window cap of 10/5min."""
        from engine import telegram_rate_limiter
        assert hasattr(telegram_rate_limiter, "TelegramRateLimiter")

    # Priority function correctness
    def test_cand_priority_tier1_bonus_500(self):
        """Tier 1 receives a 500-point bonus in priority scoring."""
        base = {"tier": "B", "conf": 60, "bq": 60, "mq": 60, "is_tier1": False, "is_meaningful_change": False}
        t1   = dict(base, is_tier1=True)
        assert _cand_priority(t1) - _cand_priority(base) == pytest.approx(500.0)

    def test_cand_priority_higher_tier_ranks_first(self):
        """S-tier ranks above A-tier regardless of Tier 1/2."""
        s = {"tier": "S", "conf": 80, "bq": 80, "mq": 80, "is_tier1": False, "is_meaningful_change": False}
        a = {"tier": "A", "conf": 80, "bq": 80, "mq": 80, "is_tier1": False, "is_meaningful_change": False}
        assert _cand_priority(s) > _cand_priority(a)

    def test_diversification_returns_list(self):
        """_apply_delivery_diversification always returns a list."""
        assert isinstance(_apply_delivery_diversification([]), list)

    def test_diversification_preserves_all_candidates(self):
        """No candidate is dropped by diversification — only reordered."""
        q = [
            {"sport": "WNBA", "stat_type": "Points", "tier": "S", "conf": 90, "bq": 90, "mq": 90,
             "is_tier1": True, "is_meaningful_change": False},
            {"sport": "NBA", "stat_type": "Pts",    "tier": "S", "conf": 90, "bq": 90, "mq": 90,
             "is_tier1": False, "is_meaningful_change": False},
            {"sport": "NHL", "stat_type": "Goals",  "tier": "A", "conf": 70, "bq": 70, "mq": 70,
             "is_tier1": True, "is_meaningful_change": False},
        ]
        result = _apply_delivery_diversification(q)
        assert len(result) == 3


# ── Gate function edge cases ───────────────────────────────────────────────────

class TestGateEdgeCases:
    """Edge cases and boundary values for _tier_delivery_gate."""

    def test_direction_case_insensitive(self):
        """OVER/UNDER matching is case-insensitive."""
        assert _tier_delivery_gate("WNBA", "over",  bq_score=50, mq_score=50) is True
        assert _tier_delivery_gate("WNBA", "under", bq_score=50, mq_score=50) is True
        assert _tier_delivery_gate("NBA",  "OVER",  bq_score=80, mq_score=80) is True

    def test_zero_bq_zero_mq_tier1_allowed(self):
        """Tier 1 with BQ=0 and MQ=0: direction is the only gate."""
        assert _tier_delivery_gate("CS2", "OVER",  bq_score=0, mq_score=0) is True
        assert _tier_delivery_gate("CS2", "UNDER", bq_score=0, mq_score=0) is True

    def test_zero_bq_zero_mq_tier2_blocked(self):
        """Tier 2 with BQ=0 and MQ=0: both fail the ≥75 gate."""
        assert _tier_delivery_gate("NBA", "OVER",  bq_score=0, mq_score=0) is False
        assert _tier_delivery_gate("NBA", "UNDER", bq_score=0, mq_score=0) is False

    def test_tier1_maximum_mq_bq_allowed(self):
        """Tier 1 with BQ=100 and MQ=100 passes."""
        assert _tier_delivery_gate("WNBA", "OVER",  bq_score=100, mq_score=100) is True

    def test_tier2_maximum_mq_bq_allowed(self):
        """Tier 2 with BQ=100 and MQ=100 passes."""
        assert _tier_delivery_gate("NBA", "OVER",  bq_score=100, mq_score=100) is True

    def test_gate_is_pure_deterministic(self):
        """Gate must return the same result for the same inputs every call."""
        for _ in range(10):
            assert _tier_delivery_gate("WNBA", "OVER",  bq_score=50, mq_score=50) is True
            assert _tier_delivery_gate("NBA",  "OVER",  bq_score=74, mq_score=80) is False

    def test_all_tier2_sports_blocked_bq74(self):
        """BQ=74 must block all Tier 2 sports."""
        for sport in ["NBA", "MLB", "NFL"]:
            assert _tier_delivery_gate(sport, "OVER",  bq_score=74, mq_score=80) is False
            assert _tier_delivery_gate(sport, "UNDER", bq_score=74, mq_score=80) is False

    def test_all_tier2_sports_blocked_mq74(self):
        """MQ=74 must block all Tier 2 sports."""
        for sport in ["NBA", "MLB", "NFL"]:
            assert _tier_delivery_gate(sport, "OVER",  bq_score=80, mq_score=74) is False

    def test_tier1_sports_never_blocked_by_mq_alone(self):
        """No MQ value alone can block a Tier 1 prop with a valid direction."""
        for mq in [0, 10, 30, 40, 47, 55, 60, 69, 74, 80, 100]:
            assert _tier_delivery_gate("WNBA", "OVER",  bq_score=0, mq_score=mq) is True

    def test_tier1_sports_never_blocked_by_bq_alone(self):
        """No BQ value alone can block a Tier 1 prop with a valid direction."""
        for bq in [0, 10, 30, 40, 47, 55, 60, 69, 74, 80, 100]:
            assert _tier_delivery_gate("NHL", "UNDER", bq_score=bq, mq_score=0) is True
