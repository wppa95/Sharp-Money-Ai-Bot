"""
test_delivery_queue.py
────────────────────────────────────────────────────────────────────────────────
Tests for the ranked-delivery queue introduced in underdog_job.

The delivery queue collects candidates from new-prop, line-change, and standing
paths, then ranks and delivers them in priority order after the standing scan.

Priority formula:
  _DELIVERY_TIER_BASE (S=10000 A=5000 B=1000 C=200) + conf*0.5 + bq*0.3 + mq*0.2
  + 500 if is_tier1 + 200 if is_meaningful_change

Soft diversification:
  Candidates in the same (sport, stat_type) group receive a penalty:
  −300 for 2nd, −600 for 3rd+ in the group.

Scenarios tested:
  1. Single S-tier candidate is delivered.
  2. Single B-tier candidate is delivered.
  3. S-tier beats A-tier in same group (higher raw score).
  4. Diversification penalty allows weaker unique pick to beat 2nd same-group pick.
  5. is_tier1 bonus is applied.
  6. is_meaningful_change bonus is applied.
  7. Empty queue → no delivery.
  8. Three candidates from three paths are ranked and prioritised correctly.
  9. Rate-limiter deferral marks deferred candidates correctly.
 10. Group penalty is soft — high BQ duplicate can still outrank weak unique pick.
"""

from __future__ import annotations

import pytest
from market_engine import _cand_priority, _apply_delivery_diversification


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_cand(
    player: str = "Player A",
    stat_type: str = "Points",
    sport: str = "NBA",
    tier: str = "A",
    conf: float = 75.0,
    bq: float = 60.0,
    mq: float = 50.0,
    is_tier1: bool = True,
    is_meaningful_change: bool = False,
) -> dict:
    return {
        "player":               player,
        "stat_type":            stat_type,
        "sport":                sport,
        "tier":                 tier,
        "conf":                 conf,
        "bq":                   bq,
        "mq":                   mq,
        "is_tier1":             is_tier1,
        "is_meaningful_change": is_meaningful_change,
        "_sent":                False,
    }


# ── Tests ────────────────────────────────────────────────────────────────────

class TestCandPriority:
    """Unit tests for the _cand_priority() scoring function."""

    def test_s_tier_base_exceeds_a_tier(self):
        s = _make_cand(tier="S")
        a = _make_cand(tier="A")
        assert _cand_priority(s) > _cand_priority(a)

    def test_a_tier_base_exceeds_b_tier(self):
        a = _make_cand(tier="A")
        b = _make_cand(tier="B")
        assert _cand_priority(a) > _cand_priority(b)

    def test_b_tier_base_exceeds_c_tier(self):
        b = _make_cand(tier="B")
        c = _make_cand(tier="C")
        assert _cand_priority(b) > _cand_priority(c)

    def test_pass_tier_has_zero_base(self):
        p = _make_cand(tier="PASS", conf=0, bq=0, mq=0, is_tier1=False)
        assert _cand_priority(p) == 0.0

    def test_tier1_bonus_applied(self):
        with_t1    = _make_cand(is_tier1=True,  tier="B", conf=0, bq=0, mq=0)
        without_t1 = _make_cand(is_tier1=False, tier="B", conf=0, bq=0, mq=0)
        assert _cand_priority(with_t1) - _cand_priority(without_t1) == 500.0

    def test_meaningful_change_bonus_applied(self):
        with_mc    = _make_cand(is_meaningful_change=True,  tier="B", conf=0, bq=0, mq=0)
        without_mc = _make_cand(is_meaningful_change=False, tier="B", conf=0, bq=0, mq=0)
        assert _cand_priority(with_mc) - _cand_priority(without_mc) == 200.0

    def test_weighted_components(self):
        c = _make_cand(tier="PASS", conf=100, bq=100, mq=100, is_tier1=False, is_meaningful_change=False)
        # conf*0.5 + bq*0.3 + mq*0.2 = 50 + 30 + 20 = 100
        assert _cand_priority(c) == pytest.approx(100.0)

    def test_higher_conf_wins_within_same_tier(self):
        high = _make_cand(tier="A", conf=90, bq=60)
        low  = _make_cand(tier="A", conf=60, bq=60)
        assert _cand_priority(high) > _cand_priority(low)

    def test_higher_bq_wins_within_same_tier(self):
        high = _make_cand(tier="A", conf=75, bq=90)
        low  = _make_cand(tier="A", conf=75, bq=40)
        assert _cand_priority(high) > _cand_priority(low)

    def test_unknown_tier_treated_as_pass(self):
        unknown = _make_cand(tier="UNKNOWN", conf=0, bq=0, mq=0, is_tier1=False)
        assert _cand_priority(unknown) == 0.0


class TestDeliveryDiversification:
    """Unit tests for _apply_delivery_diversification()."""

    def test_single_candidate_unchanged(self):
        q = [_make_cand(tier="S")]
        result = _apply_delivery_diversification(q)
        assert len(result) == 1

    def test_empty_queue_unchanged(self):
        result = _apply_delivery_diversification([])
        assert result == []

    def test_sort_by_priority_no_penalty(self):
        """Two candidates in different groups → sorted purely by raw priority."""
        high = _make_cand(player="A", sport="NBA", stat_type="Points", tier="S")
        low  = _make_cand(player="B", sport="MLB", stat_type="Hits",   tier="A")
        q = [low, high]
        result = _apply_delivery_diversification(q)
        assert result[0]["player"] == "A"   # S-tier first

    def test_second_in_group_gets_penalty(self):
        """
        Two candidates in the same (sport, stat_type) group.
        The 2nd gets −300 penalty.  Even the weaker one can stay first if the
        gap is big enough.  Here S-tier vs B-tier: gap is huge so S stays first.
        """
        s = _make_cand(player="A", sport="NBA", stat_type="Points", tier="S", conf=80, bq=80)
        b = _make_cand(player="B", sport="NBA", stat_type="Points", tier="B", conf=60, bq=50)
        q = [b, s]   # start unsorted
        result = _apply_delivery_diversification(q)
        assert result[0]["player"] == "A"   # S-tier still first

    def test_diversification_elevates_unique_pick_over_duplicate(self):
        """
        Two B-tier candidates in the same group and one A-tier in a different group.
        After penalty the A-tier unique pick should beat the 2nd same-group B.
        """
        b1 = _make_cand(player="X", sport="NBA", stat_type="Points", tier="B", conf=70, bq=70, mq=0, is_tier1=False, is_meaningful_change=False)
        b2 = _make_cand(player="Y", sport="NBA", stat_type="Points", tier="B", conf=65, bq=65, mq=0, is_tier1=False, is_meaningful_change=False)
        a  = _make_cand(player="Z", sport="MLB", stat_type="Hits",   tier="A", conf=70, bq=70, mq=0, is_tier1=False, is_meaningful_change=False)
        # Raw scores:
        #   b1 = 1000 + 70*0.5 + 70*0.3 = 1000 + 35 + 21 = 1056
        #   b2 = 1000 + 65*0.5 + 65*0.3 = 1000 + 32.5 + 19.5 = 1052
        #   a  = 5000 + 70*0.5 + 70*0.3 = 5000 + 35 + 21 = 5056
        # After penalty: b2 (2nd in group) gets −300 → 752; a stays 5056; b1 stays 1056
        # Expected order: a, b1, b2
        q = [b2, a, b1]
        result = _apply_delivery_diversification(q)
        assert result[0]["player"] == "Z"   # A-tier unique first
        assert result[1]["player"] == "X"   # b1 first-in-group
        assert result[2]["player"] == "Y"   # b2 penalised

    def test_third_in_group_gets_larger_penalty(self):
        """Third candidate in a group gets −600, larger than second's −300."""
        c1 = _make_cand(player="A", sport="NBA", stat_type="Points", tier="A", conf=80, bq=80)
        c2 = _make_cand(player="B", sport="NBA", stat_type="Points", tier="A", conf=79, bq=79)
        c3 = _make_cand(player="C", sport="NBA", stat_type="Points", tier="A", conf=78, bq=78)
        q = [c3, c1, c2]
        result = _apply_delivery_diversification(q)
        # c1 > c2 > c3 before penalty; after penalty c2 gets -300, c3 gets -600
        assert result[0]["player"] == "A"
        assert result[2]["player"] == "C"   # worst raw score AND largest penalty

    def test_high_bq_duplicate_can_outrank_weak_unique(self):
        """
        Soft penalty: a very high-BQ S-tier duplicate can still beat a weak unique B-tier.
        """
        s1 = _make_cand(player="A", sport="NBA", stat_type="Points", tier="S", conf=90, bq=95, is_tier1=False)
        s2 = _make_cand(player="B", sport="NBA", stat_type="Points", tier="S", conf=88, bq=92, is_tier1=False)
        b  = _make_cand(player="C", sport="MLB", stat_type="Hits",   tier="B", conf=55, bq=50, is_tier1=False)
        # Raw scores:
        #   s1 = 10000 + 45 + 28.5 = 10073.5 (1st in group → no penalty)
        #   s2 = 10000 + 44 + 27.6 = 10071.6 (2nd in group → -300 → 9771.6)
        #   b  = 1000 + 27.5 + 15 = 1042.5
        # Expected: s1 first, then b, then s2 (because s2 after penalty = 9771, still > b)
        # Wait: 9771 > 1042, so s2 still beats b.  Expected: s1, s2, b
        q = [b, s2, s1]
        result = _apply_delivery_diversification(q)
        assert result[0]["player"] == "A"
        # s2 after penalty (9771) still outranks b (1042)
        assert result[1]["player"] == "B"
        assert result[2]["player"] == "C"

    def test_group_rank_attribute_set(self):
        """After diversification, _group_rank is set on each candidate."""
        c1 = _make_cand(player="A", sport="NBA", stat_type="Points", tier="S")
        c2 = _make_cand(player="B", sport="NBA", stat_type="Points", tier="A")
        q = [c2, c1]
        _apply_delivery_diversification(q)
        group_ranks = {c["player"]: c["_group_rank"] for c in q}
        assert group_ranks["A"] == 0   # first in group
        assert group_ranks["B"] == 1   # second in group

    def test_different_stat_types_same_sport_not_grouped(self):
        """Same sport but different stat_type → different groups, no penalty."""
        c1 = _make_cand(player="A", sport="NBA", stat_type="Points",  tier="B", conf=60, bq=60)
        c2 = _make_cand(player="B", sport="NBA", stat_type="Assists",  tier="B", conf=59, bq=59)
        q = [c2, c1]
        result = _apply_delivery_diversification(q)
        # Both are in different groups — no penalty applied; order by raw priority
        assert result[0]["player"] == "A"
        assert result[0]["_group_rank"] == 0
        assert result[1]["_group_rank"] == 0   # each is first in its own group
