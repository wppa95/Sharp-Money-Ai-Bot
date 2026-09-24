"""Persistence tests for generic PropLine endpoint evidence."""

from datetime import datetime, timedelta, timezone

import pytest

from database import Database


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _record(target_key="event-1", **overrides):
    record = {
        "family": "event_results",
        "target_key": target_key,
        "official_sport": "basketball_nba",
        "canonical_sport": "NBA",
        "event_id": "event-1",
        "player_name": "A Player",
        "market": "points",
        "source": "api",
        "fetched_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "complete": True,
        "redacted": False,
        "payload": {"results": [1]},
    }
    record.update(overrides)
    return record


@pytest.mark.asyncio
async def test_evidence_upsert_identity_and_payload(db):
    assert await db.upsert_propline_evidence_batch(
        [_record(payload={"version": 1}), _record(payload={"version": 2})]
    ) == 1
    row = await db.get_propline_evidence("event_results", "event-1")
    assert row["payload"] == {"version": 2}
    assert row["canonical_sport"] == "NBA"


@pytest.mark.asyncio
async def test_evidence_fresh_and_stale_queries(db):
    await db.upsert_propline_evidence_batch(
        [_record(expires_at=datetime(2026, 1, 1, 12, 0))]
    )
    now = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    assert await db.get_propline_evidence(
        "event_results", "event-1", fresh_only=True, now=now
    ) is None
    await db.upsert_propline_evidence_batch(
        [_record("event-2", expires_at=datetime(2026, 1, 2))]
    )
    assert await db.get_fresh_propline_evidence_target_keys(
        "event_results", ["event-1", "event-2"], now=now
    ) == {"event-2"}


@pytest.mark.asyncio
async def test_evidence_filters_batching_and_rejection(db):
    assert await db.upsert_propline_evidence_batch(
        [
            _record("event-1"),
            _record("event-2", player_name="Other Player", market="rebounds"),
            _record("event-3", complete=False),
            _record("event-4", redacted=True),
        ]
    ) == 2
    rows = await db.list_propline_evidence(
        canonical_sport="NBA", player_name="Other Player", market="rebounds"
    )
    assert [row["target_key"] for row in rows] == ["event-2"]