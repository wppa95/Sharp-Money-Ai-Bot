"""Regression coverage for the shared Player Props durability boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database import Database, PropLineHistory, PropTrackingLog
from market_engine import _persist_propline_observations
from engine.player_props_pipeline import canonicalize_player_props


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_propline_observation_is_stored_in_history_and_tracking(db: Database):
    observation = SimpleNamespace(
        player_name="Alex Example",
        team="EX",
        sport="MLB",
        stat_type="Hits",
        line_value=1.5,
        game_time=datetime.utcnow() + timedelta(hours=2),
        event_id="event-123",
        game_id="",
        external_id="projection-123",
        line_variant="demon",
        fetched_at=datetime.utcnow(),
    )

    assert await _persist_propline_observations(db, [observation]) == 1

    async with db.session() as session:
        history_result = await session.execute(
            select(PropLineHistory).where(
                PropLineHistory.provider == "PrizePicks",
                PropLineHistory.external_id == "projection-123",
            )
        )
        tracking_result = await session.execute(
            select(PropTrackingLog).where(
                PropTrackingLog.provider == "PrizePicks",
                PropTrackingLog.player_name == "Alex Example",
            )
        )

        history = history_result.scalar_one()
        tracking = tracking_result.scalar_one()

    assert history.line_value == 1.5
    assert history.game_id == "event-123"
    assert history.line_variant == "demon"
    assert history.product == "Player Props"
    assert tracking.game_id == "event-123"
    assert tracking.product == "Player Props"
    assert tracking.result == "PENDING"


def test_canonicalization_keeps_same_source_identity_at_distinct_lines():
    base = dict(
        player_name="Alex Example",
        team="EX",
        sport="MLB",
        stat_type="Hits",
        game_time=None,
        event_id="event-123",
        game_id="",
        external_id="projection-123",
        line_variant="standard",
        fetched_at=datetime.utcnow(),
    )
    props = [
        SimpleNamespace(line_value=1.5, **base),
        SimpleNamespace(line_value=2.5, **base),
    ]

    canonical = canonicalize_player_props("PrizePicks", props)

    assert [prop.line_value for prop in canonical] == [1.5, 2.5]