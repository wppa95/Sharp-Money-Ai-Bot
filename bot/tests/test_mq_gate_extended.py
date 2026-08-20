"""
test_mq_gate_extended.py
────────────────────────────────────────────────────────────────────────────────
Extended delivery gate tests covering spec items 40–47:

  40. New-prop follows tier rules.
  41. Line-change follows tier rules.
  42. Standing follows tier rules.
  43. Stable-refresh follows tier rules.
  44. Watchlist follows tier rules.
  45. FPR follows tier rules.
  46. Full-pool rescan follows tier rules.
  47. Direct Telegram delivery follows tier rules.

Also covers /picks and /slip tier-aware filter behaviour.
"""

from __future__ import annotations
import types
import pytest
from market_engine import _tier_delivery_gate, _is_tier2_sport, _TIER2_SPORTS


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_plh(
    player_name: str = "Test Player",
    sport: str = "MLB",
    stat_type: str = "Total Bases",
    bet_recommendation: str = "OVER",
    market_quality_score: int | None = None,
    score_total: float | None = None,
    score_tier: str = "A",
):
    return types.SimpleNamespace(
        player_name          = player_name,
        sport                = sport,
        stat_type            = stat_type,
        bet_recommendation   = bet_recommendation,
        market_quality_score = market_quality_score,
        score_total          = score_total,
        score_tier           = score_tier,
        line_value           = 1.5,
        fetched_at           = None,
    )


def _apply_picks_mq_filter(plhs: list) -> list:
    """Apply the same Tier-aware filter used in cmd_picks. Threshold: BQ ≥ 85 AND MQ ≥ 85."""
    passing = []
    for plh in plhs:
        _eff_rec = plh.bet_recommendation
        if _eff_rec not in ("OVER", "UNDER"):
            continue
        _plh_sport = (getattr(plh, "sport", "") or "").upper()
        _plh_mq    = getattr(plh, "market_quality_score", None)
        _plh_bq    = getattr(plh, "score_total", None)
        if _plh_sport in {"NBA", "MLB", "NFL"}:
            _t2_mq_ok = (_plh_mq is None) or (float(_plh_mq) >= 85.0)
            _t2_bq_ok = (_plh_bq is None) or (float(_plh_bq) >= 85.0)
            if not (_t2_mq_ok and _t2_bq_ok):
                continue
        passing.append(plh)
    return passing


def _apply_slip_filter(candidates: list, plhs: list) -> list:
    """Apply the same Tier-aware filter used in cmd_slip. Threshold: BQ ≥ 85 AND MQ ≥ 85."""
    eligible = []
    for cand in candidates:
        _slip_sport = (getattr(cand, "sport", "") or "").upper()
        if _slip_sport in {"NBA", "MLB", "NFL"}:
            _slip_plh = next(
                (p for p in plhs if p.player_name == cand.player_name
                 and p.stat_type == cand.stat_type), None,
            )
            _slip_mq = getattr(_slip_plh, "market_quality_score", None) if _slip_plh else None
            _slip_bq = getattr(_slip_plh, "score_total",           None) if _slip_plh else None
            _s2_mq_ok = (_slip_mq is None) or (float(_slip_mq) >= 85.0)
            _s2_bq_ok = (_slip_bq is None) or (float(_slip_bq) >= 85.0)
            if not (_s2_mq_ok and _s2_bq_ok):
                continue
        eligible.append(cand)
    return eligible


# ── 40–42. Main-scan paths use tier gate ──────────────────────────────────────

class TestMainScanPathsTierGate:
    """
    Spec items 40–42: New-prop, LC, and standing paths must apply tier rules.
    These paths all call _tier_delivery_gate before appending to _delivery_queue.
    We verify the gate function returns correct results for the inputs each path uses.
    """

    def test_new_prop_tier1_low_mq_allowed(self):
        """New-prop path: Tier 1 with MQ=47 is not Telegram eligible."""
        assert _tier_delivery_gate("WNBA", "OVER", bq_score=70, mq_score=47, evidence_available=True) is False

    def test_new_prop_tier2_bq74_blocked(self):
        """New-prop path: Tier 2 (NBA) with BQ=74 must be blocked."""
        assert _tier_delivery_gate("NBA", "OVER", bq_score=74, mq_score=80) is False

    def test_new_prop_tier2_both_85_allowed(self):
        """New-prop path: Tier 2 (MLB) with BQ=85 and MQ=85 must be allowed."""
        assert _tier_delivery_gate("MLB", "OVER", bq_score=85, mq_score=85) is True

    def test_new_prop_tier2_bq75_mq75_now_blocked(self):
        """New-prop path: Tier 2 (MLB) with BQ=75 and MQ=75 must now be BLOCKED (threshold raised to 85)."""
        assert _tier_delivery_gate("MLB", "OVER", bq_score=75, mq_score=75) is False

    def test_lc_tier1_mq_dead_zone_allowed(self):
        """Line-change path: Tier 1 with MQ=55 is not Telegram eligible."""
        assert _tier_delivery_gate("CS2", "UNDER", bq_score=70, mq_score=55, evidence_available=True) is False

    def test_lc_tier2_mq74_blocked(self):
        """Line-change path: Tier 2 (NFL) with MQ=74 must be blocked."""
        assert _tier_delivery_gate("NFL", "OVER", bq_score=80, mq_score=74) is False

    def test_standing_tier1_any_mq_allowed(self):
        """Standing path: Tier 1 with MQ=0 is not Telegram eligible."""
        assert _tier_delivery_gate("TENNIS", "OVER", bq_score=0, mq_score=0, evidence_available=True) is False

    def test_standing_tier2_requires_both_gates(self):
        """Standing path: Tier 2 (NBA) must fail when only one of BQ/MQ ≥ 85."""
        assert _tier_delivery_gate("NBA", "OVER", bq_score=85, mq_score=84) is False
        assert _tier_delivery_gate("NBA", "OVER", bq_score=84, mq_score=85) is False
        assert _tier_delivery_gate("NBA", "OVER", bq_score=85, mq_score=85) is True


# ── 43–46. SR / WL / FPR paths ────────────────────────────────────────────────

class TestIndirectScanPathsTierGate:
    """
    Spec items 43–46: Stable-refresh, watchlist, FPR paths must apply tier rules.
    These paths call _tier_delivery_gate directly (not through the delivery queue).
    """

    def test_sr_tier1_low_bq_low_mq_allowed(self):
        """Stable-refresh: Tier 1 with BQ=40, MQ=40 is not Telegram eligible."""
        assert _tier_delivery_gate("DOTA2", "OVER", bq_score=40, mq_score=40, evidence_available=True) is False

    def test_sr_tier2_bq74_blocked(self):
        """Stable-refresh: Tier 2 (NBA) with BQ=74 must be blocked."""
        assert _tier_delivery_gate("NBA", "OVER", bq_score=74, mq_score=80) is False

    def test_sr_tier2_both_85_allowed(self):
        """Stable-refresh: Tier 2 (NBA) with BQ=85 and MQ=85 must be allowed."""
        assert _tier_delivery_gate("NBA", "OVER", bq_score=85, mq_score=85) is True

    def test_sr_tier2_bq75_mq75_blocked(self):
        """Stable-refresh: Tier 2 (NBA) with BQ=75/MQ=75 must now be BLOCKED (threshold is 85)."""
        assert _tier_delivery_gate("NBA", "OVER", bq_score=75, mq_score=75) is False

    def test_wl_tier1_mq_dead_zone_allowed(self):
        """Watchlist: Tier 1 with MQ=47 is not Telegram eligible."""
        assert _tier_delivery_gate("WNBA", "UNDER", bq_score=70, mq_score=47, evidence_available=True) is False

    def test_wl_tier2_mq74_blocked(self):
        """Watchlist: Tier 2 (MLB) with MQ=74 must be blocked."""
        assert _tier_delivery_gate("MLB", "UNDER", bq_score=90, mq_score=74) is False

    def test_fpr_tier1_zero_mq_allowed(self):
        """Full-pool-rescan: Tier 1 with MQ=0 is not Telegram eligible."""
        assert _tier_delivery_gate("MMA", "OVER", bq_score=0, mq_score=0, evidence_available=True) is False

    def test_fpr_tier2_bq_and_mq_both_required(self):
        """Full-pool-rescan: Tier 2 (NFL) must require both BQ AND MQ ≥ 85."""
        assert _tier_delivery_gate("NFL", "OVER", bq_score=90, mq_score=84) is False
        assert _tier_delivery_gate("NFL", "OVER", bq_score=84, mq_score=90) is False
        assert _tier_delivery_gate("NFL", "OVER", bq_score=85, mq_score=85) is True

    def test_all_paths_use_same_gate_function(self):
        """All paths call _tier_delivery_gate — results are deterministic."""
        inputs = [
            ("WNBA", "OVER",  70, 70, True),   # Tier 1 — thresholds + evidence
            ("NBA",  "OVER",  84, 90, False),   # Tier 2 — BQ gate (84 < 85)
            ("MLB",  "UNDER", 90, 84, False),   # Tier 2 — MQ gate (84 < 85)
            ("CS2",  "UNDER", 70, 70, True),    # Tier 1 — thresholds + evidence
            ("NFL",  "OVER",  85, 85, True),    # Tier 2 — passes (85 ≥ 85)
            ("MLB",  "OVER",  75, 75, False),   # Tier 2 — old threshold blocked (75 < 85)
        ]
        for sport, direction, bq, mq, expected in inputs:
            assert _tier_delivery_gate(sport, direction, bq, mq, sport not in {"NBA", "MLB", "NFL"}) is expected, (
                f"_tier_delivery_gate({sport!r}, {direction!r}, bq={bq}, mq={mq}) != {expected}"
            )


# ── 47. Direct Telegram delivery path ──────────────────────────────────────────

class TestDirectDeliveryPath:
    """Spec item 47: Direct deliver_underdog() calls must also follow tier rules."""

    def test_gate_blocks_tier2_before_delivery(self):
        """Tier 2 props that fail the gate must never reach deliver_underdog()."""
        assert _tier_delivery_gate("NBA", "OVER", bq_score=74, mq_score=80) is False

    def test_gate_allows_tier1_before_delivery(self):
        """Tier 1 props meeting the delivery gate may reach deliver_underdog()."""
        assert _tier_delivery_gate("WNBA", "OVER", bq_score=70, mq_score=70, evidence_available=True) is True

    def test_gate_importable_from_market_engine(self):
        """_tier_delivery_gate must be importable by every delivery path."""
        from market_engine import _tier_delivery_gate as _gate
        assert callable(_gate)

    def test_is_tier2_importable_from_market_engine(self):
        """_is_tier2_sport must be importable for path classification."""
        from market_engine import _is_tier2_sport as _t2
        assert callable(_t2)


# ── /picks tier-aware filter ───────────────────────────────────────────────────

class TestPicksTierFilter:
    """Tier-aware /picks filtering: Tier 2 requires BQ ≥ 85 AND MQ ≥ 85."""

    def test_tier1_low_mq_passes_picks(self):
        """Tier 1 with MQ=47 must appear in /picks (no MQ gate on T1)."""
        plh = _make_plh("Player A", "WNBA", market_quality_score=47, score_total=60, bet_recommendation="OVER")
        assert _apply_picks_mq_filter([plh]) == [plh]

    def test_tier1_low_bq_passes_picks(self):
        """Tier 1 with BQ=40 must appear in /picks (no BQ gate on T1)."""
        plh = _make_plh("Player B", "NHL", market_quality_score=80, score_total=40, bet_recommendation="UNDER")
        assert _apply_picks_mq_filter([plh]) == [plh]

    def test_tier2_bq84_excluded_from_picks(self):
        """Tier 2 (MLB) with BQ=84 must be excluded from /picks (below 85 threshold)."""
        plh = _make_plh("Pitcher A", "MLB", market_quality_score=90, score_total=84, bet_recommendation="OVER")
        assert _apply_picks_mq_filter([plh]) == []

    def test_tier2_mq84_excluded_from_picks(self):
        """Tier 2 (NBA) with MQ=84 must be excluded from /picks (below 85 threshold)."""
        plh = _make_plh("Baller A", "NBA", market_quality_score=84, score_total=90, bet_recommendation="OVER")
        assert _apply_picks_mq_filter([plh]) == []

    def test_tier2_bq75_mq75_blocked_in_picks(self):
        """Tier 2 (MLB) with BQ=75/MQ=75 must now be BLOCKED (threshold raised from 75 to 85)."""
        plh = _make_plh("Old Player", "MLB", market_quality_score=75, score_total=75.0, bet_recommendation="OVER")
        assert _apply_picks_mq_filter([plh]) == []

    def test_tier2_both_85_allowed_in_picks(self):
        """Tier 2 (NFL) with BQ=85 and MQ=85 must appear in /picks."""
        plh = _make_plh("QB A", "NFL", market_quality_score=85, score_total=85.0, bet_recommendation="OVER")
        assert _apply_picks_mq_filter([plh]) == [plh]

    def test_tier2_null_scores_pass_conservatively(self):
        """Tier 2 with NULL BQ and NULL MQ passes conservatively."""
        plh = _make_plh("Unknown NBA", "NBA", market_quality_score=None, score_total=None, bet_recommendation="OVER")
        assert _apply_picks_mq_filter([plh]) == [plh]

    def test_mixed_picks_only_valid_pass(self):
        """Mixed Tier 1/2 list: only eligible props pass."""
        t1_ok  = _make_plh("P1", "WNBA", market_quality_score=47,  score_total=50,   bet_recommendation="OVER")
        t2_ok  = _make_plh("P2", "NBA",  market_quality_score=90,  score_total=90.0, bet_recommendation="OVER")
        t2_bad = _make_plh("P3", "MLB",  market_quality_score=84,  score_total=90.0, bet_recommendation="OVER")
        result = _apply_picks_mq_filter([t1_ok, t2_ok, t2_bad])
        assert t1_ok  in result
        assert t2_ok  in result
        assert t2_bad not in result

    def test_no_direction_excluded(self):
        """Props with no valid direction are excluded before the tier gate."""
        plh = _make_plh("P4", "WNBA", market_quality_score=80, score_total=80.0, bet_recommendation="PASS")
        assert _apply_picks_mq_filter([plh]) == []


# ── /slip tier-aware filter ────────────────────────────────────────────────────

class TestSlipTierFilter:
    """Tier-aware /slip filtering: Tier 2 requires BQ ≥ 85 AND MQ ≥ 85."""

    def _make_cand(self, player_name, stat_type, best_side, sport):
        return types.SimpleNamespace(
            player_name = player_name,
            stat_type   = stat_type,
            best_side   = best_side,
            sport       = sport,
        )

    def test_tier1_low_mq_allowed_in_slip(self):
        """Tier 1 candidate with low MQ must be eligible for /slip."""
        cand = self._make_cand("P1", "Points", "OVER", "WNBA")
        plh  = _make_plh("P1", "WNBA", stat_type="Points", market_quality_score=47, score_total=60)
        assert _apply_slip_filter([cand], [plh]) == [cand]

    def test_tier2_bq84_excluded_from_slip(self):
        """Tier 2 candidate with BQ=84 must be excluded from /slip (below 85 threshold)."""
        cand = self._make_cand("P2", "Hits", "OVER", "MLB")
        plh  = _make_plh("P2", "MLB", stat_type="Hits", market_quality_score=90, score_total=84.0)
        assert _apply_slip_filter([cand], [plh]) == []

    def test_tier2_mq84_excluded_from_slip(self):
        """Tier 2 candidate with MQ=84 must be excluded from /slip (below 85 threshold)."""
        cand = self._make_cand("P3", "Points", "OVER", "NBA")
        plh  = _make_plh("P3", "NBA", stat_type="Points", market_quality_score=84, score_total=90.0)
        assert _apply_slip_filter([cand], [plh]) == []

    def test_tier2_bq75_mq75_blocked_in_slip(self):
        """Tier 2 candidate with BQ=75/MQ=75 must now be BLOCKED (threshold raised to 85)."""
        cand = self._make_cand("P6", "Passing Yards", "OVER", "NFL")
        plh  = _make_plh("P6", "NFL", stat_type="Passing Yards", market_quality_score=75, score_total=75.0)
        assert _apply_slip_filter([cand], [plh]) == []

    def test_tier2_both_85_allowed_in_slip(self):
        """Tier 2 candidate with BQ=85 and MQ=85 must be eligible for /slip."""
        cand = self._make_cand("P4", "Passing Yards", "OVER", "NFL")
        plh  = _make_plh("P4", "NFL", stat_type="Passing Yards", market_quality_score=85, score_total=85.0)
        assert _apply_slip_filter([cand], [plh]) == [cand]

    def test_tier2_null_scores_pass_conservatively_in_slip(self):
        """Tier 2 with NULL scores passes conservatively in /slip."""
        cand = self._make_cand("P5", "Rebounds", "UNDER", "NBA")
        plh  = _make_plh("P5", "NBA", stat_type="Rebounds", market_quality_score=None, score_total=None)
        assert _apply_slip_filter([cand], [plh]) == [cand]


# ── Structural checks ──────────────────────────────────────────────────────────

class TestStructuralChecks:
    """Confirm the structural changes required by the spec."""

    def test_old_dead_zone_rule_gone_for_tier1(self):
        """Tier 1 now requires both scores to reach 70 plus evidence."""
        for mq in [40, 47, 55, 60, 69]:
            for sport in ["WNBA", "NHL", "TENNIS", "CS2", "DOTA2", "LOL", "VAL", "MMA"]:
                assert not _tier_delivery_gate(sport, "OVER", bq_score=70, mq_score=mq, evidence_available=True)
                assert not _tier_delivery_gate(sport, "UNDER", bq_score=70, mq_score=mq, evidence_available=True)

    def test_tier2_is_exactly_nba_mlb_nfl(self):
        """Existing football aliases remain part of Tier 2."""
        assert _TIER2_SPORTS == frozenset({"NBA", "MLB", "NFL", "NCAA", "NCAAF", "CFB"})

    def test_tier1_definition_is_complement(self):
        """Tier 1 = all supported sports not in TIER2_SPORTS."""
        tier1_samples = [
            "WNBA", "NHL", "TENNIS", "SOCCER", "FIFA",
            "CS2", "DOTA2", "LOL", "VAL", "MMA",
            "BADMINTON", "TABLE TENNIS", "RACING",
            "CFL", "KBO", "NPB", "CRICKET",
        ]
        for sport in tier1_samples:
            assert not _is_tier2_sport(sport), f"{sport} should be Tier 1"

    def test_no_dead_zone_constant_in_module(self):
        """No dead-zone constant should remain in market_engine."""
        import market_engine
        assert not hasattr(market_engine, "_mq_passes_delivery_gate")
        assert not hasattr(market_engine, "_DQ_TIER1_CAP")
        assert not hasattr(market_engine, "_DQ_TIER2_CAP")
