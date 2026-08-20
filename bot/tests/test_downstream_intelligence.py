"""Focused tests for downstream pick intelligence.

These tests assert interpretation/reporting only; no alert gate is involved.
"""

from datetime import datetime, timedelta

import pytest

from database import Database, PropOpportunityLog
from engine.downstream_intelligence import (
    build_downstream_payload,
    compute_evidence_completeness,
    compute_sharp_confidence,
    interpret_movement,
)


def test_movement_captures_direction_persistence_and_reversal():
    move = interpret_movement(
        line_delta=-1.0,
        previous_delta=0.5,
        change_count=3,
        observations=[-0.5, -1.0, -1.0],
    )
    assert move.direction == "DOWN"
    assert move.magnitude == 1.0
    assert move.persistence == 100.0
    assert move.reversal is True
    assert move.sample_size == 3


def test_missing_evidence_is_explicit_not_fabricated():
    result = compute_evidence_completeness({"historical": {"n": 2}})
    assert result.score == 20
    assert set(result.missing) == {"movement", "market", "value", "matchup"}


def test_sharp_confidence_is_separate_and_sample_aware():
    result = compute_sharp_confidence(
        bet_quality=95,
        bet_confidence=80,
        evidence_score=100,
        sample_size=2,
    )
    assert result.score > 0
    assert result.calibrated is False
    assert result.sample_size == 2


def test_value_is_unavailable_without_projection():
    payload = build_downstream_payload(line=25.5, bet_quality=80)
    assert payload["value"]["available"] is False
    assert "value" in payload["evidence_completeness"]["missing"]


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_persistence_upsert_preserves_grade_and_updates_intelligence(db):
    base = dict(
        external_id="intel-1",
        player_name="Test Player",
        team="TST",
        sport="NBA",
        stat_type="Points",
        line_value=20.5,
        recommendation="OVER",
        decision_tier="A",
        confidence=78,
        game_time=datetime.utcnow() - timedelta(hours=3),
    )
    first = build_downstream_payload(
        line_delta=1.0,
        evidence={"historical": {"n": 10}, "market": {"ok": True}},
        bet_quality=82,
    )
    await db.log_prop_opportunity(**base, downstream_intelligence=first)
    async with db.session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(PropOpportunityLog).where(PropOpportunityLog.external_id == "intel-1")
        )).scalar_one()
    await db.grade_opportunity(row.id, "HIT", 25.0)

    second = build_downstream_payload(line_delta=-0.5, evidence={"movement": {"x": 1}}, bet_quality=60)
    updated = dict(base, confidence=60)
    await db.log_prop_opportunity(**updated, downstream_intelligence=second)
    async with db.session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(PropOpportunityLog).where(PropOpportunityLog.external_id == "intel-1")
        )).scalar_one()
    assert row.result == "HIT"
    assert row.actual_value == 25.0
    assert row.sharp_confidence is not None
    assert row.downstream_intelligence_json is not None