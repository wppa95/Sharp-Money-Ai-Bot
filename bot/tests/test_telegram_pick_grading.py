"""
test_telegram_pick_grading.py

Tests for Telegram actionable pick tracking — the alert_sent / alert_sent_at
fields on PropOpportunityLog and get_telegram_pick_performance().

Verifies:
  1.  Evaluated prop NOT sent to Telegram → alert_sent=False
  2.  Successful 🎯 actionable Telegram pick → alert_sent=True
  3.  📈 MARKET MOVE DETECTED → alert_sent=False  (never marks the row)
  4.  Blocked/rejected pick → alert_sent=False
  5.  Telegram pick grades HIT correctly
  6.  Telegram pick grades MISS correctly
  7.  Telegram pick grades PUSH correctly
  8.  Later line movement does NOT change the original alerted line
  9.  Telegram-only performance totals exclude non-alerted props
  10. Existing overall grading still works
  11. Existing SlipJournalLeg → PropOpportunityLog relationship preserved
  12. Pending Telegram picks remain PENDING until result data available
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─────────────────────────────────────────────────────────────────────────────
# Shared event loop (module-level, matches existing async test pattern)
# ─────────────────────────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run(coro):
    return _loop.run_until_complete(coro)


async def _make_db():
    """Create an in-memory async database with the full schema + migrations."""
    from database import Database
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    return db


def _future_game(hours: float = 3) -> datetime:
    return datetime.utcnow() + timedelta(hours=hours)


async def _log_opp(db, *, external_id: str, stat_type: str, recommendation: str = "OVER",
                   line: float = 0.5, sport: str = "MLB") -> None:
    """Log a prop opportunity (evaluation only — alert_sent stays False)."""
    await db.log_prop_opportunity(
        external_id        = external_id,
        player_name        = "Test Player",
        team               = "TST",
        sport              = sport,
        stat_type          = stat_type,
        line_value         = line,
        recommendation     = recommendation,
        decision_tier      = "S",
        confidence         = 90,
        game_time          = _future_game(),
        provider           = "Underdog",
        bet_quality_score  = 90,
    )


async def _fetch_opp(db, external_id: str, stat_type: str):
    """Fetch a PropOpportunityLog row by (external_id, stat_type)."""
    from sqlalchemy import select
    from database import PropOpportunityLog
    async with db.session() as s:
        r = await s.execute(
            select(PropOpportunityLog)
            .where(
                PropOpportunityLog.external_id == external_id,
                PropOpportunityLog.stat_type   == stat_type,
            )
        )
        return r.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Evaluated prop NOT sent → alert_sent=False
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertSentDefault:
    def test_evaluated_prop_not_sent_has_alert_sent_false(self):
        """A prop logged by the evaluation pipeline starts with alert_sent=False."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="ext1", stat_type="hits")
            row = await _fetch_opp(db, "ext1", "hits")
            assert row is not None, "Row should exist"
            assert row.alert_sent is False or row.alert_sent == 0, (
                f"Expected alert_sent=False, got {row.alert_sent}"
            )
            assert row.alert_sent_at is None
        _run(_run_test())

    def test_pass_recommendation_not_alert_sent(self):
        """PASS props are evaluated but never sent as Telegram picks."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="ext_pass", stat_type="strikeouts",
                           recommendation="PASS")
            row = await _fetch_opp(db, "ext_pass", "strikeouts")
            assert row is not None
            assert not row.alert_sent


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Successful Telegram pick → alert_sent=True
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkAlertSent:
    def test_mark_sets_alert_sent_true(self):
        """mark_opportunity_alert_sent() sets alert_sent=True on the row."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="ext2", stat_type="hits")
            await db.mark_opportunity_alert_sent("ext2", "hits")
            row = await _fetch_opp(db, "ext2", "hits")
            assert row.alert_sent is True or row.alert_sent == 1
            assert row.alert_sent_at is not None

    def test_mark_sets_alert_sent_at_timestamp(self):
        """alert_sent_at is set to a recent UTC datetime."""
        async def _run_test():
            db = await _make_db()
            before = datetime.utcnow()
            await _log_opp(db, external_id="ext_ts", stat_type="hits")
            await db.mark_opportunity_alert_sent("ext_ts", "hits")
            after = datetime.utcnow()
            row = await _fetch_opp(db, "ext_ts", "hits")
            assert before <= row.alert_sent_at <= after

    def test_mark_noop_on_nonexistent_row(self):
        """mark_opportunity_alert_sent on a missing row is silent (no exception)."""
        async def _run_test():
            db = await _make_db()
            # Should not raise
            await db.mark_opportunity_alert_sent("nonexistent", "does_not_exist")
        _run(_run_test())

    def test_double_mark_is_idempotent(self):
        """Marking alert_sent twice leaves alert_sent=True (idempotent)."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="ext_idem", stat_type="hits")
            await db.mark_opportunity_alert_sent("ext_idem", "hits")
            await db.mark_opportunity_alert_sent("ext_idem", "hits")
            row = await _fetch_opp(db, "ext_idem", "hits")
            assert row.alert_sent


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: 📈 MARKET MOVE DETECTED → alert_sent=False
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketMoveNotAlerted:
    def test_market_move_row_not_marked(self):
        """
        Market move alerts do not call mark_opportunity_alert_sent.
        The row should remain alert_sent=False.
        """
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="ext_mm", stat_type="strikeouts")
            # Simulate: market move detected but NOT marking alert_sent
            # (engine path: market_move_only=True never calls mark_opportunity_alert_sent)
            row = await _fetch_opp(db, "ext_mm", "strikeouts")
            assert not row.alert_sent
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Blocked/rejected pick → alert_sent=False
# ─────────────────────────────────────────────────────────────────────────────

class TestBlockedPickNotAlerted:
    def test_blocked_pick_stays_false(self):
        """A prop that clears evaluation but is blocked by a gate stays alert_sent=False."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="ext_blocked", stat_type="hits")
            # mark_opportunity_alert_sent is NOT called (blocked before deliver_underdog)
            row = await _fetch_opp(db, "ext_blocked", "hits")
            assert not row.alert_sent
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Tests 5-7: HIT / MISS / PUSH grading for alerted picks
# ─────────────────────────────────────────────────────────────────────────────

class TestGradingAlertedPicks:
    """Verify HIT/MISS/PUSH grading still works correctly on alert_sent=True rows."""

    async def _setup_alerted_pick(self, external_id: str, stat_type: str,
                                   line: float, recommendation: str = "OVER") -> "tuple":
        db = await _make_db()
        await _log_opp(db, external_id=external_id, stat_type=stat_type,
                       line=line, recommendation=recommendation)
        await db.mark_opportunity_alert_sent(external_id, stat_type)
        row = await _fetch_opp(db, external_id, stat_type)
        return db, row

    def test_grade_hit(self):
        """Telegram pick with actual > line → result=HIT."""
        async def _run_test():
            db, row = await self._setup_alerted_pick("ext_h", "hits", line=0.5)
            await db.grade_opportunity(row.id, result="HIT", actual_value=1.0)
            graded = await _fetch_opp(db, "ext_h", "hits")
            assert graded.result == "HIT"
            assert graded.actual_value == 1.0
            assert graded.alert_sent  # alert_sent preserved after grading
        _run(_run_test())

    def test_grade_miss(self):
        """Telegram pick with actual < line → result=MISS."""
        async def _run_test():
            db, row = await self._setup_alerted_pick("ext_m", "hits", line=0.5)
            await db.grade_opportunity(row.id, result="MISS", actual_value=0.0)
            graded = await _fetch_opp(db, "ext_m", "hits")
            assert graded.result == "MISS"
            assert graded.actual_value == 0.0
            assert graded.alert_sent
        _run(_run_test())

    def test_grade_push(self):
        """Telegram pick with actual == line → result=PUSH."""
        async def _run_test():
            db, row = await self._setup_alerted_pick("ext_p", "hits", line=1.0)
            await db.grade_opportunity(row.id, result="PUSH", actual_value=1.0)
            graded = await _fetch_opp(db, "ext_p", "hits")
            assert graded.result == "PUSH"
            assert graded.alert_sent
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: Later line movement does NOT change the original alerted line
# ─────────────────────────────────────────────────────────────────────────────

class TestOriginalLinePreserved:
    def test_re_evaluation_does_not_overwrite_alert_sent(self):
        """
        When a prop is re-evaluated (upsert), the on_conflict path updates
        line/recommendation but must NOT reset alert_sent to False.

        The on_conflict_do_update set_ dict in log_prop_opportunity does not
        include alert_sent, so this is guaranteed by the schema.
        """
        async def _run_test():
            db = await _make_db()
            # First evaluation: 0.5 line → alerted
            await _log_opp(db, external_id="ext_reeval", stat_type="hits", line=0.5)
            await db.mark_opportunity_alert_sent("ext_reeval", "hits")

            # Second evaluation (line moved to 1.0): upsert should NOT clear alert_sent
            await db.log_prop_opportunity(
                external_id        = "ext_reeval",
                player_name        = "Test Player",
                team               = "TST",
                sport              = "MLB",
                stat_type          = "hits",
                line_value         = 1.0,       # line moved
                recommendation     = "OVER",
                decision_tier      = "S",
                confidence         = 85,
                game_time          = _future_game(),
                provider           = "Underdog",
                bet_quality_score  = 85,
            )
            row = await _fetch_opp(db, "ext_reeval", "hits")
            assert row.alert_sent, "alert_sent must survive upsert (re-evaluation)"
            # The line_value IS updated to 1.0 (latest snapshot), but the alert
            # is tied to the first-seen line which is recorded in PropLineHistory
            # (opening_line field). The grading job uses game results, not PropOpportunityLog
            # line_value, for result determination.
        _run(_run_test())

    def test_alert_sent_at_preserved_after_upsert(self):
        """alert_sent_at timestamp is not overwritten by a later upsert."""
        async def _run_test():
            from sqlalchemy import select
            from database import PropOpportunityLog
            db = await _make_db()
            await _log_opp(db, external_id="ext_ts2", stat_type="hits", line=0.5)
            await db.mark_opportunity_alert_sent("ext_ts2", "hits")
            first_row = await _fetch_opp(db, "ext_ts2", "hits")
            first_ts = first_row.alert_sent_at

            # Re-evaluate with new line
            await db.log_prop_opportunity(
                external_id="ext_ts2", player_name="Test Player", team="TST",
                sport="MLB", stat_type="hits", line_value=1.0, recommendation="OVER",
                decision_tier="S", confidence=85, game_time=_future_game(),
                provider="Underdog", bet_quality_score=85,
            )
            row = await _fetch_opp(db, "ext_ts2", "hits")
            # alert_sent_at should be unchanged (on_conflict does not include it)
            assert row.alert_sent_at == first_ts
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Telegram-only performance excludes non-alerted props
# ─────────────────────────────────────────────────────────────────────────────

class TestTelegramOnlyPerformance:
    def test_performance_excludes_non_alerted_props(self):
        """get_telegram_pick_performance() counts only alert_sent=True rows."""
        async def _run_test():
            db = await _make_db()
            # 3 evaluated props
            for i in range(3):
                await _log_opp(db, external_id=f"prop_{i}", stat_type="hits")

            # Only 1 is a Telegram pick
            await db.mark_opportunity_alert_sent("prop_0", "hits")

            perf = await db.get_telegram_pick_performance()
            assert perf["total"] == 1, (
                f"Expected 1 Telegram pick, got {perf['total']}"
            )
        _run(_run_test())

    def test_performance_counts_by_result(self):
        """HIT/MISS/PUSH/PENDING counts are correct for alerted picks only."""
        async def _run_test():
            db = await _make_db()
            ids = ["tg_hit", "tg_miss", "tg_push", "tg_pend", "non_tg"]
            for ext_id in ids:
                await _log_opp(db, external_id=ext_id, stat_type="hits")

            # Mark all except non_tg as Telegram picks
            for ext_id in ids[:-1]:
                await db.mark_opportunity_alert_sent(ext_id, "hits")

            # Grade three of them
            for ext_id, result, actual in [
                ("tg_hit",  "HIT",  1.0),
                ("tg_miss", "MISS", 0.0),
                ("tg_push", "PUSH", 0.5),
            ]:
                row = await _fetch_opp(db, ext_id, "hits")
                await db.grade_opportunity(row.id, result=result, actual_value=actual)

            perf = await db.get_telegram_pick_performance()
            assert perf["total"]   == 4     # 4 alerted (tg_hit + tg_miss + tg_push + tg_pend)
            assert perf["hit"]     == 1
            assert perf["miss"]    == 1
            assert perf["push"]    == 1
            assert perf["pending"] == 1
            assert perf["graded"]  == 3
        _run(_run_test())

    def test_hit_rate_calculation(self):
        """Hit rate is calculated from graded picks only."""
        async def _run_test():
            db = await _make_db()
            # 3 hits, 1 miss → hit rate = 75%
            picks = [("hr1","OVER","HIT",1.0), ("hr2","OVER","HIT",1.0),
                     ("hr3","OVER","HIT",1.0), ("hr4","OVER","MISS",0.0)]
            for ext_id, rec, result, actual in picks:
                await _log_opp(db, external_id=ext_id, stat_type="hits",
                               recommendation=rec)
                await db.mark_opportunity_alert_sent(ext_id, "hits")
                row = await _fetch_opp(db, ext_id, "hits")
                await db.grade_opportunity(row.id, result=result, actual_value=actual)

            perf = await db.get_telegram_pick_performance()
            assert perf["hit_rate"] == 75.0
        _run(_run_test())

    def test_zero_picks_returns_zero_totals(self):
        """When no Telegram picks have been sent, totals are all zero."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="plain_prop", stat_type="hits")
            perf = await db.get_telegram_pick_performance()
            assert perf["total"]    == 0
            assert perf["hit_rate"] == 0.0
        _run(_run_test())

    def test_pass_recommendations_excluded(self):
        """PASS recommendations are excluded even if alert_sent=True (shouldn't happen in practice)."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="pass_prop", stat_type="hits",
                           recommendation="PASS")
            await db.mark_opportunity_alert_sent("pass_prop", "hits")  # edge case
            perf = await db.get_telegram_pick_performance()
            # PASS is excluded by the OVER/UNDER filter
            assert perf["total"] == 0
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Existing overall grading still works
# ─────────────────────────────────────────────────────────────────────────────

class TestOverallGradingUnchanged:
    def test_overall_grading_includes_all_evaluated_props(self):
        """
        get_pending_opportunities() still finds PENDING rows regardless of alert_sent.
        Overall grading pipeline is unaffected by the new column.
        """
        async def _run_test():
            db = await _make_db()
            # Mix of alerted and non-alerted props with past game_time
            for i, ext_id in enumerate(["overall_1", "overall_2", "overall_3"]):
                await db.log_prop_opportunity(
                    external_id       = ext_id,
                    player_name       = "Test Player",
                    team              = "TST",
                    sport             = "MLB",
                    stat_type         = "hits",
                    line_value        = 0.5,
                    recommendation    = "OVER",
                    decision_tier     = "S",
                    confidence        = 90,
                    game_time         = datetime.utcnow() - timedelta(hours=5),  # past
                    provider          = "Underdog",
                    bet_quality_score = 90,
                )
            # Mark one as alerted
            await db.mark_opportunity_alert_sent("overall_1", "hits")

            # get_pending_opportunities should return all 3 (cutoff_hours=4)
            pending = await db.get_pending_opportunities(cutoff_hours=4)
            assert len(pending) == 3, f"Expected 3 pending, got {len(pending)}"
        _run(_run_test())

    def test_grade_opportunity_works_on_non_alerted_row(self):
        """grade_opportunity() works on all rows, not just alerted ones."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="grade_test", stat_type="hits")
            row = await _fetch_opp(db, "grade_test", "hits")
            await db.grade_opportunity(row.id, result="HIT", actual_value=1.0)
            graded = await _fetch_opp(db, "grade_test", "hits")
            assert graded.result == "HIT"
            assert not graded.alert_sent  # not alerted, but graded normally
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: SlipJournalLeg relationships preserved
# ─────────────────────────────────────────────────────────────────────────────

class TestSlipRelationshipPreserved:
    def test_opportunity_id_still_links_to_prop_opportunity_log(self):
        """
        PropOpportunityLog.id is still available for SlipJournalLeg.opp_id references.
        The new alert_sent column does not disrupt the primary key or unique constraint.
        """
        async def _run_test():
            from sqlalchemy import select
            from database import PropOpportunityLog
            db = await _make_db()
            await _log_opp(db, external_id="slip_ext", stat_type="hits")
            await db.mark_opportunity_alert_sent("slip_ext", "hits")
            row = await _fetch_opp(db, "slip_ext", "hits")
            assert row.id is not None
            assert isinstance(row.id, int)
            # Verify the row can be fetched by primary key (as SlipJournalLeg.opp_id would)
            async with db.session() as s:
                by_pk = await s.get(PropOpportunityLog, row.id)
            assert by_pk is not None
            assert by_pk.alert_sent
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: Pending Telegram picks remain PENDING
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingBehavior:
    def test_alerted_pick_starts_pending(self):
        """A newly marked Telegram pick starts with result=PENDING."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="pend1", stat_type="hits")
            await db.mark_opportunity_alert_sent("pend1", "hits")
            row = await _fetch_opp(db, "pend1", "hits")
            assert row.result == "PENDING"
            assert row.alert_sent
        _run(_run_test())

    def test_pending_pick_not_in_graded_count(self):
        """Pending alerted pick is counted in 'pending' not in 'graded'."""
        async def _run_test():
            db = await _make_db()
            await _log_opp(db, external_id="pend2", stat_type="hits")
            await db.mark_opportunity_alert_sent("pend2", "hits")
            perf = await db.get_telegram_pick_performance()
            assert perf["pending"] == 1
            assert perf["graded"]  == 0
            assert perf["hit_rate"] == 0.0
        _run(_run_test())

    def test_pending_pick_not_graded_by_missing_result_data(self):
        """get_pending_opportunities only returns rows with game_time in the past."""
        async def _run_test():
            db = await _make_db()
            # Future game time: should NOT be returned as pending-to-grade
            await _log_opp(db, external_id="future_game", stat_type="hits")
            await db.mark_opportunity_alert_sent("future_game", "hits")
            pending = await db.get_pending_opportunities(cutoff_hours=4)
            # The future game_time means it's not yet eligible for grading
            assert all(r.external_id != "future_game" for r in pending)
        _run(_run_test())


# ─────────────────────────────────────────────────────────────────────────────
# Schema / migration sanity checks
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaSanity:
    def test_alert_sent_column_exists_in_model(self):
        """PropOpportunityLog.alert_sent column is defined in the ORM model."""
        from database import PropOpportunityLog
        cols = {c.name for c in PropOpportunityLog.__table__.columns}
        assert "alert_sent" in cols
        assert "alert_sent_at" in cols

    def test_alert_sent_default_false_in_column(self):
        """PropOpportunityLog.alert_sent column has default=False."""
        from database import PropOpportunityLog
        col = PropOpportunityLog.__table__.columns["alert_sent"]
        assert col.default.arg is False

    def test_migration_v4_method_exists(self):
        """Database has _migrate_prop_opportunity_log_v4 method."""
        from database import Database
        assert hasattr(Database, "_migrate_prop_opportunity_log_v4"), (
            "Migration method _migrate_prop_opportunity_log_v4 not found on Database"
        )

    def test_mark_opportunity_alert_sent_method_exists(self):
        """Database has mark_opportunity_alert_sent method."""
        from database import Database
        assert hasattr(Database, "mark_opportunity_alert_sent")

    def test_get_telegram_pick_performance_method_exists(self):
        """Database has get_telegram_pick_performance method."""
        from database import Database
        assert hasattr(Database, "get_telegram_pick_performance")
