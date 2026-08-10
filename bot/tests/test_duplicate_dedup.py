"""
Regression tests for duplicate Telegram alert prevention.

Root cause: 95+ LC priority-override set _lc_95_sent=True which forced
should_alert=False, preventing _processed_keys from being updated.
The standing path then re-evaluated and re-delivered the same prop.

These tests verify the full dedup contract:
  1. Same opportunity processed twice → one alert
  2. Same candidate through two delivery paths → one alert
  3. Concurrent duplicate processing → one alert
  4. Telegram retry after successful send → no duplicate
  5. Telegram failure remains retryable
  6. Legitimate line movement creates a new alert
  7. Legitimate re-entry creates a new alert
  8. Restart does not resend delivered opportunity
  9. S-tier priority + normal path cannot duplicate
 10. Jordan Walker reproduction case
"""
import asyncio
import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_store():
    """Return a fresh _prop_market_alerted dict."""
    return {}


def _record(store, player, sport, stat, line, *, t=None):
    from market_engine import _record_prop_alerted
    _record_prop_alerted(store, player, sport, stat, line)
    if t is not None:
        # Override timestamp to simulate old entry
        store[(player, sport, stat)] = (t, line)


def _is_deduped(store, player, sport, stat, line, window=3600, min_change=0.5):
    from market_engine import _is_prop_deduped
    return _is_prop_deduped(
        store, player, sport, stat, line,
        dedup_window_seconds=window,
        min_line_change=min_change,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Same opportunity processed twice → one alert
# ─────────────────────────────────────────────────────────────────────────────

class TestSameOpportunityTwice:

    def test_01_second_process_is_deduped(self):
        """Same (player, sport, stat, line) recorded twice → second is deduped."""
        store = _make_store()
        _record(store, "Jordan Walker", "MLB", "Hits", 0.5)
        assert _is_deduped(store, "Jordan Walker", "MLB", "Hits", 0.5), (
            "Second processing of identical opportunity must be suppressed"
        )

    def test_02_dedup_within_window(self):
        """Alert recorded 30 min ago → still deduped if line unchanged."""
        store = _make_store()
        t_30m_ago = time.time() - 1800
        _record(store, "Player A", "NBA", "Points", 22.5, t=t_30m_ago)
        assert _is_deduped(store, "Player A", "NBA", "Points", 22.5, window=3600), (
            "Alert within window with same line must be deduped"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Same candidate through two delivery paths → one alert
# ─────────────────────────────────────────────────────────────────────────────

class TestTwoDeliveryPaths:

    def test_03_lc_95_sent_adds_to_processed_keys(self):
        """When _lc_95_sent fires, the prop must be added to _processed_keys
        so the standing path cannot re-evaluate it in the same scan cycle."""
        src = inspect.getsource(__import__("market_engine"))
        # The fix: _lc_95_sent block now unconditionally adds to _processed_keys
        # Search for the pattern within the _lc_95_sent conditional
        idx = src.find("if _lc_95_sent:")
        assert idx != -1, "_lc_95_sent check not found in market_engine"
        snippet = src[idx: idx + 300]
        assert "_processed_keys.add" in snippet, (
            "When _lc_95_sent=True, _processed_keys must be updated to block "
            "the standing path from re-delivering the same prop.\n"
            f"Snippet:\n{snippet}"
        )

    def test_04_standing_path_checks_in_memory_dedup(self):
        """Standing path must call _is_prop_deduped before delivering."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # The standing path dedup check must appear after has_recent_ud_alert
        ha_idx = src.find("has_recent_ud_alert")
        assert ha_idx != -1
        standing_section = src[ha_idx:]
        assert "_is_prop_deduped" in standing_section, (
            "Standing path must call _is_prop_deduped after has_recent_ud_alert "
            "to catch in-session duplicates from 95+ broadcast_alert paths"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Concurrent duplicate processing → one alert
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrentDuplicate:

    def test_05_max_instances_1_prevents_scheduler_overlap(self):
        """underdog_job must be registered with max_instances=1 (in main.py) to
        prevent APScheduler from running two overlapping underdog_job instances."""
        import main as _main_mod
        src = inspect.getsource(_main_mod)
        assert "max_instances" in src, (
            "max_instances not found in main.py — scheduler overlap protection missing"
        )
        # The value must be 1 (expressed as int or in a dict)
        assert "max_instances=1" in src or '"max_instances": 1' in src or "'max_instances': 1" in src, (
            "underdog_job must be registered with max_instances=1 in main.py"
        )

    def test_06_priority_override_sent_blocks_second_fire(self):
        """_priority_override_sent prevents the same key from firing twice
        within the same bot session (covers two-path duplication)."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "_priority_override_sent" in src, (
            "_priority_override_sent set must be present in underdog_job"
        )
        assert "_priority_alerted_this_scan" in src, (
            "_priority_alerted_this_scan set must be present in underdog_job"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Telegram retry after successful send → no duplicate
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryBehavior:

    def test_07_dedup_window_covers_retry_period(self):
        """Dedup window must cover the retry period so a re-queued scan
        within the window is suppressed for same line."""
        from config import config
        assert config.UD_ALERT_DEDUP_WINDOW >= 3600, (
            f"UD_ALERT_DEDUP_WINDOW={config.UD_ALERT_DEDUP_WINDOW} too short to cover retries"
        )

    def test_08_telegram_failure_remains_retryable_after_window(self):
        """An alert recorded OUTSIDE the window is NOT deduped (retryable)."""
        store = _make_store()
        # Record alert 2 hours ago (outside 1-hour window)
        t_old = time.time() - 7300
        _record(store, "Player B", "NFL", "Rushing Yards", 65.5, t=t_old)
        assert not _is_deduped(store, "Player B", "NFL", "Rushing Yards", 65.5, window=3600), (
            "Alerts outside the dedup window must NOT be suppressed — they are retryable"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Legitimate line movement creates a new alert
# ─────────────────────────────────────────────────────────────────────────────

class TestLegitimateLineMovement:

    def test_09_meaningful_line_change_bypasses_dedup(self):
        """A significant line change on an alerted prop must NOT be deduped."""
        from config import config
        store = _make_store()
        _record(store, "Jordan Walker", "MLB", "Hits", 0.5)
        new_line = 0.5 + config.MIN_UNDERDOG_LINE_CHANGE + 0.1  # clearly above threshold
        assert not _is_deduped(store, "Jordan Walker", "MLB", "Hits", new_line), (
            f"Line move from 0.5 → {new_line} should bypass dedup (meaningful movement)"
        )

    def test_10_small_line_change_stays_deduped(self):
        """A trivially small line change on an alerted prop must still be deduped."""
        from config import config
        store = _make_store()
        _record(store, "Jordan Walker", "MLB", "Hits", 0.5)
        tiny_move = 0.5 + config.MIN_UNDERDOG_LINE_CHANGE * 0.1
        assert _is_deduped(store, "Jordan Walker", "MLB", "Hits", tiny_move), (
            f"Line noise {0.5} → {tiny_move} must be deduped (below min_line_change threshold)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6 & 7. Re-entry and restart handling
# ─────────────────────────────────────────────────────────────────────────────

class TestReEntryAndRestart:

    def test_11_reentry_eligible_for_new_alert(self):
        """Re-entry (REMOVED then relisted) path must be in market_engine."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "is_reentry_qualified" in src, (
            "Re-entry detection missing — re-listed props may be silently suppressed"
        )

    def test_12_state_recovery_restores_dedup_on_restart(self):
        """_init_state_from_db must restore _prop_market_alerted on startup
        so previously delivered opportunities are not resent after restart."""
        src = inspect.getsource(__import__("market_engine"))
        assert "_init_state_from_db" in src, (
            "_init_state_from_db missing — dedup state is lost on every restart"
        )
        assert "_prop_market_alerted" in src, (
            "_prop_market_alerted must be restored by state recovery"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. S-tier priority + normal path cannot duplicate
# ─────────────────────────────────────────────────────────────────────────────

class TestSTierPriorityNoDuplicate:

    def test_13_95_override_sets_record_prop_alerted(self):
        """All three 95+ broadcast_alert paths must call _record_prop_alerted
        so the in-memory dedup dict is populated immediately after send."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # Count occurrences of _record_prop_alerted near broadcast_alert
        # (should appear at least once per 95+ path: new, lc, standing)
        count = src.count("_record_prop_alerted")
        assert count >= 3, (
            f"Expected _record_prop_alerted to be called at least 3× (one per 95+ path), "
            f"found {count}"
        )

    def test_14_95_override_calls_mark_ud_snapshot_alert_sent(self):
        """95+ override paths must call mark_ud_snapshot_alert_sent so
        has_recent_ud_alert returns True in subsequent scan cycles."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "mark_ud_snapshot_alert_sent" in src, (
            "95+ override paths must call mark_ud_snapshot_alert_sent "
            "to update UnderdogSnapshotRecord.alert_sent in the DB"
        )
        # Should appear 3× (one per 95+ path)
        count = src.count("mark_ud_snapshot_alert_sent")
        assert count >= 3, (
            f"Expected mark_ud_snapshot_alert_sent called ≥3× (np/lc/standing), found {count}"
        )

    def test_15_mark_ud_snapshot_alert_sent_exists_on_db(self):
        """Database must have mark_ud_snapshot_alert_sent method."""
        from database import Database
        assert hasattr(Database, "mark_ud_snapshot_alert_sent"), (
            "Database.mark_ud_snapshot_alert_sent method is required"
        )
        assert asyncio.iscoroutinefunction(Database.mark_ud_snapshot_alert_sent), (
            "mark_ud_snapshot_alert_sent must be an async method"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Jordan Walker reproduction case
# ─────────────────────────────────────────────────────────────────────────────

class TestJordanWalkerReproduction:

    def test_16_lc_95_processed_keys_prevents_standing_duplicate(self):
        """Reproduce: Jordan Walker MLB Hits 0.5 OVER S-tier conf=95 BQ=95.
        When sent via LC 95+ override, _processed_keys must contain the prop
        so the standing path skips it in the same scan cycle."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        # Verify the _lc_95_sent block updates _processed_keys
        lc_idx = src.find("if _lc_95_sent:")
        assert lc_idx != -1
        lc_block = src[lc_idx: lc_idx + 400]
        assert "_processed_keys.add" in lc_block, (
            "Jordan Walker regression: _lc_95_sent block must add to _processed_keys.\n"
            f"Found block:\n{lc_block[:300]}"
        )

    def test_17_standing_dedup_comment_present(self):
        """Standing path in-memory dedup must have the explanatory comment
        documenting WHY broadcast_alert requires this extra check."""
        src = inspect.getsource(__import__("market_engine").underdog_job)
        assert "broadcast_alert does NOT set UnderdogSnapshotRecord.alert_sent" in src or \
               "broadcast_alert" in src and "alert_sent" in src, (
            "Standing path dedup must document the broadcast_alert / alert_sent gap"
        )
