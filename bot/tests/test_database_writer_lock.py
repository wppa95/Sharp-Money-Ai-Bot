"""Regression coverage for cross-pool SQLite session serialization."""

from __future__ import annotations

import asyncio

import pytest

from database import Database


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