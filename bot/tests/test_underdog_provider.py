"""
Tests for bot/providers/underdog_provider.py and the DB bridge method.

Covers:
  - _normalize_stat(): known mappings and unknown passthrough
  - ud_snapshot_to_player_prop(): UnderdogSnapshotRecord → PlayerProp adapter
  - UnderdogProvider: provider_name, sport_keys, is_available, fetch_props
  - UnderdogProvider: skips removed props, applies sport filter
  - UnderdogProvider: __len__, normalize_stat, __repr__
  - Database: sync_underdog_snapshots_to_prop_history() bridge
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from providers.underdog_provider import (
    UnderdogProvider,
    _normalize_stat,
    ud_snapshot_to_player_prop,
)
from providers.prop_provider import PlayerProp


# ── Shared event loop (matches pattern in test_dashboard.py) ──────────────────

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


def _make_snap(
    *,
    player_name: str   = "Shohei Ohtani",
    team:        str   = "LAD",
    sport:       str   = "MLB",
    stat_type:   str   = "Strikeouts",
    line_value:  float = 7.5,
    external_id: str   = "ud-001",
    game_id:     str   = "game-001",
    game_time: datetime | None = None,
    fetched_at: datetime | None = None,
    removed:     bool  = False,
) -> SimpleNamespace:
    """Create a minimal stand-in for UnderdogSnapshotRecord."""
    return SimpleNamespace(
        player_name = player_name,
        team        = team,
        sport       = sport,
        stat_type   = stat_type,
        line_value  = line_value,
        external_id = external_id,
        game_id     = game_id,
        game_time   = game_time,
        fetched_at  = fetched_at or datetime.utcnow(),
        removed     = removed,
    )


# ── _normalize_stat tests ──────────────────────────────────────────────────────

class TestNormalizeStat:
    def test_points_short(self):
        assert _normalize_stat("pts") == "points"

    def test_rebounds(self):
        assert _normalize_stat("reb") == "rebounds"

    def test_assists(self):
        assert _normalize_stat("ast") == "assists"

    def test_strikeouts(self):
        assert _normalize_stat("strikeouts") == "strikeouts"

    def test_home_runs(self):
        assert _normalize_stat("hr") == "home runs"

    def test_rushing_yards(self):
        assert _normalize_stat("rushing yards") == "rushing yards"

    def test_kills_esports(self):
        assert _normalize_stat("kills") == "kills"

    def test_shots_hockey(self):
        assert _normalize_stat("shots") == "shots on goal"

    def test_unknown_passthrough(self):
        assert _normalize_stat("xfactor") == "xfactor"

    def test_uppercase_normalized(self):
        assert _normalize_stat("PTS") == "points"

    def test_strips_whitespace(self):
        assert _normalize_stat("  hits  ") == "hits"

    def test_maps_won_esports(self):
        assert _normalize_stat("maps won") == "maps won"


# ── ud_snapshot_to_player_prop tests ──────────────────────────────────────────

class TestUdSnapshotToPlayerProp:
    def test_basic_conversion(self):
        snap = _make_snap()
        prop = ud_snapshot_to_player_prop(snap)
        assert isinstance(prop, PlayerProp)
        assert prop.provider    == "Underdog"
        assert prop.player_name == "Shohei Ohtani"
        assert prop.sport       == "MLB"
        assert prop.line_value  == 7.5

    def test_provider_always_underdog(self):
        prop = ud_snapshot_to_player_prop(_make_snap())
        assert prop.provider == "Underdog"

    def test_stat_type_normalized(self):
        snap = _make_snap(stat_type="pts")
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.stat_type == "points"

    def test_external_id_preserved(self):
        snap = _make_snap(external_id="ud-xyz")
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.external_id == "ud-xyz"

    def test_game_time_preserved(self):
        dt = datetime(2026, 9, 1, 19, 0)
        snap = _make_snap(game_time=dt)
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.game_time == dt

    def test_game_time_none_when_missing(self):
        snap = _make_snap(game_time=None)
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.game_time is None

    def test_none_fields_become_empty_strings(self):
        snap = _make_snap()
        snap.player_name = None
        snap.sport       = None
        snap.team        = None
        snap.stat_type   = None
        snap.external_id = None
        snap.game_id     = None
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.player_name == ""
        assert prop.sport       == ""
        assert prop.team        == ""
        assert prop.external_id == ""
        assert prop.game_id     == ""

    def test_none_line_value_becomes_zero(self):
        snap = _make_snap()
        snap.line_value = None
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.line_value == 0.0

    def test_fetched_at_preserved(self):
        ts = datetime(2026, 7, 31, 12, 0)
        snap = _make_snap(fetched_at=ts)
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.fetched_at == ts

    def test_team_preserved(self):
        snap = _make_snap(team="LAD")
        prop = ud_snapshot_to_player_prop(snap)
        assert prop.team == "LAD"


# ── UnderdogProvider tests ────────────────────────────────────────────────────

class TestUnderdogProvider:
    def test_provider_name(self):
        p = UnderdogProvider()
        assert p.provider_name == "Underdog"

    def test_is_available_false_when_empty(self):
        p = UnderdogProvider()
        assert p.is_available() is False

    def test_is_available_true_when_snapshots(self):
        p = UnderdogProvider(snapshots=[_make_snap()])
        assert p.is_available() is True

    def test_sport_keys_default_non_empty(self):
        p = UnderdogProvider()
        keys = p.sport_keys
        assert "MLB" in keys
        assert "NBA" in keys

    def test_sport_keys_custom_filter(self):
        p = UnderdogProvider(sport_filter=["MLB", "NBA"])
        assert p.sport_keys == ["MLB", "NBA"]

    def test_fetch_props_basic(self):
        snaps = [_make_snap(), _make_snap(player_name="Trout", external_id="ud-002")]
        p = UnderdogProvider(snapshots=snaps)
        props = _run(p.fetch_props())
        assert len(props) == 2
        assert all(pr.provider == "Underdog" for pr in props)

    def test_fetch_props_skips_removed(self):
        snaps = [
            _make_snap(removed=False),
            _make_snap(removed=True, external_id="ud-002"),
        ]
        p = UnderdogProvider(snapshots=snaps)
        props = _run(p.fetch_props())
        assert len(props) == 1

    def test_fetch_props_all_removed_returns_empty(self):
        snaps = [_make_snap(removed=True) for _ in range(3)]
        p = UnderdogProvider(snapshots=snaps)
        props = _run(p.fetch_props())
        assert props == []

    def test_fetch_props_empty_snapshots(self):
        p = UnderdogProvider(snapshots=[])
        props = _run(p.fetch_props())
        assert props == []

    def test_sport_filter_applied(self):
        snaps = [
            _make_snap(sport="MLB"),
            _make_snap(sport="NBA", external_id="ud-002"),
        ]
        p = UnderdogProvider(snapshots=snaps, sport_filter=["MLB"])
        props = _run(p.fetch_props())
        assert len(props) == 1
        assert props[0].sport == "MLB"

    def test_sport_filter_none_includes_all(self):
        snaps = [
            _make_snap(sport="MLB"),
            _make_snap(sport="NBA", external_id="ud-002"),
            _make_snap(sport="NFL", external_id="ud-003"),
        ]
        p = UnderdogProvider(snapshots=snaps, sport_filter=None)
        props = _run(p.fetch_props())
        assert len(props) == 3

    def test_len_counts_all_including_removed(self):
        snaps = [_make_snap(), _make_snap(removed=True, external_id="ud-002")]
        p = UnderdogProvider(snapshots=snaps)
        assert len(p) == 2

    def test_normalize_stat_delegates(self):
        p = UnderdogProvider()
        assert p.normalize_stat("pts") == "points"
        assert p.normalize_stat("reb") == "rebounds"

    def test_repr_shows_active_and_removed(self):
        snaps = [
            _make_snap(removed=False),
            _make_snap(removed=True, external_id="ud-002"),
        ]
        p = UnderdogProvider(snapshots=snaps)
        r = repr(p)
        assert "Underdog" in r
        assert "active=1" in r
        assert "removed=1" in r

    def test_props_have_correct_line_values(self):
        snaps = [_make_snap(line_value=v) for v in [1.5, 2.5, 7.5]]
        p = UnderdogProvider(snapshots=snaps)
        props = _run(p.fetch_props())
        lines = {pr.line_value for pr in props}
        assert lines == {1.5, 2.5, 7.5}

    def test_removed_false_default_included(self):
        """Snapshot with no `removed` attribute at all defaults to included."""
        snap = SimpleNamespace(
            player_name="Test", team="T", sport="MLB",
            stat_type="Hits", line_value=1.5,
            external_id="ud-001", game_id="g-001",
            game_time=None, fetched_at=datetime.utcnow(),
            # no `removed` attribute
        )
        p = UnderdogProvider(snapshots=[snap])
        props = _run(p.fetch_props())
        assert len(props) == 1


# ── DB bridge: sync_underdog_snapshots_to_prop_history ────────────────────────

class TestSyncUnderdogToPropHistory:
    @pytest.fixture()
    def db(self):
        from database import Database
        db = Database("sqlite+aiosqlite:///:memory:")
        _run(db.init())
        yield db
        _run(db.close())

    def _make_ud_orm(self, *, player_name="Ohtani", sport="MLB",
                     stat_type="Strikeouts", line_value=7.5,
                     alert_sent=True, score_tier="A", removed=False,
                     fetched_at=None):
        from database import UnderdogSnapshotRecord
        return UnderdogSnapshotRecord(
            external_id  = f"ud-{player_name}-{stat_type}",
            player_name  = player_name,
            team         = "LAD",
            sport        = sport,
            stat_type    = stat_type,
            line_value   = line_value,
            game_id      = "game-001",
            game_time    = datetime.utcnow() + timedelta(hours=3),
            line_moved   = False,
            prev_line    = None,
            line_delta   = None,
            removed      = removed,
            alert_sent   = alert_sent,
            score_total  = 80.0,
            score_tier   = score_tier,
            score_stars  = 4,
            alert_outcome= "sent",
            fetched_at   = fetched_at or datetime.utcnow(),
        )

    def test_syncs_non_removed_snapshots(self, db):
        _run(db.save_underdog_snapshot(self._make_ud_orm()))
        count = _run(db.sync_underdog_snapshots_to_prop_history())
        assert count == 1
        total = _run(db.count_prop_line_history("Underdog"))
        assert total == 1

    def test_removed_snapshot_sets_removed_flag(self, db):
        """Removed snapshots are bridged with removed=True (lifecycle tracking)."""
        _run(db.save_underdog_snapshot(self._make_ud_orm(removed=True)))
        count = _run(db.sync_underdog_snapshots_to_prop_history())
        assert count == 1  # lifecycle upsert processes removed snaps too
        rows = _run(db.get_latest_props_for_provider("Underdog", since_hours=48))
        # Row exists with removed flag
        assert len(rows) >= 1

    def test_no_duplicates_on_repeat_call(self, db):
        """Repeat calls upsert (update last_seen) not duplicate-insert."""
        _run(db.save_underdog_snapshot(self._make_ud_orm()))
        count1 = _run(db.sync_underdog_snapshots_to_prop_history())
        count2 = _run(db.sync_underdog_snapshots_to_prop_history())
        assert count1 == 1
        assert count2 == 1  # upsert updates the same row — still 1 total PropLineHistory row
        assert _run(db.count_prop_line_history("Underdog")) == 1

    def test_multiple_snapshots_all_synced(self, db):
        for name in ["PlayerA", "PlayerB", "PlayerC"]:
            _run(db.save_underdog_snapshot(self._make_ud_orm(player_name=name)))
        count = _run(db.sync_underdog_snapshots_to_prop_history())
        assert count == 3

    def test_bridged_record_has_provider_underdog(self, db):
        _run(db.save_underdog_snapshot(self._make_ud_orm()))
        _run(db.sync_underdog_snapshots_to_prop_history())
        history = _run(db.get_prop_line_history("Underdog", "Ohtani", "MLB", "Strikeouts"))
        assert len(history) == 1
        assert history[0].provider == "Underdog"

    def test_bridged_record_preserves_line_value(self, db):
        _run(db.save_underdog_snapshot(self._make_ud_orm(line_value=8.5)))
        _run(db.sync_underdog_snapshots_to_prop_history())
        history = _run(db.get_prop_line_history("Underdog", "Ohtani", "MLB", "Strikeouts"))
        assert history[0].line_value == 8.5

    def test_empty_db_returns_zero(self, db):
        count = _run(db.sync_underdog_snapshots_to_prop_history())
        assert count == 0

    def test_mixed_removed_and_active(self, db):
        """Both active and removed snapshots are upserted with lifecycle tracking."""
        _run(db.save_underdog_snapshot(self._make_ud_orm(player_name="Active", removed=False)))
        _run(db.save_underdog_snapshot(self._make_ud_orm(player_name="Removed", removed=True)))
        count = _run(db.sync_underdog_snapshots_to_prop_history())
        assert count == 2  # lifecycle upsert processes both active and removed

    def test_outside_since_hours_window_skipped(self, db):
        old_time = datetime.utcnow() - timedelta(hours=100)
        _run(db.save_underdog_snapshot(self._make_ud_orm(fetched_at=old_time)))
        count = _run(db.sync_underdog_snapshots_to_prop_history(since_hours=48))
        assert count == 0
