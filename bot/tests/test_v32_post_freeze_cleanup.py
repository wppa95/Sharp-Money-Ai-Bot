"""
test_v32_post_freeze_cleanup.py

Focused tests for V3.2 post-freeze cleanup:
  Issue 1 — /alerts wording accurately describes what alert_sent=True means
  Issue 2 — User-facing time format changed to 12-hour AM/PM
  Issue 3 — Persistent bot memory: _prop_market_alerted rebuilt from DB on restart
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import commands as cmd_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _dt(year=2026, month=8, day=8, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute)


# ═════════════════════════════════════════════════════════════════════════════
# ISSUE 1 — /alerts wording
# ═════════════════════════════════════════════════════════════════════════════

class TestAlertsWording(unittest.TestCase):
    """Wording in /alerts must accurately describe what alert_sent=True proves."""

    def _alerts_src(self) -> str:
        return inspect.getsource(cmd_mod.cmd_alerts)

    def test_no_all_time_delivered_in_src(self):
        """'all-time delivered' must not appear in cmd_alerts source."""
        src = self._alerts_src()
        self.assertNotIn(
            "all-time delivered", src,
            "Must not claim 'delivered' — only 'sent' (Telegram API accepted) is provable"
        )

    def test_all_time_sent_present(self):
        """"all-time sent" must be the wording used."""
        src = self._alerts_src()
        self.assertIn(
            "all-time sent", src,
            "'all-time sent' must be used instead of 'all-time delivered'"
        )

    def test_shown_label_includes_window(self):
        """The 'N shown' label must mention the 72h window."""
        src = self._alerts_src()
        self.assertIn(
            "shown (last 72h)", src,
            "Should indicate the display window so users know it's not all-time"
        )

    def test_delivered_not_claimed_in_empty_case(self):
        """Empty-case text must also not claim delivery."""
        src = self._alerts_src()
        # The no-alerts-sent fallback line
        self.assertNotIn(
            "all-time delivered — use", src,
            "Empty-case fallback must not claim 'delivered'"
        )
        # Should use "sent" instead
        self.assertIn(
            "all-time sent", src
        )

    def test_alert_sent_is_canonical_source_mentioned_in_comment(self):
        """The canonical source (alert_sent=True) must be noted in a comment."""
        src = self._alerts_src()
        self.assertIn(
            "alert_sent", src,
            "cmd_alerts must reference alert_sent as canonical delivery source"
        )

    def test_no_telegram_message_id_claimed(self):
        """Must not claim telegram_message_id or confirmed delivery."""
        src = self._alerts_src()
        self.assertNotIn("telegram_message_id", src)
        self.assertNotIn("confirmed delivery", src)

    def test_sent_defined_as_api_accepted(self):
        """A comment or text must clarify that 'sent' = Telegram API accepted."""
        src = self._alerts_src()
        # Either in a comment or the output text
        self.assertTrue(
            "Telegram API accepted" in src or "API accepted" in src,
            "Must clarify that 'sent' means Telegram API accepted the send"
        )

    def test_72h_window_explicit_in_query(self):
        """since_hours=72 must appear — confirms the window is intentional."""
        src = self._alerts_src()
        self.assertIn("since_hours=72", src)


# ═════════════════════════════════════════════════════════════════════════════
# ISSUE 2 — 12-hour AM/PM time format
# ═════════════════════════════════════════════════════════════════════════════

class TestFmtUserTs(unittest.TestCase):
    """_fmt_user_ts() must produce correct 12-hour AM/PM output."""

    def fmt(self, hour: int, minute: int = 0, day: int = 8) -> str:
        return cmd_mod._fmt_user_ts(_dt(hour=hour, minute=minute, day=day))

    # ── Required conversions from spec ───────────────────────────────────────

    def test_midnight_is_12am(self):
        result = self.fmt(0, 0)
        self.assertEqual(result, "Aug 08 · 12:00 AM")

    def test_noon_is_12pm(self):
        result = self.fmt(12, 0)
        self.assertEqual(result, "Aug 08 · 12:00 PM")

    def test_1357_is_157pm(self):
        result = self.fmt(13, 57)
        self.assertEqual(result, "Aug 08 · 1:57 PM")

    def test_1705_is_505pm(self):
        result = self.fmt(17, 5)
        self.assertEqual(result, "Aug 08 · 5:05 PM")

    def test_2359_is_1159pm(self):
        result = self.fmt(23, 59)
        self.assertEqual(result, "Aug 08 · 11:59 PM")

    # ── Additional edge cases ─────────────────────────────────────────────────

    def test_1am_no_leading_zero(self):
        result = self.fmt(1, 0)
        self.assertIn("1:00 AM", result)
        self.assertNotIn("01:", result)

    def test_9am_no_leading_zero(self):
        result = self.fmt(9, 30)
        self.assertIn("9:30 AM", result)
        self.assertNotIn("09:", result)

    def test_none_returns_dash(self):
        self.assertEqual(cmd_mod._fmt_user_ts(None), "—")

    def test_output_contains_no_utc_suffix(self):
        result = self.fmt(17, 5)
        self.assertNotIn("UTC", result)

    def test_output_contains_date(self):
        result = self.fmt(17, 5, day=8)
        self.assertIn("Aug 08", result)

    def test_format_helper_exists_in_commands(self):
        self.assertTrue(
            callable(getattr(cmd_mod, "_fmt_user_ts", None)),
            "_fmt_user_ts must be a module-level function in commands.py"
        )

    def test_ev_timestamp_uses_fmt_user_ts(self):
        """cmd_ev must use _fmt_user_ts, not a raw strftime with %H:%M UTC."""
        src = inspect.getsource(cmd_mod.cmd_ev)
        self.assertNotIn("%H:%M UTC", src,
                         "cmd_ev must use _fmt_user_ts(), not %H:%M UTC")
        self.assertIn("_fmt_user_ts", src)

    def test_alerts_timestamp_uses_fmt_user_ts(self):
        """cmd_alerts must use _fmt_user_ts for the alert_sent_at display."""
        src = inspect.getsource(cmd_mod.cmd_alerts)
        self.assertNotIn("%H:%M UTC", src,
                         "cmd_alerts must use _fmt_user_ts(), not %H:%M UTC")
        self.assertIn("_fmt_user_ts", src)

    def test_stored_timestamps_unchanged(self):
        """_fmt_user_ts must not modify the original datetime object."""
        dt = _dt(hour=17, minute=5)
        original_hour = dt.hour
        cmd_mod._fmt_user_ts(dt)
        self.assertEqual(dt.hour, original_hour, "Original datetime must not be mutated")


class TestTimeFormatStorageUnchanged(unittest.TestCase):
    """Stored/internal timestamps must remain in UTC — only display is changed."""

    def test_database_stores_utcnow(self):
        """Database module must use datetime.utcnow() for alert_sent_at, not local time."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.mark_opportunity_alert_sent)
        self.assertIn("utcnow", src,
                      "Database must store UTC timestamps in mark_opportunity_alert_sent")

    def test_health_module_stores_utc(self):
        """engine/health.py must store _now_iso() UTC strings (not local time)."""
        import engine.health as hmod
        src = inspect.getsource(hmod._now_iso)
        self.assertIn("UTC", src,
                      "_now_iso() must produce UTC-marked strings")

    def test_fmt_user_ts_does_not_add_tz_info(self):
        """_fmt_user_ts must not add tzinfo to the original datetime."""
        dt = _dt(hour=12, minute=0)
        self.assertIsNone(dt.tzinfo)
        cmd_mod._fmt_user_ts(dt)
        self.assertIsNone(dt.tzinfo)


# ═════════════════════════════════════════════════════════════════════════════
# ISSUE 3 — Persistent bot memory / restart persistence
# ═════════════════════════════════════════════════════════════════════════════

class TestInitStateFromDb(unittest.TestCase):
    """_init_state_from_db() must restore both _MARKET_FIRST_ALERT and _prop_market_alerted."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_prop_market_alerted_restored(self):
        """_init_state_from_db must call get_recent_alerted_props_for_dedup and populate dedup dict."""
        import market_engine as me

        fake_sent_ts = time.time() - 30 * 60  # 30 min ago
        mock_db = AsyncMock()
        mock_db.get_first_alert_times_ud = AsyncMock(return_value={})
        mock_db.get_recent_alerted_props_for_dedup = AsyncMock(return_value={
            ("LeBron James", "NBA", "Points"): (fake_sent_ts, 27.5),
        })

        orig = dict(me._prop_market_alerted)
        me._prop_market_alerted.clear()

        self._run(me._init_state_from_db(mock_db))

        self.assertIn(
            ("LeBron James", "NBA", "Points"),
            me._prop_market_alerted,
            "_prop_market_alerted must be populated from DB after _init_state_from_db"
        )
        entry = me._prop_market_alerted[("LeBron James", "NBA", "Points")]
        self.assertAlmostEqual(entry[0], fake_sent_ts, delta=1)
        self.assertAlmostEqual(entry[1], 27.5, delta=0.01)

        # Cleanup
        me._prop_market_alerted.clear()
        me._prop_market_alerted.update(orig)

    def test_existing_dedup_entries_not_overwritten(self):
        """Pre-existing in-memory dedup entries must not be clobbered by DB restore."""
        import market_engine as me

        orig = dict(me._prop_market_alerted)
        me._prop_market_alerted.clear()

        existing_ts = time.time() - 5 * 60  # 5 min ago (newer than DB entry)
        me._prop_market_alerted[("Steph Curry", "NBA", "Points")] = (existing_ts, 30.0)

        db_ts = time.time() - 60 * 60  # 1 hour ago (older)
        mock_db = AsyncMock()
        mock_db.get_first_alert_times_ud = AsyncMock(return_value={})
        mock_db.get_recent_alerted_props_for_dedup = AsyncMock(return_value={
            ("Steph Curry", "NBA", "Points"): (db_ts, 29.5),
        })

        self._run(me._init_state_from_db(mock_db))

        # The in-memory entry should win (it was already there)
        entry = me._prop_market_alerted.get(("Steph Curry", "NBA", "Points"))
        self.assertIsNotNone(entry)
        self.assertAlmostEqual(entry[0], existing_ts, delta=1,
                                msg="Pre-existing in-memory entry must not be overwritten by DB restore")

        me._prop_market_alerted.clear()
        me._prop_market_alerted.update(orig)

    def test_market_first_alert_still_restored(self):
        """_MARKET_FIRST_ALERT restore must still work alongside the new dedup restore."""
        import market_engine as me

        ts_dt = datetime(2026, 8, 8, 12, 0, 0)
        mock_db = AsyncMock()
        mock_db.get_first_alert_times_ud = AsyncMock(return_value={
            ("Aaron Judge", "Home Runs"): ts_dt,
        })
        mock_db.get_recent_alerted_props_for_dedup = AsyncMock(return_value={})

        orig_mfa = dict(me._MARKET_FIRST_ALERT)
        me._MARKET_FIRST_ALERT.clear()

        self._run(me._init_state_from_db(mock_db))

        key = "Aaron Judge__Home Runs"
        self.assertIn(key, me._MARKET_FIRST_ALERT,
                      "_MARKET_FIRST_ALERT must still be restored by _init_state_from_db")

        me._MARKET_FIRST_ALERT.clear()
        me._MARKET_FIRST_ALERT.update(orig_mfa)

    def test_db_error_does_not_crash_init(self):
        """DB failure during dedup restore must be handled gracefully."""
        import market_engine as me

        mock_db = AsyncMock()
        mock_db.get_first_alert_times_ud = AsyncMock(return_value={})
        mock_db.get_recent_alerted_props_for_dedup = AsyncMock(
            side_effect=Exception("DB connection failed")
        )

        # Must not raise
        try:
            self._run(me._init_state_from_db(mock_db))
        except Exception as exc:
            self.fail(f"_init_state_from_db must not raise on DB error: {exc}")

    def test_get_recent_alerted_props_for_dedup_called(self):
        """_init_state_from_db must call get_recent_alerted_props_for_dedup."""
        import market_engine as me

        mock_db = AsyncMock()
        mock_db.get_first_alert_times_ud = AsyncMock(return_value={})
        mock_db.get_recent_alerted_props_for_dedup = AsyncMock(return_value={})

        self._run(me._init_state_from_db(mock_db))

        mock_db.get_recent_alerted_props_for_dedup.assert_called_once()

    def test_function_signature_in_source(self):
        """get_recent_alerted_props_for_dedup must exist in database.py."""
        import database as db_mod
        self.assertTrue(
            hasattr(db_mod.Database, "get_recent_alerted_props_for_dedup"),
            "Database must have get_recent_alerted_props_for_dedup()"
        )

    def test_init_state_from_db_declares_global_prop_market_alerted(self):
        """_init_state_from_db must declare _prop_market_alerted as global."""
        import market_engine as me
        src = inspect.getsource(me._init_state_from_db)
        # May appear as "global _prop_market_alerted" alone or combined on one line
        # e.g. "global _MARKET_FIRST_ALERT, _prop_market_alerted"
        has_global = any(
            "_prop_market_alerted" in part
            for line in src.splitlines()
            if line.strip().startswith("global")
            for part in [line]
        )
        self.assertTrue(has_global,
                        "_init_state_from_db must declare _prop_market_alerted as global")


class TestRestartPersistenceGuarantees(unittest.TestCase):
    """Verify that core state already persists across restarts via DB."""

    def test_prop_line_history_in_db(self):
        """PropLineHistory must be a DB-backed model (not in-memory only)."""
        import database as db_mod
        self.assertTrue(
            hasattr(db_mod, "PropLineHistory"),
            "PropLineHistory must be a database model"
        )
        # Must be an ORM table, not a plain dict
        from sqlalchemy.orm import DeclarativeBase
        self.assertTrue(
            hasattr(db_mod.PropLineHistory, "__tablename__"),
            "PropLineHistory must be an ORM-mapped table"
        )

    def test_prop_opportunity_log_in_db(self):
        """PropOpportunityLog (alert history) must be DB-backed."""
        import database as db_mod
        self.assertTrue(hasattr(db_mod, "PropOpportunityLog"))
        self.assertTrue(hasattr(db_mod.PropOpportunityLog, "__tablename__"))

    def test_clv_record_in_db(self):
        """CLVRecord must be DB-backed."""
        import database as db_mod
        self.assertTrue(hasattr(db_mod, "CLVRecord"))
        self.assertTrue(hasattr(db_mod.CLVRecord, "__tablename__"))

    def test_alert_clv_seed_in_db(self):
        """AlertCLVSeed (pending CLV) must be DB-backed."""
        import database as db_mod
        self.assertTrue(hasattr(db_mod, "AlertCLVSeed"))
        self.assertTrue(hasattr(db_mod.AlertCLVSeed, "__tablename__"))

    def test_prop_opportunity_log_has_alert_sent(self):
        """alert_sent field on PropOpportunityLog must be write-once DB column."""
        import database as db_mod
        col = getattr(db_mod.PropOpportunityLog, "alert_sent", None)
        self.assertIsNotNone(col, "PropOpportunityLog.alert_sent must exist")

    def test_prop_opportunity_log_has_alert_sent_at(self):
        import database as db_mod
        col = getattr(db_mod.PropOpportunityLog, "alert_sent_at", None)
        self.assertIsNotNone(col, "PropOpportunityLog.alert_sent_at must exist")

    def test_get_recent_alerted_props_method_exists(self):
        import database as db_mod
        self.assertTrue(
            callable(getattr(db_mod.Database, "get_recent_alerted_props_for_dedup", None))
        )

    def test_dedup_restore_does_not_reset_existing_history(self):
        """The restore must add entries, not clear the dict first."""
        import market_engine as me
        src = inspect.getsource(me._init_state_from_db)
        # Must NOT clear the dict before restoring
        self.assertNotIn(
            "_prop_market_alerted.clear()", src,
            "_init_state_from_db must not clear _prop_market_alerted before restore"
        )

    def test_restart_does_not_reset_scoring_thresholds(self):
        """Restart persistence must not touch scoring thresholds."""
        from config import config as cfg
        self.assertGreaterEqual(getattr(cfg, "UD_MIN_CONF_S", 80), 80)

    def test_no_duplicate_alert_guard_uses_dedup_dict(self):
        """Alert dedup still uses _prop_market_alerted (unchanged logic)."""
        import market_engine as me
        src_module = inspect.getsource(me)
        self.assertIn("_prop_market_alerted", src_module)
        self.assertIn("_is_prop_deduped", src_module)


# ═════════════════════════════════════════════════════════════════════════════
# Security — no credentials in output
# ═════════════════════════════════════════════════════════════════════════════

class TestSecurityGuards(unittest.TestCase):

    def test_no_api_key_value_in_cmd_alerts(self):
        src = inspect.getsource(cmd_mod.cmd_alerts)
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "reply_text" in stripped:
                self.assertNotIn("ODDS_API_KEY", stripped)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", stripped)

    def test_no_api_key_value_in_fmt_user_ts(self):
        src = inspect.getsource(cmd_mod._fmt_user_ts)
        self.assertNotIn("ODDS_API_KEY", src)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", src)

    def test_no_credentials_in_get_recent_alerted_props(self):
        import database as db_mod
        src = inspect.getsource(db_mod.Database.get_recent_alerted_props_for_dedup)
        self.assertNotIn("ODDS_API_KEY", src)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", src)


# ═════════════════════════════════════════════════════════════════════════════
# Regression guards — nothing else changed
# ═════════════════════════════════════════════════════════════════════════════

class TestRegressionGuards(unittest.TestCase):

    def test_scoring_thresholds_unchanged(self):
        import engine.ud_scoring as uds
        src = inspect.getsource(uds)
        self.assertIn("80", src)   # S threshold
        self.assertIn("65", src)   # A threshold

    def test_min_line_change_unchanged(self):
        from config import config as cfg
        self.assertEqual(getattr(cfg, "MIN_UNDERDOG_LINE_CHANGE", 0.5), 0.5)

    def test_bq_threshold_unchanged(self):
        from config import config as cfg
        self.assertEqual(getattr(cfg, "UD_STRICT_SPORT_MIN_BET_QUALITY", 95), 95)

    def test_underdog_still_primary(self):
        src = inspect.getsource(cmd_mod.cmd_health)
        self.assertIn('"Underdog"', src)
        self.assertNotIn('"DraftKings"', src)

    def test_mlb_under_still_blocked(self):
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        self.assertIn("MLB", src)
        self.assertIn("UNDER", src)

    def test_database_utc_timestamps_not_changed(self):
        """mark_opportunity_alert_sent must still use utcnow (not local time)."""
        import database as db_mod
        src = inspect.getsource(db_mod.Database.mark_opportunity_alert_sent)
        self.assertIn("utcnow", src)

    def test_cmd_alerts_still_queries_72h(self):
        """72-hour display window for /alerts must be unchanged."""
        src = inspect.getsource(cmd_mod.cmd_alerts)
        self.assertIn("72", src)

    def test_restarts_command_absent(self):
        self.assertFalse(hasattr(cmd_mod, "cmd_restarts"))

    def test_dedup_dict_format_tuple_preserved(self):
        """_record_prop_alerted must still store (ts, line) tuples."""
        from engine.player_prop_market import _record_prop_alerted
        d: dict = {}
        _record_prop_alerted(d, "TestPlayer", "NBA", "Points", 25.0)
        key = ("TestPlayer", "NBA", "Points")
        self.assertIn(key, d)
        entry = d[key]
        self.assertIsInstance(entry, tuple)
        self.assertEqual(len(entry), 2)
        self.assertAlmostEqual(entry[1], 25.0)


if __name__ == "__main__":
    unittest.main()
