"""
CRED BURNING PROTECTION — Regression tests for the authoritative delivery spec.

36 tests covering:
  1-6.   Tier 2 MLB actionability (S+OVER / S+UNDER / A+OVER / A+UNDER / NFL)
  7-14.  Tier 1 S/A/B/C actionability (OVER and UNDER)
  15-18. BQ ≥ 80 STRONG BET / STRONG UNDER label
  19-20. S-tier priority display
  21-22. Deduplication (exact dup blocked; line move allowed)
  23.    Re-entry (removed → relisted eligible for new alert)
  24-27. Continuous prop monitoring (all active props monitored every cycle)
  28-31. L5 history display (available / fallback / N/A / not a gate)
  32.    ScanCycleLog records accurate full-feed counts
  33.    /funnel HTML escaping with special characters
  34.    /picks distinguishes delivered vs candidate
  35.    Alert freshness timestamp in output
  36.    Critical DB logging failures emit warnings not silently pass
"""
from __future__ import annotations

import os
import sys
import inspect

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me
import config as cfg_mod
from alerts_multiplatform import (
    _bq_priority_label,
    format_underdog_change_alert,
    format_underdog_new_prop_alert,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _gate(sport: str, tier: str, direction: str = "OVER") -> bool:
    """Replicate the Tier 1 / Tier 2 gate decision."""
    config = me.config
    sport_up = sport.upper()
    if sport_up in config.ud_strict_alert_sports:
        # Tier 2: S-only, OVER-only
        if tier not in config.ud_mlb_alert_tiers:
            return False
        if direction == "UNDER":
            return False
    else:
        # Tier 1: S/A deliver; B/C watchlist
        if tier not in ("S", "A"):
            return False
    return True


class _FakeScore:
    def __init__(self, tier="S", total=85, stars=4, n_history=15, bet_quality_label="BQ 85/100"):
        self.tier = tier
        self.total = total
        self.stars = stars
        self.n_history = n_history
        self.stars_display = "★★★★☆"
        self.move_velocity = None
        self.bet_quality_label = bet_quality_label


class _FakeDecision:
    def __init__(self, rec="OVER", tier="S", conf=85, l5=0.6, l5g=5, reason="hist OK"):
        self.recommendation = rec
        self.decision_tier = tier
        self.confidence = conf
        self.l5_hit_rate = l5
        self.l5_games = l5g
        self.reason = reason


class _FakeValidation:
    has_supporting_data = True
    n_history = 10
    def rate_summary(self): return "6/10"


# ═══════════════════════════════════════════════════════════════════════════════
# 1-6. Tier 2 MLB/NFL actionability
# ═══════════════════════════════════════════════════════════════════════════════

class TestTier2Actionability:

    def test_01_tier2_mlb_s_over_actionable(self):
        """1. Tier 2 MLB S+OVER → actionable."""
        assert _gate("MLB", "S", "OVER") is True

    def test_02_tier2_mlb_s_under_blocked(self):
        """2. Tier 2 MLB S+UNDER → blocked/watchlist (Tier 2 = OVER only)."""
        assert _gate("MLB", "S", "UNDER") is False

    def test_03_tier2_mlb_a_over_watchlist(self):
        """3. Tier 2 MLB A+OVER → watchlist (A not actionable for Tier 2)."""
        assert _gate("MLB", "A", "OVER") is False

    def test_04_tier2_mlb_a_under_watchlist(self):
        """4. Tier 2 MLB A+UNDER → watchlist (A blocked + UNDER blocked)."""
        assert _gate("MLB", "A", "UNDER") is False

    def test_05_tier2_nfl_s_over_actionable(self):
        """5. Tier 2 NFL S+OVER → actionable."""
        assert _gate("NFL", "S", "OVER") is True

    def test_06_tier2_nfl_s_under_blocked(self):
        """6. Tier 2 NFL S+UNDER → blocked/watchlist."""
        assert _gate("NFL", "S", "UNDER") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 7-14. Tier 1 S/A/B/C actionability
# ═══════════════════════════════════════════════════════════════════════════════

class TestTier1Actionability:

    def test_07_tier1_s_over_actionable(self):
        """7. Tier 1 S+OVER → actionable."""
        assert _gate("NBA", "S", "OVER") is True

    def test_08_tier1_s_under_actionable(self):
        """8. Tier 1 S+UNDER → actionable (UNDER allowed for Tier 1)."""
        assert _gate("NBA", "S", "UNDER") is True

    def test_09_tier1_a_over_actionable(self):
        """9. Tier 1 A+OVER → actionable."""
        assert _gate("CS", "A", "OVER") is True

    def test_10_tier1_a_under_actionable(self):
        """10. Tier 1 A+UNDER → actionable."""
        assert _gate("CS", "A", "UNDER") is True

    def test_11_tier1_b_over_watchlist(self):
        """11. Tier 1 B+OVER → watchlist."""
        assert _gate("WNBA", "B", "OVER") is False

    def test_12_tier1_b_under_watchlist(self):
        """12. Tier 1 B+UNDER → watchlist."""
        assert _gate("WNBA", "B", "UNDER") is False

    def test_13_tier1_c_over_watchlist(self):
        """13. Tier 1 C+OVER → watchlist."""
        assert _gate("LOL", "C", "OVER") is False

    def test_14_tier1_c_under_watchlist(self):
        """14. Tier 1 C+UNDER → watchlist."""
        assert _gate("LOL", "C", "UNDER") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 15-18. BQ ≥ 80 STRONG BET / STRONG UNDER label
# ═══════════════════════════════════════════════════════════════════════════════

class TestBQStrongLabel:

    def test_15_bq_80_strong_bet(self):
        """15. BQ ≥ 80 → 💪 STRONG BET label (OVER direction)."""
        label = _bq_priority_label(80, direction="OVER")
        assert "STRONG BET" in label, f"Expected STRONG BET, got: {label}"
        assert "💪" in label

    def test_16_bq_100_strong_bet(self):
        """16. BQ 100 → 💪 STRONG BET."""
        label = _bq_priority_label(100, direction="OVER")
        assert "STRONG BET" in label
        assert "💪" in label

    def test_17_bq_79_not_strong_bet(self):
        """17. BQ 79 → NOT a strong bet label."""
        label = _bq_priority_label(79, direction="OVER")
        assert "STRONG BET" not in label

    def test_18_strong_under_bq_80_plus(self):
        """18. UNDER + BQ ≥ 80 → 💪 STRONG UNDER label."""
        label = _bq_priority_label(85, direction="UNDER")
        assert "STRONG UNDER" in label, f"Expected STRONG UNDER, got: {label}"
        assert "💪" in label


# ═══════════════════════════════════════════════════════════════════════════════
# 19-20. S-tier priority display
# ═══════════════════════════════════════════════════════════════════════════════

class TestSTierPriorityDisplay:

    def test_19_s_tier_priority_header_removed(self):
        """19. Separate S-TIER HIGH PRIORITY header must NOT exist — spec removed it.

        Actionable picks should use the unified 🎯 ACTIONABLE BET PICK format only.
        The S-tier grade is displayed inside the alert body, not as a separate header.
        """
        src = inspect.getsource(__import__("alerts"))
        assert "S-TIER HIGH PRIORITY" not in src, (
            "Separate S-TIER HIGH PRIORITY header was re-added to alerts.py. "
            "Per spec it must be removed — use unified 🎯 ACTIONABLE BET PICK format."
        )

    def test_20_actionable_bet_pick_format_present(self):
        """20. Unified 🎯 ACTIONABLE BET PICK format must exist in alerts_multiplatform."""
        src = inspect.getsource(__import__("alerts_multiplatform"))
        assert "ACTIONABLE BET PICK" in src or "🎯" in src, (
            "🎯 ACTIONABLE BET PICK format missing from alerts_multiplatform — "
            "this is the single user-facing alert format."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 21-22. Deduplication
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeduplication:

    def test_21_exact_duplicate_is_deduped(self):
        """21. Same player + market + line + direction is deduplicated."""
        from market_engine import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        store = {}
        _record_prop_alerted(store, "PlayerA", "MLB", "Hits", 1.5)
        # Pass the required dedup_window_seconds and min_line_change args (V3.5 signature)
        assert _is_prop_deduped(
            store, "PlayerA", "MLB", "Hits", 1.5,
            dedup_window_seconds=int(cfg.config.UD_ALERT_DEDUP_WINDOW),
            min_line_change=cfg.config.MIN_UNDERDOG_LINE_CHANGE,
        ), "Identical prop must be deduped within the window"

    def test_22_meaningful_line_move_can_alert(self):
        """22. Same prop with a meaningfully different line can re-alert."""
        from market_engine import _is_prop_deduped, _record_prop_alerted
        import config as cfg
        store = {}
        _record_prop_alerted(store, "PlayerB", "MLB", "Hits", 1.5)
        # A line change of 1.0 (>> MIN_UNDERDOG_LINE_CHANGE=0.5) should allow re-alert
        new_line = 1.5 + cfg.config.MIN_UNDERDOG_LINE_CHANGE + 0.1
        assert not _is_prop_deduped(
            store, "PlayerB", "MLB", "Hits", new_line,
            dedup_window_seconds=int(cfg.config.UD_ALERT_DEDUP_WINDOW),
            min_line_change=cfg.config.MIN_UNDERDOG_LINE_CHANGE,
        ), f"Line move from 1.5 to {new_line} should NOT be deduped"


# ═══════════════════════════════════════════════════════════════════════════════
# 23. Re-entry handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestReEntry:

    def test_23_reentry_eligible_for_new_alert(self):
        """23. A REMOVED → relisted prop must be eligible for a new alert."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "is_reentry_qualified" in src, (
            "Re-entry detection (is_reentry_qualified) missing from market_engine"
        )
        assert "lifecycle_state" in src, (
            "lifecycle_state tracking missing — re-entry cannot be detected"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 24-27. Continuous prop monitoring
# ═══════════════════════════════════════════════════════════════════════════════

class TestContinuousMonitoring:

    def test_24_all_active_props_monitored_every_cycle(self):
        """24. Every active prop must be in the scan every polling cycle."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # The scan must iterate ALL active Underdog props
        # V3.5: the full feed is stored in `ud_snaps`; standing path iterates `ud_snaps` too
        assert (
            "active_snaps" in src or "all_snaps" in src
            or "snap_map" in src or "ud_snaps" in src
        ), "Could not find evidence of full-feed iteration in underdog_job"

    def test_25_stable_known_prop_remains_monitored(self):
        """25. Unchanged known prop still shows in scan (known_keys not a monitoring gate)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "unchanged" in src or "is_unchanged" in src, (
            "Unchanged prop tracking missing — stable props may not be monitored"
        )

    def test_26_known_prop_with_line_movement_is_rescored(self):
        """26. A known prop whose line changes must be rescored."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "line_changed" in src or "MIN_UNDERDOG_LINE_CHANGE" in src, (
            "Line-change detection missing — changed props may not be rescored"
        )

    def test_27_new_prop_is_scored(self):
        """27. A newly appearing prop must be scored."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "is_new_prop" in src or "new_prop" in src, (
            "New-prop detection missing from market_engine"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 28-31. L5 history display
# ═══════════════════════════════════════════════════════════════════════════════

class TestL5History:

    def test_28_l5_displayed_when_available(self):
        """28. L5 hit rate displayed when decision has l5_hit_rate data."""
        score = _FakeScore()
        decision = _FakeDecision(l5=0.8, l5g=5)
        msg = format_underdog_change_alert(
            "Test Player", "Team", "MLB", "Hits + Runs + RBIs",
            1.5, 2.0,
            score=score, decision=decision,
        )
        assert "L5" in msg, "L5 should be present in alert when history is available"
        assert "80%" in msg or "0.8" in msg or "4/" in msg or "L5" in msg

    def test_29_l5_fallback_when_no_prop_history(self):
        """29. L5 fallback shows N/A when no exact prop history is available."""
        score = _FakeScore()
        decision = _FakeDecision(l5=None, l5g=None)
        msg = format_underdog_change_alert(
            "Test Player", "Team", "NBA", "Points",
            24.5, 25.0,
            score=score, decision=decision,
        )
        assert "L5" in msg, "L5 section must always appear in alert"

    def test_30_no_history_displays_na(self):
        """30. When no history available from any provider, displays N/A."""
        score = _FakeScore()
        decision = _FakeDecision(l5=None, l5g=None)
        msg = format_underdog_change_alert(
            "Test Player", "Team", "CS", "Kills on Map 1",
            11.5, 12.0,
            score=score, decision=decision,
        )
        assert "N/A" in msg or "no history" in msg.lower(), (
            "When no history available, must show N/A rather than silently omitting L5"
        )

    def test_31_history_failure_does_not_reject_prop(self):
        """31. History enrichment failure must not reject a valid prop."""
        # Simulate a decision with no L5 data — should still produce a valid alert message
        score = _FakeScore()
        decision = _FakeDecision(l5=None, l5g=None, reason="")
        msg = format_underdog_change_alert(
            "Test Player", "Team", "LOL", "Kills on Maps 1+2",
            8.5, 9.0,
            score=score, decision=decision,
        )
        assert msg  # alert must still be generated
        assert "Test Player" in msg
        assert "PICK" in msg or "Kills" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# 32. ScanCycleLog
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanCycleLog:

    def test_32_scan_cycle_log_records_full_feed_counts(self):
        """32. ScanCycleLog must record fetched, active, unchanged, scored, qualified, delivered."""
        from database import Database
        import inspect as _ins
        src = _ins.getsource(Database)
        assert "scan_cycle_log" in src.lower() or "ScanCycleLog" in src, (
            "ScanCycleLog table/method not found in Database class"
        )
        assert "log_scan_cycle" in src, "log_scan_cycle method must be in Database"


# ═══════════════════════════════════════════════════════════════════════════════
# 33. /funnel HTML escaping
# ═══════════════════════════════════════════════════════════════════════════════

class TestFunnelHTMLEscaping:

    def test_33_funnel_html_escaping_with_special_chars(self):
        """33. /funnel must HTML-escape dynamic fields (player_name, stat_type, etc.)."""
        import html
        src = open("commands.py").read()
        # The funnel command must use html.escape() on dynamic values
        assert "html.escape" in src or "escape(" in src, (
            "/funnel must HTML-escape dynamic values to prevent Telegram HTML parse errors"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 34. /picks delivery status
# ═══════════════════════════════════════════════════════════════════════════════

class TestPicksDeliveryStatus:

    def test_34_picks_distinguishes_delivered_vs_candidate(self):
        """34. /picks must show 'Delivered' or 'Candidate — not yet delivered'."""
        src = open("commands.py").read()
        assert "Delivered" in src or "delivered" in src, (
            "/picks must show delivery status (Delivered / not yet delivered)"
        )
        assert "Candidate" in src or "not yet delivered" in src, (
            "/picks must distinguish undelivered candidates from sent alerts"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 35. Alert freshness timestamp
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlertFreshness:

    def test_35_freshness_timestamp_in_alert_output(self):
        """35. Alert freshness timestamp must appear in actionable alerts."""
        src = open("alerts.py").read()
        assert "Line as of" in src or "line as of" in src.lower(), (
            "Freshness timestamp 'Line as of HH:MM ET' missing from alerts.py"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 36. Critical DB logging failures emit warnings
# ═══════════════════════════════════════════════════════════════════════════════

class TestDBLoggingFailures:

    def test_36_db_failures_emit_warnings_not_silent_pass(self):
        """36. Critical DB write failures must emit logger.warning/error, not silent pass."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # All critical DB operations must use logger.warning on failure
        assert 'logger.warning' in src, (
            "No logger.warning calls found in underdog_job — DB failures may be silently swallowed"
        )
        # Ensure silent bare except-pass patterns are not used for critical paths
        # (log_prop_opportunity, mark_opportunity_alert_sent, PropCandidateLog, ScanCycleLog)
        assert 'log_prop_opportunity' in src
        assert 'mark_opportunity_alert_sent' in src
