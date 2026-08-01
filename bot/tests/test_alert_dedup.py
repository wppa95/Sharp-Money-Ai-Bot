"""
test_alert_dedup.py — Contract tests for #86.

Verifies the time-based + line-delta dedup logic for the Underdog player-prop
alert system.  Tests use _is_prop_deduped() and _record_prop_alerted() from
engine.player_prop_market to exercise the exact logic used in the live cycle.
"""

from __future__ import annotations

import time
import pytest
from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted


# ── Constants used across tests ────────────────────────────────────────────────
WINDOW      = 3600          # 60 min — default UD_ALERT_DEDUP_WINDOW
MIN_CHANGE  = 0.5           # default MIN_UNDERDOG_LINE_CHANGE
PLAYER      = "Shohei Ohtani"
SPORT       = "MLB"
STAT        = "home_runs"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fresh_store() -> dict:
    """Return an empty dedup dict."""
    return {}


def _store_with_alert(
    player: str = PLAYER,
    sport: str  = SPORT,
    stat: str   = STAT,
    line: float = 1.5,
    ts_offset: float = 0,      # seconds relative to now (negative = in the past)
) -> dict:
    """Return a dedup dict pre-populated with one alert record."""
    store: dict = {}
    now_ts = time.time()
    _record_prop_alerted(store, player, sport, stat, line, now_ts=now_ts + ts_offset)
    return store


# ── _is_prop_deduped — first alert ────────────────────────────────────────────

class TestFirstAlert:
    def test_empty_store_never_deduped(self):
        store = _fresh_store()
        assert not _is_prop_deduped(store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE)

    def test_different_player_not_deduped(self):
        store = _store_with_alert(player=PLAYER, line=1.5)
        assert not _is_prop_deduped(store, "Other Player", SPORT, STAT, 1.5, WINDOW, MIN_CHANGE)

    def test_different_sport_not_deduped(self):
        store = _store_with_alert(sport="NBA", line=1.5)
        assert not _is_prop_deduped(store, PLAYER, "MLB", STAT, 1.5, WINDOW, MIN_CHANGE)

    def test_different_stat_not_deduped(self):
        store = _store_with_alert(stat="hits", line=1.5)
        assert not _is_prop_deduped(store, PLAYER, SPORT, "home_runs", 1.5, WINDOW, MIN_CHANGE)


# ── _is_prop_deduped — same line, within window ───────────────────────────────

class TestSameLineWithinWindow:
    def test_same_line_within_window_is_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=-10)  # 10 s ago
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE,
        )

    def test_same_line_zero_seconds_ago_is_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=0)
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE,
        )

    def test_same_line_just_inside_window_is_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=-(WINDOW - 1))
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE,
        )

    def test_line_change_less_than_min_is_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=-60)
        # Change of 0.4 < MIN_CHANGE (0.5) → still deduped
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.9, WINDOW, MIN_CHANGE,
        )

    def test_zero_min_change_exact_match_is_deduped(self):
        store = _store_with_alert(line=2.0, ts_offset=-60)
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 2.0, WINDOW, 0.5,
        )


# ── _is_prop_deduped — window expired ─────────────────────────────────────────

class TestWindowExpired:
    def test_same_line_after_window_not_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=-(WINDOW + 10))
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE,
        )

    def test_same_line_exactly_at_window_boundary(self):
        # ts_offset = -WINDOW means (now - last_ts) == WINDOW which is NOT < WINDOW
        store = _store_with_alert(line=1.5, ts_offset=-WINDOW)
        # time.time() returns float, so equality is unlikely but we test >= window
        result = _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE,
        )
        # Must NOT be deduped (window has elapsed)
        assert not result

    def test_zero_window_never_deduped_on_second_call(self):
        store = _store_with_alert(line=1.5, ts_offset=-1)
        # window=0 means (elapsed < 0) is always False → never deduped
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, 0, MIN_CHANGE,
        )


# ── _is_prop_deduped — significant line movement ──────────────────────────────

class TestSignificantLineMovement:
    def test_line_change_at_min_change_threshold_not_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=-60)
        # Change of exactly 0.5 = MIN_CHANGE → NOT same_line → allow alert
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 2.0, WINDOW, MIN_CHANGE,
        )

    def test_line_change_above_min_change_not_deduped(self):
        store = _store_with_alert(line=1.5, ts_offset=-60)
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 2.5, WINDOW, MIN_CHANGE,
        )

    def test_line_decrease_by_min_change_not_deduped(self):
        store = _store_with_alert(line=2.0, ts_offset=-60)
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE,
        )

    def test_significant_move_within_window_still_fires(self):
        """A big line move should always fire even if window has not expired."""
        store = _store_with_alert(line=1.5, ts_offset=-30)  # 30 s ago
        # Move from 1.5 to 3.0 (+1.5 units) → must NOT be deduped
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 3.0, WINDOW, MIN_CHANGE,
        )

    def test_tiny_move_within_window_is_deduped(self):
        """A tiny move (0.1 units) within the window must be deduped."""
        store = _store_with_alert(line=1.5, ts_offset=-30)
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.6, WINDOW, MIN_CHANGE,
        )


# ── _record_prop_alerted ───────────────────────────────────────────────────────

class TestRecordPropAlerted:
    def test_stores_key_in_dict(self):
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5)
        key = (PLAYER, SPORT, STAT)
        assert key in store

    def test_stores_tuple_of_timestamp_and_line(self):
        store = {}
        before = time.time()
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 2.5)
        after = time.time()
        key = (PLAYER, SPORT, STAT)
        ts, line = store[key]
        assert before <= ts <= after
        assert line == 2.5

    def test_overwriting_updates_timestamp_and_line(self):
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=1000.0)
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 2.0, now_ts=2000.0)
        key = (PLAYER, SPORT, STAT)
        ts, line = store[key]
        assert ts == 2000.0
        assert line == 2.0

    def test_now_ts_injection(self):
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=12345.0)
        key = (PLAYER, SPORT, STAT)
        ts, _ = store[key]
        assert ts == 12345.0

    def test_multiple_props_stored_independently(self):
        store = {}
        _record_prop_alerted(store, "Player A", "MLB", "hits",      1.5, now_ts=100.0)
        _record_prop_alerted(store, "Player B", "NBA", "points",   20.5, now_ts=200.0)
        assert len(store) == 2
        assert store[("Player A", "MLB", "hits")][1]   == 1.5
        assert store[("Player B", "NBA", "points")][1] == 20.5


# ── Round-trip test: record then check ────────────────────────────────────────

class TestRoundTrip:
    def test_record_then_check_same_line_within_window(self):
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=1000.0)
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE, now_ts=1010.0
        )

    def test_record_then_check_same_line_window_expired(self):
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=1000.0)
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE, now_ts=1000.0 + WINDOW + 1
        )

    def test_record_then_check_significant_line_move(self):
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=1000.0)
        # Line moves +1.0 within window → should NOT be deduped
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 2.5, WINDOW, MIN_CHANGE, now_ts=1060.0
        )

    def test_record_twice_updates_correctly(self):
        """After a re-alert, the clock resets and subsequent checks dedup from the new time."""
        store = {}
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=1000.0)
        # First check: window expired
        assert not _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE, now_ts=5000.0
        )
        # Record the second alert at ts=5000
        _record_prop_alerted(store, PLAYER, SPORT, STAT, 1.5, now_ts=5000.0)
        # Immediate re-check at ts=5001 → should be deduped again
        assert _is_prop_deduped(
            store, PLAYER, SPORT, STAT, 1.5, WINDOW, MIN_CHANGE, now_ts=5001.0
        )
