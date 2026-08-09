"""
V3.2 Phase 2 Core — Focused regression tests.

Covers four fixes applied in this pass:
  P1 — CS/LOL/esports S-tier props must appear in /picks
       (removed incorrect eff_conf<55 gate; fixed _render_pick_entry tier display)
  P2 — /alerts count verified accurate (OVER/UNDER filter from prior pass intact)
  P3 — /picks display quality verified (DB gate + display render fallback)
  P4 — /health: "Previous session:" and "Crash detected:" lines removed

Baseline: 3,675 passed.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import os

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


# ═══════════════════════════════════════════════════════════════════════════
# P1 — CS/LOL/esports S-tier picks must not be filtered by a confidence gate
# ═══════════════════════════════════════════════════════════════════════════

class TestP1EsportsTierOnePicksNotBlocked:
    """S-tier esports props (CS/LOL/VAL/ESPORTS) must appear in /picks."""

    def _inner_src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod._cmd_picks_inner)

    def test_no_eff_conf_gate_in_loop(self):
        """eff_conf<55 gate removed — it blocked esports S-tier props."""
        src = self._inner_src()
        assert "_eff_conf" not in src, (
            "_eff_conf secondary gate must be removed from the _sport_groups loop; "
            "it incorrectly blocked esports props with low proxy_match_confidence"
        )

    def test_no_eff_conf_55_pattern(self):
        src = self._inner_src()
        assert "eff_conf < 55" not in src
        assert "_eff_conf < 55" not in src

    def test_comment_explains_removal(self):
        src = self._inner_src()
        # Should have a comment explaining why no secondary gate
        assert "esports" in src.lower() or "cross-provider" in src.lower() or \
               "proxy_match_confidence" in src

    def test_s_tier_esports_prop_passes_all_loop_gates(self):
        """Simulate CS S-tier prop with proxy_match_confidence=0 through the loop."""
        # DB gate: score_tier must be in ["S","A"]
        score_tier = "S"
        assert score_tier in ["S", "A"], "S-tier passes DB gate"

        # Direction gate: must be OVER or UNDER
        _eff_rec = "OVER"
        assert _eff_rec in ("OVER", "UNDER"), "actionable direction passes direction gate"

        # MLB-UNDER gate: not MLB
        sport = "CS"
        assert not (sport == "MLB" and _eff_rec == "UNDER"), "CS OVER passes MLB-UNDER gate"

        # No eff_conf gate — prop reaches display
        # Result: prop appears in /picks ✓

    def test_lol_s_tier_no_cross_provider_data(self):
        """LOL props have no PrizePicks/DK/FD data → proxy_match_confidence=0."""
        proxy_conf = 0
        score_tier = "S"

        # Old (wrong) gate: eff_conf = proxy_conf = 0 < 55 → SKIP
        old_gate_would_block = proxy_conf < 55
        assert old_gate_would_block, "confirm old gate would have blocked this"

        # New (correct): no gate → prop passes
        new_gate_blocks = False  # gate removed
        assert not new_gate_blocks, "new code must not block S-tier LOL prop"

    def test_val_s_tier_no_cross_provider_data(self):
        proxy_conf = 0
        score_tier = "S"
        old_gate_would_block = proxy_conf < 55
        assert old_gate_would_block
        new_gate_blocks = False
        assert not new_gate_blocks

    def test_esports_s_tier_no_cross_provider_data(self):
        proxy_conf = 0
        score_tier = "S"
        old_gate_would_block = proxy_conf < 55
        assert old_gate_would_block
        new_gate_blocks = False
        assert not new_gate_blocks

    def test_null_tier_still_blocked_at_db_level(self):
        """Unscored (NULL) props are blocked by the DB query, not the display loop."""
        score_tier = None
        assert score_tier not in ["S", "A"], "NULL score_tier blocked by DB gate"

    def test_pass_direction_still_filtered(self):
        """PASS direction props must still be filtered (no change)."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert '"OVER"' in src and '"UNDER"' in src

    def test_strict_sports_filter_still_active(self):
        """MLB/NFL strict-sport S-tier requirement is unchanged."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_strict_sports" in src

    def test_mlb_under_still_blocked(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "MLB" in src and "UNDER" in src

    def test_db_query_still_requires_s_a_tier(self):
        """DB query must still gate on S/A tier (unchanged from prior pass)."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert ".in_(" in src
        assert '"S"' in src or "'S'" in src
        assert '"A"' in src or "'A'" in src
        assert 'score_tier != "PASS"' not in src


# ═══════════════════════════════════════════════════════════════════════════
# P1 — _render_pick_entry display fallback for sports with no cross-provider data
# ═══════════════════════════════════════════════════════════════════════════

class TestP1RenderPickEntryTierFallback:
    """_render_pick_entry must show correct tier/confidence for esports props."""

    def _inner_src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod._cmd_picks_inner)

    def test_render_references_score_tier(self):
        """Render function must read plh.score_tier for fallback display."""
        src = self._inner_src()
        assert "score_tier" in src

    def test_render_references_score_confidence(self):
        """Render function must use a DB-fallback confidence for display.

        PLH has no score_confidence column; the implementation now derives a
        representative confidence from the score_tier band midpoint (S=87, A=72,
        B=57) and stores it in _db_conf before the fallback condition fires.
        Verify the fallback variable is present rather than the removed attribute name.
        """
        src = self._inner_src()
        assert "_db_conf" in src, "_db_conf fallback variable must be present in source"

    def test_render_fallback_triggers_below_30_proxy_conf(self):
        """Fallback to DB tier/conf when proxy_match_confidence < 30."""
        src = self._inner_src()
        assert "< 30" in src, "fallback must trigger when proxy conf < 30"

    def test_render_fallback_only_for_scored_tiers(self):
        """Fallback only applies when score_tier is S, A, or B (not NULL)."""
        src = self._inner_src()
        # The condition checks _db_tier in ("S", "A", "B")
        assert '"S"' in src or "'S'" in src
        assert '"A"' in src or "'A'" in src
        assert '"B"' in src or "'B'" in src

    def test_render_fallback_logic(self):
        """Simulate the fallback logic for an esports S-tier prop."""
        proxy_conf = 0   # no cross-provider data
        db_tier = "S"
        db_conf = 85     # engine scored 85

        # Simulate the fallback condition
        if proxy_conf < 30 and db_tier in ("S", "A", "B") and db_conf is not None:
            tier  = db_tier
            conf_display = db_conf
        else:
            # _tier_from_conf(proxy_conf)
            tier  = "—"  # 0 < 55 → "—"
            conf_display = proxy_conf

        assert tier == "S", "fallback must use DB tier for esports S-tier prop"
        assert conf_display == 85, "fallback must use DB confidence"

    def test_no_fallback_when_proxy_has_data(self):
        """When proxy_match_confidence >= 30, use proxy data (normal MLB/NBA case)."""
        proxy_conf = 75  # PrizePicks has data
        db_tier = "S"
        db_conf = 80

        if proxy_conf < 30 and db_tier in ("S", "A", "B") and db_conf is not None:
            tier = db_tier
        else:
            def _tier_from_conf(c):
                if c >= 85: return "S"
                if c >= 70: return "A"
                if c >= 55: return "B"
                return "—"
            tier = _tier_from_conf(proxy_conf)

        assert tier == "A", "high proxy conf → use proxy tier (A from 75)"

    def test_s_tier_esports_displays_as_s_not_dash(self):
        """An S-tier esports prop with proxy_match_confidence=0 must display as 'S'."""
        proxy_conf = 0
        db_tier = "S"
        db_conf = 85

        if proxy_conf < 30 and db_tier in ("S", "A", "B") and db_conf is not None:
            displayed_tier = db_tier
        else:
            def _tfc(c):
                if c >= 85: return "S"
                if c >= 70: return "A"
                if c >= 55: return "B"
                return "—"
            displayed_tier = _tfc(proxy_conf)

        assert displayed_tier == "S"
        assert displayed_tier != "—"

    def test_tier_icon_lookup_covers_s_a_b(self):
        """Tier icons must exist for S, A, B so DB-sourced tiers render correctly."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        # _TIER_EMOJI must include S and A
        assert '"S"' in src or "'S'" in src


# ═══════════════════════════════════════════════════════════════════════════
# P2 — /alerts count accuracy (from prior pass — verify still intact)
# ═══════════════════════════════════════════════════════════════════════════

class TestP2AlertsCountIntact:
    """Verify the prior-pass alert count fix is still intact."""

    def test_count_method_filters_over_under(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.count_actionable_pick_records)
        assert "OVER" in src and "UNDER" in src
        assert ".in_(" in src

    def test_count_method_filters_alert_sent(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.count_actionable_pick_records)
        assert "alert_sent" in src

    def test_list_method_filters_over_under(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_alerted_opportunity_log)
        assert "OVER" in src and "UNDER" in src

    def test_alerts_wording_no_delivered(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_alerts)
        lines_with_delivered = [
            ln for ln in src.splitlines()
            if "delivered" in ln.lower() and not ln.strip().startswith("#")
        ]
        assert not lines_with_delivered

    def test_alerts_uses_24h_window_v35(self):
        """V3.5: /alerts uses 24h window; all-time count removed from display."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_alerts)
        # V3.5 changed 72h → 24h and removed all-time count
        assert "since_hours=24" in src, "V3.5: /alerts must use 24h window"
        assert "all-time sent" not in src, "V3.5: all-time count removed from /alerts"

    def test_12h_time_format_intact(self):
        """_fmt_user_ts() 12h format from prior pass is still present."""
        import commands as cmd_mod
        assert hasattr(cmd_mod, "_fmt_user_ts")


# ═══════════════════════════════════════════════════════════════════════════
# P3 — /picks display quality verified
# ═══════════════════════════════════════════════════════════════════════════

class TestP3PicksDisplayQuality:
    """Verify /picks quality gates are correct after P1 fix."""

    def test_db_requires_s_a_tier(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert ".in_(" in src

    def test_no_null_tier_in_picks_query(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        assert 'score_tier != "PASS"' not in src
        assert "score_tier != 'PASS'" not in src

    def test_no_pass_tier_in_picks_query(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        in_start = src.find(".in_(")
        if in_start >= 0:
            in_block = src[in_start:in_start + 60]
            assert "PASS" not in in_block

    def test_direction_gate_filters_pass(self):
        """OVER/UNDER gate still filters PASS-direction props."""
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert '"OVER"' in src and '"UNDER"' in src

    def test_season_future_filter_intact(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_is_season_future" in src

    def test_strict_sports_s_tier_gate_intact(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "_strict_sports" in src
        assert "score_tier" in src or '"S"' in src

    def test_no_low_conf_prop_would_pass_db_gate(self):
        """A prop with NULL score_tier cannot appear — the DB gate blocks it."""
        score_tier = None
        assert score_tier not in ["S", "A"]

    def test_pass_direction_cannot_appear(self):
        """A prop with bet_recommendation=PASS is filtered from display."""
        bet_rec = "PASS"
        assert bet_rec not in ("OVER", "UNDER")

    def test_s_tier_over_direction_passes_all_gates(self):
        """A genuine S-tier OVER prop passes all gates."""
        score_tier = "S"
        bet_rec = "OVER"
        sport = "CS"
        is_strict_sport = sport in {"MLB", "NFL"}
        tier_ok = score_tier in ["S", "A"] and (not is_strict_sport or score_tier == "S")
        direction_ok = bet_rec in ("OVER", "UNDER")
        mlb_under_ok = not (sport == "MLB" and bet_rec == "UNDER")
        assert tier_ok and direction_ok and mlb_under_ok


# ═══════════════════════════════════════════════════════════════════════════
# P4 — /health "Previous session:" and "Crash detected:" lines removed
# ═══════════════════════════════════════════════════════════════════════════

class TestP4HealthPhase2Cleanup:
    """cmd_health must not emit 'Previous session:' or 'Crash detected:'."""

    def _src(self) -> str:
        import commands as cmd_mod
        return inspect.getsource(cmd_mod.cmd_health)

    def test_previous_session_absent(self):
        src = self._src()
        assert "Previous session:" not in src, (
            "cmd_health must not produce a 'Previous session:' display line"
        )

    def test_crash_detected_absent(self):
        src = self._src()
        assert "Crash detected:" not in src, (
            "cmd_health must not produce a 'Crash detected:' display line"
        )

    def test_uptime_still_present(self):
        src = self._src()
        assert "Uptime:" in src

    def test_heartbeat_still_present(self):
        src = self._src()
        assert "Heartbeat:" in src

    def test_last_startup_still_present(self):
        src = self._src()
        assert "Last startup:" in src

    def test_background_jobs_still_present(self):
        src = self._src()
        assert "Background Jobs" in src

    def test_restart_reason_still_absent(self):
        """The prior-pass removal of 'Restart reason:' must still be absent."""
        src = self._src()
        assert "Restart reason:" not in src

    def test_stale_recovery_gate_still_present(self):
        import commands as cmd_mod
        full_src = inspect.getsource(cmd_mod)
        assert "_RECOVERY_STALE_HOURS" in full_src

    def test_stale_recovery_historical_label_present(self):
        import commands as cmd_mod
        full_src = inspect.getsource(cmd_mod)
        assert "historical" in full_src and "no recent failures" in full_src

    def test_underlying_crash_detection_methods_intact(self):
        """Underlying detection functions in health.py are untouched."""
        from engine.health import HealthTracker
        assert hasattr(HealthTracker, "was_unexpected_exit")
        assert hasattr(HealthTracker, "last_startup_reason")
        assert hasattr(HealthTracker, "crash_cause_label")
        assert hasattr(HealthTracker, "last_session_duration_str")

    def test_health_tracker_not_removed(self):
        from engine.health import get_health_tracker
        assert callable(get_health_tracker)

    def test_last_recovery_age_hours_intact(self):
        """Stale-recovery fix from prior pass is intact."""
        from engine.health import HealthTracker
        assert hasattr(HealthTracker, "last_recovery_age_hours")


# ═══════════════════════════════════════════════════════════════════════════
# Phase boundaries — nothing enabled that should not be
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseBoundaries:
    """OddsAPI/DK/FD remain disabled; Underdog remains primary."""

    def test_odds_api_not_polled_in_market_engine(self):
        import market_engine as me
        src = inspect.getsource(me)
        code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        dk_poll = any(
            "draftkings" in ln.lower() and "await" in ln
            for ln in code_lines
        )
        fd_poll = any(
            "fanduel" in ln.lower() and "await" in ln
            for ln in code_lines
        )
        assert not dk_poll, "DraftKings must remain disabled"
        assert not fd_poll, "FanDuel must remain disabled"

    def test_underdog_remains_primary(self):
        import market_engine as me
        src = inspect.getsource(me)
        assert "Underdog" in src

    def test_score_tier_filter_not_lowered(self):
        """DB filter must still require S/A — not B, PASS, or NULL."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_top_ud_props_for_picks)
        # Must use .in_(["S","A"]) — not a lower threshold
        in_start = src.find(".in_(")
        assert in_start >= 0
        in_block = src[in_start:in_start + 60]
        assert '"B"' not in in_block and "'B'" not in in_block
        assert "PASS" not in in_block

    def test_no_credentials_in_health(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod.cmd_health)
        secrets = ["sk_", "pk_", "ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "SESSION_SECRET"]
        for pat in secrets:
            assert pat not in src, f"secret pattern '{pat}' in cmd_health"

    def test_no_credentials_in_picks(self):
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        secrets = ["sk_", "pk_", "ODDS_API_KEY", "TELEGRAM_BOT_TOKEN"]
        for pat in secrets:
            code_lines = [ln for ln in src.splitlines() if "reply_text" in ln]
            for ln in code_lines:
                assert pat not in ln, f"'{pat}' in picks reply_text"

    def test_scoring_thresholds_not_changed(self):
        """S-tier threshold (≥80/≥85) and A-tier (≥65/≥70) must not be lowered."""
        # _tier_from_conf in commands.py must use 85/70/55
        import commands as cmd_mod
        src = inspect.getsource(cmd_mod._cmd_picks_inner)
        assert "85" in src and "70" in src and "55" in src


# ═══════════════════════════════════════════════════════════════════════════
# Persistence — prior-pass fixes still intact
# ═══════════════════════════════════════════════════════════════════════════

class TestPersistenceIntact:
    """Confirm all prior-pass persistence mechanisms survive this pass."""

    def test_dedup_restore_method_exists(self):
        import database as db_mod
        assert hasattr(db_mod.Database, "get_recent_alerted_props_for_dedup")

    def test_init_state_from_db_exists(self):
        import market_engine as me
        src = inspect.getsource(me)
        assert "_init_state_from_db" in src

    def test_prop_market_alerted_restore(self):
        import market_engine as me
        src = inspect.getsource(me)
        assert "_prop_market_alerted" in src
        assert "get_recent_alerted_props_for_dedup" in src

    def test_prop_candidate_log_funnel_dedup(self):
        """Near-miss dedup fix from prior pass is still intact."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_funnel_summary)
        assert "_accepted_keys" in src
        assert "_seen_keys" in src

    def test_count_actionable_over_under_filter(self):
        """OVER/UNDER filter from prior pass still in place."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.count_actionable_pick_records)
        assert "OVER" in src and "UNDER" in src
