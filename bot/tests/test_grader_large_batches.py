"""Large-page regression tests for SQLite grader query and write batching."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from database import Database, PlayerGameResult, PropOpportunityLog


def test_grader_page_size_exceeds_two_thousand_with_db_hard_cap():
    import main as main_module

    assert 2000 < main_module._GRADE_BATCH_SIZE <= 5000


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _parameter_count(parameters) -> int:
    if isinstance(parameters, (tuple, list, dict)):
        return len(parameters)
    return 0


@pytest.mark.asyncio
async def test_grader_bulk_paths_process_more_than_two_thousand_rows(db: Database):
    total = 5000
    result_rows = [
        SimpleNamespace(
            player_name=f"Batch Player {index}",
            sport="NBA",
            stat_type="points",
            game_date="2026-09-20",
            actual_value=12.0,
            opponent=None,
            source="propline_event_results",
            game_id=f"batch-event-{index}",
        )
        for index in range(total)
    ]

    result_insert_binds = []

    def capture_result_inserts(
        connection, cursor, statement, parameters, context, executemany
    ):
        if "insert into player_game_results" in statement.lower():
            result_insert_binds.append(_parameter_count(parameters))

    event.listen(
        db._engine.sync_engine, "before_cursor_execute", capture_result_inserts
    )
    try:
        assert await db.upsert_player_results_bulk(result_rows) == total
    finally:
        event.remove(
            db._engine.sync_engine, "before_cursor_execute", capture_result_inserts
        )
    assert len(result_insert_binds) == ceil(total / 100)
    assert max(result_insert_binds) <= 999

    props = [
        (f"Batch Player {index}", "NBA", "points")
        for index in range(total)
    ]
    result_read_binds = []

    def capture_result_reads(
        connection, cursor, statement, parameters, context, executemany
    ):
        normalized = statement.lower()
        if "select" in normalized and "from player_game_results" in normalized:
            result_read_binds.append(_parameter_count(parameters))

    event.listen(
        db._engine.sync_engine, "before_cursor_execute", capture_result_reads
    )
    try:
        loaded = await db.get_player_results_bulk(props, limit=1)
    finally:
        event.remove(
            db._engine.sync_engine, "before_cursor_execute", capture_result_reads
        )
    assert len(loaded) == total
    assert len(result_read_binds) == ceil(total / 300)
    assert max(result_read_binds) <= 999

    detected_at = datetime.utcnow()
    opportunities = [
        PropOpportunityLog(
            external_id=f"batch-opportunity-{index}",
            player_name=f"Grade Player {index}",
            team="BOS",
            sport="NBA",
            stat_type="points",
            line_value=10.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=75,
            game_time=detected_at - timedelta(hours=6),
            game_id=f"grade-event-{index}",
        )
        for index in range(total)
    ]
    async with db.session() as session:
        session.add_all(opportunities)
        await session.commit()

    async with db.session() as session:
        id_result = await session.execute(
            select(PropOpportunityLog.id)
            .where(PropOpportunityLog.external_id.like("batch-opportunity-%"))
            .order_by(PropOpportunityLog.id)
        )
        opportunity_ids = [int(row_id) for row_id in id_result.scalars().all()]

    grade_select_binds = []
    evidence_insert_binds = []

    def capture_grade_sql(
        connection, cursor, statement, parameters, context, executemany
    ):
        normalized = statement.lower()
        if (
            "select" in normalized
            and "from prop_opportunity_log" in normalized
            and "id in (" in normalized
        ):
            grade_select_binds.append(_parameter_count(parameters))
        if (
            "insert into player_game_results" in normalized
            and "on conflict" in normalized
            and "do nothing" in normalized
        ):
            evidence_insert_binds.append(_parameter_count(parameters))

    event.listen(db._engine.sync_engine, "before_cursor_execute", capture_grade_sql)
    try:
        applied = await db.grade_opportunities_bulk(
            [
                {
                    "id": opportunity_id,
                    "result": "HIT",
                    "actual_value": 12.0,
                    "grade_result_source": "propline_event_results",
                }
                for opportunity_id in opportunity_ids
            ]
        )
    finally:
        event.remove(
            db._engine.sync_engine, "before_cursor_execute", capture_grade_sql
        )

    assert applied == total
    assert len(grade_select_binds) == ceil(total / 350)
    assert max(grade_select_binds) <= 350
    assert len(evidence_insert_binds) == ceil(total / 100)
    assert max(evidence_insert_binds) <= 999

    async with db.session() as session:
        graded_result = await session.execute(
            select(func.count())
            .select_from(PropOpportunityLog)
            .where(
                PropOpportunityLog.external_id.like("batch-opportunity-%"),
                PropOpportunityLog.result == "HIT",
            )
        )
    assert int(graded_result.scalar() or 0) == total