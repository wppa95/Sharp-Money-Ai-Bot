"""Focused regression coverage for the final monitoring corrections."""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from database import Database, UnderdogSnapshotRecord


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _snapshot(
    *,
    external_id: str,
    line: float,
    removed: bool = False,
    game_time: dt.datetime | None = None,
) -> UnderdogSnapshotRecord:
    return UnderdogSnapshotRecord(
        external_id=external_id,
        player_name="Rotation Player",
        team="TEAM",
        sport="NBA",
        stat_type="Points",
        line_value=line,
        game_id="game-1",
        game_time=game_time,
        removed=removed,
        fetched_at=dt.datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_active_pool_reconciles_line_movement_by_source_id(db):
    """A moved source prop leaves only its new line in active rotation."""
    await db.save_underdog_snapshot(_snapshot(external_id="projection-1", line=24.5))
    await db.save_underdog_snapshot(_snapshot(external_id="projection-1", line=25.5))

    active = await db.get_all_active_underdog_snapshots_by_line()

    assert ("Rotation Player", "Points", 24.5) not in active
    assert active[("Rotation Player", "Points", 25.5)].external_id == "projection-1"


@pytest.mark.asyncio
async def test_active_pool_keeps_distinct_alternate_lines(db):
    """Distinct Underdog source IDs remain separate active line variants."""
    await db.save_underdog_snapshot(_snapshot(external_id="projection-low", line=24.5))
    await db.save_underdog_snapshot(_snapshot(external_id="projection-high", line=25.5))

    active = await db.get_all_active_underdog_snapshots_by_line()

    assert set(active) == {
        ("Rotation Player", "Points", 24.5),
        ("Rotation Player", "Points", 25.5),
    }


@pytest.mark.asyncio
async def test_started_prop_leaves_active_pool_but_history_remains(db):
    """Game start removes rotation eligibility, not the raw snapshot record."""
    started = dt.datetime.utcnow() - dt.timedelta(minutes=1)
    await db.save_underdog_snapshot(
        _snapshot(external_id="projection-started", line=24.5, game_time=started)
    )

    active = await db.get_all_active_underdog_snapshots_by_line()

    assert active == {}
    assert (await db.get_latest_underdog_snapshot_per_prop())[
        ("Rotation Player", "Points")
    ].line_value == 24.5


def test_reporting_and_scheduler_corrections_are_explicit():
    from commands import cmd_stats
    from main import post_init

    stats_source = inspect.getsource(cmd_stats)
    startup_source = inspect.getsource(post_init)

    assert "get_tracking_summary" in stats_source
    assert "get_top_pp_edges" not in stats_source
    assert "interval=3600" in startup_source
    assert 'name="stable_refresh"' in startup_source
