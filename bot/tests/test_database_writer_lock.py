"""Regression coverage for cross-pool SQLite session serialization."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from database import (
    Database,
    UnderdogSnapshotRecord,
    _SQLITE_MAX_OVERFLOW,
    _SQLITE_POOL_SIZE,
    _SQLITE_POOL_TIMEOUT_SECONDS,
    _sqlite_session_gate,
)


@pytest.mark.asyncio
async def test_same_sqlite_url_shares_one_session_gate_across_database_pools(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'shared.sqlite'}"
    primary = Database(url)
    history = Database(url)
    await primary.init()
    await history.init()

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    active_sessions = 0
    peak_sessions = 0

    async def hold_writer(database: Database, entered: asyncio.Event) -> None:
        nonlocal active_sessions, peak_sessions
        async with database.session() as session:
            await session.flush()
            active_sessions += 1
            peak_sessions = max(peak_sessions, active_sessions)
            entered.set()
            await (release_first.wait() if entered is first_entered else asyncio.sleep(0))
            active_sessions -= 1

    first = asyncio.create_task(hold_writer(primary, first_entered))
    await first_entered.wait()
    second = asyncio.create_task(hold_writer(history, second_entered))
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    assert peak_sessions == 1

    release_first.set()
    await asyncio.gather(first, second)
    assert peak_sessions == 1

    await history.close()
    await primary.close()


@pytest.mark.asyncio
async def test_file_backed_pools_have_explicit_bounded_capacity(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'bounded.sqlite'}"
    primary = Database(url)
    history = Database(url)
    await primary.init()
    await history.init()

    try:
        pool = primary._engine.sync_engine.pool
        assert pool.size() == _SQLITE_POOL_SIZE
        assert pool._max_overflow == _SQLITE_MAX_OVERFLOW
        assert pool._timeout == _SQLITE_POOL_TIMEOUT_SECONDS
        assert _sqlite_session_gate(url) is _sqlite_session_gate(url)
    finally:
        await history.close()
        await primary.close()


@pytest.mark.asyncio
async def test_shared_session_budget_applies_across_monitor_and_history_pools(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'session-budget.sqlite'}"
    primary = Database(url)
    history = Database(url)
    await primary.init()
    await history.init()

    entered = 0
    entered_all = asyncio.Event()
    release = asyncio.Event()

    async def hold_session():
        nonlocal entered
        async with primary.session() as session:
            await session.execute(text("SELECT 1"))
            entered += 1
            if entered == _SQLITE_POOL_SIZE:
                entered_all.set()
            await release.wait()

    tasks = [asyncio.create_task(hold_session()) for _ in range(_SQLITE_POOL_SIZE)]
    try:
        await asyncio.wait_for(entered_all.wait(), timeout=5)

        history_entered = asyncio.Event()

        async def attempt_history_session():
            async with history.session() as session:
                await session.execute(text("SELECT 1"))
                history_entered.set()

        history_task = asyncio.create_task(attempt_history_session())
        await asyncio.sleep(0.05)
        assert not history_entered.is_set()

        release.set()
        await asyncio.gather(*tasks)
        await asyncio.wait_for(history_task, timeout=5)
        assert history_entered.is_set()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await history.close()
        await primary.close()


def _snapshot(index: int) -> UnderdogSnapshotRecord:
    return UnderdogSnapshotRecord(
        external_id=f"writer-lock-{index}",
        player_name=f"Writer Lock Player {index}",
        team="TST",
        sport="NBA",
        stat_type="Points",
        line_value=10.5,
        game_id=f"writer-lock-game-{index}",
    )


def _start_external_writer_lock(db_path):
    """Hold a raw SQLite write transaction outside Database's async gate."""
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        connection = None
        transaction_started = False
        try:
            connection = sqlite3.connect(
                str(db_path),
                timeout=0,
                isolation_level=None,
            )
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            acquired.set()
            if not release.wait(timeout=5):
                errors.append(TimeoutError("external writer lock was not released"))
            connection.commit()
        except BaseException as exc:
            errors.append(exc)
            acquired.set()
        finally:
            if connection is not None:
                if transaction_started:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                connection.close()

    thread = threading.Thread(target=hold_lock, name="sqlite-external-writer")
    thread.start()
    return thread, acquired, release, errors


async def _set_sqlite_busy_timeout(database: Database, milliseconds: int) -> None:
    # The production timeout is intentionally generous. Lower it only for
    # these tests so the raw external lock produces a bounded lock error and
    # exercises the Python-level fresh-transaction retry.
    async with database._engine.begin() as connection:
        await connection.execute(text(f"PRAGMA busy_timeout={milliseconds}"))


async def _count_snapshots(database: Database) -> int:
    async with database.session() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(UnderdogSnapshotRecord)
            )
            or 0
        )


@pytest.mark.asyncio
async def test_file_backed_external_writer_lock_fails_loudly_and_preserves_rows(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "external-writer-recovery.sqlite"
    database = Database(f"sqlite+aiosqlite:///{db_path}")
    await database.init()
    await _set_sqlite_busy_timeout(database, 1)

    try:
        await database.save_underdog_snapshots_bulk([_snapshot(0)])
        thread, acquired, release, errors = _start_external_writer_lock(db_path)
        assert await asyncio.to_thread(acquired.wait, 2)
        assert not errors

        try:
            with pytest.raises(Exception, match="database is locked"):
                await database.save_underdog_snapshots_bulk(
                    [_snapshot(1), _snapshot(2)]
                )
        finally:
            release.set()
            await asyncio.to_thread(thread.join, 2)

        assert not thread.is_alive()
        assert not errors
        assert await _count_snapshots(database) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_file_backed_external_writer_lock_exhaustion_fails_loudly(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "external-writer-exhaustion.sqlite"
    database = Database(f"sqlite+aiosqlite:///{db_path}")
    await database.init()
    await _set_sqlite_busy_timeout(database, 1)

    thread = None
    release = None
    try:
        thread, acquired, release, errors = _start_external_writer_lock(db_path)
        assert await asyncio.to_thread(acquired.wait, 2)
        assert not errors

        monkeypatch.setattr("database._UNDERDOG_BULK_RETRY_DELAYS", (0, 0))
        with pytest.raises(OperationalError, match="database is locked"):
            await database.save_underdog_snapshots_bulk([_snapshot(1)])
        assert await _count_snapshots(database) == 0
    finally:
        if release is not None:
            release.set()
        if thread is not None:
            await asyncio.to_thread(thread.join, 2)
        await database.close()

    assert not thread.is_alive()
    assert not errors