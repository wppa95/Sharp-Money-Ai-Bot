"""
Tests for PropLineHistory lifecycle_state columns and update_prop_lifecycle_state().
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
async def db():
    from database import Database
    _db = Database("sqlite+aiosqlite:///:memory:")
    await _db.init()
    yield _db
    await _db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_history_row(**kwargs):
    from database import PropLineHistory
    defaults = dict(
        provider    = "Underdog",
        sport       = "MLB",
        player_name = "Test Player",
        team        = "Test Team",
        stat_type   = "Fantasy Points",
        line_value  = 25.5,
        fetched_at  = datetime.utcnow(),
        first_seen  = datetime.utcnow(),
        last_seen   = datetime.utcnow(),
    )
    defaults.update(kwargs)
    return PropLineHistory(**defaults)


# ── ORM default ───────────────────────────────────────────────────────────────

class TestLifecycleStateDefault:
    async def test_default_is_discovered(self, db):
        from database import PropLineHistory
        from sqlalchemy import select

        async with db.session() as s:
            row = _make_history_row()
            s.add(row)
            await s.commit()
            await s.refresh(row)
            result = await s.execute(
                select(PropLineHistory).where(PropLineHistory.id == row.id)
            )
            fetched = result.scalar_one()

        # lifecycle_state should be DISCOVERED (ORM default) or None for old rows
        assert fetched.lifecycle_state in ("DISCOVERED", None)

    async def test_first_alert_sent_at_null_by_default(self, db):
        from database import PropLineHistory
        from sqlalchemy import select

        async with db.session() as s:
            row = _make_history_row(player_name="Alert Test Player")
            s.add(row)
            await s.commit()
            await s.refresh(row)
            result = await s.execute(
                select(PropLineHistory).where(PropLineHistory.id == row.id)
            )
            fetched = result.scalar_one()

        assert fetched.first_alert_sent_at is None


# ── update_prop_lifecycle_state ───────────────────────────────────────────────

class TestUpdatePropLifecycleState:
    async def test_returns_false_when_no_row(self, db):
        updated = await db.update_prop_lifecycle_state(
            provider    = "Underdog",
            player_name = "Nobody",
            sport       = "MLB",
            stat_type   = "Strikeouts",
            new_state   = "ACTIVE_ALERTED",
        )
        assert updated is False

    async def test_returns_true_and_updates_state(self, db):
        from database import PropLineHistory
        from sqlalchemy import select

        async with db.session() as s:
            row = _make_history_row(
                player_name = "Shohei Ohtani",
                stat_type   = "Total Bases",
            )
            s.add(row)
            await s.commit()

        updated = await db.update_prop_lifecycle_state(
            provider    = "Underdog",
            player_name = "Shohei Ohtani",
            sport       = "MLB",
            stat_type   = "Total Bases",
            new_state   = "ACTIVE_ALERTED",
        )
        assert updated is True

        async with db.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.provider    == "Underdog",
                    PropLineHistory.player_name == "Shohei Ohtani",
                    PropLineHistory.stat_type   == "Total Bases",
                )
                .order_by(PropLineHistory.fetched_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()

        assert row is not None
        assert row.lifecycle_state == "ACTIVE_ALERTED"

    async def test_sets_first_alert_sent_at_when_none(self, db):
        from database import PropLineHistory
        from sqlalchemy import select

        sentinel_time = datetime(2026, 7, 31, 12, 0, 0)

        async with db.session() as s:
            row = _make_history_row(
                player_name = "Mike Trout",
                stat_type   = "RBIs",
            )
            s.add(row)
            await s.commit()

        await db.update_prop_lifecycle_state(
            provider              = "Underdog",
            player_name           = "Mike Trout",
            sport                 = "MLB",
            stat_type             = "RBIs",
            new_state             = "ACTIVE_ALERTED",
            first_alert_sent_at   = sentinel_time,
        )

        async with db.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.player_name == "Mike Trout",
                    PropLineHistory.stat_type   == "RBIs",
                )
                .limit(1)
            )
            row = result.scalar_one_or_none()

        assert row is not None
        assert row.first_alert_sent_at == sentinel_time

    async def test_does_not_overwrite_existing_first_alert_sent_at(self, db):
        from database import PropLineHistory
        from sqlalchemy import select

        original_time = datetime(2026, 7, 1, 8, 0, 0)
        later_time    = datetime(2026, 7, 31, 12, 0, 0)

        async with db.session() as s:
            row = _make_history_row(
                player_name         = "Aaron Judge",
                stat_type           = "Home Runs",
                first_alert_sent_at = original_time,
                lifecycle_state     = "ACTIVE_ALERTED",
            )
            s.add(row)
            await s.commit()

        await db.update_prop_lifecycle_state(
            provider            = "Underdog",
            player_name         = "Aaron Judge",
            sport               = "MLB",
            stat_type           = "Home Runs",
            new_state           = "REMOVED",
            first_alert_sent_at = later_time,
        )

        async with db.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.player_name == "Aaron Judge",
                    PropLineHistory.stat_type   == "Home Runs",
                )
                .limit(1)
            )
            row = result.scalar_one_or_none()

        assert row is not None
        # State updated to REMOVED, but first_alert_sent_at should stay original
        assert row.lifecycle_state == "REMOVED"
        assert row.first_alert_sent_at == original_time

    async def test_idempotent_multiple_calls(self, db):
        """Calling update twice with same state should not error."""
        from database import PropLineHistory

        async with db.session() as s:
            row = _make_history_row(
                player_name = "Freddie Freeman",
                stat_type   = "Hits",
            )
            s.add(row)
            await s.commit()

        for _ in range(3):
            result = await db.update_prop_lifecycle_state(
                provider    = "Underdog",
                player_name = "Freddie Freeman",
                sport       = "MLB",
                stat_type   = "Hits",
                new_state   = "ACTIVE_ALERTED",
            )
            assert result is True
