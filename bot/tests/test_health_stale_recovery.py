"""
test_health_stale_recovery.py

Focused tests for the /health stale-recovery display fix.

Requirements verified:
1. Old/stale recovery (≥6h) is labeled historical, not presented as current.
2. Recent recovery (<6h) still displays with ✅ and reason text.
3. Current-session restart reason is unaffected.
4. Heartbeat/job health rows are unaffected.
5. No regression on existing /health behavior.
6. last_recovery_age_hours() returns correct values from HealthTracker.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import engine.health as _hmod
from engine.health import HealthTracker
import commands as cmd_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_tracker(tmp_path: str | None = None) -> HealthTracker:
    """Return a fresh HealthTracker backed by a temp file."""
    import tempfile
    import pathlib
    p = pathlib.Path(tmp_path or tempfile.mktemp(suffix=".json"))
    ht = HealthTracker(path=p)
    ht.record_startup()
    ht.update_heartbeat()
    return ht


def _inject_recovery(
    tracker: HealthTracker,
    age_hours: float,
    job: str = "underdog_job",
    reason: str = "fail_streak=3: 'list' object has no attribute 'has_real_data'",
) -> None:
    """Directly inject a recovery event with the given age (in hours)."""
    ts_unix = time.time() - age_hours * 3600
    from engine.health import _now_iso  # type: ignore[attr-defined]
    event = {
        "job":     job,
        "reason":  reason,
        "ts":      _now_iso(),
        "ts_unix": ts_unix,
    }
    with tracker._lock:
        tracker._state["last_recovery_event"] = event
        tracker._state.setdefault("recovery_history", []).append(event)


def _run_cmd_health(ht: HealthTracker) -> str:
    """
    Run cmd_health with the given tracker injected via the module singleton
    and return all reply text joined by ' | '.
    """
    orig_tracker = _hmod._tracker
    _hmod._tracker = ht

    update = MagicMock()
    reply = AsyncMock()
    update.message.reply_text = reply
    context = MagicMock()

    # Bypass auth check
    orig_check = cmd_mod._check_allowed
    cmd_mod._check_allowed = lambda u: True

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cmd_mod.cmd_health(update, context))
    finally:
        loop.close()
        _hmod._tracker = orig_tracker
        cmd_mod._check_allowed = orig_check

    return " | ".join(str(c.args[0]) for c in reply.call_args_list)


# ─────────────────────────────────────────────────────────────────────────────
# 1. HealthTracker.last_recovery_age_hours()
# ─────────────────────────────────────────────────────────────────────────────

class TestLastRecoveryAgeHours(unittest.TestCase):

    def setUp(self):
        self.ht = _make_tracker()

    def test_returns_none_when_no_recovery(self):
        self.assertIsNone(self.ht.last_recovery_age_hours())

    def test_returns_correct_age_recent(self):
        _inject_recovery(self.ht, age_hours=0.5)
        age = self.ht.last_recovery_age_hours()
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 0.5, delta=0.05)

    def test_returns_correct_age_stale(self):
        _inject_recovery(self.ht, age_hours=61.0)
        age = self.ht.last_recovery_age_hours()
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 61.0, delta=0.1)

    def test_method_exists_on_healthtracker(self):
        self.assertTrue(callable(getattr(self.ht, "last_recovery_age_hours", None)))

    def test_no_ts_unix_returns_none(self):
        """If ts_unix is missing, method returns None gracefully."""
        with self.ht._lock:
            self.ht._state["last_recovery_event"] = {
                "job": "test_job",
                "reason": "x",
                "ts": "2026-08-06 10:00:00 UTC",
                # no ts_unix key
            }
        self.assertIsNone(self.ht.last_recovery_age_hours())

    def test_age_increases_over_time(self):
        _inject_recovery(self.ht, age_hours=2.0)
        age1 = self.ht.last_recovery_age_hours()
        time.sleep(0.05)
        age2 = self.ht.last_recovery_age_hours()
        self.assertGreater(age2, age1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Stale recovery (≥6h) is labeled historical in /health output
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleRecoveryLabel(unittest.TestCase):

    def setUp(self):
        self.ht = _make_tracker()

    def test_stale_61h_shows_historical_label(self):
        _inject_recovery(self.ht, age_hours=61.0, job="underdog_job")
        output = _run_cmd_health(self.ht)
        self.assertIn("historical", output.lower(),
                      "Stale recovery must be labeled 'historical' in /health output")

    def test_stale_61h_not_shown_with_green_checkmark(self):
        _inject_recovery(self.ht, age_hours=61.0, job="underdog_job")
        output = _run_cmd_health(self.ht)
        # Isolate just the "Last recovery:" line (not the whole reply which has ✅ for jobs)
        recovery_lines = [
            line for line in output.splitlines()
            if "Last recovery" in line
        ]
        self.assertTrue(recovery_lines, "Recovery line must still appear somewhere in output")
        for line in recovery_lines:
            self.assertNotIn("✅", line,
                             "Stale (61h) recovery must not use ✅ — must be ℹ️ or suppressed")

    def test_stale_6h_boundary_shows_historical(self):
        """At exactly the 6h boundary the event must be treated as historical."""
        _inject_recovery(self.ht, age_hours=6.0, job="test_job")
        output = _run_cmd_health(self.ht)
        self.assertIn("historical", output.lower())

    def test_stale_12h_shows_historical(self):
        _inject_recovery(self.ht, age_hours=12.0, job="underdog_job")
        output = _run_cmd_health(self.ht)
        self.assertIn("historical", output.lower())

    def test_reason_text_not_active_subline_for_stale(self):
        """The stale failure reason must not appear as a prominent ↳ sub-line."""
        _inject_recovery(
            self.ht, age_hours=61.0,
            reason="fail_streak=3: 'list' object has no attribute 'has_real_data'"
        )
        output = _run_cmd_health(self.ht)
        # A stale recovery reason must not be shown as an active ↳ error detail
        active_sublines = [
            seg for seg in output.split(" | ")
            if "↳" in seg and "fail_streak=3" in seg
            and "historical" not in seg.lower()
        ]
        self.assertEqual(
            len(active_sublines), 0,
            "Stale recovery reason must not appear as an active ↳ sub-line"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Recent recovery (<6h) still displays correctly
# ─────────────────────────────────────────────────────────────────────────────

class TestRecentRecoveryDisplay(unittest.TestCase):

    def setUp(self):
        self.ht = _make_tracker()

    def test_recent_30min_shows_green_checkmark(self):
        _inject_recovery(self.ht, age_hours=0.5, job="underdog_job",
                         reason="fail_streak=2: timeout")
        output = _run_cmd_health(self.ht)
        recovery_segs = [s for s in output.split(" | ") if "Last recovery" in s]
        self.assertTrue(recovery_segs, "Recovery section must appear")
        self.assertIn("✅", recovery_segs[0],
                      "Recent (30 min) recovery must show ✅")

    def test_recent_2h_shows_green_checkmark(self):
        _inject_recovery(self.ht, age_hours=2.0, job="underdog_job",
                         reason="fail_streak=1: connection reset")
        output = _run_cmd_health(self.ht)
        recovery_segs = [s for s in output.split(" | ") if "Last recovery" in s]
        self.assertTrue(recovery_segs)
        self.assertIn("✅", recovery_segs[0])

    def test_recent_reason_appears_as_subline(self):
        _inject_recovery(self.ht, age_hours=1.0, job="underdog_job",
                         reason="fail_streak=1: socket timeout error")
        output = _run_cmd_health(self.ht)
        self.assertIn("socket timeout error", output,
                      "Recent recovery reason must appear in /health output")

    def test_5h59m_is_treated_as_recent(self):
        """Just below the 6h boundary must still be treated as recent."""
        _inject_recovery(self.ht, age_hours=5.98)
        output = _run_cmd_health(self.ht)
        recovery_segs = [s for s in output.split(" | ") if "Last recovery" in s]
        self.assertIn("✅", recovery_segs[0],
                      "5h59m recovery must still show ✅ (below stale threshold)")

    def test_no_historical_label_for_recent(self):
        _inject_recovery(self.ht, age_hours=1.0)
        output = _run_cmd_health(self.ht)
        recovery_segs = [s for s in output.split(" | ") if "Last recovery" in s]
        for seg in recovery_segs:
            self.assertNotIn("historical", seg.lower(),
                             "Recent recovery must not be labeled historical")


# ─────────────────────────────────────────────────────────────────────────────
# 4. No recovery recorded — original fallback still works
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRecovery(unittest.TestCase):

    def setUp(self):
        self.ht = _make_tracker()

    def test_no_recovery_shows_no_events_message(self):
        output = _run_cmd_health(self.ht)
        self.assertIn("No recovery events recorded", output)

    def test_no_recovery_section_present(self):
        output = _run_cmd_health(self.ht)
        self.assertIn("Last recovery", output)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Source-level guards — staleness constant and logic are in commands.py
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceLevelGuards(unittest.TestCase):

    def _health_src(self) -> str:
        return inspect.getsource(cmd_mod.cmd_health)

    def test_stale_hours_constant_defined(self):
        self.assertIn("_RECOVERY_STALE_HOURS", self._health_src())

    def test_stale_hours_value_is_6(self):
        self.assertIn("_RECOVERY_STALE_HOURS = 6.0", self._health_src())

    def test_age_hours_method_called(self):
        self.assertIn("last_recovery_age_hours", self._health_src())

    def test_historical_label_present(self):
        self.assertIn("historical", self._health_src())

    def test_stale_gate_uses_ge_comparison(self):
        self.assertTrue(
            ">= _RECOVERY_STALE_HOURS" in self._health_src()
            or ">=_RECOVERY_STALE_HOURS" in self._health_src(),
            "cmd_health must use >= comparison against _RECOVERY_STALE_HOURS"
        )

    def test_method_exists_on_healthtracker_class(self):
        self.assertTrue(
            hasattr(HealthTracker, "last_recovery_age_hours"),
            "HealthTracker must expose last_recovery_age_hours()"
        )

    def test_global_error_staleness_gate_unchanged(self):
        """The 2-hour global error gate must not have been altered."""
        self.assertIn("_err_age_h < 2.0", self._health_src())

    def test_pipeline_failure_staleness_gate_unchanged(self):
        """The 2-hour pipeline failure gate must not have been altered."""
        self.assertIn("_pf_age_h < 2.0", self._health_src())


# ─────────────────────────────────────────────────────────────────────────────
# 6. Regression — existing /health sections unaffected
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionGuards(unittest.TestCase):

    def setUp(self):
        self.ht = _make_tracker()

    def test_jobs_section_still_present(self):
        output = _run_cmd_health(self.ht)
        self.assertIn("Jobs", output)

    def test_market_providers_section_still_present(self):
        output = _run_cmd_health(self.ht)
        self.assertIn("Market Providers", output)

    def test_underdog_pipeline_section_still_present(self):
        output = _run_cmd_health(self.ht)
        self.assertIn("Underdog Pipeline", output)

    def test_restart_reason_section_still_present(self):
        output = _run_cmd_health(self.ht)
        self.assertIn("Restart reason", output)

    def test_restarts_command_not_present(self):
        """cmd_restarts must remain removed."""
        self.assertFalse(
            hasattr(cmd_mod, "cmd_restarts"),
            "cmd_restarts must not exist"
        )

    def test_no_credentials_in_health_output(self):
        """API key names/values must never appear in /health output."""
        output = _run_cmd_health(self.ht)
        self.assertNotIn("ODDS_API_KEY=", output)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", output)

    def test_underdog_still_primary_in_src(self):
        src = inspect.getsource(cmd_mod.cmd_health)
        self.assertIn('"Underdog"', src)
        self.assertNotIn('"DraftKings"', src)
        self.assertNotIn('"FanDuel"', src)

    def test_stale_recovery_does_not_hide_job_green_status(self):
        """A stale recovery must not change green ✅ job icons."""
        _inject_recovery(self.ht, age_hours=61.0)
        output = _run_cmd_health(self.ht)
        # Underdog job has no fail_streak → should show ✅ somewhere in jobs area
        # (we can't guarantee order, just that ✅ appears at all outside recovery)
        self.assertIn("✅", output)

    def test_recent_and_stale_threshold_consistent(self):
        """5.99h is recent; 6.0h is stale — boundary is exact."""
        ht_recent = _make_tracker()
        _inject_recovery(ht_recent, age_hours=5.99)
        out_recent = _run_cmd_health(ht_recent)

        ht_stale = _make_tracker()
        _inject_recovery(ht_stale, age_hours=6.0)
        out_stale = _run_cmd_health(ht_stale)

        # recent → ✅, stale → historical
        # Use per-line check to isolate just the "Last recovery:" line
        rec_recent = [l for l in out_recent.splitlines() if "Last recovery" in l]
        rec_stale  = [l for l in out_stale.splitlines()  if "Last recovery" in l]

        self.assertIn("✅", rec_recent[0], "5.99h must be treated as recent (✅)")
        self.assertNotIn("✅", rec_stale[0], "6.0h must be treated as stale (no ✅)")
        self.assertIn("historical", rec_stale[0].lower())


if __name__ == "__main__":
    unittest.main()
