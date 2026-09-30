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


@pytest.mark.asyncio
async def test_same_source_variants_keep_separate_history_and_future_context(
    db: Database,
):
    now = datetime.utcnow()
    base = dict(
        player_name="Alex Example",
        team="EX",
        sport="MLB",
        stat_type="Hits",
        game_time=now + timedelta(days=1),
        event_id="history-event",
        game_id="",
        external_id="shared-projection-id",
    )
    first = [
        SimpleNamespace(
            line_value=1.5,
            line_variant="standard",
            fetched_at=now,
            **base,
        ),
        SimpleNamespace(
            line_value=2.5,
            line_variant="demon",
            fetched_at=now,
            **base,
        ),
    ]
    assert await db.upsert_prop_line_history_batch(
        "PrizePicks", first, append_snapshot=True
    ) == 2
    moved = [
        SimpleNamespace(
            line_value=1.75,
            line_variant="standard",
            fetched_at=now + timedelta(minutes=1),
            **base,
        ),
        SimpleNamespace(
            line_value=3.0,
            line_variant="demon",
            fetched_at=now + timedelta(minutes=1),
            **base,
        ),
    ]
    assert await db.upsert_prop_line_history_batch(
        "PrizePicks", moved, append_snapshot=True
    ) == 2

    active = await db.get_all_active_prop_line_history_by_line(
        providers=("PrizePicks",)
    )
    assert {
        key[6]: row.line_value
        for key, row in active.items()
        if key[5] == "shared-projection-id"
    } == {"standard": 1.75, "demon": 3.0}

    contexts = await db.get_prop_line_movement_context_bulk(
        [
            ("PrizePicks", "MLB", "Alex Example", "Hits", "standard", "next-event"),
            ("PrizePicks", "MLB", "Alex Example", "Hits", "demon", "next-event"),
        ],
        before=now + timedelta(minutes=2),
    )
    standard = contexts[
        ("PrizePicks", "MLB", "Alex Example", "Hits", "standard")
    ][0]["lines"][0]
    demon = contexts[
        ("PrizePicks", "MLB", "Alex Example", "Hits", "demon")
    ][0]["lines"][0]

    assert standard["source_id"] == demon["source_id"] == "shared-projection-id"
    assert standard["opening_line"] == 1.5
    assert standard["previous_line"] == 1.5
    assert standard["current_line"] == 1.75
    assert standard["movement_count"] == 1
    assert demon["opening_line"] == 2.5
    assert demon["previous_line"] == 2.5
    assert demon["current_line"] == 3.0
    assert demon["movement_count"] == 1
    same_event_context = await db.get_prop_line_movement_context_bulk(
        [
            (
                "PrizePicks",
                "MLB",
                "Alex Example",
                "Hits",
                "standard",
                "history-event",
            )
        ],
        before=now + timedelta(minutes=2),
    )
    assert same_event_context[
        ("PrizePicks", "MLB", "Alex Example", "Hits", "standard")
    ] == []


@pytest.mark.asyncio
async def test_unlabeled_underdog_lines_remain_separate_by_source_in_context(
    db: Database,
):
    now = datetime.utcnow()
    props = [
        SimpleNamespace(
            player_name="Alex Example",
            team="EX",
            sport="MLB",
            stat_type="Hits",
            line_value=line,
            game_time=now + timedelta(days=1),
            event_id="history-event",
            game_id="",
            external_id=source_id,
            line_variant="",
            fetched_at=now,
        )
        for source_id, line in (
            ("ud-low", 1.5),
            ("ud-high", 2.5),
        )
    ]
    await db.upsert_prop_line_history_batch("Underdog", props)

    contexts = await db.get_prop_line_movement_context_bulk(
        [("Underdog", "MLB", "Alex Example", "Hits", "", "next-event")]
    )
    lines = contexts[
        ("Underdog", "MLB", "Alex Example", "Hits", "")
    ][0]["lines"]

    assert {line["source_id"] for line in lines} == {"ud-low", "ud-high"}
    assert {line["current_line"] for line in lines} == {1.5, 2.5}


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