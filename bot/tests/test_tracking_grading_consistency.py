"""Regression tests for the delivery-independent tracked prop lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database import Database, PropLineHistory, PropOpportunityLog
from engine.backtesting import BacktestEngine


@pytest.fixture()
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _opportunity(
    *,
    external_id: str,
    provider: str,
    recommendation: str = "OVER",
    result: str = "PENDING",
    alert_sent: bool = False,
):
    return PropOpportunityLog(
        external_id=external_id,
        player_name="Tracked Player",
        team="TEAM",
        sport="NBA",
        stat_type="Points",
        line_value=20.5,
        recommendation=recommendation,
        decision_tier="A",
        confidence=80,
        game_time=datetime.utcnow() - timedelta(hours=5),
        game_id=f"game-{external_id}",
        result=result,
        provider=provider,
        alert_sent=alert_sent,
    )


@pytest.mark.asyncio
async def test_directional_tracking_and_grading_do_not_require_delivery(db):
    undelivered = _opportunity(external_id="ud-1", provider="Underdog")
    prizepicks = _opportunity(
        external_id="pp-1",
        provider="PrizePicks",
        recommendation="UNDER",
    )
    async with db.session() as session:
        session.add_all([undelivered, prizepicks])
        await session.commit()
        undelivered_id = undelivered.id
        prizepicks_id = prizepicks.id

    assert await db.count_directional_opportunity_records() == 2
    assert await db.count_actionable_pick_records() == 0

    await db.grade_opportunity(undelivered_id, "HIT", 25.0)
    await db.grade_opportunity(prizepicks_id, "MISS", 18.0)
    resolved = await db.get_resolved_tracked_opportunities(limit=10)

    assert {row.external_id for row in resolved} == {"ud-1", "pp-1"}
    assert {row.provider for row in resolved} == {"Underdog", "PrizePicks"}


@pytest.mark.asyncio
async def test_resolved_tracked_opportunity_is_not_duplicated_as_evidence(db):
    row = _opportunity(external_id="ud-duplicate", provider="Underdog")
    async with db.session() as session:
        session.add(row)
        await session.commit()
        row_id = row.id

    await db.grade_opportunity(row_id, "HIT", 25.0)
    await db.grade_opportunity(row_id, "HIT", 25.0)

    async with db.session() as session:
        results = await session.execute(
            select(PropOpportunityLog).where(
                PropOpportunityLog.external_id == "ud-duplicate"
            )
        )
        rows = list(results.scalars().all())
        evidence = await session.execute(
            select(PropLineHistory).where(
                PropLineHistory.external_id == "ud-duplicate"
            )
        )

    assert len(rows) == 1
    assert len(list(evidence.scalars().all())) == 0


@pytest.mark.asyncio
async def test_backtest_preserves_provider_identity_and_under_direction(db):
    opportunities = [
        SimpleNamespace(
            external_id="ud-backtest",
            game_id="ud-game",
            player_name="UD Player",
            provider="Underdog",
            sport="NBA",
            stat_type="Points",
            line_value=20.5,
            recommendation="OVER",
            result="HIT",
            confidence=80,
            bet_quality_score=None,
        ),
        SimpleNamespace(
            external_id="pp-backtest",
            game_id="pp-game",
            player_name="PP Player",
            provider="PrizePicks",
            sport="MLB",
            stat_type="Hits",
            line_value=1.5,
            recommendation="UNDER",
            result="MISS",
            confidence=75,
            bet_quality_score=None,
        ),
    ]

    report = BacktestEngine().run_prop_opportunities(opportunities)

    assert report.total_evaluated == 2
    assert report.overall.wins == 2
    assert report.by_provider["Underdog"].wins == 1
    assert report.by_provider["PrizePicks"].wins == 1