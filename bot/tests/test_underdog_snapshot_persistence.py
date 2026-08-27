"""Regression tests for reliable Underdog snapshot bulk persistence."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import Database


class _FakeSession:
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.commit_error = commit_error
        self.added: list[object] = []
        self.commits = 0
        self.exited = False

    def add_all(self, records) -> None:
        self.added.extend(records)

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error


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


@pytest.mark.asyncio
async def test_empty_bulk_snapshot_save_reports_zero():
    database = Database("sqlite+aiosqlite:///:memory:")

    assert await database.save_underdog_snapshots_bulk([]) == {
        "records_written": 0,
        "retry_count": 0,
    }