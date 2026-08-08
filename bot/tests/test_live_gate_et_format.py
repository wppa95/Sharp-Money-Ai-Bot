"""
test_live_gate_et_format.py

Tests for:
  - _is_game_live_or_past()  (Priority #5 — game-live hard gate)
  - _format_game_time_et()   (Priority #7 — readable ET time format)
  - Config helper methods: min_stars_for_sport, min_conf_for_sport_tier  (#2)
  - Explicit scheduler max_instances configuration  (#18)
  - MLB/NFL BQ ≥ 85 gate  (#1)

Alex Bregman regression: game already live for 30 min → alert must be blocked.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# _is_game_live_or_past  — Priority #5
# ─────────────────────────────────────────────────────────────────────────────

def _make_snap(game_time=None, game_status=None):
    """Build a minimal snap-like object."""
    ns = SimpleNamespace()
    if game_time is not None:
        ns.game_time = game_time
    else:
        ns.game_time = None
    if game_status is not None:
        ns.game_status = game_status
    # no game_status attribute if not provided → getattr fallback to None
    return ns


class TestIsGameLiveOrPast:
    """Tests for market_engine._is_game_live_or_past()."""

    def _fn(self):
        from market_engine import _is_game_live_or_past
        return _is_game_live_or_past

    def _now(self):
        return datetime.utcnow()

    # ── game_time checks ──────────────────────────────────────────────────────

    def test_future_game_not_blocked(self):
        fn = self._fn()
        snap = _make_snap(game_time=datetime.utcnow() + timedelta(hours=2))
        assert fn(snap, self._now()) is False

    def test_game_time_past_is_blocked(self):
        """Alex Bregman regression: game started 30 min ago → must be blocked."""
        fn = self._fn()
        snap = _make_snap(game_time=datetime.utcnow() - timedelta(minutes=30))
        assert fn(snap, self._now()) is True

    def test_game_time_none_allows_through(self):
        """Many valid props lack a scheduled time — must not be blocked."""
        fn = self._fn()
        snap = _make_snap(game_time=None)
        assert fn(snap, self._now()) is False

    def test_game_just_started_is_blocked(self):
        """Game that started 1 second ago must be blocked."""
        fn = self._fn()
        now = datetime.utcnow()
        snap = _make_snap(game_time=now - timedelta(seconds=1))
        assert fn(snap, now) is True

    # ── game_status field ─────────────────────────────────────────────────────

    def test_live_status_blocks_even_with_future_game_time(self):
        """LIVE status overrides a future game_time (Underdog status field)."""
        fn = self._fn()
        snap = _make_snap(
            game_time=datetime.utcnow() + timedelta(hours=1),
            game_status="LIVE",
        )
        assert fn(snap, self._now()) is True

    def test_in_progress_status_blocked(self):
        fn = self._fn()
        snap = _make_snap(game_status="IN_PROGRESS")
        assert fn(snap, self._now()) is True

    def test_final_status_blocked(self):
        fn = self._fn()
        snap = _make_snap(game_status="FINAL")
        assert fn(snap, self._now()) is True

    def test_completed_status_blocked(self):
        fn = self._fn()
        snap = _make_snap(game_status="COMPLETED")
        assert fn(snap, self._now()) is True

    def test_closed_status_blocked(self):
        fn = self._fn()
        snap = _make_snap(game_status="closed")  # lowercase
        assert fn(snap, self._now()) is True

    def test_open_status_not_blocked(self):
        fn = self._fn()
        snap = _make_snap(game_status="OPEN")
        assert fn(snap, self._now()) is False

    def test_no_game_status_attribute_not_blocked(self):
        """snap without game_status attr at all must not raise."""
        fn = self._fn()
        snap = SimpleNamespace(game_time=None)  # no game_status
        assert fn(snap, self._now()) is False


# ─────────────────────────────────────────────────────────────────────────────
# _format_game_time_et  — Priority #7
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatGameTimeEt:
    """Tests for alerts_multiplatform._format_game_time_et()."""

    def _fn(self):
        from alerts_multiplatform import _format_game_time_et
        return _format_game_time_et

    def test_returns_string(self):
        fn = self._fn()
        dt = datetime(2026, 8, 7, 20, 40, 0)  # 20:40 UTC = 16:40 EDT = 4:40 PM ET
        result = fn(dt)
        assert isinstance(result, str)

    def test_output_contains_et(self):
        fn = self._fn()
        dt = datetime(2026, 8, 7, 20, 40, 0)
        result = fn(dt)
        assert "ET" in result, f"Expected 'ET' in result, got: {result!r}"

    def test_output_contains_am_or_pm(self):
        fn = self._fn()
        dt = datetime(2026, 8, 7, 20, 40, 0)
        result = fn(dt)
        assert "AM" in result or "PM" in result, f"Expected AM/PM in result, got: {result!r}"

    def test_does_not_contain_utc(self):
        """Telegram output must not show UTC."""
        fn = self._fn()
        dt = datetime(2026, 8, 7, 20, 40, 0)
        result = fn(dt)
        # The formatter falls back to UTC only on failure; in normal operation, ET is used.
        try:
            from zoneinfo import ZoneInfo  # noqa: F401
            assert "UTC" not in result, f"Expected no 'UTC' in result, got: {result!r}"
        except ImportError:
            pass  # fallback mode is acceptable

    def test_no_leading_zero_on_hour(self):
        """8:40 PM ET not 08:40 PM ET."""
        fn = self._fn()
        # 20:40 UTC = 4:40 PM EDT (UTC-4 in summer)
        dt = datetime(2026, 8, 7, 20, 40, 0)
        result = fn(dt)
        # Should not start with "0"
        assert not result.startswith("0"), f"Leading zero found: {result!r}"

    def test_midnight_utc_is_readable(self):
        """UTC midnight should convert to a readable afternoon/evening ET time."""
        fn = self._fn()
        dt = datetime(2026, 8, 7, 0, 0, 0)  # midnight UTC = 8 PM ET previous day
        result = fn(dt)
        assert "ET" in result


# ─────────────────────────────────────────────────────────────────────────────
# Config helper methods  — Priority #2
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigHelperMethods:
    """Tests for min_stars_for_sport() and min_conf_for_sport_tier()."""

    def _cfg(self, **overrides):
        import config as cfg_mod
        c = cfg_mod.Config()
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    # ── min_stars_for_sport ───────────────────────────────────────────────────

    def test_mlb_uses_strict_stars(self):
        c = self._cfg()
        assert c.min_stars_for_sport("MLB") == c.UD_MIN_STARS_TO_ALERT

    def test_nfl_uses_strict_stars(self):
        c = self._cfg()
        assert c.min_stars_for_sport("NFL") == c.UD_MIN_STARS_TO_ALERT

    def test_other_sport_uses_relaxed_stars(self):
        c = self._cfg()
        for sport in ("CS", "TENNIS", "NBA", "WNBA", "LOL", "MMA"):
            result = c.min_stars_for_sport(sport)
            assert result == c.UD_NON_STRICT_MIN_STARS, (
                f"{sport}: expected {c.UD_NON_STRICT_MIN_STARS}, got {result}"
            )

    def test_relaxed_stars_lower_than_strict(self):
        c = self._cfg()
        assert c.UD_NON_STRICT_MIN_STARS < c.UD_MIN_STARS_TO_ALERT

    def test_case_insensitive(self):
        c = self._cfg()
        assert c.min_stars_for_sport("mlb") == c.min_stars_for_sport("MLB")
        assert c.min_stars_for_sport("cs") == c.min_stars_for_sport("CS")

    # ── min_conf_for_sport_tier ───────────────────────────────────────────────

    def test_s_tier_same_for_all_sports(self):
        """S-tier threshold is uniform — no relaxation for S."""
        c = self._cfg()
        mlb_s = c.min_conf_for_sport_tier("MLB", "S")
        cs_s  = c.min_conf_for_sport_tier("CS",  "S")
        assert mlb_s == cs_s == c.UD_MIN_CONF_S

    def test_a_tier_strict_for_mlb(self):
        c = self._cfg()
        assert c.min_conf_for_sport_tier("MLB", "A") == c.UD_MIN_CONF_A

    def test_a_tier_relaxed_for_non_strict(self):
        c = self._cfg()
        cs_a = c.min_conf_for_sport_tier("CS", "A")
        assert cs_a == c.UD_NON_STRICT_MIN_CONF_A
        assert cs_a < c.UD_MIN_CONF_A

    def test_b_tier_strict_for_nfl(self):
        c = self._cfg()
        assert c.min_conf_for_sport_tier("NFL", "B") == c.UD_MIN_CONF_B

    def test_b_tier_relaxed_for_tennis(self):
        c = self._cfg()
        tennis_b = c.min_conf_for_sport_tier("TENNIS", "B")
        assert tennis_b == c.UD_NON_STRICT_MIN_CONF_B
        assert tennis_b < c.UD_MIN_CONF_B

    def test_unknown_tier_returns_zero(self):
        c = self._cfg()
        assert c.min_conf_for_sport_tier("MLB", "Z") == 0
        assert c.min_conf_for_sport_tier("CS",  "Z") == 0


# ─────────────────────────────────────────────────────────────────────────────
# MLB/NFL Bet Quality ≥ 85 gate  — Priority #1
# ─────────────────────────────────────────────────────────────────────────────

class TestStrictSportBQConfig:
    """Tests for UD_STRICT_SPORT_MIN_BET_QUALITY default and property."""

    def _cfg(self, **overrides):
        import config as cfg_mod
        c = cfg_mod.Config()
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    def test_default_bq_is_95(self):
        """Default MLB/NFL BQ threshold is 95 (raised from 85 — fix #117)."""
        c = self._cfg()
        assert c.UD_STRICT_SPORT_MIN_BET_QUALITY == 95

    def test_bq_94_would_be_below_threshold(self):
        """BQ=94 < 95 → should be blocked by gate logic."""
        c = self._cfg()
        assert 94 < c.UD_STRICT_SPORT_MIN_BET_QUALITY

    def test_bq_95_meets_threshold(self):
        """BQ=95 == 95 → should be allowed by gate logic."""
        c = self._cfg()
        assert 95 >= c.UD_STRICT_SPORT_MIN_BET_QUALITY

    def test_bq_threshold_configurable(self):
        """UD_STRICT_SPORT_MIN_BET_QUALITY can be overridden."""
        c = self._cfg(UD_STRICT_SPORT_MIN_BET_QUALITY=90)
        assert c.UD_STRICT_SPORT_MIN_BET_QUALITY == 90

    def test_mlb_and_nfl_in_strict_sports(self):
        """Only MLB and NFL are subject to the BQ gate."""
        c = self._cfg()
        assert "MLB" in c.ud_strict_alert_sports
        assert "NFL" in c.ud_strict_alert_sports
        assert "NBA" not in c.ud_strict_alert_sports


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler explicit max_instances  — Priority #18
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerMaxInstances:
    """Verify underdog_monitor is scheduled with explicit max_instances=1."""

    def test_underdog_job_kwargs_max_instances(self):
        """
        main.py must schedule underdog_monitor with max_instances=1 explicitly
        so APScheduler never runs two scans concurrently.
        """
        import ast
        import pathlib
        src = pathlib.Path(__file__).parent.parent / "main.py"
        tree = ast.parse(src.read_text())

        found = False
        for node in ast.walk(tree):
            # Look for run_repeating(..., job_kwargs={...}) calls
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "run_repeating"):
                continue
            # Check for name="underdog_monitor" keyword
            name_kw = next(
                (kw for kw in node.keywords if kw.arg == "name"), None
            )
            if name_kw is None:
                continue
            name_val = getattr(name_kw.value, "s", None) or getattr(name_kw.value, "value", None)
            if name_val != "underdog_monitor":
                continue
            # Found the underdog_monitor call — check job_kwargs
            jk_kw = next(
                (kw for kw in node.keywords if kw.arg == "job_kwargs"), None
            )
            assert jk_kw is not None, (
                "underdog_monitor run_repeating() must have a job_kwargs= argument"
            )
            # job_kwargs must be a dict literal containing max_instances: 1
            assert isinstance(jk_kw.value, ast.Dict), "job_kwargs must be a dict literal"
            keys = [getattr(k, "s", None) or getattr(k, "value", None)
                    for k in jk_kw.value.keys]
            assert "max_instances" in keys, (
                "job_kwargs must include max_instances"
            )
            max_idx = keys.index("max_instances")
            max_val_node = jk_kw.value.values[max_idx]
            max_val = getattr(max_val_node, "n", None) or getattr(max_val_node, "value", None)
            assert max_val == 1, f"max_instances must be 1, got {max_val}"
            found = True
            break

        assert found, (
            "Could not find underdog_monitor run_repeating() call with job_kwargs in main.py"
        )
