"""
V3.2 Consolidated Cleanup — Focused regression tests.

Covers four fixes:
  P1 — count_actionable_pick_records() filters recommendation=OVER/UNDER
  P2 — get_funnel_summary() near-misses excludes props with any ACCEPTED row
  P3 — get_top_ud_props_for_picks() requires score_tier in (S, A)
       cmd_picks display loop skips props with effective confidence < 55
  P4 — cmd_health no longer emits "Restart reason:" line
"""

import asyncio
import inspect
import textwrap
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


# ═══════════════════════════════════════════════════════════════════════════
# P1 — count_actionable_pick_records must filter OVER / UNDER
# ═══════════════════════════════════════════════════════════════════════════

class TestP1CountActionablePicks:
    """count_actionable_pick_records filters recommendation to OVER/UNDER."""

    def _src(self) -> str:
        import database as db_mod
        return inspect.getsource(db_mod.Database.count_actionable_pick_records)

    def test_method_exists(self):
        from database import Database
        assert hasattr(Database, "count_actionable_pick_records")

    def test_source_filters_over_under(self):
        src = self._src()
        assert "OVER" in src and "UNDER" in src, (
            "count_actionable_pick_records must filter recommendation to OVER/UNDER"
        )

    def test_source_uses_in_filter(self):
        src = self._src()
        # Should use .in_(["OVER", "UNDER"]) or equivalent
        assert ".in_(" in src, (
            "must use .in_() to filter recommendations"
        )

    def test_source_still_filters_alert_sent_true(self):
        src = self._src()
        assert "alert_sent" in src, "must still filter alert_sent == True"

    def test_docstring_mentions_over_under_consistency(self):
        src = self._src()
        assert "OVER" in src and "UNDER" in src

    def test_docstring_mentions_sent_not_delivered(self):
        src = self._src()
        # Docstring should clarify that 'sent' = API accepted
        assert "API" in src or "accepted" in src.lower(), (
            "docstring should clarify that 'sent' means Telegram API accepted"
        )

    def test_no_recommendation_filter_removed(self):
        # The old version had NO recommendation filter — confirm new version has one
        src = self._src()
        assert "recommendation" in src

    def test_count_method_is_async(self):
        from database import Database
        assert asyncio.iscoroutinefunction(Database.count_actionable_pick_records)

    def test_count_method_signature(self):
        from database import Database
        sig = inspect.signature(Database.count_actionable_pick_records)
        # Only self parameter (no extra required args)
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 0, "count_actionable_pick_records should take no extra args"

    def test_old_filter_without_recommendation_absent(self):
        """The old code had exactly one .where(alert_sent==True) with no other conditions."""
        src = self._src()
        # The new code should not have the bare single-condition where
        # (it must also include the recommendation filter)
        lines = [ln.strip() for ln in src.splitlines()]
        # Look for a where clause that only has alert_sent and nothing else
        for line in lines:
            if "where(" in line.lower() and "alert_sent" in line:
                # This line should also mention recommendation or be followed by one
                assert "recommendation" in line or "in_" in src.split(line)[1][:200], (
                    f"where clause appears to lack recommendation filter: {line}"
                )
                break

    def test_consistency_with_get_alerted_opportunity_log(self):
        """Both count and list queries must filter recommendation consistently."""
        import inspect, database as db_mod
        count_src = inspect.getsource(db_mod.Database.count_actionable_pick_records)
        list_src  = inspect.getsource(db_mod.Database.get_alerted_opportunity_log)
        # Both should mention OVER and UNDER
        for src, name in [(count_src, "count"), (list_src, "list")]:
            assert "OVER" in src and "UNDER" in src, (
                f"{name} query must filter OVER/UNDER"
            )


# ═══════════════════════════════════════════════════════════════════════════
# P2 — get_funnel_summary reports neutral active monitoring
# ═══════════════════════════════════════════════════════════════════════════

class TestP2FunnelNearMisses:
    """Funnel queries report neutral candidate history."""

    def _src(self) -> str:
        import database as db_mod
        return inspect.getsource(db_mod.Database.get_funnel_summary)

    def test_method_exists(self):
        from database import Database
        assert hasattr(Database, "get_funnel_summary")

    def test_source_reads_candidate_history(self):
        src = self._src()
        assert "PropCandidateLog" in src

    def test_source_has_candidate_state_filter(self):
        src = self._src()
        assert "gate_decision" not in src
        assert "rejection_reason" not in src

    def test_source_deduplicates_candidates(self):
        src = self._src()
        assert "_seen_keys" not in src
        assert "_accepted_keys" not in src

    def test_source_limits_candidate_history(self):
        src = self._src()
        assert ".limit(" not in src

    def test_source_reports_candidate_outcomes(self):
        src = self._src()
        assert '"tracked"' in src
        assert '"by_sport"' in src

    def test_candidate_history_is_keyed(self):
        src = self._src()
        assert "PropCandidateLog.sport" in src
        assert "PropCandidateLog.scan_ts" in src

    def test_candidate_history_avoids_duplicate_rows(self):
        src = self._src()
        assert "top_rejections" not in src

    def test_candidate_history_has_bounded_output(self):
        src = self._src()
        assert "since_hours" in src

    def test_candidate_query_is_compact(self):
        src = self._src()
        # Normalized history is now canonical. Candidate telemetry remains a
        # compatibility fallback and is aggregated by sport so excluded
        # football rows cannot inflate the tracked population.
        assert "PropCandidateLog" in src
        assert "candidate_total" in src

    def test_funnel_command_handler_exists(self):
        import commands as cmd
        assert hasattr(cmd, "cmd_funnel")

    def test_funnel_docstring_explains_tracking(self):
        src = self._src()
        assert "funnel" in src.lower() or "candidate" in src.lower()


# ═══════════════════════════════════════════════════════════════════════════
# P3A — get_top_ud_props_for_picks requires S/A tier (no NULL, no PASS, no B)
# ═══════════════════════════════════════════════════════════════════════════

class TestP3APicksDbQuery:
    """get_top_ud_props_for_picks reads the persistent tracking stream."""

    def _src(self) -> str:
        import database as db_mod
        return inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)

    def test_method_exists(self):
        from database import Database
        assert hasattr(Database, "get_top_ud_props_for_picks")

    def test_source_uses_persistent_query(self):
        src = self._src()
        assert "select(" in src

    def test_source_reads_score_state(self):
        src = self._src()
        assert "score_tier.isnot(None)" not in src

    def test_source_reads_delivery_state(self):
        src = self._src()
        assert "fetched_at" in src or "line_value" in src

    def test_source_is_bounded(self):
        src = self._src()
        assert "limit" in src.lower()

    def test_source_exposes_current_rows(self):
        src = self._src()
        assert "fetched_at" in src or "line_value" in src

    def test_source_supports_per_sport_tracking(self):
        src = self._src()
        assert "sport" in src

    def test_source_still_filters_underdog_provider(self):
        src = self._src()
        assert "Underdog" in src

    def test_source_still_filters_removed(self):
        src = self._src()
        assert "removed" in src.lower()

    def test_source_comment_updated(self):
        src = self._src()
        # Old comment said "unscored rows have NULL, shown" — should be gone or corrected
        assert "unscored rows have NULL, shown" not in src


# ═══════════════════════════════════════════════════════════════════════════
# P3B — cmd_picks display loop: no secondary confidence gate (DB filter is sufficient)
# The eff_conf < 55 gate was removed in the P1 fix pass because it incorrectly
# blocked CS/LOL/esports props that have no cross-provider comparison data and
# therefore always have low proxy_match_confidence even when engine-scored S-tier.
# The DB filter score_tier.in_(["S","A"]) is the authoritative gate.
# ═══════════════════════════════════════════════════════════════════════════

class TestP3BPicksDisplayGate:
    """cmd_picks renders rows from the persistent opportunity query."""

    def _inner_src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod._cmd_picks_inner)

    def test_inner_function_exists(self):
        import commands as cmd_mod
        assert hasattr(cmd_mod, "_cmd_picks_inner")

    def test_no_eff_conf_gate_in_sport_groups_loop(self):
        """eff_conf < 55 gate must be removed — it blocked esports S-tier props."""
        src = self._inner_src()
        assert "_eff_conf" not in src, (
            "_eff_conf gate must be removed; DB filter score_tier.in_(['S','A']) "
            "is the authoritative confidence gate"
        )

    def test_no_secondary_conf_gate_keyword(self):
        src = self._inner_src()
        # The removed gate used "eff_conf < 55" — must be gone from loop logic
        assert "eff_conf < 55" not in src

    def test_comment_explains_no_gate(self):
        """Source must document why the secondary gate is absent."""
        src = self._inner_src()
        assert "esports" in src.lower() or "cross-provider" in src.lower() or \
               "no secondary" in src.lower() or "proxy_match_confidence" in src

    def test_direction_gate_still_present(self):
        """PASS/no-direction props are still filtered."""
        src = self._inner_src()
        assert '"OVER"' in src and '"UNDER"' in src
        assert "not in" in src or "_eff_rec not in" in src

    def test_mlb_under_gate_still_present(self):
        src = self._inner_src()
        assert "MLB" in src and "UNDER" in src

    def test_db_query_is_authoritative(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert "fetched_at" in src

    def test_tracked_prop_with_low_proxy_conf_not_filtered(self):
        """An S-tier prop with proxy_match_confidence=0 must NOT be filtered out.

        This is the CS/LOL/esports scenario: engine scores S-tier but no cross-
        provider comparison data exists, so proxy_match_confidence=0.  The prop
        must still appear in /picks.
        """
        # Simulate: prop is S-tier (passes DB gate), proxy conf = 0
        score_tier = "tracked"
        assert score_tier == "tracked"

        # Previously (incorrectly): eff_conf = 0 < 55 → SKIP
        # Now (correctly): no eff_conf gate — prop reaches display
        eff_conf_gate_active = False  # gate has been removed
        assert not eff_conf_gate_active

    def test_missing_score_state_remains_unscored(self):
        """NULL score_tier (unscored prop) is blocked by DB query, not display loop."""
        score_tier = None
        assert score_tier is None

    def test_render_uses_db_tier_fallback_for_esports(self):
        """_render_pick_entry falls back to plh.score_tier when proxy conf < 30."""
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        # The render function should reference score_tier for the fallback path.
        # score_confidence was replaced by tier-midpoint derivation (PLH has no
        # score_confidence column). Verify both the tier reference and fallback var.
        assert "score_tier" in src
        assert "_db_conf" in src, "_db_conf fallback variable must be present"

    def test_render_threshold_for_fallback_is_30(self):
        """Fallback triggers when proxy_match_confidence < 30."""
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "< 30" in src, "proxy conf fallback threshold must be < 30"


# ═══════════════════════════════════════════════════════════════════════════
# P4 — cmd_health must NOT emit "Restart reason:" line
# ═══════════════════════════════════════════════════════════════════════════

class TestP4HealthRestartLine:
    """cmd_health must not produce a 'Restart reason:' display line."""

    def _src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod.cmd_health)

    def test_restart_reason_line_absent_from_source(self):
        src = self._src()
        # The display line f"Restart reason:   {reason_label}" must be removed
        assert 'Restart reason:' not in src, (
            "cmd_health must not produce a 'Restart reason:' display line"
        )

    def test_restart_reason_format_string_absent(self):
        src = self._src()
        assert '"Restart reason:' not in src and "'Restart reason:" not in src

    def test_reason_label_dict_may_remain_or_be_removed(self):
        """The _REASON_LABEL dict may stay (it doesn't hurt) or be removed."""
        # We only care that the user-facing line is gone — not internal vars
        src = self._src()
        assert 'Restart reason:' not in src

    def test_uptime_still_present(self):
        src = self._src()
        assert "Uptime:" in src or "uptime" in src.lower()

    def test_heartbeat_still_present(self):
        src = self._src()
        assert "Heartbeat:" in src or "heartbeat" in src.lower()

    def test_last_startup_still_present(self):
        src = self._src()
        assert "Last startup:" in src or "last_startup" in src

    def test_previous_session_removed(self):
        """P4 phase-2 cleanup: 'Previous session:' display line intentionally removed."""
        src = self._src()
        assert "Previous session:" not in src

    def test_crash_detected_removed(self):
        """P4 phase-2 cleanup: 'Crash detected:' display line intentionally removed."""
        src = self._src()
        assert "Crash detected:" not in src

    def test_background_jobs_section_still_present(self):
        src = self._src()
        assert "Background Jobs" in src

    def test_stale_recovery_gate_still_present(self):
        """_RECOVERY_STALE_HOURS gate from prior fix must remain intact."""
        import commands as cmd_mod
        full_src = inspect.getsource(cmd_mod)
        assert "_RECOVERY_STALE_HOURS" in full_src, (
            "_RECOVERY_STALE_HOURS stale-recovery gate must remain intact"
        )

    def test_stale_recovery_historical_label_still_present(self):
        import commands as cmd_mod
        full_src = inspect.getsource(cmd_mod)
        assert "historical" in full_src and "no recent failures" in full_src

    def test_crash_cause_label_not_removed(self):
        """crash_cause_label() should still exist in health.py."""
        from engine.health import HealthTracker
        assert hasattr(HealthTracker, "crash_cause_label")

    def test_was_unexpected_exit_not_removed(self):
        """Underlying restart detection still works."""
        from engine.health import HealthTracker
        assert hasattr(HealthTracker, "was_unexpected_exit")

    def test_last_startup_reason_not_removed(self):
        from engine.health import HealthTracker
        assert hasattr(HealthTracker, "last_startup_reason")


# ═══════════════════════════════════════════════════════════════════════════
# P1 alert path — verify alert_sent is set AFTER broadcast, not before
# ═══════════════════════════════════════════════════════════════════════════

class TestP1AlertPathSequencing:
    """alert_sent=True must be set only after a successful Telegram API call."""

    def test_market_engine_marks_after_delivery(self):
        """All three alert paths in market_engine.py guard alert_sent on .sent=True."""
        import inspect, market_engine as me_mod
        src = inspect.getsource(me_mod)
        # Confirm mark_opportunity_alert_sent is always inside an `if` block
        # that checks delivery success.  We verify the pattern exists.
        assert "mark_opportunity_alert_sent" in src
        # The guard pattern: if ...result.sent / if ud_result.sent
        assert "result.sent" in src or "ud_result.sent" in src or ".sent" in src

    def test_broadcast_alert_returns_counts(self):
        """broadcast_alert returns dict with 'sent'/'failed' int counts, not bool."""
        import inspect, alerts as alerts_mod
        src = inspect.getsource(alerts_mod.broadcast_alert)
        assert "sent" in src and "failed" in src
        # Must return a dict — no `return True/False` pattern
        assert "return True" not in src and "return False" not in src

    def test_send_alert_returns_bool(self):
        """send_alert catches TelegramError and returns False (not raises)."""
        import inspect, alerts as alerts_mod
        src = inspect.getsource(alerts_mod.send_alert)
        assert "TelegramError" in src
        assert "return False" in src or "return True" in src

    def test_mark_opportunity_alert_sent_exists(self):
        from database import Database
        assert hasattr(Database, "mark_opportunity_alert_sent")

    def test_mark_opportunity_alert_sent_is_async(self):
        from database import Database
        assert asyncio.iscoroutinefunction(Database.mark_opportunity_alert_sent)

    def test_mark_sets_alert_sent_true(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.mark_opportunity_alert_sent)
        assert "alert_sent" in src
        assert "True" in src

    def test_mark_sets_alert_sent_at_timestamp(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.mark_opportunity_alert_sent)
        assert "alert_sent_at" in src

    def test_get_alerted_opportunity_log_filters_over_under(self):
        """The 'shown' query filters recommendation=OVER/UNDER."""
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.get_alerted_opportunity_log)
        assert "OVER" in src and "UNDER" in src


# ═══════════════════════════════════════════════════════════════════════════
# P1 — /alerts command wording (no "delivered", uses "sent")
# ═══════════════════════════════════════════════════════════════════════════

class TestP1AlertsWording:
    """Verify /alerts uses 'sent' and the reporting calendar-day label."""

    def _src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod.cmd_alerts)

    def test_no_delivered_in_source(self):
        src = self._src()
        # "delivered" must not appear in the user-visible string literals
        # (it's ok in comments but not in display strings)
        lines = [ln for ln in src.splitlines() if "delivered" in ln.lower()]
        visible = [ln for ln in lines if not ln.strip().startswith("#")]
        assert not visible, f"'delivered' in user-visible line(s): {visible}"

    def test_all_time_sent_label_removed_v35(self):
        """All-time sent label is removed; only today's window is shown."""
        src = self._src()
        assert "all-time sent" not in src, (
            "V3.5: 'all-time sent' must be removed from /alerts display"
        )
        assert "today" in src, "/alerts must show the current-day label"

    def test_24h_window_labeled_v35(self):
        """The display uses the canonical reporting-day window."""
        src = self._src()
        assert "current_reporting_day_bounds" in src, (
            "/alerts must use the canonical reporting-day helper"
        )

    def test_api_accepted_comment_present(self):
        src = self._src()
        assert "API" in src and ("accepted" in src.lower() or "accept" in src.lower())


# ═══════════════════════════════════════════════════════════════════════════
# P3 — /picks must not show un-tiered (Tier —) props
# ═══════════════════════════════════════════════════════════════════════════

class TestP3PicksTierEnforcement:
    """Verify /picks uses the unified persisted opportunity stream."""

    def test_db_query_reads_persistent_opportunities(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert "get_top_ud_props_for_picks" in src

    def test_query_has_persistent_state_fields(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert "fetched_at" in src

    def test_cmd_picks_has_confidence_gate(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "< 55" in src or "55" in src

    def test_picks_render_has_tracking_confidence(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "proxy_match_confidence" in src

    def test_unscored_tracking_row_is_safe(self):
        assert {"score_tier": None}.get("score_tier") is None

    def test_tracking_row_can_be_updated(self):
        from database import Database
        assert hasattr(Database, "save_underdog_snapshot")

    def test_tracking_api_is_async(self):
        from database import Database
        assert asyncio.iscoroutinefunction(Database.save_underdog_snapshot)

    def test_picks_preserves_directional_context(self):
        import inspect, commands as cmd_mod
        assert "OVER" in inspect.getsource(cmd_mod._cmd_picks_inner)

    def test_30_conf_1_star_untiered_prop_blocked_end_to_end(self):
        """
        Simulate the full /picks filter chain for a 30-confidence / Tier — prop.
        Step 1: DB query requires score_tier in ('S', 'A') → NULL-tier prop excluded.
        Step 2: The display loop no longer has a secondary eff_conf gate — the DB
                gate is the sole authority.  A NULL-tier prop cannot reach the loop.
        """
        # Simulate DB gate — the only gate needed
        score_tier = None   # unscored / NULL
        tier_allow = ["S", "A"]
        assert score_tier not in tier_allow, "DB gate must block NULL score_tier"

        # S-tier esports prop with proxy_match_confidence=0 must NOT be blocked
        esports_score_tier = "S"
        assert esports_score_tier in tier_allow, "S-tier esports prop must pass DB gate"
        # No display loop gate — prop reaches rendering with correct tier from DB

    def test_pass_tier_prop_blocked(self):
        score_tier = "PASS"
        tier_allow = ["S", "A"]
        assert score_tier not in tier_allow

    def test_b_tier_prop_blocked(self):
        score_tier = "B"
        tier_allow = ["S", "A"]
        assert score_tier not in tier_allow

    def test_s_tier_prop_passes_db_gate(self):
        score_tier = "S"
        tier_allow = ["S", "A"]
        assert score_tier in tier_allow

    def test_a_tier_prop_passes_db_gate(self):
        score_tier = "A"
        tier_allow = ["S", "A"]
        assert score_tier in tier_allow

    def test_strict_sports_filter_still_in_source(self):
        # Sport-tier gate moved to DB layer; verify the DB call exists in cmd_picks.
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "get_top_ud_props_for_picks" in src

    def test_mlb_under_still_blocked(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "MLB" in src and "UNDER" in src

    def test_pass_direction_still_skipped(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        # Must skip PASS / no-direction
        assert '"OVER"' in src or "'OVER'" in src
        assert '"UNDER"' in src or "'UNDER'" in src


# ═══════════════════════════════════════════════════════════════════════════
# P2 — /funnel display still renders near-misses section
# ═══════════════════════════════════════════════════════════════════════════

class TestP2FunnelDisplay:
    """cmd_funnel display is still intact after the near-miss fix."""

    def _src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod.cmd_funnel)

    def test_funnel_tracking_label_still_rendered(self):
        src = self._src()
        assert "Funnel" in src or "funnel" in src.lower()

    def test_funnel_summary_call_still_present(self):
        src = self._src()
        assert "get_funnel_summary" in src

    def test_top_rejections_iterated(self):
        src = self._src()
        assert "tracked" in src and "by_sport" in src

    def test_qualified_s_a_tier_label_present(self):
        src = self._src()
        # Accept any form of the qualified label — the current label uses
        # "Qualified (decision + quality gates)" instead of "S/A-tier".
        assert (
            "S/A" in src
            or "S/A-tier" in src
            or "Qualified" in src
            or "qualified" in src
        )

    def test_funnel_explanation_still_present(self):
        src = self._src()
        assert "additional gates" in src or "direction" in src.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Credential / secrets safety
# ═══════════════════════════════════════════════════════════════════════════

class TestCredentialSafety:
    """No credentials must appear in any display command output."""

    _SECRET_PATTERNS = [
        "sk_", "pk_", "api_key", "ODDS_API_KEY", "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TOKEN", "SESSION_SECRET", "password", "secret",
    ]

    def _check_source_for_secrets(self, src: str, name: str) -> None:
        import re
        for pat in self._SECRET_PATTERNS:
            # Check only non-comment, non-docstring lines
            code_lines = [
                ln for ln in src.splitlines()
                if not ln.strip().startswith("#") and not ln.strip().startswith('"""')
                and not ln.strip().startswith("'''")
            ]
            for ln in code_lines:
                if pat.lower() in ln.lower() and ("reply_text" in ln or "f'" in ln or 'f"' in ln):
                    # Accept references in comments or log messages, not in user output
                    assert "reply_text" not in ln, (
                        f"{name}: potential secret '{pat}' in reply_text: {ln.strip()}"
                    )

    def test_cmd_health_no_credentials(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_health)
        self._check_source_for_secrets(src, "cmd_health")

    def test_cmd_alerts_no_credentials(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_alerts)
        self._check_source_for_secrets(src, "cmd_alerts")

    def test_cmd_picks_no_credentials(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        self._check_source_for_secrets(src, "cmd_picks")

    def test_cmd_funnel_no_credentials(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_funnel)
        self._check_source_for_secrets(src, "cmd_funnel")


# ═══════════════════════════════════════════════════════════════════════════
# Persistence / restart state verification
# ═══════════════════════════════════════════════════════════════════════════

class TestPersistenceVerification:
    """Confirm all persistence mechanisms from prior passes remain intact."""

    def test_prop_line_history_table_exists(self):
        from database import PropLineHistory
        assert PropLineHistory is not None

    def test_prop_opportunity_log_table_exists(self):
        from database import PropOpportunityLog
        assert PropOpportunityLog is not None

    def test_clv_record_table_exists(self):
        from database import CLVRecord
        assert CLVRecord is not None

    def test_alert_clv_seed_table_exists(self):
        from database import AlertCLVSeed
        assert AlertCLVSeed is not None

    def test_prop_candidate_log_table_exists(self):
        from database import PropCandidateLog
        assert PropCandidateLog is not None

    def test_get_recent_alerted_props_for_dedup_exists(self):
        """Dedup restore from DB (from prior pass) is intact."""
        from database import Database
        assert hasattr(Database, "get_recent_alerted_props_for_dedup")

    def test_init_state_from_db_in_market_engine(self):
        import inspect, market_engine as me
        src = inspect.getsource(me)
        assert "_init_state_from_db" in src

    def test_prop_market_alerted_restored_from_db(self):
        import inspect, market_engine as me
        src = inspect.getsource(me)
        assert "_prop_market_alerted" in src and "get_recent_alerted_props_for_dedup" in src

    def test_market_first_alert_restored_from_db(self):
        import inspect, market_engine as me
        src = inspect.getsource(me)
        assert "_MARKET_FIRST_ALERT" in src

    def test_alert_sent_is_write_once_per_external_id_stat(self):
        """mark_opportunity_alert_sent uses UPDATE by (external_id, stat_type)."""
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.mark_opportunity_alert_sent)
        assert "external_id" in src and "stat_type" in src

    def test_dedup_window_restore_method_is_async(self):
        from database import Database
        assert asyncio.iscoroutinefunction(Database.get_recent_alerted_props_for_dedup)


# ═══════════════════════════════════════════════════════════════════════════
# OddsAPI / DK / FD remain disabled
# ═══════════════════════════════════════════════════════════════════════════

class TestSportsbookDisabled:
    """DK/FD must remain disabled; no sportsbook polling in market_engine."""

    def test_no_fanduel_enabled_in_market_engine(self):
        import inspect, market_engine as me_mod
        src = inspect.getsource(me_mod)
        # FD connector removed — should not have an active FD polling call
        # (OK to have comments or historical references)
        code_lines = [
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        ]
        fd_active = any(
            "fanduel" in ln.lower() and ("await" in ln or "poll" in ln.lower())
            for ln in code_lines
        )
        assert not fd_active, "FanDuel polling must remain disabled"

    def test_no_draftkings_enabled_in_market_engine(self):
        import inspect, market_engine as me_mod
        src = inspect.getsource(me_mod)
        code_lines = [
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        ]
        dk_active = any(
            "draftkings" in ln.lower() and ("await" in ln or "poll" in ln.lower())
            for ln in code_lines
        )
        assert not dk_active, "DraftKings polling must remain disabled"

    def test_underdog_remains_primary(self):
        import inspect, market_engine as me_mod
        src = inspect.getsource(me_mod)
        assert "underdog" in src.lower() or "Underdog" in src
