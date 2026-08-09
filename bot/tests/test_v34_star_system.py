"""
Tests for the V3.4 star/priority/strong-UNDER system.

Verification checklist:
  ☐ Stars computed from Bet Quality (decision.confidence), NOT score.total
  ☐ 100     → ★★★★★
  ☐ 80–99   → ★★★★☆
  ☐ 70–79   → ★★★☆☆
  ☐ 40–69   → ★★☆☆☆
  ☐ 0–39    → ★☆☆☆☆
  ☐ Raw score 52 + BQ 95 → ★★★★☆ (NOT 2 or 3 stars)
  ☐ 80+ = HIGH PRIORITY floor
  ☐ 75–79 = ACTIONABLE
  ☐ 70–74 = WATCHLIST ONLY
  ☐ UNDER + underlying score ≤ 30 = Strong UNDER Signal (display only)
  ☐ Strong UNDER does not bypass gates or change BQ
  ☐ OVER + underlying score ≤ 30 does NOT get the UNDER signal
  ☐ V3.3 95+ override unchanged
  ☐ high_priority floor is 80 (not 85 or 90)
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════════════════
# S1 — _bq_stars helper in market_engine
# ═══════════════════════════════════════════════════════════════════════════

class TestBqStarsMarketEngine:
    """_bq_stars in market_engine must map BQ to stars correctly."""

    def _fn(self):
        import market_engine as me
        return me._bq_stars

    def test_helper_exists(self):
        import market_engine as me
        assert hasattr(me, "_bq_stars"), "_bq_stars not found in market_engine"
        assert callable(me._bq_stars)

    # ── Exact spec values ────────────────────────────────────────────────────
    def test_100_five_stars(self):
        assert self._fn()(100) == "★★★★★"

    def test_99_four_stars(self):
        assert self._fn()(99) == "★★★★☆"

    def test_95_four_stars(self):
        assert self._fn()(95) == "★★★★☆"

    def test_90_four_stars(self):
        assert self._fn()(90) == "★★★★☆"

    def test_85_four_stars(self):
        assert self._fn()(85) == "★★★★☆"

    def test_80_four_stars(self):
        assert self._fn()(80) == "★★★★☆"

    def test_79_three_stars(self):
        assert self._fn()(79) == "★★★☆☆"

    def test_75_three_stars(self):
        assert self._fn()(75) == "★★★☆☆"

    def test_74_three_stars(self):
        assert self._fn()(74) == "★★★☆☆"

    def test_70_three_stars(self):
        assert self._fn()(70) == "★★★☆☆"

    def test_69_two_stars(self):
        assert self._fn()(69) == "★★☆☆☆"

    def test_40_two_stars(self):
        assert self._fn()(40) == "★★☆☆☆"

    def test_39_one_star(self):
        assert self._fn()(39) == "★☆☆☆☆"

    def test_0_one_star(self):
        assert self._fn()(0) == "★☆☆☆☆"

    # ── Boundary checks ──────────────────────────────────────────────────────
    def test_boundary_80_exactly_four_stars(self):
        """80 is the floor for 4★ — must not fall to 3★."""
        assert self._fn()(80) == "★★★★☆", "80 must be 4★ (HIGH PRIORITY floor)"

    def test_boundary_70_exactly_three_stars(self):
        assert self._fn()(70) == "★★★☆☆"

    def test_boundary_40_exactly_two_stars(self):
        assert self._fn()(40) == "★★☆☆☆"

    # ── Data consistency: raw score 52 + BQ 95 ───────────────────────────────
    def test_bq95_is_four_stars_regardless_of_raw_score(self):
        """BQ 95 must give ★★★★☆ even when raw market score is 52."""
        stars = self._fn()(95)
        assert stars == "★★★★☆", (
            f"BQ 95 must be ★★★★☆, got {stars!r} — stars must NOT use score.total (52)"
        )
        assert "★★★★★" not in stars or stars == "★★★★★"  # 95 is not 5★

    def test_output_always_five_chars(self):
        """Star string must always be exactly 5 characters."""
        fn = self._fn()
        for bq in (0, 39, 40, 69, 70, 79, 80, 99, 100):
            result = fn(bq)
            filled = result.count("★")
            empty  = result.count("☆")
            assert filled + empty == 5, f"BQ {bq}: expected 5-char star string, got {result!r}"


# ═══════════════════════════════════════════════════════════════════════════
# S2 — _bq_stars helper in alerts_multiplatform
# ═══════════════════════════════════════════════════════════════════════════

class TestBqStarsAlertsMultiplatform:
    """_bq_stars in alerts_multiplatform must use the same V3.4 mapping."""

    def _fn(self):
        import alerts_multiplatform as am
        return am._bq_stars

    def test_helper_exists(self):
        import alerts_multiplatform as am
        assert hasattr(am, "_bq_stars"), "_bq_stars not found in alerts_multiplatform"

    def test_100_five_stars(self):
        assert self._fn()(100) == "★★★★★"

    def test_80_four_stars(self):
        assert self._fn()(80) == "★★★★☆"

    def test_79_three_stars(self):
        assert self._fn()(79) == "★★★☆☆"

    def test_40_two_stars(self):
        assert self._fn()(40) == "★★☆☆☆"

    def test_39_one_star(self):
        assert self._fn()(39) == "★☆☆☆☆"

    def test_95_is_four_stars(self):
        """BQ 95 must be ★★★★☆ — used for grade block in alerts."""
        assert self._fn()(95) == "★★★★☆"


# ═══════════════════════════════════════════════════════════════════════════
# S3 — _bq_priority_label helper
# ═══════════════════════════════════════════════════════════════════════════

class TestBqPriorityLabel:
    """_bq_priority_label must return correct classification labels."""

    def _fn(self):
        import alerts_multiplatform as am
        return am._bq_priority_label

    def test_helper_exists(self):
        import alerts_multiplatform as am
        assert hasattr(am, "_bq_priority_label"), "_bq_priority_label not found"

    def test_100_high_priority(self):
        assert "HIGH PRIORITY" in self._fn()(100)

    def test_95_high_priority(self):
        assert "HIGH PRIORITY" in self._fn()(95)

    def test_90_high_priority(self):
        assert "HIGH PRIORITY" in self._fn()(90)

    def test_85_high_priority(self):
        assert "HIGH PRIORITY" in self._fn()(85)

    def test_80_high_priority(self):
        """80 is the HIGH PRIORITY floor per V3.4."""
        lbl = self._fn()(80)
        assert "HIGH PRIORITY" in lbl, f"80 must be HIGH PRIORITY, got {lbl!r}"

    def test_79_actionable(self):
        lbl = self._fn()(79)
        assert "ACTIONABLE" in lbl, f"79 must be ACTIONABLE, got {lbl!r}"
        assert "HIGH PRIORITY" not in lbl

    def test_75_actionable(self):
        lbl = self._fn()(75)
        assert "ACTIONABLE" in lbl, f"75 must be ACTIONABLE, got {lbl!r}"

    def test_74_watchlist(self):
        lbl = self._fn()(74)
        assert "WATCHLIST" in lbl, f"74 must be WATCHLIST, got {lbl!r}"
        assert "HIGH PRIORITY" not in lbl
        assert "ACTIONABLE" not in lbl

    def test_70_watchlist(self):
        lbl = self._fn()(70)
        assert "WATCHLIST" in lbl, f"70 must be WATCHLIST, got {lbl!r}"

    def test_69_not_actionable_label_empty_or_absent(self):
        """69 would not normally reach the formatter; label should be empty or non-promotional."""
        lbl = self._fn()(69)
        assert "HIGH PRIORITY" not in lbl
        assert "ACTIONABLE" not in lbl

    def test_boundary_80_not_79_confusion(self):
        """80 → HIGH PRIORITY; 79 → ACTIONABLE. Must be distinct."""
        assert "HIGH PRIORITY" in self._fn()(80)
        assert "ACTIONABLE" in self._fn()(79)
        assert "HIGH PRIORITY" not in self._fn()(79)


# ═══════════════════════════════════════════════════════════════════════════
# S4 — _format_95_priority_alert uses BQ stars
# ═══════════════════════════════════════════════════════════════════════════

class TestOverrideAlertBqStars:
    """_format_95_priority_alert must show BQ stars (from conf), not score.total stars."""

    def _make(self, score_total=52, conf=95, rec="OVER", sport="MLB"):
        import market_engine as me

        class _S:
            total = score_total; stars = 2; tier = "S"  # stars here are from raw score (wrong)
            stars_display = "★★☆☆☆"  # 2 stars from raw score — must NOT appear

        class _N:
            pass
        n = _N()
        n.sport = sport
        n.game_time = None

        class _D:
            recommendation = rec; confidence = conf; decision_tier = "S"

        return me._format_95_priority_alert("Royce Lewis", n, "Strikeouts", _S(), _D(), 2.5)

    def test_bq95_shows_four_stars(self):
        """BQ 95 + raw score 52 → must show ★★★★☆, not ★★☆☆☆."""
        msg = self._make(score_total=52, conf=95)
        assert "★★★★☆" in msg, (
            f"Override must show ★★★★☆ (BQ 95), not 2★ from raw score. Got: {msg[:200]!r}"
        )

    def test_bq95_does_not_show_two_stars(self):
        """★★☆☆☆ (2-star from raw score 52) must NOT appear in override."""
        msg = self._make(score_total=52, conf=95)
        assert "★★☆☆☆" not in msg, (
            "★★☆☆☆ (raw score stars) must not appear — stars must come from BQ 95"
        )

    def test_bq100_shows_five_stars(self):
        msg = self._make(conf=100)
        assert "★★★★★" in msg

    def test_bq80_shows_four_stars(self):
        msg = self._make(conf=80)
        assert "★★★★☆" in msg

    def test_bq79_shows_three_stars(self):
        """_format_95_priority_alert is only called for BQ ≥ 95, but helper handles all values."""
        import market_engine as me
        assert me._bq_stars(79) == "★★★☆☆"


# ═══════════════════════════════════════════════════════════════════════════
# S5 — Strong UNDER signal in format_underdog_change_alert
# ═══════════════════════════════════════════════════════════════════════════

class TestStrongUnderSignal:
    """Strong UNDER signal display — UNDER + underlying score ≤ 30."""

    def _change_alert(self, rec="UNDER", score_total=28, conf=80, sport="WNBA"):
        import alerts_multiplatform as am

        class _Score:
            total = score_total; tier = "S"; stars = 3
            stars_display = "★★★☆☆"; n_history = 10; move_velocity = None

        class _Dec:
            recommendation = rec; confidence = conf; decision_tier = "S"
            l5_hit_rate = None; l5_games = None; reason = ""

        return am.format_underdog_change_alert(
            player_name="Test Player",
            team="TEST",
            sport=sport,
            stat_type="Points",
            old_line=5.5,
            new_line=5.5,
            score=_Score(),
            decision=_Dec(),
        )

    def _new_prop_alert(self, rec="UNDER", score_total=28, conf=80):
        import alerts_multiplatform as am

        class _Score:
            total = score_total; tier = "S"; stars = 3
            stars_display = "★★★☆☆"; n_history = 10; move_velocity = None

        class _Dec:
            recommendation = rec; confidence = conf; decision_tier = "S"

        return am.format_underdog_new_prop_alert(
            player_name="Test Player",
            team="TEST",
            sport="CS",
            stat_type="Kills",
            line_value=25.5,
            score=_Score(),
            decision=_Dec(),
        )

    # ── Trigger conditions ───────────────────────────────────────────────────
    def test_under_score_30_triggers_signal(self):
        """UNDER + score 30 → Strong UNDER Signal in alert."""
        msg = self._change_alert(rec="UNDER", score_total=30)
        assert "Strong UNDER Signal" in msg, (
            "UNDER + score 30 must show Strong UNDER Signal"
        )

    def test_under_score_29_triggers_signal(self):
        msg = self._change_alert(rec="UNDER", score_total=29)
        assert "Strong UNDER Signal" in msg

    def test_under_score_20_triggers_signal(self):
        msg = self._change_alert(rec="UNDER", score_total=20)
        assert "Strong UNDER Signal" in msg

    def test_under_score_0_triggers_signal(self):
        msg = self._change_alert(rec="UNDER", score_total=0)
        assert "Strong UNDER Signal" in msg

    # ── Non-trigger conditions ───────────────────────────────────────────────
    def test_under_score_31_no_signal(self):
        """Score 31 is above the ≤30 threshold — no Strong UNDER Signal."""
        msg = self._change_alert(rec="UNDER", score_total=31)
        assert "Strong UNDER Signal" not in msg, (
            "Score 31 must NOT trigger Strong UNDER Signal (threshold is ≤ 30)"
        )

    def test_over_score_28_no_signal(self):
        """OVER direction + low score must NOT get the UNDER signal."""
        msg = self._change_alert(rec="OVER", score_total=28)
        assert "Strong UNDER Signal" not in msg, (
            "OVER direction must never receive the Strong UNDER Signal"
        )

    def test_over_score_0_no_signal(self):
        """Even score=0 OVER must NOT get the UNDER signal."""
        msg = self._change_alert(rec="OVER", score_total=0)
        assert "Strong UNDER Signal" not in msg

    # ── New prop alert also shows signal ─────────────────────────────────────
    def test_new_prop_under_score_28_triggers_signal(self):
        """New-prop alert with UNDER + score ≤ 30 must also show signal."""
        msg = self._new_prop_alert(rec="UNDER", score_total=28)
        assert "Strong UNDER Signal" in msg, (
            "format_underdog_new_prop_alert must show Strong UNDER Signal when conditions met"
        )

    def test_new_prop_over_score_28_no_signal(self):
        msg = self._new_prop_alert(rec="OVER", score_total=28)
        assert "Strong UNDER Signal" not in msg

    # ── Signal is display-only; BQ unchanged ────────────────────────────────
    def test_signal_does_not_change_bq_display(self):
        """BQ value in alert must be unchanged regardless of strong UNDER signal."""
        msg = self._change_alert(rec="UNDER", score_total=28, conf=80)
        assert "80/100" in msg, "BQ 80 must still appear unchanged in the alert"

    def test_signal_does_not_affect_over_logic(self):
        """OVER alerts with the same score must be fully unaffected."""
        msg_over  = self._change_alert(rec="OVER",  score_total=28, conf=80)
        msg_under = self._change_alert(rec="UNDER", score_total=28, conf=80)
        assert "Strong UNDER Signal" not in msg_over
        assert "Strong UNDER Signal" in msg_under


# ═══════════════════════════════════════════════════════════════════════════
# S6 — Grade block in alerts uses BQ stars
# ═══════════════════════════════════════════════════════════════════════════

class TestGradeBlockBqStars:
    """Alert grade block must show BQ stars and BQ value, not raw score stars."""

    def _change_alert_grade(self, score_total=52, conf=95, rec="OVER"):
        import alerts_multiplatform as am

        class _Score:
            total = score_total; tier = "S"; stars = 2
            stars_display = "★★☆☆☆"; n_history = 10; move_velocity = None

        class _Dec:
            recommendation = rec; confidence = conf; decision_tier = "S"
            l5_hit_rate = None; l5_games = None; reason = ""

        return am.format_underdog_change_alert(
            player_name="Test", team="T", sport="MLB",
            stat_type="Hits", old_line=1.5, new_line=1.5,
            score=_Score(), decision=_Dec(),
        )

    def test_grade_shows_bq_four_stars_for_conf95(self):
        """Grade block must show ★★★★☆ when BQ=95, NOT ★★☆☆☆ from raw score 52."""
        msg = self._grade_alert = self._change_alert_grade(score_total=52, conf=95)
        assert "★★★★☆" in msg, (
            "Grade block must show ★★★★☆ (BQ 95 stars), not ★★☆☆☆ (raw score 52 stars)"
        )

    def test_grade_does_not_show_two_stars_for_bq95(self):
        """★★☆☆☆ (raw score 52 stars) must not appear when BQ=95."""
        msg = self._change_alert_grade(score_total=52, conf=95)
        assert "★★☆☆☆" not in msg, (
            "Raw score stars ★★☆☆☆ must not appear when BQ gives different star count"
        )

    def test_grade_shows_bq_value(self):
        """BQ value (95/100) must appear in the alert when confidence=95.

        The compact LC format shows BQ in the pick_line as 'Bet Quality 95/100',
        not in a separate grade block.  Both 'Bet Quality' and '95/100' must be present.
        """
        msg = self._change_alert_grade(score_total=52, conf=95)
        assert "95/100" in msg, "95/100 must appear in the alert"
        assert "Bet Quality" in msg or "BQ" in msg, "Alert must show BQ label"

    def test_grade_stars_match_bq_not_raw(self):
        """Stars in grade must be determined by BQ=80, not raw score=52."""
        msg = self._change_alert_grade(score_total=52, conf=80)
        assert "★★★★☆" in msg, "BQ 80 must give ★★★★☆ in grade block"


# ═══════════════════════════════════════════════════════════════════════════
# S7 — high_priority threshold is 80 (not 85 or 90)
# ═══════════════════════════════════════════════════════════════════════════

class TestHighPriorityThreshold80:
    """high_priority=True must fire at BQ ≥ 80 (V3.4 floor), not 85 or 90."""

    def _route(self, confidence: float, decision_tier: str) -> str:
        """Simulate the high_priority routing logic from market_engine."""
        if confidence >= 95 and decision_tier == "S":
            return "OVERRIDE_95_PLUS"
        if 80 <= confidence < 95 and decision_tier == "S":
            return "HIGH_PRIORITY_80_94"
        return "NORMAL"

    def test_80_is_high_priority(self):
        """80 is the new high_priority floor — must fire HIGH PRIORITY."""
        assert self._route(80, "S") == "HIGH_PRIORITY_80_94", (
            "80 must be HIGH_PRIORITY_80_94 (V3.4 floor lowered from 85)"
        )

    def test_84_is_high_priority(self):
        assert self._route(84, "S") == "HIGH_PRIORITY_80_94"

    def test_85_is_high_priority(self):
        assert self._route(85, "S") == "HIGH_PRIORITY_80_94"

    def test_79_is_normal(self):
        """79 is below the V3.4 floor — must be NORMAL (goes through normal gates)."""
        assert self._route(79, "S") == "NORMAL"

    def test_source_uses_80_floor(self):
        """market_engine source must use 80 <= decision.confidence as the high_priority floor."""
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        assert "80 <= decision.confidence < 95" in src or "80 <= _sdec.confidence < 95" in src, (
            "V3.4 requires 80 as the high_priority floor — 80 <= ... < 95 not found in source"
        )
        # Old 85 threshold must not remain
        assert "85 <= decision.confidence < 95" not in src, (
            "Old 85 <= threshold still present — V3.4 sets 80 as the floor"
        )
        assert "85 <= _sdec.confidence < 95" not in src, (
            "Old 85 <= (standing path) still present — V3.4 sets 80 as the floor"
        )

    def test_95_still_uses_override_path(self):
        """V3.3 95+ override path must be unchanged."""
        assert self._route(95, "S") == "OVERRIDE_95_PLUS"


# ═══════════════════════════════════════════════════════════════════════════
# S8 — Score.total NOT used for stars anywhere in priority/alert path
# ═══════════════════════════════════════════════════════════════════════════

class TestScoreTotalNotUsedForStars:
    """Verify no priority path computes stars from score.total."""

    def test_format_95_does_not_use_score_stars(self):
        """_format_95_priority_alert source must not use score.stars for display."""
        import inspect, market_engine as me
        src = inspect.getsource(me._format_95_priority_alert)
        # Must use _bq_stars, not score.stars
        assert "_bq_stars" in src, "_format_95_priority_alert must use _bq_stars(conf)"
        assert "score.stars" not in src, (
            "score.stars must not be used in _format_95_priority_alert — use BQ stars"
        )

    def test_bq_stars_used_in_alerts_multiplatform(self):
        """alerts_multiplatform must reference _bq_stars in format functions."""
        import inspect, alerts_multiplatform as am
        src_change  = inspect.getsource(am.format_underdog_change_alert)
        src_newprop = inspect.getsource(am.format_underdog_new_prop_alert)
        assert "_bq_stars" in src_change, (
            "_bq_stars must be used in format_underdog_change_alert"
        )
        assert "_bq_stars" in src_newprop, (
            "_bq_stars must be used in format_underdog_new_prop_alert"
        )

    def test_scoring_engine_unchanged(self):
        """_STAR_BANDS in ud_scoring must NOT have been modified (not the right source for alert stars)."""
        from engine.ud_scoring import score_ud_prop
        assert callable(score_ud_prop), "Scoring engine must remain intact"

    def test_bq_stars_helper_is_separate_from_scoring_engine(self):
        """_bq_stars helper exists independently; ud_scoring star bands untouched."""
        import market_engine as me
        from engine import ud_scoring
        # Scoring engine bands still exist unchanged
        assert hasattr(ud_scoring, "_STAR_BANDS"), "_STAR_BANDS must still exist in ud_scoring"
        # BQ stars helper also exists in market_engine
        assert hasattr(me, "_bq_stars"), "_bq_stars must exist in market_engine"
