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


async def _opportunity(
    db,
    external_id: str,
    recommendation: str = "OVER",
    *,
    sport: str = "MLB",
):
    await db.log_prop_opportunity(
        external_id=external_id,
        player_name="CLV Player",
        team="TST",
        sport=sport,
        stat_type="hits",
        line_value=0.5,
        recommendation=recommendation,
        decision_tier="S",
        confidence=90,
        game_time=datetime.utcnow() - timedelta(hours=2),
        provider="Underdog",
        bet_quality_score=90,
    )


async def _fetch_opportunity(db, external_id: str):
    from sqlalchemy import select
    from database import PropOpportunityLog

    async with db.session() as session:
        result = await session.execute(
            select(PropOpportunityLog).where(
                PropOpportunityLog.external_id == external_id,
                PropOpportunityLog.stat_type == "hits",
            )
        )
        return result.scalar_one()


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
    await _opportunity(db, "under-miss", recommendation="UNDER")
    await _opportunity(db, "no-clv")

    for external_id, result in (
        ("over-hit", "HIT"),
        ("over-miss", "MISS"),
        ("under-hit", "HIT"),  # Stored result already reflects UNDER direction.
        ("under-miss", "MISS"),
        ("no-clv", "HIT"),
    ):
        row = await _fetch_opportunity(db, external_id)
        await db.grade_opportunity(
            row.id,
            result=result,
            actual_value=1.0,
            grade_result_source="propline_history",
        )

    await _snapshot_and_seed(db, "over-hit", 4.0)
    await _snapshot_and_seed(db, "over-miss", -2.0)
    await _snapshot_and_seed(db, "under-hit", 3.0)

    summary = await db.get_directional_performance_summary()
    row = summary["by_sport"]["MLB"]

    assert row["hit"] == 3
    assert row["miss"] == 2
    assert row["clv_hit_count"] == 2
    assert row["clv_hit_avg"] == pytest.approx(3.5)
    assert row["clv_miss_count"] == 1
    assert row["clv_miss_avg"] == pytest.approx(-2.0)
    assert row["clv_push_count"] == 0
    assert row["clv_count"] == 3
    assert row["clv_coverage_pct"] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_performance_summary_deduplicates_moving_snapshots(db):
    await _opportunity(db, "moving")
    row = await _fetch_opportunity(db, "moving")
    await db.grade_opportunity(
        row.id,
        result="HIT",
        actual_value=1.0,
        grade_result_source="propline_history",
    )

    await _snapshot_and_seed(db, "moving", 1.0, offset=0)
    await _snapshot_and_seed(db, "moving", 7.0, offset=1)

    summary = await db.get_directional_performance_summary()
    sport = summary["by_sport"]["MLB"]

    assert sport["clv_count"] == 1
    assert sport["clv_hit_avg"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_reports_exclude_unverified_grades_and_filter_enabled_sports(db):
    for external_id, sport in (
        ("verified-mlb", "MLB"),
        ("push-mlb", "MLB"),
        ("void-mlb", "MLB"),
        ("verified-nba", "NBA"),
        ("legacy-nba", "NBA"),
    ):
        await _opportunity(db, external_id, sport=sport)

    grade_rows = (
        ("verified-mlb", "HIT", "propline_history"),
        ("push-mlb", "PUSH", "propline_history"),
        ("void-mlb", "VOID", "propline_event_results_void"),
        ("verified-nba", "MISS", "propline_history"),
        ("legacy-nba", "HIT", None),
    )
    for external_id, result, source in grade_rows:
        row = await _fetch_opportunity(db, external_id)
        await db.grade_opportunity(
            row.id,
            result=result,
            actual_value=1.0,
            grade_result_source=source,
        )

    enabled = await db.get_directional_performance_summary(
        enabled_sports={"MLB"}
    )
    assert set(enabled["by_sport"]) == {"MLB"}
    mlb = enabled["by_sport"]["MLB"]
    assert mlb["hit"] == 1
    assert mlb["push"] == 1
    assert mlb["void"] == 1
    assert mlb["unverified"] == 0
    assert mlb["denominator"] == 1
    assert mlb["hit_rate"] == 100.0

    all_sports = await db.get_directional_performance_summary()
    nba = all_sports["by_sport"]["NBA"]
    assert nba["miss"] == 1
    assert nba["unverified"] == 1
    assert nba["denominator"] == 1

    rollups = await db.get_learning_rollups(enabled_sports={"MLB"})
    assert rollups["total_graded"] == 2
    assert rollups["unverified_graded"] == 0
    all_rollups = await db.get_learning_rollups()
    assert all_rollups["unverified_graded"] == 1