"""Regression tests for the delivery-independent tracked prop lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database import (
    Database,
    PropLineHistory,
    PropOpportunityLog,
    PropTrackingLog,
)
from engine.backtesting import BacktestEngine
from engine.calibration import _prop_bet_outcome


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

    await db.grade_opportunity(
        undelivered_id, "HIT", 25.0, grade_result_source="propline_history"
    )
    await db.grade_opportunity(
        prizepicks_id, "HIT", 18.0, grade_result_source="external_player_stats"
    )
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

    await db.grade_opportunity(
        row_id, "HIT", 25.0, grade_result_source="propline_history"
    )
    await db.grade_opportunity(
        row_id, "HIT", 25.0, grade_result_source="propline_history"
    )

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
            result="HIT",
            confidence=75,
            bet_quality_score=None,
        ),
        SimpleNamespace(
            external_id="pp-backtest-miss",
            game_id="pp-game-miss",
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

    assert report.total_evaluated == 3
    assert report.overall.wins == 2
    assert report.overall.losses == 1
    assert report.by_provider["Underdog"].wins == 1
    assert report.by_provider["PrizePicks"].wins == 1
    assert report.by_provider["PrizePicks"].losses == 1


def test_calibration_uses_directional_result_without_flipping_under():
    assert _prop_bet_outcome(
        SimpleNamespace(recommendation="UNDER", result="HIT")
    ) == "WIN"
    assert _prop_bet_outcome(
        SimpleNamespace(recommendation="UNDER", result="MISS")
    ) == "LOSS"


@pytest.mark.asyncio
async def test_funnel_separates_observations_active_directional_and_delivery(db):
    now = datetime.utcnow()
    async with db.session() as session:
        session.add_all([
            PropLineHistory(
                external_id="ud-funnel-1",
                provider="Underdog",
                player_name="Tracked Player",
                team="TEAM",
                sport="NBA",
                stat_type="Points",
                line_value=20.5,
                game_time=now + timedelta(hours=2),
                fetched_at=now - timedelta(hours=2),
                removed=False,
            ),
            PropLineHistory(
                external_id="ud-funnel-1",
                provider="Underdog",
                player_name="Tracked Player",
                team="TEAM",
                sport="NBA",
                stat_type="Points",
                line_value=21.5,
                game_time=now + timedelta(hours=2),
                fetched_at=now,
                removed=False,
            ),
            PropOpportunityLog(
                external_id="ud-funnel-1",
                player_name="Tracked Player",
                team="TEAM",
                sport="NBA",
                stat_type="Points",
                line_value=21.5,
                recommendation="OVER",
                decision_tier="A",
                confidence=80,
                game_time=now + timedelta(hours=2),
                game_id="game-ud-funnel-1",
                provider="Underdog",
                result="PENDING",
                alert_sent=False,
                detected_at=now,
            ),
            PropTrackingLog(
                external_id="ud-tracked-1:line=20.5",
                player_name="Tracked Player",
                team="TEAM",
                sport="NBA",
                stat_type="Rebounds",
                line_value=20.5,
                game_time=now + timedelta(hours=2),
                game_id="game-ud-tracked-1",
                provider="Underdog",
                detected_at=now,
                result="PENDING",
            ),
        ])
        await session.commit()

    summary = await db.get_funnel_summary(since_hours=24)

    assert summary["tracked"] == 1
    assert summary["tracked_observations"] == 2
    assert summary["observations_by_sport"] == {"NBA": 2}
    assert summary["active_by_sport"] == {"NBA": 1}
    assert summary["active_tracked"] <= summary["historical_observations"]
    assert summary["directional_total"] == 1
    assert summary["delivered"] == 0
    assert summary["pending"] == 1
    assert summary["resolved"] == 0
    assert summary["tracked_pending"] == 1
    assert summary["tracked_resolved"] == 0
    assert summary["tracked_result_counts"] == {"PENDING": 1}
    assert summary["provider_breakdown"]["Underdog"]["pending"] == 1
    assert summary["directional_by_provider"] == {"Underdog": 1}
    assert summary["delivered_by_provider"] == {}


@pytest.mark.asyncio
async def test_tracking_summary_includes_football_and_soccer(db):
    now = datetime.utcnow()
    async with db.session() as session:
        session.add_all([
            PropLineHistory(
                external_id="legacy-football",
                provider="Underdog",
                player_name="Football Player",
                team="TEAM",
                sport="NFL",
                stat_type="Passing Yards",
                line_value=250.5,
                game_time=now + timedelta(hours=2),
                fetched_at=now,
                removed=False,
            ),
            PropLineHistory(
                external_id="soccer-supported",
                provider="PrizePicks",
                player_name="Soccer Player",
                team="TEAM",
                sport="SOCCER",
                stat_type="Goals",
                line_value=0.5,
                game_time=now + timedelta(hours=2),
                fetched_at=now,
                removed=False,
            ),
        ])
        await session.commit()

    summary = await db.get_tracking_summary()

    assert summary["tracked_observations"] == 2
    assert summary["tracked"] == 2
    assert summary["active_by_sport"] == {"NFL": 1, "SOCCER": 1}


@pytest.mark.asyncio
async def test_universal_tracking_grade_summary_keeps_neutral_results_separate(db):
    async with db.session() as session:
        session.add_all([
            PropTrackingLog(
                external_id="tracked-hit",
                provider="Underdog",
                player_name="Tracked Player",
                sport="NBA",
                stat_type="Points",
                line_value=20.5,
                result="HIT",
                settlement_source="auto_grade",
            ),
            PropTrackingLog(
                external_id="tracked-miss",
                provider="Underdog",
                player_name="Tracked Player",
                sport="NBA",
                stat_type="Rebounds",
                line_value=8.5,
                result="MISS",
                settlement_source="auto_grade",
            ),
            PropTrackingLog(
                external_id="tracked-push",
                provider="PrizePicks",
                player_name="Tracked Player",
                sport="NFL",
                stat_type="Passing Yards",
                line_value=250.5,
                result="PUSH",
                settlement_source="auto_grade",
            ),
            PropTrackingLog(
                external_id="tracked-pending",
                provider="PrizePicks",
                player_name="Tracked Player",
                sport="NFL",
                stat_type="Rushing Yards",
                line_value=50.5,
                result="PENDING",
            ),
            PropTrackingLog(
                external_id="tracked-unverified",
                provider="FanDuel",
                player_name="Tracked Player",
                sport="WNBA",
                stat_type="Points",
                line_value=15.5,
                result="HIT",
                settlement_source="manual",
            ),
            PropTrackingLog(
                external_id="tracked-void",
                provider="DraftKings",
                player_name="Tracked Player",
                sport="SOCCER",
                stat_type="Goals",
                line_value=0.5,
                result="VOID",
                settlement_source="auto_grade",
            ),
        ])
        await session.commit()

    summary = await db.get_tracked_prop_grade_summary()

    assert summary["total"] == 6
    assert summary["tracked"] == 6
    assert summary["pending"] == 1
    assert summary["resolved"] == 5
    assert summary["verified"] == 4
    assert summary["unverified"] == 1
    assert summary["hit"] == 1
    assert summary["miss"] == 1
    assert summary["push"] == 1
    assert summary["void"] == 1
    assert summary["denominator"] == 3
    assert summary["above_line_rate"] == 33.3
    assert summary["by_provider"]["Underdog"]["total"] == 2
    assert summary["by_provider"]["FanDuel"]["unverified"] == 1
    assert summary["by_sport"]["SOCCER"]["void"] == 1


@pytest.mark.asyncio
async def test_funnel_includes_sportsbook_provider_observations(db):
    now = datetime.utcnow()
    async with db.session() as session:
        session.add(
            PropLineHistory(
                external_id="dk-funnel-1",
                provider="DraftKings",
                player_name="Sportsbook Player",
                team="TEAM",
                sport="NBA",
                stat_type="Points",
                line_value=22.5,
                game_time=now + timedelta(hours=2),
                fetched_at=now,
                removed=False,
            )
        )
        await session.commit()

    summary = await db.get_funnel_observation_summary(since_hours=24)

    assert summary["historical_observations"] == 1
    assert summary["observations_by_sport"] == {"NBA": 1}