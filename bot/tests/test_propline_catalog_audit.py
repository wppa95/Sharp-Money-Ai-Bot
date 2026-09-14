"""Durable history for observational PropLine catalog drift."""

from __future__ import annotations

from datetime import datetime

import pytest

from database import Database


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _identity(*, mapped: bool, sample_count: int) -> dict:
    return {
        "kind": "market",
        "provider_key": "player_points_renamed",
        "canonical_sport": "NBA",
        "canonical_market": "points",
        "provider_label": "Player Points",
        "sample_count": sample_count,
        "mapped": mapped,
    }


@pytest.mark.asyncio
async def test_catalog_audit_keeps_first_seen_and_resolves_mapping(db):
    first_seen = datetime(2026, 9, 14, 12, 0, 0)
    repeated = datetime(2026, 9, 14, 12, 5, 0)
    resolved = datetime(2026, 9, 14, 12, 10, 0)

    assert await db.save_propline_catalog_audit(
        [_identity(mapped=False, sample_count=4)],
        observed_at=first_seen,
    ) == {
        "observed": 1,
        "new": 1,
        "mapped": 0,
        "unmapped": 1,
        "unchanged": 0,
    }
    assert await db.save_propline_catalog_audit(
        [_identity(mapped=False, sample_count=7)],
        observed_at=repeated,
    ) == {
        "observed": 1,
        "new": 0,
        "mapped": 0,
        "unmapped": 0,
        "unchanged": 1,
    }
    assert await db.save_propline_catalog_audit(
        [_identity(mapped=True, sample_count=9)],
        observed_at=resolved,
    ) == {
        "observed": 1,
        "new": 0,
        "mapped": 1,
        "unmapped": 0,
        "unchanged": 0,
    }

    history = await db.get_propline_catalog_audit_history()
    assert len(history) == 1
    assert history[0]["provider_key"] == "player_points_renamed"
    assert history[0]["canonical_context"] == {
        "sport": "NBA",
        "market": "points",
    }
    assert history[0]["sample_count"] == 9
    assert history[0]["status"] == "mapped"
    assert history[0]["first_seen_at"] == first_seen.isoformat()
    assert history[0]["last_seen_at"] == resolved.isoformat()
    assert history[0]["resolved_at"] == resolved.isoformat()


@pytest.mark.asyncio
async def test_known_mapped_identity_does_not_create_catalog_drift_row(db):
    result = await db.save_propline_catalog_audit(
        [
            {
                **_identity(mapped=True, sample_count=3),
                "provider_key": "player_points",
            }
        ]
    )

    assert result["observed"] == 1
    assert result["new"] == 0
    assert await db.get_propline_catalog_audit_history() == []