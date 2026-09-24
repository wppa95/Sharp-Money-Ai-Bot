"""Tests for the explicit, non-scheduled PropLine export backfill."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from database import Database
from propline_backfill import (
    backfill_propline_resolved_props,
    persist_resolved_export_csv,
)


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


_EXPORT_CSV = """event_id,sport_key,commence_time,market,bookmaker,player_name,player_id,outcome_id,direction,line,resolution,actual_value,resolved_at,customer_token
game-1,basketball_nba,2026-09-20T20:00:00Z,player_points,prizepicks,Player One,p-1,outcome-1,OVER,20.5,WON,25,2026-09-20T21:00:00Z,customer-private
game-2,basketball_nba,2026-09-20T20:00:00Z,player_points,prizepicks,Player Two,p-2,outcome-2,UNDER,18.5,VOID,,2026-09-20T21:00:00Z,customer-private
game-3,basketball_nba,2026-09-21T20:00:00Z,player_points,prizepicks,Player Three,p-3,outcome-3,OVER,15.5,UNRESOLVED,,,
game-4,basketball_nba,2026-09-21T20:00:00Z,player_points,prizepicks,(Watermark) canary,,watermark-1,,,,,,customer-private
"""


@pytest.mark.asyncio
async def test_export_persists_identity_payload_and_only_numeric_settlements(db):
    report = await persist_resolved_export_csv(
        db,
        _EXPORT_CSV,
        official_sport="basketball_nba",
        fetched_at=datetime(2026, 9, 24, 12, 0),
    )

    rows = await db.list_propline_evidence(
        family="resolved_props_export",
        official_sport="basketball_nba",
        limit=10,
    )
    player_results = await db.get_player_results(
        "Player One", "NBA", "points", limit=10
    )

    assert report["evidence_rows"] == 3
    assert report["game_rows"] == 1
    assert report["skipped_rows"] == 1
    assert {row["outcome_id"] for row in rows} == {
        "outcome-1",
        "outcome-2",
        "outcome-3",
    }
    assert all("customer_token" not in row["payload"] for row in rows)
    assert len(player_results) == 1
    assert player_results[0].actual_value == 25.0
    assert player_results[0].game_id == "game-1"
    assert player_results[0].game_date == "2026-09-20"


class _ExportClient:
    is_configured = True

    def __init__(self, *, succeeded=True, complete=True, redacted=False):
        self.calls = []
        self.result = SimpleNamespace(
            payload=_EXPORT_CSV,
            fetched_at=datetime(2026, 9, 24, 12, 0),
            succeeded=succeeded,
            complete=complete,
            redacted=redacted,
            failure_kind="",
        )

    async def fetch_resolved_props_export(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_backfill_fetches_once_and_reuses_sqlite_request_marker(db):
    client = _ExportClient()
    kwargs = {
        "sport": "basketball_nba",
        "since": "2026-07-01T00:00:00Z",
        "until": "2026-09-24T00:00:00Z",
    }

    first = await backfill_propline_resolved_props(db, client, **kwargs)
    second = await backfill_propline_resolved_props(db, client, **kwargs)

    assert first["status"] == "imported"
    assert second["status"] == "already_imported"
    assert len(client.calls) == 1
    assert client.calls[0]["sport"] == "basketball_nba"


@pytest.mark.asyncio
async def test_backfill_rejects_incomplete_export_without_writing(db):
    client = _ExportClient(complete=False)

    with pytest.raises(RuntimeError, match="incomplete_response"):
        await backfill_propline_resolved_props(
            db, client, sport="basketball_nba"
        )

    assert await db.list_propline_evidence(limit=10) == []


@pytest.mark.asyncio
async def test_backfill_rejects_unmapped_sport_before_provider_call(db):
    client = _ExportClient()

    with pytest.raises(ValueError, match="mapped official"):
        await backfill_propline_resolved_props(
            db, client, sport="unknown_sport_key"
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_backfill_rejects_football_before_provider_call(db):
    client = _ExportClient()

    with pytest.raises(ValueError, match="football-related"):
        await backfill_propline_resolved_props(
            db, client, sport="football_nfl"
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_backfill_rejects_ranges_older_than_90_days_before_provider_call(db):
    client = _ExportClient()
    since = (datetime.utcnow() - timedelta(days=91)).isoformat()

    with pytest.raises(ValueError, match="within 90 days"):
        await backfill_propline_resolved_props(
            db,
            client,
            sport="basketball_nba",
            since=since,
        )

    assert client.calls == []