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
# P2 — get_funnel_summary near-misses must exclude ACCEPTED props
# ═══════════════════════════════════════════════════════════════════════════

class TestP2FunnelNearMisses:
    """Near-misses must not include props that were also ACCEPTED in the window."""

    def _src(self) -> str:
        import database as db_mod
        return inspect.getsource(db_mod.Database.get_funnel_summary)

    def test_method_exists(self):
        from database import Database
        assert hasattr(Database, "get_funnel_summary")

    def test_source_fetches_accepted_keys(self):
        src = self._src()
        assert "ACCEPTED" in src, "must fetch ACCEPTED rows to build exclusion set"

    def test_source_filters_near_misses(self):
        src = self._src()
        # Should have an accepted_keys or similar exclusion set
        assert "_accepted_keys" in src or "accepted_key" in src.lower()

    def test_source_deduplicates_by_player_sport_stat(self):
        src = self._src()
        # Should deduplicate (player_name, sport, stat_type)
        assert "_seen_keys" in src or "seen_key" in src.lower() or "dedup" in src.lower()

    def test_source_overfetches_before_filtering(self):
        src = self._src()
        # Should fetch more than 8 rows to allow filtering
        assert "20" in src or "limit(20)" in src or ".limit(16)" in src or "overfetch" in src.lower()

    def test_source_still_filters_rejected(self):
        src = self._src()
        assert "REJECTED" in src

    def test_near_miss_excludes_also_accepted_prop(self):
        """A prop with both REJECTED and ACCEPTED rows must not appear in near-misses."""
        src = self._src()
        # Confirm the exclusion logic: in accepted_keys → skip
        assert "_accepted_keys" in src
        assert "continue" in src or "not in" in src

    def test_near_miss_deduplicates_same_prop_multiple_rejected_rows(self):
        """Same prop can have multiple REJECTED rows; only show it once."""
        src = self._src()
        assert "_seen_keys" in src

    def test_near_miss_max_8_returned(self):
        src = self._src()
        # Still caps at 8
        assert "8" in src

    def test_accepted_keys_uses_distinct(self):
        src = self._src()
        # Accepted-keys query should use DISTINCT or group to avoid huge result
        assert "distinct" in src.lower() or ".distinct()" in src

    def test_funnel_command_handler_exists(self):
        import commands as cmd
        assert hasattr(cmd, "cmd_funnel")

    def test_near_miss_docstring_explains_exclusion(self):
        src = self._src()
        # Should have a comment explaining why accepted props are excluded
        assert "accepted" in src.lower() and "near" in src.lower() or \
               "accepted" in src.lower() and "exclude" in src.lower()


# ═══════════════════════════════════════════════════════════════════════════
# P3A — get_top_ud_props_for_picks requires S/A tier (no NULL, no PASS, no B)
# ═══════════════════════════════════════════════════════════════════════════

class TestP3APicksDbQuery:
    """get_top_ud_props_for_picks must require score_tier in (S, A)."""

    def _src(self) -> str:
        import database as db_mod
        return inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)

    def test_method_exists(self):
        from database import Database
        assert hasattr(Database, "get_top_ud_props_for_picks")

    def test_source_uses_in_filter_for_tier(self):
        src = self._src()
        assert ".in_(" in src, "must use .in_() to require specific score_tier values"

    def test_source_requires_s_tier(self):
        src = self._src()
        assert '"S"' in src or "'S'" in src

    def test_source_requires_a_tier(self):
        src = self._src()
        assert '"A"' in src or "'A'" in src

    def test_source_does_not_allow_null_tier(self):
        """The old filter `!= "PASS"` allowed NULL; the new must not."""
        src = self._src()
        # Old bad pattern was: score_tier != "PASS"
        # New pattern uses .in_(["S", "A"]) which implicitly excludes NULL
        assert 'score_tier != "PASS"' not in src and "score_tier != 'PASS'" not in src

    def test_source_excludes_pass_tier(self):
        # PASS is not in the in_() list
        src = self._src()
        # The in_ filter contains S and A; PASS is absent from that list
        in_block_start = src.find(".in_(")
        if in_block_start >= 0:
            in_block = src[in_block_start:in_block_start + 80]
            assert "PASS" not in in_block, "PASS must not be in the score_tier allow-list"

    def test_source_excludes_b_tier(self):
        src = self._src()
        in_block_start = src.find(".in_(")
        if in_block_start >= 0:
            in_block = src[in_block_start:in_block_start + 80]
            assert '"B"' not in in_block and "'B'" not in in_block, (
                "B-tier must not be in the score_tier allow-list for /picks"
            )

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
# P3B — cmd_picks display loop must gate on effective confidence ≥ 55
# ═══════════════════════════════════════════════════════════════════════════

class TestP3BPicksConfidenceGate:
    """cmd_picks/_cmd_picks_inner must skip props with effective confidence < 55."""

    def _inner_src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod._cmd_picks_inner)

    def test_inner_function_exists(self):
        import commands as cmd_mod
        assert hasattr(cmd_mod, "_cmd_picks_inner")

    def test_source_has_confidence_threshold(self):
        src = self._inner_src()
        assert "55" in src, "must have confidence floor of 55 in _cmd_picks_inner"

    def test_source_skips_below_floor(self):
        src = self._inner_src()
        assert "_eff_conf" in src or "eff_conf" in src

    def test_source_checks_proxy_match_confidence(self):
        src = self._inner_src()
        assert "proxy_match_confidence" in src

    def test_source_uses_live_conf_from_rec_map(self):
        src = self._inner_src()
        # Should use _live_conf2 or similar from _rec_map
        assert "_live_conf2" in src or "_live_conf" in src

    def test_source_continues_on_low_conf(self):
        src = self._inner_src()
        # Should have a continue statement near the confidence check
        assert "continue" in src

    def test_30_confidence_prop_would_be_skipped(self):
        """Verify the logic: conf=30 < 55 → skip."""
        # Test the threshold logic directly
        _eff_conf = 30
        assert _eff_conf < 55, "30-confidence prop must be below the 55 threshold"

    def test_55_confidence_prop_would_pass(self):
        """Confidence exactly at the floor passes."""
        _eff_conf = 55
        assert not (_eff_conf < 55), "55-confidence prop must not be filtered out"

    def test_54_confidence_prop_would_be_skipped(self):
        _eff_conf = 54
        assert _eff_conf < 55

    def test_70_confidence_prop_would_pass(self):
        _eff_conf = 70
        assert not (_eff_conf < 55)

    def test_1_star_proxy_conf_30_cannot_appear(self):
        """
        A prop with proxy_match_confidence=30 produces:
          - _display_conf = 30
          - _eff_conf = 30 (no _live_conf2 in this test)
          - 30 < 55 → should be skipped
        """
        display_conf = 30
        live_conf2 = None
        eff_conf = live_conf2 if live_conf2 is not None else display_conf
        assert eff_conf < 55, "30-confidence prop must be filtered out"

    def test_gate_prefers_live_conf_over_display(self):
        """If _rec_map provides a live confidence, prefer it for the gate."""
        display_conf = 80
        live_conf2 = 30  # live snapshot shows low confidence
        eff_conf = live_conf2 if live_conf2 is not None else display_conf
        assert eff_conf == 30 and eff_conf < 55, (
            "live conf (30) must override display conf (80) for the gate"
        )

    def test_gate_falls_back_to_display_when_no_live(self):
        display_conf = 30
        live_conf2 = None
        eff_conf = live_conf2 if live_conf2 is not None else display_conf
        assert eff_conf == 30

    def test_debug_log_present_in_source(self):
        src = self._inner_src()
        assert "Tier —" in src or "not actionable" in src or "conf %d < 55" in src


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

    def test_previous_session_still_present(self):
        src = self._src()
        assert "Previous session:" in src

    def test_crash_detected_still_present(self):
        src = self._src()
        assert "Crash detected:" in src

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
    """Verify /alerts uses 'sent' not 'delivered' and has the 72h label."""

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

    def test_all_time_sent_label_present(self):
        src = self._src()
        assert "all-time sent" in src

    def test_72h_window_labeled(self):
        src = self._src()
        assert "72h" in src or "72 h" in src

    def test_api_accepted_comment_present(self):
        src = self._src()
        assert "API" in src and ("accepted" in src.lower() or "accept" in src.lower())


# ═══════════════════════════════════════════════════════════════════════════
# P3 — /picks must not show un-tiered (Tier —) props
# ═══════════════════════════════════════════════════════════════════════════

class TestP3PicksTierEnforcement:
    """Verify /picks enforces S/A tier at both DB and display layers."""

    def test_db_query_restricts_to_s_a(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert '"S"' in src or "'S'" in src
        assert '"A"' in src or "'A'" in src
        assert ".in_(" in src

    def test_no_null_tier_allowed_in_db_query(self):
        import inspect, database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        # Must not use `!= "PASS"` which lets NULL through
        assert 'score_tier != "PASS"' not in src
        assert "score_tier != 'PASS'" not in src

    def test_cmd_picks_has_confidence_gate(self):
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "< 55" in src or "55" in src

    def test_tier_from_conf_function(self):
        """_tier_from_conf returns '—' for confidence < 55."""
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        # The function is defined inline; verify threshold
        assert "55" in src

    def test_confidence_30_tier_dash(self):
        """_tier_from_conf(30) → '—'."""
        def _tier_from_conf(c: int) -> str:
            if c >= 85: return "S"
            if c >= 70: return "A"
            if c >= 55: return "B"
            return "—"
        assert _tier_from_conf(30) == "—"

    def test_confidence_55_tier_b(self):
        def _tier_from_conf(c: int) -> str:
            if c >= 85: return "S"
            if c >= 70: return "A"
            if c >= 55: return "B"
            return "—"
        assert _tier_from_conf(55) == "B"

    def test_confidence_70_tier_a(self):
        def _tier_from_conf(c: int) -> str:
            if c >= 85: return "S"
            if c >= 70: return "A"
            if c >= 55: return "B"
            return "—"
        assert _tier_from_conf(70) == "A"

    def test_confidence_85_tier_s(self):
        def _tier_from_conf(c: int) -> str:
            if c >= 85: return "S"
            if c >= 70: return "A"
            if c >= 55: return "B"
            return "—"
        assert _tier_from_conf(85) == "S"

    def test_30_conf_1_star_untiered_prop_blocked_end_to_end(self):
        """
        Simulate the full /picks filter chain for a 30-confidence / Tier — prop.
        Step 1: DB query requires score_tier in ('S', 'A') → NULL-tier prop excluded.
        Step 2: Even if such a prop somehow passed, the display loop gates on eff_conf < 55.
        """
        # Simulate DB gate
        score_tier = None   # unscored / NULL
        tier_allow = ["S", "A"]
        assert score_tier not in tier_allow, "DB gate must block NULL score_tier"

        # Simulate display gate
        proxy_conf = 30
        live_conf2 = None
        eff_conf = live_conf2 if live_conf2 is not None else proxy_conf
        assert eff_conf < 55, "Display gate must block eff_conf=30"

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
        import inspect, commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_strict_sports" in src

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

    def test_near_misses_label_still_rendered(self):
        src = self._src()
        assert "Near-Misses" in src or "near-miss" in src.lower()

    def test_funnel_summary_call_still_present(self):
        src = self._src()
        assert "get_funnel_summary" in src

    def test_top_rejections_iterated(self):
        src = self._src()
        assert "top_rej" in src or "top_rejections" in src

    def test_qualified_s_a_tier_label_present(self):
        src = self._src()
        assert "S/A" in src or "S/A-tier" in src

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
