"""Migration coverage for direction-only prop persistence.

These tests model the schema used before active prop score persistence was
removed.  The rows must survive the initialization rebuild, while current
direction-only writes must not manufacture legacy score metadata.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from database import Database, PropCandidateLog, PropOpportunityLog


def _legacy_table_ddl(model, required_legacy_column: str) -> str:
    """Compile the current table and restore its historical required column."""
    ddl = str(CreateTable(model.__table__).compile(dialect=dialect()))
    for sql_type in ("VARCHAR(8)", "VARCHAR(16)"):
        historical_definition = f"{required_legacy_column} {sql_type},"
        if historical_definition in ddl:
            return ddl.replace(
                historical_definition,
                f"{required_legacy_column} {sql_type} NOT NULL,",
                1,
            )
    raise AssertionError(
        f"Could not find {required_legacy_column} in compiled table definition"
    )


async def _seed_pre_compat_schema(path) -> None:
    """Create pre-migration tables and rows using the old required fields."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(_legacy_table_ddl(PropOpportunityLog, "decision_tier"))
            )
            await connection.execute(
                text(_legacy_table_ddl(PropCandidateLog, "gate_decision"))
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO prop_opportunity_log (
                        id, external_id, player_name, team, sport, stat_type,
                        line_value, recommendation, decision_tier, confidence,
                        game_id, detected_at, result, alert_sent,
                        bet_quality_score
                    ) VALUES (
                        41, 'legacy-opportunity', 'Historical Player', 'HIS',
                        'MLB', 'Hits', 1.5, 'OVER', 'A', 78, 'legacy-game',
                        '2026-09-01 18:00:00', 'HIT', 1, 82
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO prop_candidate_log (
                        id, scan_ts, player_name, team, sport, stat_type,
                        line_value, provider, score_total, score_tier,
                        confidence, gate_decision, rejection_reason,
                        reason_codes, snapshot_external_id
                    ) VALUES (
                        42, '2026-09-01 18:00:00', 'Historical Candidate',
                        'HIS', 'NBA', 'Points', 20.5, 'Underdog', 74.5, 'A',
                        76, 'ACCEPTED', NULL, '["L10"]', 'legacy-snapshot'
                    )
                    """
                )
            )
    finally:
        await engine.dispose()


async def _nullable_flags(database: Database, table_name: str) -> dict[str, bool]:
    async with database._engine.connect() as connection:
        result = await connection.execute(text(f"PRAGMA table_info({table_name})"))
        return {row[1]: not bool(row[3]) for row in result.fetchall()}


@pytest.mark.asyncio
async def test_init_rebuilds_legacy_prop_tables_idempotently(tmp_path):
    path = tmp_path / "legacy-props.sqlite"
    await _seed_pre_compat_schema(path)

    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.init()
    try:
        opportunity_nullable = await _nullable_flags(
            database, "prop_opportunity_log"
        )
        candidate_nullable = await _nullable_flags(database, "prop_candidate_log")

        assert opportunity_nullable["decision_tier"] is True
        assert candidate_nullable["gate_decision"] is True

        async with database.session() as session:
            opportunity = (
                await session.execute(
                    select(PropOpportunityLog).where(
                        PropOpportunityLog.external_id == "legacy-opportunity"
                    )
                )
            ).scalar_one()
            candidate = (
                await session.execute(
                    select(PropCandidateLog).where(
                        PropCandidateLog.snapshot_external_id == "legacy-snapshot"
                    )
                )
            ).scalar_one()

        assert opportunity.result == "HIT"
        assert opportunity.decision_tier == "A"
        assert opportunity.bet_quality_score == 82
        assert candidate.score_total == pytest.approx(74.5)
        assert candidate.score_tier == "A"
        assert candidate.gate_decision == "ACCEPTED"
    finally:
        await database.close()

    # A restart must be able to initialize the already-upgraded file again.
    # In particular, the compatibility rebuild must not run a second time or
    # disturb the historical rows it preserved.
    restarted_database = Database(f"sqlite+aiosqlite:///{path}")
    await restarted_database.init()
    try:
        opportunity_nullable = await _nullable_flags(
            restarted_database, "prop_opportunity_log"
        )
        candidate_nullable = await _nullable_flags(
            restarted_database, "prop_candidate_log"
        )

        assert opportunity_nullable["decision_tier"] is True
        assert candidate_nullable["gate_decision"] is True

        async with restarted_database.session() as session:
            opportunity = (
                await session.execute(
                    select(PropOpportunityLog).where(
                        PropOpportunityLog.external_id == "legacy-opportunity"
                    )
                )
            ).scalar_one()
            candidate = (
                await session.execute(
                    select(PropCandidateLog).where(
                        PropCandidateLog.snapshot_external_id == "legacy-snapshot"
                    )
                )
            ).scalar_one()

        assert opportunity.id == 41
        assert opportunity.result == "HIT"
        assert opportunity.decision_tier == "A"
        assert opportunity.bet_quality_score == 82
        assert candidate.id == 42
        assert candidate.score_total == pytest.approx(74.5)
        assert candidate.score_tier == "A"
        assert candidate.gate_decision == "ACCEPTED"
    finally:
        await restarted_database.close()


@pytest.mark.asyncio
async def test_direction_only_writes_leave_retired_score_fields_unset(tmp_path):
    path = tmp_path / "direction-only-props.sqlite"
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.init()
    try:
        await database.log_prop_opportunity(
            external_id="direction-only-opportunity",
            player_name="Current Player",
            team="CUR",
            sport="NFL",
            stat_type="Receptions",
            line_value=4.5,
            recommendation="UNDER",
            confidence=71,
            game_time=None,
        )
        assert (
            await database.log_prop_candidate_batch(
                [
                    {
                        "scan_ts": datetime.utcnow(),
                        "player_name": "Current Candidate",
                        "team": "CUR",
                        "sport": "NBA",
                        "stat_type": "Assists",
                        "line_value": 7.5,
                        "provider": "Underdog",
                        "confidence": 69,
                        "reason_codes": '["DIRECTIONAL_EDGE"]',
                        "snapshot_external_id": "current-snapshot",
                    }
                ],
                strict=True,
            )
        ) == 1

        async with database.session() as session:
            opportunity = (
                await session.execute(
                    select(PropOpportunityLog).where(
                        PropOpportunityLog.external_id
                        == "direction-only-opportunity"
                    )
                )
            ).scalar_one()
            candidate = (
                await session.execute(
                    select(PropCandidateLog).where(
                        PropCandidateLog.snapshot_external_id == "current-snapshot"
                    )
                )
            ).scalar_one()

        assert opportunity.decision_tier is None
        assert opportunity.bet_quality_score is None
        assert candidate.score_total is None
        assert candidate.score_tier is None
        assert candidate.rejection_reason is None
    finally:
        await database.close()