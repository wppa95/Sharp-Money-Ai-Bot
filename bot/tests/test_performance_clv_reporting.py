"""Focused tests for CLV values shown alongside directional performance."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
async def db():
    from database import Database

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


async def _opportunity(db, external_id: str, recommendation: str = "OVER"):
    await db.log_prop_opportunity(
        external_id=external_id,
        player_name="CLV Player",
        team="TST",
        sport="MLB",
        stat_type="hits",
        line_value=0.5,
        recommendation=recommendation,
        decision_tier="S",
        confidence=90,
        game_time=datetime.utcnow() - timedelta(hours=2),
        provider="Underdog",
        bet_quality_score=90,
    )


async def _snapshot_and_seed(db, external_id: str, clv: float, *, offset: int = 0):
    from database import AlertCLVSeed, UnderdogSnapshotRecord

    async with db.session() as session:
        snapshot = UnderdogSnapshotRecord(
            external_id=external_id,
            player_name="CLV Player",
            team="TST",
            sport="MLB",
            stat_type="hits",
            line_value=0.5,
            game_id=f"game-{external_id}-{offset}",
            fetched_at=datetime.utcnow() + timedelta(minutes=offset),
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            AlertCLVSeed(
                source_table="underdog_snapshots",
                source_id=snapshot.id,
                alert_type="UNDERDOG",
                sport="MLB",
                market_type="hits",
                event=snapshot.game_id,
                selection="CLV Player hits 0.5",
                alerted_at=datetime.utcnow() + timedelta(minutes=offset),
                clv_pct=clv,
                clv_computed=True,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_performance_summary_joins_clv_and_maps_under_direction(db):
    await _opportunity(db, "over-hit")
    await _opportunity(db, "over-miss")
    await _opportunity(db, "under-hit", recommendation="UNDER")
    await _opportunity(db, "no-clv")

    for external_id, result in (
        ("over-hit", "HIT"),
        ("over-miss", "MISS"),
        ("under-hit", "MISS"),  # raw result MISS is a directional UNDER hit
        ("no-clv", "HIT"),
    ):
        row = await db.get_prop_opportunity(external_id, "hits")
        await db.grade_opportunity(row.id, result=result, actual_value=1.0)

    await _snapshot_and_seed(db, "over-hit", 4.0)
    await _snapshot_and_seed(db, "over-miss", -2.0)
    await _snapshot_and_seed(db, "under-hit", 3.0)

    summary = await db.get_directional_performance_summary()
    row = summary["by_sport"]["MLB"]

    assert row["hit"] == 3
    assert row["miss"] == 1
    assert row["clv_hit_count"] == 2
    assert row["clv_hit_avg"] == pytest.approx(3.5)
    assert row["clv_miss_count"] == 1
    assert row["clv_miss_avg"] == pytest.approx(-2.0)
    assert row["clv_push_count"] == 0
    assert row["clv_count"] == 3
    assert row["clv_coverage_pct"] == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_performance_summary_deduplicates_moving_snapshots(db):
    await _opportunity(db, "moving")
    row = await db.get_prop_opportunity("moving", "hits")
    await db.grade_opportunity(row.id, result="HIT", actual_value=1.0)

    await _snapshot_and_seed(db, "moving", 1.0, offset=0)
    await _snapshot_and_seed(db, "moving", 7.0, offset=1)

    summary = await db.get_directional_performance_summary()
    sport = summary["by_sport"]["MLB"]

    assert sport["clv_count"] == 1
    assert sport["clv_hit_avg"] == pytest.approx(7.0)