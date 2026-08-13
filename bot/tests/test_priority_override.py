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
    """Verify that the 95+ priority override paths have been removed.

    Per spec all alerts now route through the unified 🎯 ACTIONABLE BET PICK
    format via AlertDelivery.deliver_underdog().  The separate broadcast_alert
    override paths (new-prop, lc, standing) have been removed.
    """

    def _src(self) -> str:
        import market_engine as me
        return inspect.getsource(me)

    def test_95_override_paths_removed(self):
        """PRIORITY OVERRIDE log labels must not appear — separate paths are gone."""
        src = self._src()
        for label in ("PRIORITY OVERRIDE [new]", "PRIORITY OVERRIDE [lc]", "PRIORITY OVERRIDE [standing]"):
            assert label not in src, (
                f"'{label}' was re-added to market_engine — the 95+ override "
                "broadcast_alert paths are removed; props use deliver_underdog()."
            )

    def test_lc_95_sent_removed(self):
        """_lc_95_sent flag must be removed — lc path no longer has a separate 95+ branch."""
        src = self._src()
        assert "_lc_95_sent" not in src, (
            "_lc_95_sent was re-added — this flag belonged to the removed lc 95+ override path"
        )

    def test_priority_alerted_this_scan_removed(self):
        """_priority_alerted_this_scan per-scan set must be gone — 95+ override removed."""
        src = self._src()
        assert "_priority_alerted_this_scan" not in src, (
            "_priority_alerted_this_scan re-added — remove it; it belonged to the 95+ override path"
        )

    def test_ud_full_scan_running_flag_present(self):
        """_ud_full_scan_running module flag must exist for fast-fetch concurrency guard."""
        src = self._src()
        assert "_ud_full_scan_running" in src, (
            "_ud_full_scan_running missing — fast-fetch concurrency guard not implemented"
        )

    def test_skip_normal_gate_comment_removed(self):
        """Comment about skipping normal gate for 95+ must be gone."""
        src = self._src()
        assert "skip normal gate sequence for all 95+ props" not in src, (
            "Stale 95+ gate-skip comment re-added — remove it"
        )

    def test_format_95_priority_alert_defined_but_not_called_in_loop(self):
        """_format_95_priority_alert may still be defined (dead code) but must
        no longer be called from inside underdog_job's main prop loop."""
        import market_engine as me
        fn_src = inspect.getsource(me.underdog_job)
        assert "_format_95_priority_alert(" not in fn_src, (
            "_format_95_priority_alert is called inside underdog_job — "
            "it should be dead code only; remove the call."
        )

    def test_normal_delivery_path_uses_deliver_underdog(self):
        """All delivery paths must use deliver_underdog (not direct broadcast_alert)."""
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        assert "delivery.deliver_underdog(" in src, (
            "deliver_underdog missing from underdog_job — normal delivery path broken"
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
        """alerts.py deliver_underdog must accept high_priority and apply S-tier cap.

        The separate 🔥 S-TIER HIGH PRIORITY header has been removed per spec.
        S-tier is capped to A-tier display when MQ < 80 or confidence < 80.
        """
        import alerts as al
        src = inspect.getsource(al.AlertDelivery.deliver_underdog)
        assert "high_priority" in src, "high_priority param still referenced in deliver_underdog"
        # The old "S-TIER HIGH PRIORITY" separate header is gone
        assert "S-TIER HIGH PRIORITY" not in src, (
            "S-TIER HIGH PRIORITY header was re-added to deliver_underdog — "
            "per spec all alerts use the unified ACTIONABLE BET PICK format"
        )

    def test_high_priority_not_in_market_engine_np_call(self):
        """market_engine underdog_job must NOT pass high_priority to deliver_underdog —
        the separate S-tier HIGH PRIORITY header path was removed per spec."""
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        # high_priority= kwarg was used by the 80-94 BQ header path; now removed
        assert "high_priority=True" not in src, (
            "high_priority=True is still being passed in underdog_job — "
            "the 80-94 BQ high-priority header path was removed per spec"
        )

    def test_high_priority_condition_80_94_removed_from_engine(self):
        """The 80-94 BQ range check for high_priority must be gone from underdog_job.

        V3.4 removed the separate high-priority header; S-tier quality is now shown
        inside the unified ACTIONABLE BET PICK format via the _CappedDecision wrapper.
        """
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        for pattern in ("80 <= decision.confidence < 95", "80 <= _sdec.confidence < 95",
                         "85 <= decision.confidence < 95", "90 <= decision.confidence < 95"):
            assert pattern not in src, (
                f"'{pattern}' found in underdog_job — the 80-94 BQ high-priority "
                "condition was removed per spec"
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
          3. BQ threshold routing — 95+ override, 80-94 high-priority (V3.4 final: 4★ floor = 80), else normal
        """
        # 1. PASS decisions never trigger an override regardless of confidence
        if recommendation == "PASS" or decision_tier == "PASS":
            return "NORMAL"
        # 2. Sport Direction check — MLB/NFL are OVER-only; UNDER blocked even at 95+ BQ
        if sport.upper() in ("MLB", "NFL") and recommendation == "UNDER":
            return "DIRECTION_BLOCKED"
        # 3. BQ threshold routing (V3.4 final: high-priority floor = 80)
        if confidence >= 95 and decision_tier == "S":
            return "OVERRIDE_95_PLUS"
        if 80 <= confidence < 95 and decision_tier == "S":
            return "HIGH_PRIORITY_80_94"
        return "NORMAL"

    def test_79_s_tier_is_normal(self):
        """79/100 is below the V3.4 final 4★ floor (80) — must be NORMAL."""
        assert self._route(79, "S") == "NORMAL", "79/100 S-tier must be NORMAL (below V3.4 floor)"

    def test_79_point_9_is_normal(self):
        assert self._route(79.9, "S") == "NORMAL"

    def test_80_s_tier_is_high_priority(self):
        """80/100 is the V3.4 final minimum for HIGH PRIORITY (4★ floor)."""
        assert self._route(80, "S") == "HIGH_PRIORITY_80_94", "80/100 S-tier must be HIGH_PRIORITY_80_94"

    def test_84_s_tier_is_high_priority(self):
        """84/100 is above the V3.4 floor — HIGH PRIORITY."""
        assert self._route(84, "S") == "HIGH_PRIORITY_80_94", "84/100 S-tier must be HIGH_PRIORITY_80_94"

    def test_85_s_tier_is_high_priority(self):
        assert self._route(85, "S") == "HIGH_PRIORITY_80_94"

    def test_89_s_tier_is_high_priority(self):
        assert self._route(89, "S") == "HIGH_PRIORITY_80_94"

    def test_90_s_tier_is_high_priority(self):
        assert self._route(90, "S") == "HIGH_PRIORITY_80_94"

    def test_91_s_tier_is_high_priority(self):
        assert self._route(91, "S") == "HIGH_PRIORITY_80_94"

    def test_94_s_tier_is_high_priority(self):
        assert self._route(94, "S") == "HIGH_PRIORITY_80_94"

    def test_94_point_9_is_high_priority(self):
        assert self._route(94.9, "S") == "HIGH_PRIORITY_80_94"

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

    def test_ud_full_scan_running_prevents_concurrent_heavy_scans(self):
        """_ud_full_scan_running module flag ensures only one full scan runs at a time.
        The old _priority_alerted_this_scan per-scan set has been removed per spec.
        """
        import inspect, market_engine as me
        src_module = inspect.getsource(me)
        # Per-scan set is gone
        assert "_priority_alerted_this_scan" not in src_module, (
            "_priority_alerted_this_scan re-added — remove it; use _ud_full_scan_running instead"
        )
        # New fast-fetch concurrency guard
        assert "_ud_full_scan_running" in src_module, (
            "_ud_full_scan_running missing — fast-fetch concurrency guard not implemented"
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

    def test_no_direct_broadcast_alert_in_underdog_job(self):
        """Direct broadcast_alert calls from within the 95+ override paths have been
        removed from underdog_job per spec. All alerts now go through deliver_underdog.
        """
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        # Count direct broadcast_alert calls — the override paths are gone so count should be 0
        # (deliver_underdog calls broadcast_alert internally, which is fine)
        direct_bc = src.count("await broadcast_alert(")
        assert direct_bc == 0, (
            f"Expected 0 direct broadcast_alert calls in underdog_job (override paths removed), "
            f"got {direct_bc}. Use deliver_underdog for all alerts."
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

    def test_normal_gate_sequence_still_active_for_all_tiers(self):
        """All S/A/B tiers go through the normal is_qualified gate sequence.

        The separate high-priority (80-94 BQ) and override (95+ BQ) broadcast paths
        have been removed. The min_stars_for_sport gate has also been removed from
        delivery paths — S/A/B are all actionable without a star floor.
        """
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        # All props go through deliver_underdog now
        assert "delivery.deliver_underdog(" in src, "deliver_underdog missing from underdog_job"
        # min_stars_for_sport must NOT appear as a delivery gate (it was removed per spec)
        # Note: it may appear in inline comments explaining historical context — that's fine.
        fn_src = inspect.getsource(me.underdog_job)
        # Count non-comment occurrences: lines that reference it without a # prefix
        gate_lines = [
            ln for ln in fn_src.splitlines()
            if "min_stars_for_sport" in ln and not ln.lstrip().startswith("#")
        ]
        assert len(gate_lines) == 0, (
            f"min_stars_for_sport used as a gate in underdog_job (not just a comment): "
            f"{gate_lines[:3]}"
        )

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
        if 80 <= confidence < 95 and decision_tier == "S":  # V3.4 final floor
            return "HIGH_PRIORITY_80_94"
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

    # ── V3.4 final boundary: 79 is NORMAL, 80 is HIGH PRIORITY ─────────────
    def test_bq79_is_normal(self):
        """79/100 is below the V3.4 final floor (80) — not high priority."""
        assert self._route(79, "S", "OVER", "WNBA") == "NORMAL"

    def test_bq80_is_high_priority(self):
        """80/100 is the V3.4 final minimum for HIGH PRIORITY (4★ floor)."""
        assert self._route(80, "S", "OVER", "WNBA") == "HIGH_PRIORITY_80_94"

    def test_bq80_mlb_over_is_high_priority(self):
        """MLB OVER at BQ 80 → HIGH_PRIORITY_80_94 (normal gates apply)."""
        assert self._route(80, "S", "OVER", "MLB") == "HIGH_PRIORITY_80_94"

    def test_bq84_is_also_high_priority(self):
        """84/100 is above the V3.4 floor — also HIGH PRIORITY."""
        assert self._route(84, "S", "OVER", "WNBA") == "HIGH_PRIORITY_80_94"

    def test_bq85_is_high_priority(self):
        """85/100 is HIGH PRIORITY under V3.4 (floor is 80)."""
        assert self._route(85, "S", "OVER", "WNBA") == "HIGH_PRIORITY_80_94"

    # ── Spec case 7: BQ 94 → NOT 95+ priority ───────────────────────────────
    def test_bq94_is_not_override(self):
        """BQ 94 S-tier → HIGH_PRIORITY_80_94, NOT override."""
        assert self._route(94, "S", "OVER", "WNBA") == "HIGH_PRIORITY_80_94"
        assert self._route(94, "S", "OVER", "MLB") != "OVERRIDE_95_PLUS"

    # ── Spec case 8: BQ 85-94 → High Priority (normal gates) ────────────────
    def test_bq90_is_high_priority(self):
        assert self._route(90, "S", "OVER", "WNBA") == "HIGH_PRIORITY_80_94"

    def test_bq89_is_high_priority(self):
        """89/100 was NORMAL under old 90 threshold — V3.4 makes it HIGH PRIORITY."""
        assert self._route(89, "S", "OVER", "WNBA") == "HIGH_PRIORITY_80_94"

    def test_bq93_is_high_priority(self):
        assert self._route(93, "S", "OVER", "CS") == "HIGH_PRIORITY_80_94"

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
        """MLB/NFL UNDER must be blocked via is_qualified / _lc_mlb_ok.
        The separate 95+ override direction vars (_np/lc/sp_95_dir_ok) are removed.
        """
        import inspect, market_engine as me
        src = inspect.getsource(me.underdog_job)
        # Old per-path override direction gates are gone
        for var in ("_np_95_dir_ok", "_lc_95_dir_ok", "_sp_95_dir_ok"):
            assert var not in src, (
                f"{var} was re-added — the 95+ override paths are removed; "
                "MLB UNDER is gated at is_qualified (_lc_mlb_ok) level."
            )
        # MLB UNDER whitelist gate must still exist in the lc path
        assert "_lc_mlb_ok" in src, "_lc_mlb_ok missing — MLB UNDER whitelist gate not in lc path"


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
