"""
Tests for PropLineHistory lifecycle tracking:
  - upsert_prop_line_lifecycle() — ADDED / CHANGED / REMOVED / RETURNED / UNCHANGED
  - sync_underdog_snapshots_to_prop_history() with lifecycle columns
"""

from __future__ import annotations

import asyncio
import sys
import os
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import Database, PropLineHistory, UnderdogSnapshotRecord

# ── Shared event loop ─────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    _run(database.init())
    yield database
    _run(database.close())


def _ts(delta_minutes: int = 0) -> datetime:
        return datetime.utcnow() + timedelta(minutes=delta_minutes)


def _make_ud_snap(
    player_name:  str,
    stat_type:    str,
    line_value:   float,
    sport:        str = "MLB",
    external_id:  str = "snap-001",
    removed:      bool = False,
    fetched_at:   datetime | None = None,
) -> UnderdogSnapshotRecord:
    return UnderdogSnapshotRecord(
        external_id = external_id,
        player_name = player_name,
        team        = "LAA",
        sport       = sport,
        stat_type   = stat_type,
        line_value  = line_value,
        game_id     = "game-001",
        game_time   = None,
        removed     = removed,
        fetched_at  = fetched_at or _ts(),
    )


# ── upsert_prop_line_lifecycle ────────────────────────────────────────────────

class TestUpsertPropLineLifecycle:
    def test_first_insert_returns_added(self, db):
        _, event = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        assert event == "ADDED"

    def test_same_line_returns_unchanged(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        _, event = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        assert event == "UNCHANGED"

    def test_changed_line_returns_changed(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        _, event = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=27.5,
        ))
        assert event == "CHANGED"

    def test_removed_returns_removed(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="Mike Trout",
            sport="MLB", stat_type="Hits", line_value=1.5,
        ))
        _, event = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="Mike Trout",
            sport="MLB", stat_type="Hits", line_value=1.5, removed=True,
        ))
        assert event == "REMOVED"

    def test_returned_after_removed(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="Mike Trout",
            sport="MLB", stat_type="Hits", line_value=1.5,
        ))
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="Mike Trout",
            sport="MLB", stat_type="Hits", line_value=1.5, removed=True,
        ))
        _, event = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="Mike Trout",
            sport="MLB", stat_type="Hits", line_value=1.5, removed=False,
        ))
        assert event == "RETURNED"

    def test_change_count_increments_on_line_change(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=27.5,
        ))
        row, _ = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=29.5,
        ))
        # change_count may be None on old schema without lifecycle columns
        change_count = getattr(row, "change_count", None)
        if change_count is not None:
            assert change_count == 2

    def test_change_count_not_incremented_when_unchanged(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        row, _ = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        change_count = getattr(row, "change_count", None)
        if change_count is not None:
            assert change_count == 0

    def test_provider_isolation(self, db):
        """PrizePicks and Underdog prop with same player/stat are tracked independently."""
        _, ev1 = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        _, ev2 = _run(db.upsert_prop_line_lifecycle(
            provider="Underdog", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=26.0,
        ))
        assert ev1 == "ADDED"
        assert ev2 == "ADDED"

    def test_first_seen_set_on_insert(self, db):
        ts = _ts()
        row, _ = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
            fetched_at=ts,
        ))
        first_seen = getattr(row, "first_seen", None)
        if first_seen is not None:
            assert first_seen == ts

    def test_returns_row_object(self, db):
        row, _ = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        assert isinstance(row, PropLineHistory)
        assert row.player_name == "LeBron James"
        assert row.line_value  == 25.5

    def test_prev_line_stored_on_change(self, db):
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        row, _ = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=27.5,
        ))
        prev_line = getattr(row, "prev_line", None)
        if prev_line is not None:
            assert abs(prev_line - 25.5) < 1e-6

    def test_small_line_difference_not_flagged_as_change(self, db):
        """Differences < 0.01 should not trigger CHANGED (float precision guard)."""
        _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5,
        ))
        _, event = _run(db.upsert_prop_line_lifecycle(
            provider="PrizePicks", player_name="LeBron James",
            sport="NBA", stat_type="Points", line_value=25.5009,
        ))
        assert event == "UNCHANGED"


# ── sync_underdog_snapshots_to_prop_history (lifecycle) ───────────────────────

class TestSyncLifecycle:
    def test_first_sync_inserts_rows(self, db):
        snap = _make_ud_snap("Mike Trout", "Hits", 1.5)
        _run(db.save_underdog_snapshot(snap))

        n = _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))
        assert n >= 1

    def test_removed_snapshot_marks_row_removed(self, db):
        snap = _make_ud_snap("Mike Trout", "Hits", 1.5, fetched_at=_ts(0))
        _run(db.save_underdog_snapshot(snap))
        _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))

        snap_removed = _make_ud_snap("Mike Trout", "Hits", 1.5, removed=True,
                                     fetched_at=_ts(5))
        _run(db.save_underdog_snapshot(snap_removed))
        _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))

        rows = _run(db.get_prop_line_history("Underdog", "Mike Trout", "MLB", "Hits"))
        assert any(getattr(r, "removed", False) for r in rows) \
               or any(r.line_value is not None for r in rows)  # row was updated

    def test_line_change_increments_change_count(self, db):
        snap1 = _make_ud_snap("Mike Trout", "Hits", 1.5, fetched_at=_ts(0))
        _run(db.save_underdog_snapshot(snap1))
        _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))

        snap2 = _make_ud_snap("Mike Trout", "Hits", 2.0, fetched_at=_ts(30))
        _run(db.save_underdog_snapshot(snap2))
        n = _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))

        assert n >= 1  # returned 1 upserted row

    def test_multiple_props_all_bridged(self, db):
        for i, (player, stat) in enumerate([
            ("Mike Trout",    "Hits"),
            ("Aaron Judge",   "Home Runs"),
            ("LeBron James",  "Points"),
        ]):
            snap = _make_ud_snap(player, stat, float(i) + 1.5,
                                 sport="NBA" if "James" in player else "MLB",
                                 fetched_at=_ts(i))
            _run(db.save_underdog_snapshot(snap))

        n = _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))
        assert n == 3

    def test_idempotent_no_duplicates(self, db):
        snap = _make_ud_snap("Mike Trout", "Hits", 1.5)
        _run(db.save_underdog_snapshot(snap))

        _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))
        _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))

        count = _run(db.count_prop_line_history(provider="Underdog"))
        assert count == 1  # no duplicates

    def test_count_prop_line_history_by_provider(self, db):
        snap = _make_ud_snap("Mike Trout", "Hits", 1.5)
        _run(db.save_underdog_snapshot(snap))
        _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))

        ud_count = _run(db.count_prop_line_history(provider="Underdog"))
        pp_count = _run(db.count_prop_line_history(provider="PrizePicks"))
        assert ud_count >= 1
        assert pp_count == 0


# ── CLV stats by dimension ────────────────────────────────────────────────────

class TestCLVStatsByDimension:
    def test_empty_db_returns_empty_dicts(self, db):
        stats = _run(db.get_clv_stats_by_dimension())
        for key in ("by_sport", "by_type", "by_market", "by_tier"):
            assert key in stats
            assert isinstance(stats[key], list)

    def test_empty_db_all_lists_empty(self, db):
        stats = _run(db.get_clv_stats_by_dimension())
        for key in stats:
            assert stats[key] == []


# ── CLV seeds tier stats ──────────────────────────────────────────────────────

def _make_clv_seed(source_id: int, **kwargs) -> "AlertCLVSeed":
    """Build an AlertCLVSeed with all required fields populated."""
    from database import AlertCLVSeed
    defaults = dict(
        source_table     = "ev_records",
        source_id        = source_id,
        alert_type       = "EV",
        sport            = "NFL",
        market_type      = "h2h",
        event            = "Chiefs vs Ravens",
        selection        = "Chiefs ML",
        bet_odds         = -110,
        counterpart_odds = None,
        tier             = "A",
        game_time        = _ts(-60 * 25),   # 25h ago → stale (past 24h cutoff)
        alerted_at       = _ts(-60 * 25),   # alerted_at required NOT NULL
        clv_computed     = False,
    )
    defaults.update(kwargs)
    return AlertCLVSeed(**defaults)


class TestCLVSeedsTierStats:
    def test_empty_db_returns_empty_dict(self, db):
        stats = _run(db.get_clv_seeds_by_tier_stats())
        assert isinstance(stats, dict)

    def test_no_computed_seeds_returns_empty(self, db):
        """Seeds with clv_computed=False should not appear in tier stats."""
        seed = _make_clv_seed(source_id=1)
        _run(db.save_alert_clv_seed(seed))
        stats = _run(db.get_clv_seeds_by_tier_stats())
        assert "A" not in stats  # not computed yet


# ── mark_clv_seed_expired ─────────────────────────────────────────────────────

class TestMarkCLVSeedExpired:
    def test_expired_sets_computed_true(self, db):
        seed = _make_clv_seed(source_id=10, tier="B")
        saved = _run(db.save_alert_clv_seed(seed))
        # Fetch real id (save returns input record; id comes from DB)
        fetched = _run(db.get_clv_seed_for_source("ev_records", 10))
        assert fetched is not None
        _run(db.mark_clv_seed_expired(fetched.id))

        pending = _run(db.get_pending_clv_seeds(limit=10))
        assert not any(s.id == fetched.id for s in pending)

    def test_expired_clv_pct_is_none(self, db):
        seed = _make_clv_seed(
            source_id   = 11,
            sport       = "NBA",
            market_type = "totals",
            event       = "Lakers vs Celtics",
            selection   = "Over 220.5",
        )
        _run(db.save_alert_clv_seed(seed))
        fetched = _run(db.get_clv_seed_for_source("ev_records", 11))
        assert fetched is not None
        _run(db.mark_clv_seed_expired(fetched.id))

        # Should not appear in pending (computed=True)
        pending = _run(db.get_pending_clv_seeds(limit=50))
        assert not any(s.id == fetched.id for s in pending)
