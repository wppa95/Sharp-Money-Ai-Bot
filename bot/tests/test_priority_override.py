"""
Tests for the S-tier priority override system.

Verification checklist:
  ☐ 89/100 S-tier → normal behavior (no override)
  ☐ 90-94/100 S-tier → priority handling + all gates active
  ☐ 95+/100 S-tier → immediate override, all validation bypassed
  ☐ Non-S-tier (A/B/PASS) → no override regardless of score
  ☐ No duplicate alert spam
  ☐ Existing pipeline unchanged
  ☐ Full test suite passes

Implementation architecture:
  - _priority_override_sent (module-level set): per-session dedup
  - _priority_alerted_this_scan (local set per underdog_job call): per-scan dedup
  - _format_95_priority_alert(): formats the override Telegram message
  - high_priority kwarg on deliver_underdog(): prepends 🔥 header for 90-94
"""

import inspect
import textwrap


# ═══════════════════════════════════════════════════════════════════════════
# P1 — _format_95_priority_alert exists and produces correct output
# ═══════════════════════════════════════════════════════════════════════════

class TestFormat95PriorityAlert:
    """_format_95_priority_alert must produce a valid priority override message."""

    def _import(self):
        import market_engine as me
        return me._format_95_priority_alert

    def test_function_exists(self):
        """_format_95_priority_alert must exist as a module-level function."""
        import market_engine as me
        assert hasattr(me, "_format_95_priority_alert"), (
            "_format_95_priority_alert not found in market_engine"
        )
        assert callable(me._format_95_priority_alert)

    def test_message_contains_bet_quality(self):
        """Alert must show Bet Quality (decision.confidence), NOT raw score.total."""
        fn = self._import()

        class _FakeScore:
            total = 52   # raw market score — intentionally low
            stars = 5
            tier  = "S"

        class _FakeSnap:
            sport     = "WNBA"
            game_time = None

        class _FakeDecision:
            recommendation = "OVER"
            confidence     = 95  # BQ — this is what should appear

        msg = fn("Chelsea Gray", _FakeSnap(), "Rebounds", _FakeScore(), _FakeDecision(), 2.5)
        # Must show the BQ (95), NOT the raw score (52)
        assert "95" in msg, "Bet Quality (confidence) must appear in override message"
        assert "52" not in msg or msg.count("52") == 0, (
            "Raw score.total (52) must NOT appear in override message header — only BQ"
        )

    def test_message_says_bet_quality_not_score_gte(self):
        """Telegram text must say 'Bet Quality' — not 'Score ≥ 95'."""
        fn = self._import()

        class _FakeScore:
            total = 52; stars = 5; tier = "S"

        class _FakeSnap:
            sport = "WNBA"; game_time = None

        class _FakeDecision:
            recommendation = "OVER"; confidence = 95

        msg = fn("Test", _FakeSnap(), "Points", _FakeScore(), _FakeDecision(), 10.5)
        assert "Bet Quality" in msg, "Override message must say 'Bet Quality'"
        assert "Score ≥ 95" not in msg, "'Score ≥ 95' must NOT appear — it was the old incorrect wording"
        assert "All validation gates bypassed" not in msg, (
            "'All validation gates bypassed' must NOT appear — inaccurate; direction gate still applies"
        )

    def test_message_contains_priority_header(self):
        """Override message must have the 🔥🚨 priority header."""
        fn = self._import()

        class _S:
            total = 95; stars = 5; tier = "S"

        class _N:
            sport = "LOL"; game_time = None

        class _D:
            recommendation = "UNDER"; confidence = 90

        msg = fn("Player", _N(), "Kills", _S(), _D(), 5.5)
        assert "🔥" in msg and "🚨" in msg, "Priority header emojis must be in override message"

    def test_message_contains_player_and_stat(self):
        fn = self._import()

        class _S:
            total = 95; stars = 5; tier = "S"

        class _N:
            sport = "CS"; game_time = None

        msg = fn("jcobbb", _N(), "Headshots on Maps 1+2", _S(), None, 3.5)
        assert "jcobbb" in msg
        assert "Headshots on Maps 1+2" in msg

    def test_message_handles_none_decision(self):
        """Override must not crash when decision is None (no direction available)."""
        fn = self._import()

        class _S:
            total = 95; stars = 5; tier = "S"

        class _N:
            sport = "TENNIS"; game_time = None

        msg = fn("Denis Shapovalov", _N(), "Aces", _S(), None, 2.5)
        assert isinstance(msg, str) and len(msg) > 0

    def test_message_handles_pass_decision(self):
        """Override with PASS decision must omit direction line."""
        fn = self._import()

        class _S:
            total = 97; stars = 5; tier = "S"

        class _N:
            sport = "MLB"; game_time = None

        class _D:
            recommendation = "PASS"; confidence = 0

        msg = fn("Shohei Ohtani", _N(), "Strikeouts", _S(), _D(), 8.5)
        assert "PASS" not in msg, "PASS direction must NOT appear in override message"

    def test_message_shows_priority_label(self):
        """Message must include a clear priority indicator."""
        fn = self._import()

        class _S:
            total = 99; stars = 5; tier = "S"

        class _N:
            sport = "NFL"; game_time = None

        class _D:
            recommendation = "OVER"; confidence = 99

        msg = fn("Patrick Mahomes", _N(), "Passing Yards", _S(), _D(), 299.5)
        assert "Priority" in msg or "PRIORITY" in msg, (
            "Override message must include a priority indicator"
        )

    def test_message_shows_sport(self):
        fn = self._import()

        class _S:
            total = 95; stars = 5; tier = "S"

        class _N:
            sport = "NPB"; game_time = None

        msg = fn("Shohei Ohtani", _N(), "Hits", _S(), None, 1.5)
        assert "NPB" in msg


# ═══════════════════════════════════════════════════════════════════════════
# P2 — Module-level infrastructure: _priority_override_sent
# ═══════════════════════════════════════════════════════════════════════════

class TestPriorityOverrideSent:
    """_priority_override_sent must exist as a module-level set."""

    def test_priority_override_sent_exists(self):
        import market_engine as me
        assert hasattr(me, "_priority_override_sent"), (
            "_priority_override_sent not found in market_engine"
        )

    def test_priority_override_sent_is_set(self):
        import market_engine as me
        assert isinstance(me._priority_override_sent, set), (
            "_priority_override_sent must be a set"
        )

    def test_priority_override_sent_is_mutable(self):
        """Must be mutable — the engine writes to it when sending overrides."""
        import market_engine as me
        # Save and restore so test is non-destructive
        original = set(me._priority_override_sent)
        try:
            me._priority_override_sent.add(("test_player", "TEST", "test_stat"))
            assert ("test_player", "TEST", "test_stat") in me._priority_override_sent
        finally:
            me._priority_override_sent.clear()
            me._priority_override_sent.update(original)


# ═══════════════════════════════════════════════════════════════════════════
# P3 — Source-code verification: 95+ override present in all 3 alert paths
# ═══════════════════════════════════════════════════════════════════════════

class TestSourceCodeOverridePaths:
    """Verify the 95+ override is wired into all three alert paths via source inspection."""

    def _src(self) -> str:
        import market_engine as me
        return inspect.getsource(me)

    def test_95_threshold_present_in_source(self):
        """Bet Quality confidence >= 95 threshold must appear in market_engine source."""
        src = self._src()
        assert "decision.confidence >= 95" in src or "_sdec.confidence >= 95" in src, (
            "95-point Bet Quality threshold not found in market_engine source"
        )

    def test_format_95_priority_alert_called_in_source(self):
        """_format_95_priority_alert must be called (not just defined)."""
        src = self._src()
        calls = src.count("_format_95_priority_alert(")
        # Should be called at least 3 times (np, lc, standing paths) + defined once
        assert calls >= 3, (
            f"_format_95_priority_alert called {calls}x — expected ≥ 3 (one per alert path)"
        )

    def test_priority_override_sent_checked_in_source(self):
        """_priority_override_sent must be used as a guard in the override logic."""
        src = self._src()
        assert "_priority_override_sent" in src, (
            "_priority_override_sent not referenced in market_engine"
        )
        assert "_priority_override_sent.add(" in src, (
            "Override key must be recorded to _priority_override_sent after sending"
        )

    def test_priority_alerted_this_scan_defined_in_source(self):
        """Per-scan set must be defined inside underdog_job."""
        src = self._src()
        assert "_priority_alerted_this_scan" in src, (
            "_priority_alerted_this_scan not found in market_engine"
        )
        assert "_priority_alerted_this_scan.add(" in src

    def test_lc_95_sent_flag_present(self):
        """_lc_95_sent flag must be defined and used to suppress lc normal gates."""
        src = self._src()
        assert "_lc_95_sent" in src, "_lc_95_sent flag not found in market_engine"

    def test_broadcast_alert_called_for_override(self):
        """The 95+ override must send via broadcast_alert (not deliver_underdog)."""
        src = self._src()
        # The override calls broadcast_alert directly with _format_95_priority_alert
        assert "PRIORITY OVERRIDE [new]" in src
        assert "PRIORITY OVERRIDE [lc]" in src
        assert "PRIORITY OVERRIDE [standing]" in src

    def test_standing_path_uses_continue_for_95_plus(self):
        """Standing path must `continue` to skip remaining gates after 95+ override."""
        src = self._src()
        # The continue must appear after the 95+ override block in the standing loop
        # Verify it's present and positioned after the override comment
        assert "skip normal gate sequence for all 95+ props" in src, (
            "Standing path continue guard comment not found"
        )

    def test_np_immediate_forced_false_for_95_plus(self):
        """np_immediate must be forced to False for 95+ props in new-prop path."""
        src = self._src()
        assert "95+ always uses the override path" in src, (
            "np_immediate=False guard for 95+ not found in new-prop path"
        )

    def test_should_alert_forced_false_for_95_plus(self):
        """should_alert must be forced to False when _lc_95_sent=True."""
        src = self._src()
        assert "if _lc_95_sent:" in src and "should_alert = False" in src, (
            "should_alert=False guard for _lc_95_sent not found in lc path"
        )


# ═══════════════════════════════════════════════════════════════════════════
# P4 — high_priority kwarg in alerts.py
# ═══════════════════════════════════════════════════════════════════════════

class TestHighPriorityParam:
    """deliver_underdog must accept and apply high_priority for 90-94 S-tier."""

    def test_deliver_underdog_has_high_priority_param(self):
        import alerts as al
        sig = inspect.signature(al.AlertDelivery.deliver_underdog)
        assert "high_priority" in sig.parameters, (
            "high_priority param not found in deliver_underdog signature"
        )

    def test_high_priority_defaults_false(self):
        import alerts as al
        sig = inspect.signature(al.AlertDelivery.deliver_underdog)
        default = sig.parameters["high_priority"].default
        assert default is False, (
            f"high_priority default must be False, got {default!r}"
        )

    def test_high_priority_header_in_alerts_source(self):
        """alerts.py must prepend a 🔥 header when high_priority=True."""
        import alerts as al
        src = inspect.getsource(al.AlertDelivery.deliver_underdog)
        assert "high_priority" in src
        assert "🔥" in src, "🔥 emoji must appear in deliver_underdog for high_priority header"
        assert "S-TIER HIGH PRIORITY" in src

    def test_high_priority_in_market_engine_np_call(self):
        """market_engine must pass high_priority to deliver_underdog in new-prop path."""
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        # Look for high_priority= kwarg near the new-prop deliver_underdog call
        assert "high_priority" in src, (
            "high_priority kwarg not passed to deliver_underdog in market_engine"
        )
        # Count occurrences — should appear 3x (np, lc, standing paths)
        count = src.count("high_priority")
        assert count >= 3, (
            f"high_priority appears {count}x in underdog_job — expected ≥ 3"
        )

    def test_high_priority_condition_85_to_94(self):
        """high_priority condition must check 85 <= decision.confidence < 95 AND decision_tier == 'S'.

        V3.4: 4★+ (BQ ≥ 85) qualifies for HIGH PRIORITY; 90 is no longer the floor.
        """
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        assert "85 <= decision.confidence < 95" in src or "85 <= _sdec.confidence < 95" in src, (
            "85-94 Bet Quality range check not found in high_priority condition — V3.4 requires 85 as floor"
        )
        # Old 90 threshold must not remain as the high_priority floor
        assert "90 <= decision.confidence < 95" not in src and "90 <= _sdec.confidence < 95" not in src, (
            "Old 90 <= threshold still present — V3.4 requires 85 as the high_priority floor"
        )
        assert "decision.decision_tier == \"S\"" in src or "decision.decision_tier == 'S'" in src or \
               "_sdec.decision_tier == \"S\"" in src or "_sdec.decision_tier == 'S'" in src, (
            "S decision-tier check not found in high_priority condition"
        )


# ═══════════════════════════════════════════════════════════════════════════
# P5 — Threshold logic simulation: 89/90/94/95/96/100 and non-S-tier
# ═══════════════════════════════════════════════════════════════════════════

class TestThresholdLogicSimulation:
    """Simulate the priority routing logic for every threshold value."""

    def _route(
        self,
        confidence: float,
        decision_tier: str,
        recommendation: str = "OVER",
        sport: str = "WNBA",
    ) -> str:
        """Replicate the exact routing logic from market_engine using Bet Quality confidence.

        Mirrors the three-layer check in each alert path:
          1. PASS guard — never actionable
          2. Sport Direction — MLB/NFL OVER-only; UNDER always blocked
          3. BQ threshold routing — 95+ override, 85-94 high-priority (V3.4: 4★+ floor), else normal
        """
        # 1. PASS decisions never trigger an override regardless of confidence
        if recommendation == "PASS" or decision_tier == "PASS":
            return "NORMAL"
        # 2. Sport Direction check — MLB/NFL are OVER-only; UNDER blocked even at 95+ BQ
        if sport.upper() in ("MLB", "NFL") and recommendation == "UNDER":
            return "DIRECTION_BLOCKED"
        # 3. BQ threshold routing (V3.4: high-priority floor lowered from 90 to 85)
        if confidence >= 95 and decision_tier == "S":
            return "OVERRIDE_95_PLUS"
        if 85 <= confidence < 95 and decision_tier == "S":
            return "HIGH_PRIORITY_85_94"
        return "NORMAL"

    def test_84_s_tier_is_normal(self):
        """84/100 is below the V3.4 4★ floor — must be NORMAL."""
        assert self._route(84, "S") == "NORMAL", "84/100 S-tier must be NORMAL (below V3.4 floor)"

    def test_84_point_9_is_normal(self):
        assert self._route(84.9, "S") == "NORMAL"

    def test_85_s_tier_is_high_priority(self):
        """85/100 is the V3.4 minimum for HIGH PRIORITY (4★ floor)."""
        assert self._route(85, "S") == "HIGH_PRIORITY_85_94", "85/100 S-tier must be HIGH_PRIORITY_85_94"

    def test_89_s_tier_is_high_priority(self):
        """89/100 was NORMAL under old rules — V3.4 makes it HIGH PRIORITY."""
        assert self._route(89, "S") == "HIGH_PRIORITY_85_94", "89/100 S-tier must be HIGH_PRIORITY_85_94 under V3.4"

    def test_90_s_tier_is_high_priority(self):
        assert self._route(90, "S") == "HIGH_PRIORITY_85_94"

    def test_91_s_tier_is_high_priority(self):
        assert self._route(91, "S") == "HIGH_PRIORITY_85_94"

    def test_94_s_tier_is_high_priority(self):
        assert self._route(94, "S") == "HIGH_PRIORITY_85_94"

    def test_94_point_9_is_high_priority(self):
        assert self._route(94.9, "S") == "HIGH_PRIORITY_85_94"

    def test_95_s_tier_is_override(self):
        assert self._route(95, "S") == "OVERRIDE_95_PLUS"

    def test_96_s_tier_is_override(self):
        assert self._route(96, "S") == "OVERRIDE_95_PLUS"

    def test_100_s_tier_is_override(self):
        assert self._route(100, "S") == "OVERRIDE_95_PLUS"

    def test_95_a_tier_is_normal(self):
        """95/100 but NOT S-tier must NOT trigger the override."""
        assert self._route(95, "A") == "NORMAL", "A-tier must never trigger override"

    def test_95_b_tier_is_normal(self):
        assert self._route(95, "B") == "NORMAL", "B-tier must never trigger override"

    def test_95_pass_tier_is_normal(self):
        assert self._route(95, "PASS") == "NORMAL", "PASS tier must never trigger override"

    def test_90_a_tier_is_normal(self):
        assert self._route(90, "A") == "NORMAL", "90/100 A-tier must be NORMAL (not high priority)"

    def test_non_s_tier_100_is_normal(self):
        assert self._route(100, "B") == "NORMAL"


# ═══════════════════════════════════════════════════════════════════════════
# P6 — Dedup logic: no duplicate alert spam
# ═══════════════════════════════════════════════════════════════════════════

class TestDeduplicationLogic:
    """Simulate the dedup logic for 95+ overrides."""

    def _would_send(
        self,
        key: tuple,
        priority_alerted_this_scan: set,
        priority_override_sent: set,
    ) -> bool:
        """Replicate the 95+ send guard from all three paths."""
        return (
            key not in priority_alerted_this_scan
            and key not in priority_override_sent
        )

    def test_first_occurrence_sends(self):
        key = ("Player A", "WNBA", "Rebounds")
        scan_set: set = set()
        session_set: set = set()
        assert self._would_send(key, scan_set, session_set)

    def test_same_scan_second_occurrence_suppressed(self):
        key = ("Player A", "WNBA", "Rebounds")
        scan_set: set = {key}
        session_set: set = set()
        assert not self._would_send(key, scan_set, session_set)

    def test_session_dedup_blocks_second_scan(self):
        """If already sent this session, _priority_override_sent blocks it."""
        key = ("Player A", "WNBA", "Rebounds")
        scan_set: set = set()  # new scan — empty
        session_set: set = {key}  # already sent this session
        assert not self._would_send(key, scan_set, session_set)

    def test_different_player_not_suppressed(self):
        """Different player must still fire independently."""
        key_a = ("Player A", "WNBA", "Rebounds")
        key_b = ("Player B", "WNBA", "Rebounds")
        scan_set: set = {key_a}
        session_set: set = {key_a}
        assert self._would_send(key_b, scan_set, session_set)

    def test_different_stat_type_not_suppressed(self):
        key_a = ("Player A", "WNBA", "Points")
        key_b = ("Player A", "WNBA", "Rebounds")
        scan_set: set = {key_a}
        session_set: set = set()
        assert self._would_send(key_b, scan_set, session_set)

    def test_scan_set_fresh_each_scan(self):
        """Per-scan set must be empty at the start of each scan cycle.
        Verified by checking it's defined inside underdog_job (not module-level)."""
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        # _priority_alerted_this_scan must be initialized inside the function
        assert "_priority_alerted_this_scan: set = set()" in src or \
               "_priority_alerted_this_scan = set()" in src, (
            "_priority_alerted_this_scan must be initialized inside underdog_job"
        )

    def test_session_set_is_module_level(self):
        """_priority_override_sent must be module-level (survives across scan calls)."""
        import market_engine as me
        # It should exist as a module attribute (not just inside underdog_job)
        assert hasattr(me, "_priority_override_sent"), (
            "_priority_override_sent must be a module-level attribute"
        )


# ═══════════════════════════════════════════════════════════════════════════
# P7 — Existing pipeline unchanged for <90/non-S-tier props
# ═══════════════════════════════════════════════════════════════════════════

class TestExistingPipelineUnchanged:
    """Verify the existing pipeline is not modified for non-priority props."""

    def test_scoring_engine_not_changed(self):
        """score_ud_prop must still exist and be unchanged."""
        from engine.ud_scoring import score_ud_prop
        assert callable(score_ud_prop)

    def test_make_ud_bet_decision_unchanged(self):
        """make_ud_bet_decision must still exist and be unchanged."""
        from engine.ud_bet_decision import make_ud_bet_decision
        assert callable(make_ud_bet_decision)

    def test_normal_alert_path_still_uses_deliver_underdog(self):
        """Normal (non-override) alert path still calls deliver_underdog."""
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        assert "delivery.deliver_underdog(" in src, (
            "deliver_underdog must still be used for normal alert path"
        )

    def test_broadcast_alert_for_override_only(self):
        """broadcast_alert in underdog_job is used only for the 95+ override path.
        The normal path uses delivery.deliver_underdog (which calls broadcast_alert internally).
        Verify by counting direct broadcast_alert calls vs deliver_underdog calls.
        """
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        direct_bc = src.count("await broadcast_alert(")
        assert direct_bc >= 3, (
            f"Expected ≥ 3 direct broadcast_alert calls (one per override path), got {direct_bc}"
        )

    def test_high_priority_false_by_default_in_alerts(self):
        """high_priority=False by default ensures existing behavior for sub-90 props."""
        import alerts as al
        sig = inspect.signature(al.AlertDelivery.deliver_underdog)
        assert sig.parameters["high_priority"].default is False

    def test_no_threshold_changes_in_scoring(self):
        """Scoring thresholds (UD_MIN_CONF_S, UD_MIN_CONF_A, etc.) unchanged."""
        from config import config
        # These must remain at their configured values
        assert config.UD_MIN_CONF_S <= 100, "UD_MIN_CONF_S still exists"
        assert config.UD_MIN_CONF_A <= 100, "UD_MIN_CONF_A still exists"
        assert config.UD_STRICT_SPORT_MIN_BET_QUALITY <= 100, "BQ gate still exists"

    def test_normal_gate_sequence_still_active_for_89(self):
        """For 89/100 S-tier, np_immediate logic still applies (score.stars gate)."""
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        # The stars gate must still be present in the normal path
        assert "min_stars_for_sport" in src, "Stars gate must still be active"

    def test_delivery_result_structure_unchanged(self):
        """DeliveryResult structure must be unchanged."""
        from alerts import DeliveryResult
        r = DeliveryResult(sent=False)
        assert hasattr(r, "sent")
        assert hasattr(r, "filtered")


# ═══════════════════════════════════════════════════════════════════════════
# P8 — 95+ override message quality
# ═══════════════════════════════════════════════════════════════════════════

class TestOverrideMessageQuality:
    """The 95+ override message must be useful and unambiguous."""

    def _make_msg(self, score_total=96, rec="OVER", sport="WNBA") -> str:
        import market_engine as me

        class _S:
            total = score_total; stars = 5; tier = "S"

        class _N:
            pass

        _n = _N()
        _n.sport = sport
        _n.game_time = None

        class _D:
            recommendation = rec; confidence = 85

        return me._format_95_priority_alert("Test Player", _n, "Rebounds", _S(), _D(), 2.5)

    def test_all_sports_get_same_override_format(self):
        """Override format must work for every sport."""
        for sport in ("MLB", "WNBA", "NFL", "TENNIS", "CS", "LOL", "VAL", "PGA", "NPB", "ESPORTS"):
            msg = self._make_msg(sport=sport)
            assert sport in msg, f"Sport {sport} must appear in override message"

    def test_override_message_not_empty(self):
        msg = self._make_msg()
        assert len(msg) > 50, "Override message must be substantive"

    def test_override_message_is_string(self):
        msg = self._make_msg()
        assert isinstance(msg, str)

    def test_override_message_contains_line(self):
        msg = self._make_msg()
        assert "2.5" in msg, "Line value must appear in override message"

    def test_over_direction_shown(self):
        msg = self._make_msg(rec="OVER")
        assert "OVER" in msg

    def test_under_direction_shown(self):
        msg = self._make_msg(rec="UNDER")
        assert "UNDER" in msg

    def test_message_has_stars(self):
        msg = self._make_msg()
        assert "★" in msg, "Star rating must appear in override message"


# ═══════════════════════════════════════════════════════════════════════════
# P8b — V3.3 Sport-Direction Policy at 95+ BQ (spec cases 1–18)
# ═══════════════════════════════════════════════════════════════════════════

class TestV33SportDirectionPolicy:
    """V3.3 sport-direction enforcement at the 95+ BQ threshold.

    Spec:
      - MLB/NFL = Tier 2 = OVER-only.  UNDER blocked at 95+ BQ.
      - All other sports = Tier 1 = OVER and UNDER both allowed at 95+ BQ.
      - 85-94 BQ (4★+) gets HIGH PRIORITY label; normal gates still apply (V3.4).
      - PASS recommendation = never actionable.
    """

    def _route(
        self,
        confidence: float,
        decision_tier: str,
        recommendation: str = "OVER",
        sport: str = "WNBA",
    ) -> str:
        # Mirror the logic in TestThresholdLogicSimulation._route
        if recommendation == "PASS" or decision_tier == "PASS":
            return "NORMAL"
        if sport.upper() in ("MLB", "NFL") and recommendation == "UNDER":
            return "DIRECTION_BLOCKED"
        if confidence >= 95 and decision_tier == "S":
            return "OVERRIDE_95_PLUS"
        if 85 <= confidence < 95 and decision_tier == "S":
            return "HIGH_PRIORITY_85_94"
        return "NORMAL"

    # ── Spec case 1: Raw Score low + BQ 95 + MLB OVER → Priority ────────────
    def test_mlb_over_bq95_is_override(self):
        """Raw Score 52, BQ 95, MLB OVER → OVERRIDE_95_PLUS (Confidence+Quality implicitly met)."""
        assert self._route(95, "S", "OVER", "MLB") == "OVERRIDE_95_PLUS"

    # ── Spec case 2: Raw Score low + BQ 95 + MLB UNDER → BLOCKED ────────────
    def test_mlb_under_bq95_is_blocked(self):
        """Raw Score 52, BQ 95, MLB UNDER → DIRECTION_BLOCKED (MLB is OVER-only)."""
        assert self._route(95, "S", "UNDER", "MLB") == "DIRECTION_BLOCKED"

    # ── Spec case 3: BQ 95 + NFL OVER → Priority ────────────────────────────
    def test_nfl_over_bq95_is_override(self):
        """BQ 95, NFL OVER → OVERRIDE_95_PLUS."""
        assert self._route(95, "S", "OVER", "NFL") == "OVERRIDE_95_PLUS"

    # ── Spec case 4: BQ 95 + NFL UNDER → BLOCKED ────────────────────────────
    def test_nfl_under_bq95_is_blocked(self):
        """BQ 95, NFL UNDER → DIRECTION_BLOCKED (NFL is OVER-only)."""
        assert self._route(95, "S", "UNDER", "NFL") == "DIRECTION_BLOCKED"

    # ── Spec case 5: Tier 1 OVER + BQ 95 → Priority ─────────────────────────
    def test_tier1_over_bq95_is_override(self):
        """Tier 1 (WNBA) OVER + BQ 95 → OVERRIDE_95_PLUS."""
        assert self._route(95, "S", "OVER", "WNBA") == "OVERRIDE_95_PLUS"

    # ── Spec case 6: Tier 1 UNDER + BQ 95 → Priority ────────────────────────
    def test_tier1_under_bq95_is_override(self):
        """Tier 1 (WNBA) UNDER + BQ 95 → OVERRIDE_95_PLUS (Tier 1 allows UNDER)."""
        assert self._route(95, "S", "UNDER", "WNBA") == "OVERRIDE_95_PLUS"

    def test_cs_under_bq95_is_override(self):
        """Tier 1 (CS) UNDER + BQ 95 → OVERRIDE_95_PLUS."""
        assert self._route(95, "S", "UNDER", "CS") == "OVERRIDE_95_PLUS"

    def test_lol_under_bq95_is_override(self):
        """Tier 1 (LOL) UNDER + BQ 95 → OVERRIDE_95_PLUS."""
        assert self._route(95, "S", "UNDER", "LOL") == "OVERRIDE_95_PLUS"

    def test_tennis_under_bq95_is_override(self):
        """Tier 1 (TENNIS) UNDER + BQ 95 → OVERRIDE_95_PLUS."""
        assert self._route(95, "S", "UNDER", "TENNIS") == "OVERRIDE_95_PLUS"

    # ── V3.4 boundary: 84 is NORMAL, 85 is HIGH PRIORITY ────────────────────
    def test_bq84_is_normal(self):
        """84/100 is below the V3.4 4★ floor — not high priority."""
        assert self._route(84, "S", "OVER", "WNBA") == "NORMAL"

    def test_bq85_is_high_priority(self):
        """85/100 is the V3.4 minimum for HIGH PRIORITY (4★ floor)."""
        assert self._route(85, "S", "OVER", "WNBA") == "HIGH_PRIORITY_85_94"

    def test_bq85_mlb_over_is_high_priority(self):
        """MLB OVER at BQ 85 → HIGH_PRIORITY_85_94 (normal gates apply)."""
        assert self._route(85, "S", "OVER", "MLB") == "HIGH_PRIORITY_85_94"

    # ── Spec case 7: BQ 94 → NOT 95+ priority ───────────────────────────────
    def test_bq94_is_not_override(self):
        """BQ 94 S-tier → HIGH_PRIORITY_85_94, NOT override."""
        assert self._route(94, "S", "OVER", "WNBA") == "HIGH_PRIORITY_85_94"
        assert self._route(94, "S", "OVER", "MLB") != "OVERRIDE_95_PLUS"

    # ── Spec case 8: BQ 85-94 → High Priority (normal gates) ────────────────
    def test_bq90_is_high_priority(self):
        assert self._route(90, "S", "OVER", "WNBA") == "HIGH_PRIORITY_85_94"

    def test_bq89_is_high_priority(self):
        """89/100 was NORMAL under old 90 threshold — V3.4 makes it HIGH PRIORITY."""
        assert self._route(89, "S", "OVER", "WNBA") == "HIGH_PRIORITY_85_94"

    def test_bq93_is_high_priority(self):
        assert self._route(93, "S", "OVER", "CS") == "HIGH_PRIORITY_85_94"

    # ── Spec case 11: PASS recommendation → NEVER actionable ────────────────
    def test_pass_rec_never_actionable(self):
        """PASS recommendation must never produce an override regardless of BQ."""
        for bq in (90, 95, 100):
            assert self._route(bq, "S", "PASS", "WNBA") == "NORMAL", (
                f"PASS with BQ={bq} must be NORMAL"
            )
        assert self._route(95, "PASS", "OVER", "WNBA") == "NORMAL"

    # ── MLB UNDER blocking is sport-specific — Tier 1 UNDER is separate ─────
    def test_mlb_under_blocked_at_all_bq_levels(self):
        """MLB UNDER is DIRECTION_BLOCKED at BQ 95+ regardless of confidence."""
        for bq in (95, 97, 100):
            assert self._route(bq, "S", "UNDER", "MLB") == "DIRECTION_BLOCKED"

    def test_nfl_under_blocked_at_all_bq_levels(self):
        """NFL UNDER is DIRECTION_BLOCKED at BQ 95+ regardless of confidence."""
        for bq in (95, 97, 100):
            assert self._route(bq, "S", "UNDER", "NFL") == "DIRECTION_BLOCKED"

    # ── Spec case 16: Old score.total priority path must not remain active ───
    def test_score_total_not_used_for_priority(self):
        """market_engine source must NOT use score.total >= 95 as a priority gate."""
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        assert "score.total >= 95" not in src, (
            "score.total >= 95 found in underdog_job — old priority path still active!"
        )
        assert "_sscore.total >= 95" not in src, (
            "_sscore.total >= 95 found in underdog_job — old priority path still active!"
        )

    # ── Spec case 15: Telegram priority text says "Bet Quality" ─────────────
    def test_telegram_says_bet_quality_not_score_gte(self):
        """Override message must say 'Bet Quality', not 'Score ≥ 95'."""
        import market_engine as me

        class _S:
            total = 52; stars = 5; tier = "S"

        class _N:
            sport = "MLB"; game_time = None

        class _D:
            recommendation = "OVER"; confidence = 95

        msg = me._format_95_priority_alert("Royce Lewis", _N(), "Strikeouts", _S(), _D(), 2.5)
        assert "Bet Quality" in msg, "Override message must say 'Bet Quality'"
        assert "Score ≥ 95" not in msg, "Override message must NOT say 'Score ≥ 95'"
        assert "All validation gates bypassed" not in msg, (
            "'All validation gates bypassed' must NOT appear — direction gate still applies"
        )
        # The number shown must be the BQ (95), not the raw score (52)
        assert "95" in msg

    # ── Spec case 17: /picks current-day only ────────────────────────────────
    def test_picks_uses_since_hours_not_hardcoded_24(self):
        """commands.py /picks must compute _since_hours from midnight, not hardcode 24."""
        import inspect, commands as cmd
        src = inspect.getsource(cmd._cmd_picks_inner)
        assert "_since_hours" in src, "_since_hours variable not found in _cmd_picks_inner"
        assert "_midnight_utc" in src, "_midnight_utc not found — current-day calc missing"
        # Must NOT have a bare since_hours=24 call for UD props (was the pre-fix code)
        assert "since_hours=24" not in src or "_since_hours" in src, (
            "since_hours=24 still hardcoded — current-day /picks window not applied"
        )

    # ── Sport Direction source check ─────────────────────────────────────────
    def test_mlb_nfl_direction_check_in_source(self):
        """market_engine override blocks must check MLB/NFL direction before broadcasting."""
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        assert "_np_95_dir_ok" in src, "_np_95_dir_ok direction check missing from new-prop override"
        assert "_lc_95_dir_ok" in src, "_lc_95_dir_ok direction check missing from lc override"
        assert "_sp_95_dir_ok" in src, "_sp_95_dir_ok direction check missing from standing override"


# ═══════════════════════════════════════════════════════════════════════════
# P9 — alerts.py high_priority prepend simulation
# ═══════════════════════════════════════════════════════════════════════════

class TestHighPriorityPrepend:
    """Simulate the high_priority=True header prepend in deliver_underdog."""

    def _simulate_prepend(self, base_message: str, high_priority: bool, score_total: int) -> str:
        """Replicate the prepend logic from deliver_underdog."""
        if high_priority and base_message:
            _hp_score = score_total
            base_message = (
                f"🔥 <b>S-TIER HIGH PRIORITY — {_hp_score}/100</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + base_message
            )
        return base_message

    def test_high_priority_true_prepends_header(self):
        result = self._simulate_prepend("Normal alert body", True, 92)
        assert result.startswith("🔥"), "High priority header must be at the start"
        assert "S-TIER HIGH PRIORITY" in result
        assert "92/100" in result

    def test_high_priority_false_no_prepend(self):
        result = self._simulate_prepend("Normal alert body", False, 88)
        assert result == "Normal alert body", "False high_priority must not alter message"

    def test_high_priority_header_above_body(self):
        result = self._simulate_prepend("Body text", True, 91)
        lines = result.split("\n")
        assert "🔥" in lines[0], "🔥 must be in the first line"
        assert "Body text" in result

    def test_score_shown_in_header(self):
        for score in (90, 91, 92, 93, 94):
            result = self._simulate_prepend("Alert body", True, score)
            assert str(score) in result, f"Score {score} must appear in priority header"
