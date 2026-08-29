"""Regression tests for the neutral active-prop monitoring contract."""

from __future__ import annotations

import inspect
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import market_engine
from database import Database, PropCandidateLog, PropOpportunityLog
from engine.dashboard import DashboardEngine, DashboardReport


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_legacy_candidate_labels_are_counted_only_as_neutral_tracked_rows(db):
    async with db.session() as session:
        session.add_all([
            PropCandidateLog(
                scan_ts=datetime.utcnow(),
                player_name="Legacy One",
                team="",
                sport="NBA",
                stat_type="Points",
                line_value=20.5,
                provider="Underdog",
                gate_decision="REJECTED",
                rejection_reason="decision_pass",
            ),
            PropCandidateLog(
                scan_ts=datetime.utcnow(),
                player_name="Legacy Two",
                team="",
                sport="NBA",
                stat_type="Assists",
                line_value=5.5,
                provider="Underdog",
                gate_decision="WATCHLIST",
                rejection_reason="not_immediate",
            ),
        ])
        await session.commit()

    summary = await db.get_funnel_summary()

    assert summary["since_hours"] == 24
    assert summary["tracked"] == 0
    assert summary["active_tracked"] == 0
    assert summary["tracked_observations"] == 0
    assert summary["evaluated"] == 2
    assert summary["by_sport"] == [
        {"sport": "NBA", "tracked": 2, "evaluated": 2},
    ]


@pytest.mark.asyncio
async def test_tracking_and_dashboard_ignore_legacy_non_directional_rows(db):
    async with db.session() as session:
        session.add_all([
            PropOpportunityLog(
                external_id="directional",
                player_name="Directional",
                team="",
                sport="NBA",
                stat_type="Points",
                line_value=20.5,
                recommendation="OVER",
                decision_tier="A",
                confidence=75,
                result="HIT",
            ),
            PropOpportunityLog(
                external_id="legacy-pass",
                player_name="Legacy",
                team="",
                sport="NBA",
                stat_type="Assists",
                line_value=5.5,
                recommendation="PASS",
                decision_tier="PASS",
                confidence=0,
                result="MISS",
            ),
        ])
        await session.commit()

    tracking = await db.get_tracking_summary()
    assert tracking["total"] == 1
    assert tracking["counts"] == {"OVER": {"HIT": 1}}
    assert "by_tier" not in tracking

    report = DashboardReport()
    await DashboardEngine._gather_intelligence_panel(db, report)
    assert report.intelligence_panel is not None
    assert report.intelligence_panel.total_graded == 1
    assert report.intelligence_panel.total_hits == 1
    assert report.intelligence_panel.total_misses == 0


@pytest.mark.asyncio
async def test_candidate_batch_uses_neutral_evaluated_state_by_default(db):
    inserted = await db.log_prop_candidate_batch([{
        "player_name": "Directional",
        "sport": "NBA",
        "stat_type": "Points",
        "line_value": 20.5,
        "provider": "Underdog",
        "score_total": 72,
        "confidence": 75,
    }])
    assert inserted == 1

    async with db.session() as session:
        row = (
            await session.execute(select(PropCandidateLog))
        ).scalar_one()
    assert row.gate_decision == "EVALUATED"
    assert row.rejection_reason is None


def test_active_scan_logs_and_candidate_writes_have_no_retired_classifications():
    source = inspect.getsource(market_engine.underdog_job)
    for retired in (
        "decision_pass",
        "not_immediate",
        "rejection breakdown",
        '"rejection":',
    ):
        assert retired not in source
    assert '"gate_decision":        "EVALUATED"' in source
    assert '"rejection_reason":     None' in source


@pytest.mark.asyncio
async def test_stable_refresh_keeps_no_direction_prop_in_active_rotation_without_row():
    from tests.test_stable_refresh import (
        _build_minimal_db,
        _make_context,
        _make_decision,
        _make_score,
        _make_snap,
        _make_validation,
    )

    snap = _make_snap()
    test_db = _build_minimal_db(
        active_pool={("Test Player", "Points"): snap},
        hist=[1, 2, 3],
    )
    context = _make_context(test_db)
    decide = _make_decision(rec="PASS", tier="PASS", conf=0)

    with (
        patch("market_engine.get_health_tracker", return_value=None),
        patch("market_engine._is_futures_stat", return_value=False),
        patch("market_engine._is_game_live_or_past", return_value=False),
        patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
        patch(
            "engine.player_validator.validate_player_prop",
            return_value=_make_validation(supported=True),
        ),
        patch(
            "market_engine._fetch_and_compute_hit_rates",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "engine.ud_bet_decision.make_ud_bet_decision",
            return_value=decide,
        ),
    ):
        await market_engine._stable_refresh_job(context)

    test_db.get_active_underdog_snapshot_per_prop.assert_awaited_once()
    test_db.log_prop_opportunity.assert_not_awaited()


@pytest.mark.asyncio
async def test_fpr_rescores_no_direction_prop_without_persisting_classification():
    from tests.test_full_pool_rescan import (
        _make_context,
        _make_db,
        _make_decision,
        _make_health,
        _make_score,
        _make_snap,
        _make_validation,
    )

    snap = _make_snap()
    test_db = _make_db(pool={("Test Player", "Points", 22.5): snap})
    context = _make_context(test_db)
    health = _make_health()

    score = _make_score()
    decision = _make_decision(rec="PASS", tier="PASS", conf=0)
    validation = _make_validation(supported=True)
    with (
        patch("market_engine.get_health_tracker", return_value=health),
        patch("market_engine._is_game_live_or_past", return_value=False),
        patch(
            "market_engine._fetch_and_compute_hit_rates",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("engine.ud_scoring.score_ud_prop", return_value=score),
        patch(
            "engine.player_validator.validate_player_prop",
            return_value=validation,
        ),
        patch(
            "engine.ud_bet_decision.make_ud_bet_decision",
            return_value=decision,
        ),
    ):
        await market_engine._full_pool_rescan_job(context)

    test_db.log_prop_opportunity.assert_not_awaited()
    health.set_fpr_cursor.assert_called()