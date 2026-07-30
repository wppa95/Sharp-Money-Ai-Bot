"""
Tests for engine/timing.py — game timing filter.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from engine.timing import is_game_alertable, format_game_time_label


# ── Helpers ───────────────────────────────────────────────────────────────────

def _future(minutes: float) -> datetime:
    """Return a naive UTC datetime *minutes* from now."""
    return datetime.utcnow() + timedelta(minutes=minutes)


def _past(minutes: float) -> datetime:
    """Return a naive UTC datetime *minutes* ago."""
    return datetime.utcnow() - timedelta(minutes=minutes)


DEFAULT = dict(
    min_minutes=30,
    max_minutes=120,
    urgent_edge=8.0,
)


# ── is_game_alertable ─────────────────────────────────────────────────────────

class TestGameAlreadyStarted:
    def test_blocks_game_in_progress(self):
        ok, reason = is_game_alertable(_past(1), edge=5.0, **DEFAULT)
        assert ok is False
        assert "IN PROGRESS" in reason or "in progress" in reason.lower()

    def test_blocks_game_started_exactly_now(self):
        ok, reason = is_game_alertable(_past(0.001), edge=5.0, **DEFAULT)
        assert ok is False

    def test_blocks_regardless_of_edge(self):
        ok, _ = is_game_alertable(_past(5), edge=99.0, **DEFAULT)
        assert ok is False


class TestGameTooFarOut:
    def test_blocks_beyond_max_window(self):
        ok, reason = is_game_alertable(_future(130), edge=5.0, **DEFAULT)
        assert ok is False
        assert "130" in reason or "window" in reason.lower() or "starts" in reason.lower()

    def test_allows_at_max_boundary(self):
        ok, _ = is_game_alertable(_future(119), edge=5.0, **DEFAULT)
        assert ok is True

    def test_blocks_at_exactly_max(self):
        ok, _ = is_game_alertable(_future(121), edge=5.0, **DEFAULT)
        assert ok is False


class TestGameTooClose:
    def test_blocks_below_min_when_not_urgent(self):
        ok, reason = is_game_alertable(_future(15), edge=5.0, **DEFAULT)
        assert ok is False
        # Reason should mention urgency or threshold
        assert any(w in reason.lower() for w in ["urgent", "threshold", "close", "start"])

    def test_allows_below_min_when_urgent(self):
        # Edge >= urgent_edge should bypass the min-minutes gate
        ok, _ = is_game_alertable(_future(10), edge=9.0, **DEFAULT)
        assert ok is True

    def test_allows_at_urgent_edge_exactly(self):
        ok, _ = is_game_alertable(_future(20), edge=8.0, **DEFAULT)
        assert ok is True

    def test_blocks_just_below_urgent_edge(self):
        ok, _ = is_game_alertable(_future(20), edge=7.99, **DEFAULT)
        assert ok is False


class TestWindowInRange:
    def test_allows_game_inside_window(self):
        ok, reason = is_game_alertable(_future(60), edge=5.0, **DEFAULT)
        assert ok is True
        assert reason == ""

    def test_allows_at_min_boundary(self):
        # Add a small buffer so floating-point timing doesn't cause a flake
        ok, _ = is_game_alertable(_future(30.5), edge=5.0, **DEFAULT)
        assert ok is True

    def test_allows_just_past_min(self):
        ok, _ = is_game_alertable(_future(32), edge=5.0, **DEFAULT)
        assert ok is True


class TestNoneGameTime:
    def test_none_always_allows(self):
        ok, reason = is_game_alertable(None, edge=0.0, **DEFAULT)
        assert ok is True
        assert reason == ""


class TestAwareDatetime:
    def test_aware_datetime_normalised_correctly(self):
        # Timezone-aware future datetime — should still be allowed
        aware = datetime.now(timezone.utc) + timedelta(minutes=60)
        ok, _ = is_game_alertable(aware, edge=5.0, **DEFAULT)
        assert ok is True

    def test_aware_past_always_blocked(self):
        aware = datetime.now(timezone.utc) - timedelta(minutes=10)
        ok, _ = is_game_alertable(aware, edge=5.0, **DEFAULT)
        assert ok is False


# ── format_game_time_label ────────────────────────────────────────────────────

class TestFormatGameTimeLabel:
    def test_none_returns_empty(self):
        assert format_game_time_label(None) == ""

    def test_past_returns_in_progress(self):
        lbl = format_game_time_label(_past(5))
        assert lbl == "🔴 IN PROGRESS"

    def test_minutes_format(self):
        # Use 48 minutes so even after ~1s elapsed it still shows 47m
        lbl = format_game_time_label(_future(48))
        assert "m" in lbl
        assert "starts in" in lbl
        # Should NOT yet have switched to hours
        assert "h" not in lbl

    def test_hours_and_minutes_format(self):
        lbl = format_game_time_label(_future(90))
        assert "h" in lbl
        assert "starts in" in lbl

    def test_exactly_one_hour(self):
        # Add a small buffer so the label shows "1h" rather than "59m"
        lbl = format_game_time_label(_future(61))
        assert "1h" in lbl

    def test_aware_datetime_format(self):
        aware = datetime.now(timezone.utc) + timedelta(minutes=45)
        lbl = format_game_time_label(aware)
        assert "starts in" in lbl
        assert "m" in lbl
