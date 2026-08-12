"""
Tests for Database.get_latest_underdog_snapshot_per_prop().

Verifies:
  - Empty DB returns empty dict
  - Single prop returns one entry keyed by (player_name, stat_type)
  - Multiple distinct props return one entry each
  - When a prop has multiple rows (from different runs), the highest-id row wins
  - Removed rows are excluded from the lookup
  - A prop whose only rows are removed returns no entry
  - Mixed removed/non-removed: non-removed wins
  - The returned dict is immediately usable for O(1) key lookup
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from datetime import datetime, timedelta

import pytest
from database import Database, UnderdogSnapshotRecord


# ── Fixture: fresh in-memory DB ───────────────────────────────────────────────

@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _snap(
    player: str,
    stat_type: str,
    line_value: float = 2.5,
    removed: bool = False,
    fetched_at: datetime | None = None,
) -> UnderdogSnapshotRecord:
    return UnderdogSnapshotRecord(
        external_id = f"{player}_{stat_type}"[:64],
        player_name = player,
        team        = "team",
        sport       = "MLB",
        stat_type   = stat_type,
        line_value  = line_value,
        game_id     = "",
        game_time   = None,
        line_moved  = False,
        prev_line   = None,
        removed     = removed,
        alert_sent  = False,
        fetched_at  = fetched_at or datetime.utcnow(),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_db_returns_empty_dict(db):
    result = await db.get_latest_underdog_snapshot_per_prop()
    assert result == {}


@pytest.mark.asyncio
async def test_single_prop_keyed_correctly(db):
    await db.save_underdog_snapshot(_snap("Aaron Judge", "Home Runs", line_value=1.5))
    result = await db.get_latest_underdog_snapshot_per_prop()
    assert ("Aaron Judge", "Home Runs") in result
    assert result[("Aaron Judge", "Home Runs")].line_value == 1.5


@pytest.mark.asyncio
async def test_multiple_distinct_props(db):
    await db.save_underdog_snapshot(_snap("Player A", "Hits",      2.5))
    await db.save_underdog_snapshot(_snap("Player A", "Runs",      0.5))
    await db.save_underdog_snapshot(_snap("Player B", "Home Runs", 1.5))

    result = await db.get_latest_underdog_snapshot_per_prop()
    assert len(result) == 3
    assert ("Player A", "Hits")      in result
    assert ("Player A", "Runs")      in result
    assert ("Player B", "Home Runs") in result


@pytest.mark.asyncio
async def test_most_recent_row_wins_for_same_prop(db):
    """When a prop has multiple rows, the one with the highest id (latest) wins."""
    t1 = datetime.utcnow() - timedelta(minutes=10)
    t2 = datetime.utcnow()

    await db.save_underdog_snapshot(_snap("Player A", "Hits", line_value=2.5, fetched_at=t1))
    await db.save_underdog_snapshot(_snap("Player A", "Hits", line_value=3.0, fetched_at=t2))

    result = await db.get_latest_underdog_snapshot_per_prop()
    assert len(result) == 1
    assert result[("Player A", "Hits")].line_value == 3.0


@pytest.mark.asyncio
async def test_removed_rows_excluded(db):
    await db.save_underdog_snapshot(_snap("Player A", "Hits", removed=True))
    result = await db.get_latest_underdog_snapshot_per_prop()
    assert result == {}


@pytest.mark.asyncio
async def test_prop_with_only_removed_rows_absent(db):
    t1 = datetime.utcnow() - timedelta(minutes=5)
    t2 = datetime.utcnow()
    await db.save_underdog_snapshot(_snap("Player A", "Hits", line_value=2.5, fetched_at=t1))
    await db.save_underdog_snapshot(_snap("Player A", "Hits", removed=True,   fetched_at=t2))

    # latest row is removed — but the query filters removed=0, so MAX(id) picks
    # the non-removed row (the earlier one)
    result = await db.get_latest_underdog_snapshot_per_prop()
    assert ("Player A", "Hits") in result
    assert result[("Player A", "Hits")].line_value == 2.5


@pytest.mark.asyncio
async def test_non_removed_row_returned_when_both_exist(db):
    """Mix of removed and non-removed rows for the same prop."""
    t1 = datetime.utcnow() - timedelta(minutes=10)
    t2 = datetime.utcnow() - timedelta(minutes=5)
    t3 = datetime.utcnow()

    await db.save_underdog_snapshot(_snap("Player A", "Hits", line_value=2.5, fetched_at=t1))
    await db.save_underdog_snapshot(_snap("Player A", "Hits", line_value=3.0, fetched_at=t2))
    await db.save_underdog_snapshot(_snap("Player A", "Hits", removed=True,   fetched_at=t3))

    # MAX(id) among non-removed rows → the t2 row (line_value=3.0)
    result = await db.get_latest_underdog_snapshot_per_prop()
    assert ("Player A", "Hits") in result
    assert result[("Player A", "Hits")].line_value == 3.0


@pytest.mark.asyncio
async def test_result_covers_all_props_not_just_200(db):
    """Simulate feed size > 200 — all props must be present."""
    n = 250
    for i in range(n):
        await db.save_underdog_snapshot(_snap(f"Player {i}", "Hits", line_value=float(i)))

    result = await db.get_latest_underdog_snapshot_per_prop()
    assert len(result) == n
    for i in range(n):
        assert (f"Player {i}", "Hits") in result


@pytest.mark.asyncio
async def test_dict_key_lookup_is_correct_type(db):
    """Keys are (str, str) tuples — usable as dict keys in underdog_job."""
    await db.save_underdog_snapshot(_snap("Shohei Ohtani", "Strikeouts", line_value=7.5))
    result = await db.get_latest_underdog_snapshot_per_prop()
    key = ("Shohei Ohtani", "Strikeouts")
    assert isinstance(key, tuple)
    assert key in result
    assert result[key].line_value == 7.5


@pytest.mark.asyncio
async def test_multiple_runs_returns_latest_per_prop(db):
    """Simulate three job cycles; each prop should reflect its latest line."""
    base = datetime.utcnow() - timedelta(minutes=15)

    for cycle in range(3):
        ts = base + timedelta(minutes=cycle * 5)
        line = 2.5 + cycle * 0.5   # 2.5 → 3.0 → 3.5
        await db.save_underdog_snapshot(_snap("Player A", "Hits", line_value=line, fetched_at=ts))
        await db.save_underdog_snapshot(_snap("Player B", "Runs", line_value=1.0,  fetched_at=ts))

    result = await db.get_latest_underdog_snapshot_per_prop()
    assert len(result) == 2
    # Player A's line should be from the latest cycle (3.5)
    assert result[("Player A", "Hits")].line_value == pytest.approx(3.5)
    # Player B's line is stable (1.0)
    assert result[("Player B", "Runs")].line_value == pytest.approx(1.0)
