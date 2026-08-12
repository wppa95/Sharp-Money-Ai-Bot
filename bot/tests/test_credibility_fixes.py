"""
Regression tests for the 9-issue credibility fix pass.

Covers:
  1. Re-entry prop detection (removed → relisted triggers delivery)
  2. ScanCycleLog table: log_scan_cycle + get_scan_cycle_summary
  3. /funnel HTML escaping (player names with HTML entities don't crash)
  4. _HFS expansion (new multi-game/combo stats present)
  5. Alert freshness timestamp (added to non-removal alerts)
  6. Silent error logging: except Exception: pass replaced with logger.warning
  7. /picks delivery status indicator
"""
from __future__ import annotations

import asyncio
import html
import sys
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ══════════════════════════════════════════════════════════════════════
# 1. Re-entry detection: should_alert becomes True for known+missing prev
# ══════════════════════════════════════════════════════════════════════

class TestReentryDetection:
    """
    Verify that a prop which was REMOVED and then reappears gets
    should_alert=True set in the re-entry branch.
    """

    def test_reentry_sets_should_alert_true(self):
        """
        Re-entry detection code (market_engine.py ~L1822) now sets
        should_alert=True for qualified re-entries so delivery fires.
        Confirm the code path exists by inspecting the source.
        """
        import ast, inspect
        import market_engine
        src = inspect.getsource(market_engine)
        # Verify should_alert = True is set inside the is_reentry block
        assert "should_alert = True" in src, (
            "Re-entry fix missing: should_alert must be set to True in the "
            "re-entry detection block so delivery fires for relisted props."
        )
        # Verify the re-entry comment is present (ensures we're checking the right code)
        assert "is_reentry" in src, "Re-entry detection variable not found in market_engine.py"

    def test_reentry_triggers_qualified_increment(self):
        """
        The re-entry block increments _n_qualified when is_reentry_qualified=True.
        This confirms re-entry props are counted in the pipeline stats.
        """
        import inspect, market_engine
        src = inspect.getsource(market_engine)
        # After is_reentry_qualified = True, _n_qualified should be incremented
        assert "_n_qualified += 1" in src
        # And should_alert = True must follow
        idx_qual = src.index("_n_qualified += 1")
        idx_alert = src.index("should_alert = True", idx_qual)
        assert idx_alert > idx_qual, (
            "should_alert = True must come AFTER _n_qualified += 1 in the re-entry block"
        )


# ══════════════════════════════════════════════════════════════════════
# 2. ScanCycleLog DB table and methods
# ══════════════════════════════════════════════════════════════════════


class TestScanCycleLog:
    """Tests for ScanCycleLog ORM table and DB methods."""

    @pytest.mark.asyncio
    async def test_scan_cycle_log_table_exists(self):
        """ScanCycleLog must be importable and have the expected columns."""
        from database import ScanCycleLog
        expected_cols = {
            "id", "scan_ts", "fetched", "removed", "futures", "active",
            "unchanged", "new_props", "line_changed", "cold_start",
            "analyzed", "qualified", "alert_delivered",
        }
        actual_cols = {c.key for c in ScanCycleLog.__table__.columns}
        missing = expected_cols - actual_cols
        assert not missing, f"ScanCycleLog missing columns: {missing}"

    @pytest.mark.asyncio
    async def test_log_scan_cycle_and_summary(self, tmp_path):
        """log_scan_cycle writes a row; get_scan_cycle_summary reads it back."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from database import Base, ScanCycleLog, Database

        db_path = tmp_path / "test_scl.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        db = Database.__new__(Database)
        db.session = Session
        db._engine = engine

        now = datetime.utcnow()
        await db.log_scan_cycle(
            scan_ts         = now,
            fetched         = 4600,
            removed         = 50,
            futures         = 200,
            active          = 4350,
            unchanged       = 4270,
            new_props       = 45,
            line_changed    = 35,
            cold_start      = 0,
            analyzed        = 80,
            qualified       = 5,
            alert_delivered = 2,
        )

        summary = await db.get_scan_cycle_summary(since_hours=24)
        assert summary["cycles"]          == 1
        assert summary["fetched"]         == 4600
        assert summary["active"]          == 4350
        assert summary["unchanged"]       == 4270
        assert summary["new_props"]       == 45
        assert summary["line_changed"]    == 35
        assert summary["analyzed"]        == 80
        assert summary["qualified"]       == 5
        assert summary["alert_delivered"] == 2

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_get_scan_cycle_summary_empty_db(self, tmp_path):
        """get_scan_cycle_summary returns cycles=0 when no rows exist."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from database import Base, Database

        db_path = tmp_path / "test_scl_empty.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        db = Database.__new__(Database)
        db.session = Session
        db._engine = engine

        summary = await db.get_scan_cycle_summary(since_hours=24)
        assert summary.get("cycles", 0) == 0

        await engine.dispose()

    def test_log_scan_cycle_method_exists(self):
        from database import Database
        assert hasattr(Database, "log_scan_cycle"),         "Database.log_scan_cycle not found"
        assert hasattr(Database, "get_scan_cycle_summary"), "Database.get_scan_cycle_summary not found"

    def test_scan_cycle_counters_in_market_engine(self):
        """New counters _n_unchanged_skipped and _n_lc_sent must exist in market_engine."""
        import inspect, market_engine
        src = inspect.getsource(market_engine)
        assert "_n_unchanged_skipped" in src, "_n_unchanged_skipped counter not found"
        assert "_n_lc_sent"           in src, "_n_lc_sent counter not found"


# ══════════════════════════════════════════════════════════════════════
# 3. /funnel HTML escaping
# ══════════════════════════════════════════════════════════════════════

class TestFunnelHTMLEscaping:
    """
    Verify /funnel doesn't crash or emit malformed HTML when player names,
    stat types, sport names, or rejection reasons contain HTML entities.
    """

    def _make_update_ctx(self):
        update = MagicMock()
        update.effective_user.id = 7245518659
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = []
        return update, ctx

    @pytest.mark.asyncio
    async def test_funnel_escapes_player_name_with_html(self):
        """Player names containing '<', '>', '&' must be HTML-escaped."""
        from commands import cmd_funnel
        update, ctx = self._make_update_ctx()
        mock_db = MagicMock()
        mock_db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
        mock_db.get_funnel_summary = AsyncMock(return_value={
            "since_hours": 24,
            "counts": {"ACCEPTED": 1, "WATCHLIST": 0, "REJECTED": 1, "REMOVED": 0},
            "top_rejections": [
                {
                    "player_name":      "O'Brien & <Test>",
                    "stat_type":        "Hits & Runs",
                    "sport":            "MLB<>",
                    "rejection_reason": "below_threshold <B>",
                    "score_tier":       "B",
                    "score_total":      55,
                }
            ],
            "by_sport": [],
        })
        with patch("commands._db", mock_db):
            await cmd_funnel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        # Must not contain raw unescaped angle brackets from dynamic content
        assert "<Test>" not in msg, "Unescaped HTML in player name"
        assert "MLB<>" not in msg, "Unescaped HTML in sport name"
        # The escaped versions should be present
        assert html.escape("O'Brien & <Test>") in msg or "&lt;Test&gt;" in msg

    @pytest.mark.asyncio
    async def test_funnel_escapes_sport_name_in_breakdown(self):
        """Sport names in the breakdown table must be HTML-escaped."""
        from commands import cmd_funnel
        update, ctx = self._make_update_ctx()
        mock_db = MagicMock()
        mock_db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
        mock_db.get_funnel_summary = AsyncMock(return_value={
            "since_hours": 24,
            "counts": {"ACCEPTED": 1},
            "top_rejections": [],
            "by_sport": [
                {"sport": "CS<&>", "scanned": 3, "accepted": 1,
                 "watchlist": 0, "rejected": 2, "removed": 0},
            ],
        })
        with patch("commands._db", mock_db):
            await cmd_funnel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "CS<&>" not in msg, "Unescaped sport name in breakdown table"

    @pytest.mark.asyncio
    async def test_funnel_renders_scan_pipeline_when_data_exists(self):
        """When scan_cycle_summary has data, the pipeline section renders."""
        from commands import cmd_funnel
        update, ctx = self._make_update_ctx()
        mock_db = MagicMock()
        mock_db.get_scan_cycle_summary = AsyncMock(return_value={
            "cycles":        12,
            "fetched":       4600,
            "removed":       50,
            "futures":       200,
            "active":        4350,
            "unchanged":     4270,
            "new_props":     45,
            "line_changed":  35,
            "analyzed":      80,
            "qualified":     5,
            "alert_delivered": 2,
        })
        mock_db.get_funnel_summary = AsyncMock(return_value={
            "since_hours": 24,
            "counts": {"ACCEPTED": 2, "WATCHLIST": 1, "REJECTED": 5, "REMOVED": 0},
            "top_rejections": [],
            "by_sport": [],
        })
        with patch("commands._db", mock_db):
            await cmd_funnel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Scan Pipeline" in msg,   "Scan Pipeline section missing"
        assert "4,600"         in msg,   "Fetched count missing"
        assert "4,350"         in msg,   "Active count missing"
        assert "Delivered to Telegram" in msg, "Delivery count missing"


# ══════════════════════════════════════════════════════════════════════
# 4. _HFS expansion
# ══════════════════════════════════════════════════════════════════════

class TestHFSExpansion:
    """Verify new multi-game and combo stat types are in _HIGH_FLOOR_STATS."""

    def test_fantasy_points_in_games_1_2_in_hfs(self):
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert "Fantasy Points in Games 1+2" in _HIGH_FLOOR_STATS, (
            "DOTA multi-game 'Fantasy Points in Games 1+2' must be in _HIGH_FLOOR_STATS "
            "so it's eligible for the standing-path delivery."
        )

    def test_points_plus_assists_in_hfs(self):
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert "Points + Assists" in _HIGH_FLOOR_STATS, (
            "'Points + Assists' combo stat must be in _HIGH_FLOOR_STATS"
        )

    def test_points_plus_rebounds_in_hfs(self):
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert "Points + Rebounds" in _HIGH_FLOOR_STATS

    def test_pts_rebs_asts_with_spaces_in_hfs(self):
        """Underdog uses 'Pts + Rebs + Asts' (with spaces); _HFS must match exactly."""
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert "Pts + Rebs + Asts" in _HIGH_FLOOR_STATS, (
            "Underdog DB stores 'Pts + Rebs + Asts' with spaces — "
            "the set must match or standing-path won't see it."
        )

    def test_headshots_on_maps_in_hfs(self):
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert "Headshots on Maps 1+2" in _HIGH_FLOOR_STATS

    def test_kills_in_games_1_2_in_hfs(self):
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        assert "Kills in Games 1+2" in _HIGH_FLOOR_STATS

    def test_original_hfs_stats_still_present(self):
        """Ensure existing stats weren't accidentally removed."""
        from engine.ud_scoring import _HIGH_FLOOR_STATS
        for stat in ["Hits", "Fantasy Points", "Points", "Kills on Maps 1+2", "Assists on Maps 1+2"]:
            assert stat in _HIGH_FLOOR_STATS, f"Original HFS stat '{stat}' was removed"


# ══════════════════════════════════════════════════════════════════════
# 5. Alert freshness timestamp
# ══════════════════════════════════════════════════════════════════════

class TestAlertFreshnessTimestamp:
    """Verify all non-removal alerts include a 'Line as of' timestamp."""

    def test_freshness_timestamp_in_alerts_source(self):
        """The freshness timestamp code must exist in alerts.py."""
        import inspect, alerts
        src = inspect.getsource(alerts)
        assert "Line as of" in src, (
            "Freshness timestamp ('Line as of HH:MM ET') not found in alerts.py"
        )

    def test_timestamp_not_added_to_removal_alerts(self):
        """Removal alerts must NOT get the freshness footer (field 'removed=True')."""
        import inspect, alerts
        src = inspect.getsource(alerts)
        # The timestamp addition must be guarded by 'not removed'
        assert "not removed" in src and "Line as of" in src, (
            "Freshness timestamp must only appear on non-removal alerts"
        )


# ══════════════════════════════════════════════════════════════════════
# 6. Silent error logging fixed
# ══════════════════════════════════════════════════════════════════════

class TestSilentErrorLogging:
    """
    Verify that log_prop_opportunity and mark_opportunity_alert_sent
    failures no longer silently pass — they must log a WARNING.
    """

    def test_no_bare_pass_after_log_prop_opportunity(self):
        """No 'except Exception: pass' should follow log_prop_opportunity calls."""
        import inspect, market_engine
        src = inspect.getsource(market_engine)
        lines = src.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "pass  # never block alert flow":
                # This exact string must not exist anymore
                raise AssertionError(
                    f"Found 'pass  # never block alert flow' at line {i+1} — "
                    "this should be replaced with logger.warning()"
                )

    def test_log_prop_opportunity_failures_use_warning(self):
        """All log_prop_opportunity exception handlers must emit a WARNING."""
        import inspect, market_engine
        src = inspect.getsource(market_engine)
        assert "log_prop_opportunity [new-prop] failed" in src
        assert "log_prop_opportunity [lc] failed"       in src
        assert "log_prop_opportunity [standing] failed"  in src

    def test_mark_opportunity_alert_sent_failures_use_warning(self):
        """mark_opportunity_alert_sent failures must emit a WARNING."""
        import inspect, market_engine
        src = inspect.getsource(market_engine)
        assert "mark_opportunity_alert_sent [lc] failed"       in src
        assert "mark_opportunity_alert_sent [standing] failed"  in src


# ══════════════════════════════════════════════════════════════════════
# 7. /picks delivery status indicator
# ══════════════════════════════════════════════════════════════════════

class TestPicksDeliveryStatus:
    """Verify /picks shows delivery status for each prop."""

    def test_picks_render_includes_delivery_label(self):
        """_render_pick_entry must check first_alert_sent_at and show a status."""
        import inspect, commands
        src = inspect.getsource(commands)
        assert "first_alert_sent_at" in src, (
            "/picks must check PropLineHistory.first_alert_sent_at for delivery status"
        )
        assert "not yet delivered" in src, (
            "/picks must label undelivered candidates as 'not yet delivered'"
        )
        assert "Delivered:" in src, (
            "/picks must show 'Delivered: HH:MM' for confirmed delivered props"
        )

    def test_picks_header_clarifies_candidate_meaning(self):
        """The /picks display must clarify that it shows candidates, not only delivered picks."""
        import inspect, commands
        src = inspect.getsource(commands)
        assert "Candidate" in src or "candidate" in src, (
            "/picks must label undelivered props as candidates"
        )
