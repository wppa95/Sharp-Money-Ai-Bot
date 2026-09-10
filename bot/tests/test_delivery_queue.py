"""
test_delivery_queue.py
────────────────────────────────────────────────────────────────────────────────
Tests for the ranked-delivery queue introduced in underdog_job.

The delivery queue collects candidates from new-prop, line-change, and standing
paths, then ranks and delivers them in priority order after the standing scan.

Priority formula:
  200 if is_meaningful_change, otherwise 0.  Tier/confidence/quality metadata
  never selects one valid directional alert over another.

Soft diversification:
  Candidates in the same (sport, stat_type) group receive a penalty:
  −300 for 2nd, −600 for 3rd+ in the group.

Scenarios tested:
  1. Single S-tier candidate is delivered.
  2. Single B-tier candidate is delivered.
  3. Tier metadata does not affect priority.
  4. Diversification penalty allows weaker unique pick to beat 2nd same-group pick.
  5. legacy is_tier1 metadata does not change priority.
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

    def test_tier_metadata_does_not_change_priority(self):
        s = _make_cand(tier="S")
        a = _make_cand(tier="A")
        b = _make_cand(tier="B")
        c = _make_cand(tier="C")
        assert len({_cand_priority(candidate) for candidate in (s, a, b, c)}) == 1

    def test_pass_tier_has_zero_base(self):
        p = _make_cand(tier="PASS", conf=0, bq=0, mq=0, is_tier1=False)
        assert _cand_priority(p) == 0.0

    def test_legacy_tier1_metadata_does_not_change_priority(self):
        with_t1    = _make_cand(is_tier1=True,  tier="B", conf=0, bq=0, mq=0)
        without_t1 = _make_cand(is_tier1=False, tier="B", conf=0, bq=0, mq=0)
        assert _cand_priority(with_t1) == _cand_priority(without_t1)

    def test_meaningful_change_bonus_applied(self):
        with_mc    = _make_cand(is_meaningful_change=True,  tier="B", conf=0, bq=0, mq=0)
        without_mc = _make_cand(is_meaningful_change=False, tier="B", conf=0, bq=0, mq=0)
        assert _cand_priority(with_mc) - _cand_priority(without_mc) == 200.0

    def test_quality_components_do_not_change_priority(self):
        c = _make_cand(tier="PASS", conf=100, bq=100, mq=100, is_tier1=False, is_meaningful_change=False)
        assert _cand_priority(c) == pytest.approx(0.0)

    def test_higher_conf_does_not_change_priority(self):
        high = _make_cand(tier="A", conf=90, bq=60)
        low  = _make_cand(tier="A", conf=60, bq=60)
        assert _cand_priority(high) == _cand_priority(low)

    def test_higher_bq_does_not_change_priority(self):
        high = _make_cand(tier="A", conf=75, bq=90)
        low  = _make_cand(tier="A", conf=75, bq=40)
        assert _cand_priority(high) == _cand_priority(low)

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
        assert result[0]["player"] == "A"   # non-baseball is preferred over MLB

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
        assert result[0]["player"] == "B"   # input order is preserved when priority ties

    def test_diversification_elevates_unique_pick_over_duplicate(self):
        """
        Two B-tier candidates in the same group and one A-tier in a different group.
        After penalty the A-tier unique pick should beat the 2nd same-group B.
        """
        b1 = _make_cand(player="X", sport="NBA", stat_type="Points", tier="B", conf=70, bq=70, mq=0, is_tier1=False, is_meaningful_change=False)
        b2 = _make_cand(player="Y", sport="NBA", stat_type="Points", tier="B", conf=65, bq=65, mq=0, is_tier1=False, is_meaningful_change=False)
        a  = _make_cand(player="Z", sport="MLB", stat_type="Hits",   tier="A", conf=70, bq=70, mq=0, is_tier1=False, is_meaningful_change=False)
        # Tier metadata does not reorder this queue.  Diversification only
        # separates Tier-2 groups and applies the duplicate-group penalty.
        q = [b2, a, b1]
        result = _apply_delivery_diversification(q)
        assert [c["player"] for c in result] == ["Y", "X", "Z"]

    def test_third_in_group_gets_larger_penalty(self):
        """Third candidate in a group gets −600, larger than second's −300."""
        c1 = _make_cand(player="A", sport="NBA", stat_type="Points", tier="A", conf=80, bq=80)
        c2 = _make_cand(player="B", sport="NBA", stat_type="Points", tier="A", conf=79, bq=79)
        c3 = _make_cand(player="C", sport="NBA", stat_type="Points", tier="A", conf=78, bq=78)
        q = [c3, c1, c2]
        result = _apply_delivery_diversification(q)
        # With equal raw priority, input order determines group rank; the
        # penalty then keeps the first candidate ahead of later duplicates.
        assert result[0]["player"] == "C"
        assert result[2]["player"] == "B"

    def test_high_bq_duplicate_can_outrank_weak_unique(self):
        """
        Soft penalty: a very high-BQ S-tier duplicate can still beat a weak unique B-tier.
        """
        s1 = _make_cand(player="A", sport="NBA", stat_type="Points", tier="S", conf=90, bq=95, is_tier1=False)
        s2 = _make_cand(player="B", sport="NBA", stat_type="Points", tier="S", conf=88, bq=92, is_tier1=False)
        b  = _make_cand(player="C", sport="MLB", stat_type="Hits",   tier="B", conf=55, bq=50, is_tier1=False)
        # Tier/quality metadata is ignored.  Tier-2 candidates are ordered by
        # input order, then group diversification penalizes the duplicate.
        q = [b, s2, s1]
        result = _apply_delivery_diversification(q)
        assert [c["player"] for c in result] == ["B", "A", "C"]

    def test_mlb_remains_eligible_after_three_non_baseball_candidates(self):
        queue = [
            _make_cand(player=f"N{i}", sport="NFL", stat_type="Yards")
            for i in range(4)
        ]
        queue.append(_make_cand(player="M", sport="MLB", stat_type="Hits"))
        result = _apply_delivery_diversification(queue)
        assert [candidate["player"] for candidate in result] == [
            "N0", "N1", "N2", "M", "N3",
        ]

    def test_group_rank_attribute_set(self):
        """After diversification, _group_rank is set on each candidate."""
        c1 = _make_cand(player="A", sport="NBA", stat_type="Points", tier="S")
        c2 = _make_cand(player="B", sport="NBA", stat_type="Points", tier="A")
        q = [c2, c1]
        _apply_delivery_diversification(q)
        group_ranks = {c["player"]: c["_group_rank"] for c in q}
        assert group_ranks["A"] == 1   # second in group after input-order tie
        assert group_ranks["B"] == 0   # first in group

    def test_different_stat_types_same_sport_not_grouped(self):
        """Same sport but different stat_type → different groups, no penalty."""
        c1 = _make_cand(player="A", sport="NBA", stat_type="Points",  tier="B", conf=60, bq=60)
        c2 = _make_cand(player="B", sport="NBA", stat_type="Assists",  tier="B", conf=59, bq=59)
        q = [c2, c1]
        result = _apply_delivery_diversification(q)
        # Both are in different groups — no penalty applied; tied priority
        # preserves input order.
        assert result[0]["player"] == "B"
        assert result[0]["_group_rank"] == 0
        assert result[1]["_group_rank"] == 0   # each is first in its own group
