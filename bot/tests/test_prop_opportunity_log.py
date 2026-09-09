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

from database import (
    Database,
    PlayerGameResult,
    PropOpportunityLog,
    PropTrackingLog,
    UnderdogSnapshotRecord,
)


@pytest.fixture
async def db():
    """Fresh in-memory DB per test so async loops cannot share connections."""
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
    async def test_old_event_identity_without_game_time_is_gradeable(
        self, db: Database
    ) -> None:
        """Provider feeds may omit kickoff time but still expose an event ID."""
        await _log(
            db,
            external_id="ext-no-time",
            game_id="event-no-time",
            game_time=None,
        )
        pending = await db.get_pending_opportunities(cutoff_hours=0)
        assert [row.external_id for row in pending] == ["ext-no-time"]

    @pytest.mark.asyncio
    async def test_missing_time_and_event_identity_stays_out_of_grading(
        self, db: Database
    ) -> None:
        """Never grade a stale row by player/stat alone."""
        await _log(db, external_id="ext-no-identity", game_time=None, game_id="")
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        assert pending == []

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

        # PASS is not a persisted evaluation and cannot overwrite the
        # directional opportunity.
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
        assert row.recommendation == "OVER"
        assert row.confidence == 60

    @pytest.mark.asyncio
    async def test_tracks_neutral_prop_before_directional_evaluation(
        self, db: Database
    ) -> None:
        inserted = await db.track_prop_records([{
            "external_id": "nba-event-1",
            "player_name": "A. Guard",
            "team": "NY",
            "sport": "NBA",
            "stat_type": "Points",
            "line_value": 21.5,
            "game_id": "nba-event-1",
            "game_time": _past(8),
            "provider": "Underdog",
        }])
        assert inserted == 1

        pending = await db.get_pending_tracked_props(cutoff_hours=4)
        assert len(pending) == 1
        assert not hasattr(pending[0], "recommendation")
        assert pending[0].provider == "Underdog"

        # Repeated ingestion must not erase a later directional decision.
        await db.log_prop_opportunity(
            external_id="nba-event-1",
            player_name="A. Guard",
            team="NY",
            sport="NBA",
            stat_type="Points",
            line_value=21.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=75,
            game_time=_past(8),
            game_id="nba-event-1",
            provider="Underdog",
        )
        await db.track_prop_records([{
            "external_id": "nba-event-1",
            "player_name": "A. Guard",
            "team": "NY",
            "sport": "NBA",
            "stat_type": "Points",
            "line_value": 21.5,
            "game_id": "nba-event-1",
            "game_time": _past(8),
            "provider": "Underdog",
        }])
        pending = await db.get_pending_tracked_props(cutoff_hours=4)
        assert len(pending) == 1
        assert pending[0].line_value == 21.5
        async with db.session() as s:
            from sqlalchemy import select
            row = (
                await s.execute(
                    select(PropOpportunityLog).where(
                        PropOpportunityLog.external_id == "nba-event-1"
                    )
                )
            ).scalar_one()
        assert row.recommendation == "OVER"

    @pytest.mark.asyncio
    async def test_tracks_each_line_and_provider_variant_separately(
        self, db: Database
    ) -> None:
        base = {
            "external_id": "pp-projection-1",
            "player_name": "A. Guard",
            "team": "NY",
            "sport": "NBA",
            "stat_type": "Points",
            "game_id": "nba-event-1",
            "game_time": _past(8),
            "provider": "PrizePicks",
        }
        await db.track_prop_records([
            {**base, "line_value": 21.5, "line_variant": "standard"},
            {**base, "line_value": 19.5, "line_variant": "goblin"},
            {**base, "line_value": 23.5, "line_variant": "demon"},
        ])

        pending = await db.get_pending_tracked_props(cutoff_hours=4)

        assert len(pending) == 3
        assert {row.line_value for row in pending} == {19.5, 21.5, 23.5}
        assert {row.line_variant for row in pending} == {
            "standard",
            "goblin",
            "demon",
        }

    @pytest.mark.asyncio
    async def test_reseen_unalerted_line_refreshes_tracking_metadata(self, db: Database) -> None:
        """Tracking remains current even when delivery never happens."""
        base = {
            "external_id": "nba-reseen-1",
            "player_name": "A. Guard",
            "team": "NY",
            "sport": "NBA",
            "stat_type": "Points",
            "line_value": 21.5,
            "provider": "Underdog",
        }
        first_time = _past(8)
        second_time = _past(5)
        assert await db.track_prop_records([
            {**base, "game_id": "old-event", "game_time": first_time},
        ]) == 1
        assert await db.track_prop_records([
            {**base, "game_id": "current-event", "game_time": second_time},
        ]) == 1

        async with db.session() as s:
            from sqlalchemy import select
            row = (
                await s.execute(
                    select(PropTrackingLog).where(
                        PropTrackingLog.external_id.like("nba-reseen-1%")
                    )
                )
            ).scalar_one()
        assert row.game_id == "current-event"
        assert row.game_time == second_time
        assert row.result == "PENDING"

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

    @pytest.mark.asyncio
    async def test_falls_back_when_source_and_result_event_ids_differ(
        self, db: Database
    ) -> None:
        game_date = "2026-08-31"
        async with db.session() as s:
            s.add(PlayerGameResult(
                player_name="A. Guard",
                sport="NBA",
                stat_type="points",
                game_date=game_date,
                opponent="LA",
                actual_value=24.0,
                source="espn_gamelog",
                game_id="espn-event-1",
            ))
            await s.commit()

        row = await db.get_game_result_for_grading(
            "A. Guard",
            "NBA",
            "Points",
            game_date,
            game_id="underdog-event-1",
            provider="Underdog",
        )
        assert row is not None
        assert row.actual_value == 24.0

    @pytest.mark.asyncio
    async def test_derives_composite_from_stored_component_results(
        self, db: Database
    ) -> None:
        for stat_type, actual_value in (
            ("hits", 1.0),
            ("runs", 2.0),
            ("rbis", 2.0),
        ):
            await db.upsert_player_result(PlayerGameResult(
                player_name="Cedric Mullins",
                sport="MLB",
                stat_type=stat_type,
                game_date="2026-09-08",
                actual_value=actual_value,
                opponent="BOS",
                source="graded_prop",
                game_id="mlb-event",
            ))

        row = await db.get_game_result_for_grading(
            "Cedric Mullins",
            "MLB",
            "Hits + Runs + RBIs",
            "2026-09-08",
            game_id="underdog-event",
            provider="Underdog",
        )

        assert row is not None
        assert row.actual_value == 5.0
        assert row.source == "derived_from_db"


class TestGetTrackingSummary:
    @pytest.mark.asyncio
    async def test_summary_counts_by_recommendation(self, db: Database) -> None:
        # Seed two OVER plays graded HIT; PASS is intentionally not persisted.
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

        # Only directional opportunities are tracked.
        assert counts.get("OVER", {}).get("HIT", 0) >= 2
        assert counts.get("PASS", {}).get("MISS", 0) == 0

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
        assert "by_sport" in summary
        assert "total"    in summary
        assert "pending"  in summary
        assert "by_tier" not in summary


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
        # A PASS re-evaluation is ignored rather than overwriting the
        # directional record.
        assert row.recommendation == "OVER"


class TestBulkPropOpportunityLog:
    @pytest.mark.asyncio
    async def test_bulk_filters_passes_and_writes_all_directional_rows(
        self, db: Database
    ) -> None:
        rows = [
            {
                "external_id": f"bulk-{index}",
                "player_name": f"Player {index}",
                "team": "TEAM",
                "sport": "NBA",
                "stat_type": "Points",
                "line_value": 20.5 + index,
                "recommendation": "OVER" if index % 2 else "UNDER",
                "decision_tier": "A",
                "confidence": 70 + index,
                "game_time": _future(6),
                "provider": "Underdog",
                "bet_quality_score": 80,
                "qualification_path": ["score", "direction"],
                "reason_codes": ["L5", "EDGE"],
            }
            for index in range(65)
        ]
        rows.append({
            "external_id": "bulk-pass",
            "player_name": "Transient",
            "team": "TEAM",
            "sport": "NBA",
            "stat_type": "Points",
            "line_value": 20.5,
            "recommendation": "PASS",
            "decision_tier": "PASS",
            "confidence": 20,
            "game_time": _future(6),
        })

        inserted = await db.log_prop_opportunity_bulk(rows)

        assert inserted == 65
        async with db.session() as s:
            from sqlalchemy import func, select

            count_result = await s.execute(
                select(func.count(PropOpportunityLog.id)).where(
                    PropOpportunityLog.external_id.like("bulk-%")
                )
            )
            assert count_result.scalar() == 65
            pass_result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "bulk-pass"
                )
            )
            assert pass_result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_bulk_upsert_uses_last_duplicate_and_preserves_grading(
        self, db: Database
    ) -> None:
        base = {
            "external_id": "bulk-upsert",
            "player_name": "Bulk Player",
            "team": "TEAM",
            "sport": "MLB",
            "stat_type": "Hits",
            "line_value": 1.5,
            "recommendation": "OVER",
            "decision_tier": "A",
            "confidence": 72,
            "game_time": _past(8),
        }
        await db.log_prop_opportunity_bulk([base])

        async with db.session() as s:
            from sqlalchemy import select

            result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "bulk-upsert"
                )
            )
            original = result.scalar_one()
        await db.grade_opportunity(original.id, "HIT", 3.0)

        updated = dict(base)
        updated.update(
            line_value=2.5,
            recommendation="UNDER",
            decision_tier="S",
            confidence=91,
            stars=5,
            provider="Underdog",
        )
        duplicate = dict(updated, line_value=3.5, confidence=95)
        await db.log_prop_opportunity_bulk([updated, duplicate])

        async with db.session() as s:
            from sqlalchemy import select

            result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "bulk-upsert"
                )
            )
            row = result.scalar_one()

        assert row.id == original.id
        assert row.line_value == 3.5
        assert row.recommendation == "UNDER"
        assert row.confidence == 95
        assert row.result == "HIT"
        assert row.actual_value == 3.0
        assert row.graded_at is not None

    @pytest.mark.asyncio
    async def test_bulk_upsert_does_not_erase_event_identity_with_blank_value(
        self, db: Database
    ) -> None:
        base = {
            "external_id": "event-identity",
            "player_name": "Event Player",
            "team": "TEAM",
            "sport": "MLB",
            "stat_type": "Hits",
            "line_value": 1.5,
            "recommendation": "OVER",
            "decision_tier": "A",
            "confidence": 80,
            "game_time": _future(6),
            "game_id": "144297",
            "provider": "Underdog",
        }
        await db.log_prop_opportunity_bulk([base])
        await db.log_prop_opportunity_bulk([dict(base, game_id="")])

        async with db.session() as s:
            from sqlalchemy import select

            result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "event-identity"
                )
            )
            row = result.scalar_one()

        assert row.game_id == "144297"

    @pytest.mark.asyncio
    async def test_event_identity_backfill_uses_matching_underdog_snapshot(
        self, db: Database
    ) -> None:
        async with db.session() as s:
            s.add(UnderdogSnapshotRecord(
                external_id="snapshot-event",
                player_name="Event Player",
                team="TEAM",
                sport="MLB",
                stat_type="Hits",
                line_value=1.5,
                game_id="144297",
                game_time=_future(6),
            ))
            s.add(PropOpportunityLog(
                external_id="snapshot-event",
                player_name="Event Player",
                team="TEAM",
                sport="MLB",
                stat_type="Hits",
                line_value=1.5,
                recommendation="OVER",
                decision_tier="A",
                confidence=80,
                game_time=_future(6),
                game_id="",
                provider="Underdog",
                result="PENDING",
            ))
            await s.commit()

        await db.repair_underdog_opportunity_game_ids()

        async with db.session() as s:
            from sqlalchemy import select

            result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "snapshot-event"
                )
            )
            row = result.scalar_one()

        assert row.game_id == "144297"

    @pytest.mark.asyncio
    async def test_bulk_persists_evidence_and_intelligence_summaries(
        self, db: Database
    ) -> None:
        intelligence = {
            "evidence_completeness": {"score": 88},
            "sharp_confidence": {
                "score": 76,
                "components": {"market": 40, "sample": 36},
            },
        }
        await db.log_prop_opportunity_bulk([{
            "external_id": "bulk-intelligence",
            "player_name": "Evidence Player",
            "team": "TEAM",
            "sport": "NFL",
            "stat_type": "Passing Yards",
            "line_value": 250.5,
            "recommendation": "OVER",
            "decision_tier": "S",
            "confidence": 90,
            "game_time": _future(10),
            "stars": 5,
            "risk_level": "LOW",
            "explanation": "Strong evidence",
            "provider": "Underdog",
            "bet_quality_score": 92,
            "qualification_path": ["history", "market"],
            "reason_codes": ["STRONG_L5"],
            "downstream_intelligence": intelligence,
        }])

        async with db.session() as s:
            from sqlalchemy import select
            import json

            result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "bulk-intelligence"
                )
            )
            row = result.scalar_one()

        assert row.stars == 5
        assert row.provider == "Underdog"
        assert row.bet_quality_score == 92
        assert json.loads(row.qualification_path) == ["history", "market"]
        assert json.loads(row.reason_codes) == ["STRONG_L5"]
        assert json.loads(row.downstream_intelligence_json) == intelligence
        assert row.evidence_completeness == 88
        assert row.sharp_confidence == 76
        assert json.loads(row.sharp_confidence_components) == {
            "market": 40,
            "sample": 36,
        }
