"""
Tests for PropOpportunityLog — PLAY/PASS tracking and grading pipeline.

Covers:
  - log_prop_opportunity upserts correctly (no duplicate on re-evaluation)
  - get_pending_opportunities filters by game_time cutoff
  - grade_opportunity writes HIT / MISS / PUSH
  - get_game_result_for_grading returns correct row
  - get_tracking_summary aggregates correctly
  - Upsert preserves grading when re-evaluated
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from database import Database, PropOpportunityLog, PlayerGameResult


@pytest.fixture(scope="module")
async def db():
    """In-memory DB for isolation."""
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _past(hours: int = 5) -> datetime:
    return datetime.utcnow() - timedelta(hours=hours)


def _future(hours: int = 5) -> datetime:
    return datetime.utcnow() + timedelta(hours=hours)


async def _log(db: Database, **kwargs) -> None:
    defaults = dict(
        external_id    = "ext-001",
        player_name    = "Aaron Judge",
        team           = "NYY",
        sport          = "MLB",
        stat_type      = "Home Runs",
        line_value     = 0.5,
        recommendation = "OVER",
        decision_tier  = "S",
        confidence     = 85,
        game_time      = _past(6),
    )
    defaults.update(kwargs)
    await db.log_prop_opportunity(**defaults)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestLogPropOpportunity:
    @pytest.mark.asyncio
    async def test_inserts_new_record(self, db: Database) -> None:
        await _log(db, external_id="ext-ins-001")
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        ext_ids = [r.external_id for r in pending]
        assert "ext-ins-001" in ext_ids

    @pytest.mark.asyncio
    async def test_upsert_updates_recommendation(self, db: Database) -> None:
        """Re-evaluating the same prop updates recommendation, not duplicates."""
        await _log(db, external_id="ext-upsert", stat_type="Hits",
                   recommendation="OVER", confidence=60)
        await _log(db, external_id="ext-upsert", stat_type="Hits",
                   recommendation="PASS", confidence=40)

        async with db.session() as s:
            from sqlalchemy import select, func
            count_r = await s.execute(
                select(func.count(PropOpportunityLog.id))
                .where(
                    PropOpportunityLog.external_id == "ext-upsert",
                    PropOpportunityLog.stat_type   == "Hits",
                )
            )
            count = count_r.scalar()

        assert count == 1  # upsert, not duplicate

        # Latest values applied
        async with db.session() as s:
            from sqlalchemy import select
            row_r = await s.execute(
                select(PropOpportunityLog)
                .where(
                    PropOpportunityLog.external_id == "ext-upsert",
                    PropOpportunityLog.stat_type   == "Hits",
                )
            )
            row = row_r.scalar_one()
        assert row.recommendation == "PASS"
        assert row.confidence == 40

    @pytest.mark.asyncio
    async def test_different_stat_types_are_separate_rows(self, db: Database) -> None:
        await _log(db, external_id="ext-multi", stat_type="Hits")
        await _log(db, external_id="ext-multi", stat_type="RBIs")

        async with db.session() as s:
            from sqlalchemy import select, func
            r = await s.execute(
                select(func.count(PropOpportunityLog.id))
                .where(PropOpportunityLog.external_id == "ext-multi")
            )
        assert r.scalar() == 2


class TestGetPendingOpportunities:
    @pytest.mark.asyncio
    async def test_returns_only_past_game_times(self, db: Database) -> None:
        # Past game — should appear
        await _log(db, external_id="ext-past", stat_type="Total Bases",
                   game_time=_past(10))
        # Future game — should NOT appear
        await _log(db, external_id="ext-future", stat_type="Total Bases",
                   game_time=_future(5))

        pending = await db.get_pending_opportunities(cutoff_hours=4)
        ext_ids = [r.external_id for r in pending]
        assert "ext-past"   in ext_ids
        assert "ext-future" not in ext_ids

    @pytest.mark.asyncio
    async def test_excludes_already_graded(self, db: Database) -> None:
        await _log(db, external_id="ext-graded", stat_type="Stolen Bases",
                   game_time=_past(10))
        # Find its id and grade it
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        target = next((r for r in pending if r.external_id == "ext-graded"), None)
        assert target is not None
        await db.grade_opportunity(target.id, "HIT", 1.0)

        pending2 = await db.get_pending_opportunities(cutoff_hours=4)
        ext_ids2 = [r.external_id for r in pending2]
        assert "ext-graded" not in ext_ids2


class TestGradeOpportunity:
    @pytest.mark.asyncio
    async def test_grade_hit(self, db: Database) -> None:
        await _log(db, external_id="ext-grade-hit", stat_type="Walks",
                   game_time=_past(8))
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        target = next((r for r in pending if r.external_id == "ext-grade-hit"), None)
        assert target is not None

        await db.grade_opportunity(target.id, "HIT", 1.0)

        async with db.session() as s:
            from sqlalchemy import select
            r = await s.execute(
                select(PropOpportunityLog).where(PropOpportunityLog.id == target.id)
            )
            row = r.scalar_one()
        assert row.result       == "HIT"
        assert row.actual_value == 1.0
        assert row.graded_at    is not None

    @pytest.mark.asyncio
    async def test_grade_miss(self, db: Database) -> None:
        await _log(db, external_id="ext-grade-miss", stat_type="Strikeouts",
                   game_time=_past(8))
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        target = next((r for r in pending if r.external_id == "ext-grade-miss"), None)
        assert target is not None
        await db.grade_opportunity(target.id, "MISS", 0.0)

        async with db.session() as s:
            from sqlalchemy import select
            r = await s.execute(
                select(PropOpportunityLog).where(PropOpportunityLog.id == target.id)
            )
            row = r.scalar_one()
        assert row.result == "MISS"

    @pytest.mark.asyncio
    async def test_grade_push(self, db: Database) -> None:
        await _log(db, external_id="ext-grade-push", stat_type="Assists",
                   game_time=_past(8))
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        target = next((r for r in pending if r.external_id == "ext-grade-push"), None)
        assert target is not None
        await db.grade_opportunity(target.id, "PUSH", 0.5)

        async with db.session() as s:
            from sqlalchemy import select
            r = await s.execute(
                select(PropOpportunityLog).where(PropOpportunityLog.id == target.id)
            )
            row = r.scalar_one()
        assert row.result == "PUSH"


class TestGetGameResultForGrading:
    @pytest.mark.asyncio
    async def test_returns_matching_result(self, db: Database) -> None:
        async with db.session() as s:
            s.add(PlayerGameResult(
                player_name  = "Shohei Ohtani",
                sport        = "MLB",
                stat_type    = "hits",
                game_date    = "2026-07-15",
                actual_value = 2.0,
                opponent     = "NYY",
                source       = "api",
            ))
            await s.commit()

        row = await db.get_game_result_for_grading(
            "Shohei Ohtani", "MLB", "hits", "2026-07-15"
        )
        assert row is not None
        assert row.actual_value == 2.0

    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, db: Database) -> None:
        row = await db.get_game_result_for_grading(
            "Nobody Known", "MLB", "hits", "2020-01-01"
        )
        assert row is None


class TestGetTrackingSummary:
    @pytest.mark.asyncio
    async def test_summary_counts_by_recommendation(self, db: Database) -> None:
        # Seed two OVER plays graded HIT, one PASS graded MISS
        for i, (rec, result, ext) in enumerate([
            ("OVER", "HIT",  "sum-ext-001"),
            ("OVER", "HIT",  "sum-ext-002"),
            ("PASS", "MISS", "sum-ext-003"),
        ]):
            await _log(db, external_id=ext, stat_type=f"Stat{i}",
                       recommendation=rec, game_time=_past(8))
            pending = await db.get_pending_opportunities(cutoff_hours=4)
            target = next((r for r in pending if r.external_id == ext), None)
            if target:
                await db.grade_opportunity(target.id, result, 1.0 if result == "HIT" else 0.0)

        summary = await db.get_tracking_summary()
        counts = summary["counts"]

        # We should have at least 2 OVER HIT and 1 PASS MISS logged
        assert counts.get("OVER", {}).get("HIT", 0) >= 2
        assert counts.get("PASS", {}).get("MISS", 0) >= 1

    @pytest.mark.asyncio
    async def test_summary_total_and_pending(self, db: Database) -> None:
        summary = await db.get_tracking_summary()
        assert isinstance(summary["total"], int)
        assert isinstance(summary["pending"], int)
        assert summary["total"] >= 0
        assert summary["pending"] >= 0
        assert summary["total"] >= summary["pending"]

    @pytest.mark.asyncio
    async def test_summary_has_expected_keys(self, db: Database) -> None:
        summary = await db.get_tracking_summary()
        assert "counts"   in summary
        assert "by_tier"  in summary
        assert "by_sport" in summary
        assert "total"    in summary
        assert "pending"  in summary


class TestUpsertPreservesGrading:
    @pytest.mark.asyncio
    async def test_regrading_not_wiped_by_upsert(self, db: Database) -> None:
        """
        If a prop is re-evaluated after being graded, the upsert must NOT
        overwrite result / actual_value / graded_at.
        """
        await _log(db, external_id="ext-preserve", stat_type="Points",
                   game_time=_past(8))
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        target = next((r for r in pending if r.external_id == "ext-preserve"), None)
        assert target is not None

        await db.grade_opportunity(target.id, "HIT", 2.5)

        # Re-evaluate (new confidence, same ext_id+stat_type)
        await _log(db, external_id="ext-preserve", stat_type="Points",
                   recommendation="PASS", confidence=30, game_time=_past(8))

        async with db.session() as s:
            from sqlalchemy import select
            r = await s.execute(
                select(PropOpportunityLog).where(PropOpportunityLog.id == target.id)
            )
            row = r.scalar_one()

        # Grading fields must be preserved — upsert only touches rec/tier/confidence/line
        assert row.result       == "HIT"
        assert row.actual_value == 2.5
        assert row.graded_at    is not None
        # New recommendation applied
        assert row.recommendation == "PASS"
