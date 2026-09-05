"""Regression coverage for cross-pool SQLite session serialization."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from database import (
    Database,
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