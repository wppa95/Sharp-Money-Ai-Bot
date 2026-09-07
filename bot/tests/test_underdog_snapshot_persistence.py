"""Regression tests for reliable Underdog snapshot bulk persistence."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import traceback

import pytest
from sqlalchemy import func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import (
    Database,
    UnderdogSnapshotRecord,
    _is_sqlite_cantopen_error,
    _is_sqlite_lock_error,
    _sqlite_error_kind,
)


class _FakeSession:
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.added: list[object] = []
        self.commits = 0
        self.exited = False
        self.invalidated = False

    def add_all(self, records) -> None:
        self.added.extend(records)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def invalidate(self) -> None:
        self.invalidated = True


class _SessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.session.exited = True


@pytest.mark.asyncio
async def test_bulk_snapshot_save_retries_lock_in_fresh_transaction(monkeypatch):
    first = _FakeSession(RuntimeError("database is locked"))
    second = _FakeSession()
    sessions = iter((first, second))
    database = Database("sqlite+aiosqlite:///:memory:")
    database.session = lambda: _SessionContext(next(sessions))
    monkeypatch.setattr("database._UNDERDOG_BULK_MAX_RETRIES", 2)
    monkeypatch.setattr("database._UNDERDOG_BULK_RETRY_DELAYS", (0, 0))

    records = [object(), object()]
    result = await database.save_underdog_snapshots_bulk(records)

    assert result == {"records_written": 2, "retry_count": 1}
    assert first.added == records
    assert second.added == records
    assert first.exited and second.exited


@pytest.mark.asyncio
async def test_bulk_snapshot_save_propagates_exhausted_lock(monkeypatch):
    session_list = [
        _FakeSession(RuntimeError("database is locked")),
        _FakeSession(RuntimeError("database is locked")),
        _FakeSession(RuntimeError("database is locked")),
    ]
    sessions = list(session_list)
    database = Database("sqlite+aiosqlite:///:memory:")
    database.session = lambda: _SessionContext(sessions.pop(0))
    monkeypatch.setattr("database._UNDERDOG_BULK_MAX_RETRIES", 2)
    monkeypatch.setattr("database._UNDERDOG_BULK_RETRY_DELAYS", (0, 0))

    with pytest.raises(RuntimeError, match="database is locked"):
        await database.save_underdog_snapshots_bulk([object()])

    assert all(session.commits == 1 for session in session_list)
    assert all(session.exited for session in session_list)


@pytest.mark.asyncio
async def test_bulk_snapshot_save_does_not_retry_non_lock_failure():
    session = _FakeSession(RuntimeError("disk I/O error"))
    database = Database("sqlite+aiosqlite:///:memory:")
    database.session = lambda: _SessionContext(session)

    with pytest.raises(RuntimeError, match="disk I/O error"):
        await database.save_underdog_snapshots_bulk([object()])

    assert session.commits == 1


def test_sqlite_cantopen_is_distinct_from_lock():
    cantopen = sqlite3.OperationalError("unable to open database file")
    locked = sqlite3.OperationalError("database is locked")

    assert _sqlite_error_kind(cantopen) == "cantopen"
    assert _is_sqlite_cantopen_error(cantopen)
    assert not _is_sqlite_lock_error(cantopen)
    assert _sqlite_error_kind(locked) == "lock"
    assert _is_sqlite_lock_error(locked)
    assert not _is_sqlite_cantopen_error(locked)


@pytest.mark.asyncio
async def test_cantopen_transient_retry_uses_fresh_invalidated_session(monkeypatch):
    first = _FakeSession(sqlite3.OperationalError("unable to open database file"))
    second = _FakeSession()
    sessions = iter((first, second))
    database = Database("sqlite+aiosqlite:///:memory:")
    database.session = lambda: _SessionContext(next(sessions))
    monkeypatch.setattr("database._UNDERDOG_BULK_MAX_RETRIES", 2)
    monkeypatch.setattr("database._UNDERDOG_CANTOPEN_RETRY_DELAYS", (0, 0))

    result = await database.save_underdog_snapshots_bulk([object()])

    assert result == {"records_written": 1, "retry_count": 1}
    assert first.invalidated
    assert first.commits == 1
    assert second.commits == 1
    assert first.exited and second.exited


@pytest.mark.asyncio
async def test_cantopen_preserves_original_traceback_and_stage(caplog, monkeypatch):
    session = _FakeSession(sqlite3.OperationalError("unable to open database file"))
    database = Database("sqlite+aiosqlite:///:memory:")
    database.session = lambda: _SessionContext(session)
    monkeypatch.setattr("database._UNDERDOG_BULK_MAX_RETRIES", 0)

    with pytest.raises(sqlite3.OperationalError) as raised:
        await database.save_underdog_snapshots_bulk([object()])

    frames = traceback.extract_tb(raised.value.__traceback__)
    assert any(frame.name == "commit" for frame in frames)
    assert session.invalidated
    assert "stage=commit" in caplog.text
    assert "kind=cantopen" in caplog.text


@pytest.mark.asyncio
async def test_cantopen_exhausted_retries_propagate_and_never_reuse_sessions(
    monkeypatch,
):
    sessions = [
        _FakeSession(sqlite3.OperationalError("unable to open database file"))
        for _ in range(3)
    ]
    original_sessions = list(sessions)
    database = Database("sqlite+aiosqlite:///:memory:")
    database.session = lambda: _SessionContext(sessions.pop(0))
    monkeypatch.setattr("database._UNDERDOG_BULK_MAX_RETRIES", 2)
    monkeypatch.setattr("database._UNDERDOG_CANTOPEN_RETRY_DELAYS", (0, 0))

    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        await database.save_underdog_snapshots_bulk([object()])

    assert all(session.invalidated for session in original_sessions)
    assert all(session.commits == 1 for session in original_sessions)
    assert all(session.exited for session in original_sessions)


def _snapshot(index: int) -> UnderdogSnapshotRecord:
    return UnderdogSnapshotRecord(
        external_id=f"concurrent-{index}",
        player_name=f"Player {index}",
        team="TST",
        sport="NBA",
        stat_type="Points",
        line_value=10.5,
        game_id=f"game-{index}",
    )


@pytest.mark.asyncio
async def test_file_backed_concurrent_bulk_persistence_does_not_drop_records(tmp_path):
    db_path = tmp_path / "concurrent.sqlite"
    database = Database(f"sqlite+aiosqlite:///{db_path}")
    await database.init()
    try:
        batches = [[_snapshot(batch * 10 + index) for index in range(10)] for batch in range(8)]
        results = await asyncio.gather(
            *(
                database.save_underdog_snapshots_bulk(batch)
                for batch in batches
            )
        )

        async with database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(UnderdogSnapshotRecord)
            )

        assert sum(result["records_written"] for result in results) == 80
        assert count == 80
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_empty_bulk_snapshot_save_reports_zero():
    database = Database("sqlite+aiosqlite:///:memory:")

    assert await database.save_underdog_snapshots_bulk([]) == {
        "records_written": 0,
        "retry_count": 0,
    }


@pytest.mark.asyncio
async def test_large_bulk_snapshot_save_uses_bounded_transactions(monkeypatch):
    """Large feeds release the SQLite writer gate between complete chunks."""
    sessions = [_FakeSession() for _ in range(3)]
    original_sessions = list(sessions)
    database = Database("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr("database._UNDERDOG_BULK_BATCH_SIZE", 2)
    database.session = lambda: _SessionContext(sessions.pop(0))

    result = await database.save_underdog_snapshots_bulk(
        [object(), object(), object(), object(), object()]
    )

    assert result == {"records_written": 5, "retry_count": 0}
    assert all(session.commits == 1 for session in original_sessions)
    assert all(session.exited for session in original_sessions)