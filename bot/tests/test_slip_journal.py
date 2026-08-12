"""
Tests for P9/P10/P11/P12 — Slip Journal DB methods, commands, and player history.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════════
# P9/P10 — SlipJournal and SlipJournalLeg ORM models exist
# ═══════════════════════════════════════════════════════════════════════════

class TestSlipJournalModels:
    """SlipJournal and SlipJournalLeg must be importable ORM classes."""

    def test_slip_journal_importable(self):
        from database import SlipJournal
        assert SlipJournal.__tablename__ == "slip_journal"

    def test_slip_journal_leg_importable(self):
        from database import SlipJournalLeg
        assert SlipJournalLeg.__tablename__ == "slip_journal_legs"

    def test_slip_journal_columns(self):
        from database import SlipJournal
        cols = {c.key for c in SlipJournal.__table__.columns}
        required = {"id", "slip_code", "created_at", "stake", "slip_type",
                    "status", "payout", "roi_pct", "notes", "graded_at"}
        assert required <= cols, f"Missing columns: {required - cols}"

    def test_slip_journal_leg_columns(self):
        from database import SlipJournalLeg
        cols = {c.key for c in SlipJournalLeg.__table__.columns}
        required = {"id", "slip_code", "opp_id", "player_name", "sport",
                    "stat_type", "line_value", "direction", "tier",
                    "confidence", "result", "actual_value", "graded_at"}
        assert required <= cols, f"Missing columns: {required - cols}"

    def test_slip_journal_default_status(self):
        from database import SlipJournal
        # Column defaults apply on DB insert, not plain construction.
        # Verify the column's default arg is set to "OPEN".
        col = SlipJournal.__table__.c["status"]
        assert col.default.arg == "OPEN"

    def test_slip_journal_leg_default_result(self):
        from database import SlipJournalLeg
        col = SlipJournalLeg.__table__.c["result"]
        assert col.default.arg == "PENDING"


# ═══════════════════════════════════════════════════════════════════════════
# P9/P10 — Database CRUD methods exist with correct signatures
# ═══════════════════════════════════════════════════════════════════════════

class TestSlipJournalDbMethods:
    """All slip journal DB methods must exist with correct signatures."""

    def setup_method(self):
        import inspect
        from database import Database
        self.Database = Database
        self.inspect  = inspect

    def _sig(self, method_name):
        return self.inspect.signature(getattr(self.Database, method_name))

    def test_create_slip_journal_exists(self):
        sig = self._sig("create_slip_journal")
        assert "stake" in sig.parameters
        assert "notes" in sig.parameters

    def test_get_open_slip_journal_exists(self):
        self._sig("get_open_slip_journal")  # just verify it exists

    def test_add_slip_journal_leg_exists(self):
        sig = self._sig("add_slip_journal_leg")
        assert "slip_code"   in sig.parameters
        assert "player_name" in sig.parameters
        assert "stat_type"   in sig.parameters

    def test_get_slip_journal_legs_exists(self):
        sig = self._sig("get_slip_journal_legs")
        assert "slip_code" in sig.parameters

    def test_grade_slip_journal_exists(self):
        sig = self._sig("grade_slip_journal")
        assert "slip_code" in sig.parameters
        assert "payout"    in sig.parameters

    def test_get_slip_journal_history_exists(self):
        sig = self._sig("get_slip_journal_history")
        assert "limit" in sig.parameters

    def test_get_slip_journal_stats_exists(self):
        self._sig("get_slip_journal_stats")

    def test_find_opportunity_for_slip_exists(self):
        sig = self._sig("find_opportunity_for_slip")
        assert "query" in sig.parameters

    def test_get_player_prop_history_exists(self):
        sig = self._sig("get_player_prop_history")
        assert "player_name" in sig.parameters
        assert "limit"       in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════
# P9/P10 — In-memory DB integration tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestSlipJournalIntegration:
    """Full round-trip tests using an in-memory SQLite database."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from database import Database
        self.db = Database("sqlite+aiosqlite:///:memory:")
        await self.db.init()
        yield
        await self.db.close()

    @pytest.mark.asyncio
    async def test_create_slip_returns_code(self):
        code = await self.db.create_slip_journal(stake=25.0)
        assert code.startswith("SLP-")
        assert len(code) == 7  # "SLP-001"

    @pytest.mark.asyncio
    async def test_create_multiple_slips_increments(self):
        c1 = await self.db.create_slip_journal()
        c2 = await self.db.create_slip_journal()
        assert c1 != c2
        n1 = int(c1.replace("SLP-", ""))
        n2 = int(c2.replace("SLP-", ""))
        assert n2 == n1 + 1

    @pytest.mark.asyncio
    async def test_get_open_slip_returns_none_when_empty(self):
        result = await self.db.get_open_slip_journal()
        assert result is None

    @pytest.mark.asyncio
    async def test_get_open_slip_after_create(self):
        code   = await self.db.create_slip_journal(stake=50.0)
        result = await self.db.get_open_slip_journal()
        assert result is not None
        assert result.slip_code == code
        assert result.status == "OPEN"
        assert result.stake == 50.0

    @pytest.mark.asyncio
    async def test_add_leg_to_slip(self):
        code = await self.db.create_slip_journal(stake=10.0)
        leg  = await self.db.add_slip_journal_leg(
            code, "LeBron James", "points",
            sport="NBA", line_value=25.5, direction="OVER", tier="S", confidence=88,
        )
        assert leg.player_name == "LeBron James"
        assert leg.stat_type   == "points"
        assert leg.direction   == "OVER"
        assert leg.line_value  == 25.5
        assert leg.result      == "PENDING"

    @pytest.mark.asyncio
    async def test_get_slip_journal_legs(self):
        code = await self.db.create_slip_journal()
        await self.db.add_slip_journal_leg(code, "Player A", "rebounds", sport="NBA", line_value=8.5)
        await self.db.add_slip_journal_leg(code, "Player B", "assists",  sport="NBA", line_value=6.5)
        legs = await self.db.get_slip_journal_legs(code)
        assert len(legs) == 2
        assert legs[0].player_name == "Player A"
        assert legs[1].player_name == "Player B"

    @pytest.mark.asyncio
    async def test_get_slip_journal_history(self):
        await self.db.create_slip_journal()
        await self.db.create_slip_journal()
        history = await self.db.get_slip_journal_history(limit=5)
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_grade_slip_no_opp_id_stays_pending(self):
        code = await self.db.create_slip_journal(stake=20.0)
        await self.db.add_slip_journal_leg(code, "Player A", "points", sport="NBA", line_value=22.5)
        summary = await self.db.grade_slip_journal(code)
        assert summary["total"]   == 1
        assert summary["pending"] == 1
        assert summary["graded"]  == 0

    @pytest.mark.asyncio
    async def test_grade_slip_stats_empty(self):
        stats = await self.db.get_slip_journal_stats()
        assert stats["total_slips"]  == 0
        assert stats["total_staked"] == 0.0
        assert stats["by_size"]      == {}

    @pytest.mark.asyncio
    async def test_find_opportunity_by_digit_returns_none_if_not_found(self):
        result = await self.db.find_opportunity_for_slip("9999")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_opportunity_by_name_returns_none_if_not_found(self):
        result = await self.db.find_opportunity_for_slip("NoSuchPlayer")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_player_prop_history_empty(self):
        rows = await self.db.get_player_prop_history("Nobody")
        assert rows == []


# ═══════════════════════════════════════════════════════════════════════════
# P9/P10 — Slip journal command signatures
# ═══════════════════════════════════════════════════════════════════════════

class TestSlipCommandSignatures:
    """_cmd_slip_journal, cmd_player, cmd_slipstats must exist in commands."""

    def test_cmd_slip_journal_helper_exists(self):
        from commands import _cmd_slip_journal
        import inspect
        sig = inspect.signature(_cmd_slip_journal)
        assert "args" in sig.parameters

    def test_cmd_slip_has_correct_signature(self):
        from commands import cmd_slip
        import inspect
        sig = inspect.signature(cmd_slip)
        assert "update"  in sig.parameters
        assert "context" in sig.parameters

    def test_cmd_player_exists(self):
        from commands import cmd_player
        import inspect
        sig = inspect.signature(cmd_player)
        assert "update"  in sig.parameters
        assert "context" in sig.parameters

    def test_cmd_slipstats_exists(self):
        from commands import cmd_slipstats
        import inspect
        sig = inspect.signature(cmd_slipstats)
        assert "update"  in sig.parameters
        assert "context" in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════
# P9/P10 — Journal subcommand routing
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestSlipSubcommandRouting:
    """cmd_slip routes journal subcommands and falls through for numeric args."""

    def _make_update(self, args):
        update  = MagicMock()
        update.effective_user.id = 7245518659
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = args
        return update, ctx

    @pytest.mark.asyncio
    async def test_create_subcommand_routed(self):
        """'create' subcommand must not fall into numeric parsing."""
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["create"])
        mock_db = MagicMock()
        mock_db.create_slip_journal = AsyncMock(return_value="SLP-001")
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["create"])
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "SLP-001" in msg

    @pytest.mark.asyncio
    async def test_create_with_stake_parses_stake(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["create", "50"])
        mock_db = MagicMock()
        mock_db.create_slip_journal = AsyncMock(return_value="SLP-002")
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["create", "50"])
        # DB called — stake=50.0 must be present in kwargs
        call_kwargs = mock_db.create_slip_journal.call_args
        assert call_kwargs.kwargs.get("stake") == 50.0 or (
            len(call_kwargs.args) > 0 and call_kwargs.args[0] == 50.0
        )

    @pytest.mark.asyncio
    async def test_journal_subcommand_shows_history(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["journal"])
        mock_db = MagicMock()
        mock_db.get_slip_journal_history = AsyncMock(return_value=[])
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["journal"])
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "Slip Journal" in msg

    @pytest.mark.asyncio
    async def test_add_without_args_shows_usage(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["add"])
        mock_db = MagicMock()
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["add"])
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg or "usage" in msg.lower()

    @pytest.mark.asyncio
    async def test_add_with_no_open_slip_prompts_create(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["add", "LeBron"])
        mock_db = MagicMock()
        mock_db.get_open_slip_journal = AsyncMock(return_value=None)
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["add", "LeBron"])
        msg = update.message.reply_text.call_args[0][0]
        assert "No open slip" in msg

    @pytest.mark.asyncio
    async def test_add_with_player_not_found(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["add", "UnknownPerson"])
        mock_db = MagicMock()
        slip = MagicMock()
        slip.slip_code = "SLP-001"
        mock_db.get_open_slip_journal = AsyncMock(return_value=slip)
        mock_db.find_opportunity_for_slip = AsyncMock(return_value=None)
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["add", "UnknownPerson"])
        msg = update.message.reply_text.call_args[0][0]
        assert "No matching" in msg

    @pytest.mark.asyncio
    async def test_add_with_valid_player_adds_leg(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["add", "LeBron"])
        mock_db = MagicMock()
        slip = MagicMock()
        slip.slip_code = "SLP-001"
        opp = MagicMock()
        opp.player_name  = "LeBron James"
        opp.stat_type    = "points"
        opp.recommendation = "OVER"
        opp.line_value   = 25.5
        opp.confidence   = 88
        opp.sport        = "NBA"
        opp.decision_tier = "S"
        opp.id           = 42
        opp.team         = "LAL"
        opp.game_time    = None
        mock_db.get_open_slip_journal     = AsyncMock(return_value=slip)
        mock_db.find_opportunity_for_slip = AsyncMock(return_value=opp)
        mock_db.add_slip_journal_leg      = AsyncMock(return_value=MagicMock())
        mock_db.get_slip_journal_legs     = AsyncMock(return_value=[MagicMock()])
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["add", "LeBron"])
        mock_db.add_slip_journal_leg.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "LeBron James" in msg

    @pytest.mark.asyncio
    async def test_grade_with_no_open_slip(self):
        from commands import _cmd_slip_journal
        update, ctx = self._make_update(["grade"])
        mock_db = MagicMock()
        mock_db.get_open_slip_journal = AsyncMock(return_value=None)
        mock_db.get_slip_journal_history = AsyncMock(return_value=[])
        with patch("commands._db", mock_db):
            await _cmd_slip_journal(update, ctx, ["grade"])
        msg = update.message.reply_text.call_args[0][0]
        assert "No slip" in msg


# ═══════════════════════════════════════════════════════════════════════════
# P11 — cmd_player tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestCmdPlayer:
    """cmd_player shows player history and hit rate."""

    def _make_update(self):
        update = MagicMock()
        update.effective_user.id = 7245518659
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        return update, ctx

    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from commands import cmd_player
        update, ctx = self._make_update()
        ctx.args = []
        mock_db = MagicMock()
        with patch("commands._db", mock_db):
            await cmd_player(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Usage" in msg or "/player" in msg

    @pytest.mark.asyncio
    async def test_no_rows_found(self):
        from commands import cmd_player
        update, ctx = self._make_update()
        ctx.args = ["Nobody"]
        mock_db = MagicMock()
        mock_db.get_player_prop_history = AsyncMock(return_value=[])
        with patch("commands._db", mock_db):
            await cmd_player(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "No tracked" in msg

    @pytest.mark.asyncio
    async def test_shows_hit_rate(self):
        from commands import cmd_player
        from datetime import datetime
        update, ctx = self._make_update()
        ctx.args = ["LeBron", "James"]

        def _make_opp(result, line=25.5):
            o = MagicMock()
            o.player_name   = "LeBron James"
            o.result        = result
            o.stat_type     = "points"
            o.sport         = "NBA"
            o.recommendation = "OVER"
            o.line_value    = line
            o.decision_tier = "S"
            o.confidence    = 88
            o.actual_value  = None
            o.detected_at   = datetime(2026, 8, 1)
            return o

        rows = [_make_opp("HIT"), _make_opp("HIT"), _make_opp("HIT"), _make_opp("MISS")]
        mock_db = MagicMock()
        mock_db.get_player_prop_history = AsyncMock(return_value=rows)
        with patch("commands._db", mock_db):
            await cmd_player(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "75%" in msg
        assert "LeBron James" in msg

    @pytest.mark.asyncio
    async def test_low_hit_rate_warning(self):
        from commands import cmd_player
        from datetime import datetime
        update, ctx = self._make_update()
        ctx.args = ["Player"]

        def _make_opp(result):
            o = MagicMock()
            o.player_name   = "Player X"
            o.result        = result
            o.stat_type     = "points"
            o.sport         = "NBA"
            o.recommendation = "OVER"
            o.line_value    = 20.5
            o.decision_tier = "B"
            o.confidence    = 60
            o.actual_value  = None
            o.detected_at   = datetime(2026, 8, 1)
            return o

        # 1 hit, 4 misses = 20% hit rate → warning
        rows = [_make_opp("HIT")] + [_make_opp("MISS")] * 4
        mock_db = MagicMock()
        mock_db.get_player_prop_history = AsyncMock(return_value=rows)
        with patch("commands._db", mock_db):
            await cmd_player(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Warning" in msg or "Caution" in msg


# ═══════════════════════════════════════════════════════════════════════════
# P12 — cmd_slipstats tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestCmdSlipStats:
    """cmd_slipstats shows pick accuracy and slip journal performance."""

    def _make_update(self):
        update = MagicMock()
        update.effective_user.id = 7245518659
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = []
        return update, ctx

    @pytest.mark.asyncio
    async def test_renders_empty_stats(self):
        from commands import cmd_slipstats
        update, ctx = self._make_update()
        mock_db = MagicMock()
        mock_db.get_slip_journal_stats = AsyncMock(return_value={
            "by_size": {}, "total_staked": 0.0, "total_payout": 0.0,
            "overall_roi": None, "total_slips": 0,
        })
        mock_db.get_pick_accuracy_by_sport = AsyncMock(return_value=[])
        with patch("commands._db", mock_db):
            await cmd_slipstats(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Performance Intelligence" in msg
        assert "SLIP JOURNAL" in msg

    @pytest.mark.asyncio
    async def test_renders_slip_stats_with_data(self):
        from commands import cmd_slipstats
        update, ctx = self._make_update()
        mock_db = MagicMock()
        mock_db.get_slip_journal_stats = AsyncMock(return_value={
            "by_size": {
                "2-man": {"win": 3, "loss": 1, "staked": 80.0, "payout": 240.0},
                "3-man": {"win": 1, "loss": 2, "staked": 60.0, "payout": 120.0},
            },
            "total_staked": 140.0,
            "total_payout": 360.0,
            "overall_roi":  157.1,
            "total_slips":  7,
        })
        mock_db.get_pick_accuracy_by_sport = AsyncMock(return_value=[
            {"sport": "NBA", "hits": 10, "misses": 3, "pushes": 0},
            {"sport": "MLB", "hits": 4,  "misses": 6, "pushes": 1},
        ])
        with patch("commands._db", mock_db):
            await cmd_slipstats(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "2-man" in msg
        assert "3-man" in msg
        assert "NBA"   in msg
        assert "157.1%" in msg or "+157.1%" in msg

    @pytest.mark.asyncio
    async def test_db_error_handled(self):
        from commands import cmd_slipstats
        update, ctx = self._make_update()
        mock_db = MagicMock()
        mock_db.get_slip_journal_stats = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("commands._db", mock_db):
            await cmd_slipstats(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "error" in msg.lower() or "DB" in msg


# ═══════════════════════════════════════════════════════════════════════════
# P7 — DK/FD disabled state verified
# ═══════════════════════════════════════════════════════════════════════════

class TestDkFdDisabledState:
    """DK/FD connectors are registered but remain disabled (P7 failsafe)."""

    def test_registry_keeps_disabled_connectors(self):
        from connectors.registry import ConnectorRegistry
        registry = ConnectorRegistry()

        mock_dk = MagicMock()
        mock_dk.name      = "DraftKings"
        mock_dk.enabled   = False
        mock_dk.is_pickem = False
        registry.register(mock_dk)

        assert mock_dk in registry.connectors
        assert mock_dk not in registry.enabled_connectors
        assert mock_dk not in registry.sportsbook_connectors

    async def test_fetch_all_skips_disabled(self):
        from connectors.registry import ConnectorRegistry

        registry = ConnectorRegistry()
        mock_dk = MagicMock()
        mock_dk.name      = "DraftKings"
        mock_dk.enabled   = False
        mock_dk.is_pickem = False
        registry.register(mock_dk)

        result = await registry.fetch_all()
        assert result == []
        mock_dk.fetch.assert_not_called()
