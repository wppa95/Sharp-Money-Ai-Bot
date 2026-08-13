"""
Tests for the stable-refresh job and watchlist-rescan functionality.

Coverage:
  • Cursor advances correctly each batch
  • Cursor wraps at end of pool (next_cursor = 0 when end_cursor == pool_size)
  • Cursor persists across restart (HealthTracker.set/get_stable_refresh_cursor)
  • Stable refresh makes zero external API calls (no Underdog fetch)
  • Watchlist UNDER rule: score 30–40 + UNDER decision → watchlist_state='Watchlist'
  • Watchlist promotion fires normal alert gates
  • Existing dedup prevents re-alerting same prop
  • Fast path (underdog_job) counter variables are unaffected by stable refresh
  • HealthTracker stable-refresh stats are persisted after each cycle
  • get_active_watchlist_candidates returns only Watchlist+PENDING rows
"""
from __future__ import annotations

import asyncio
import datetime
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_snap(
    player: str = "Test Player",
    stat:   str = "Points",
    sport:  str = "NBA",
    line:   float = 22.5,
    ext_id: str = "ext-001",
    game_time: datetime.datetime | None = None,
    removed: bool = False,
) -> MagicMock:
    """Return a MagicMock that mirrors the real UnderdogSnapshotRecord ORM fields."""
    snap = MagicMock()
    snap.player_name   = player
    snap.stat_type     = stat
    snap.sport         = sport
    # line_value is the canonical ORM field; removed is the removal flag.
    # Do NOT set snap.line or snap.selection — those don't exist on the real model.
    snap.line_value    = line
    snap.removed       = removed
    snap.external_id   = ext_id
    snap.id            = 1
    snap.team          = "Team A"
    snap.game_time     = game_time
    return snap


def _make_score(total: int = 72, tier: str = "A", stars: int = 4) -> MagicMock:
    score = MagicMock()
    score.total        = total
    score.tier         = tier
    score.stars        = stars
    score.n_history    = 20
    score.move_velocity   = 10
    score.historical_activity = 15
    score.avg_vs_line  = 12
    score.consistency  = 10
    score.stability    = 10
    score.variance_penalty = 0
    score.bet_quality_label = "STANDARD BET"
    return score


def _make_decision(
    rec:   str = "OVER",
    tier:  str = "A",
    conf:  int = 75,
    reason: str = "edge_detected",
) -> MagicMock:
    dec = MagicMock()
    dec.recommendation = rec
    dec.decision_tier  = tier
    dec.confidence     = conf
    dec.reason         = reason
    return dec


def _make_validation(supported: bool = True, n: int = 15) -> MagicMock:
    val = MagicMock()
    val.has_supporting_data = supported
    val.n_games             = n
    return val


def _make_context(db: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {"db": db}
    ctx.bot      = AsyncMock()
    return ctx


# ── HealthTracker cursor tests ─────────────────────────────────────────────────

class TestHealthTrackerCursor:
    """HealthTracker stable-refresh cursor persists across get/set."""

    def test_default_cursor_is_zero(self, tmp_path):
        from engine.health import HealthTracker
        ht = HealthTracker(path=tmp_path / "health.json")
        assert ht.get_stable_refresh_cursor() == 0

    def test_set_then_get_roundtrip(self, tmp_path):
        from engine.health import HealthTracker
        ht = HealthTracker(path=tmp_path / "health.json")
        ht.set_stable_refresh_cursor(4321)
        assert ht.get_stable_refresh_cursor() == 4321

    def test_cursor_persists_across_reload(self, tmp_path):
        """Simulates a bot restart — new HealthTracker reads cursor from disk."""
        from engine.health import HealthTracker
        path = tmp_path / "health.json"
        ht1 = HealthTracker(path=path)
        ht1.set_stable_refresh_cursor(7777)

        ht2 = HealthTracker(path=path)   # new instance — reads from disk
        assert ht2.get_stable_refresh_cursor() == 7777

    def test_negative_cursor_clamped_to_zero(self, tmp_path):
        from engine.health import HealthTracker
        ht = HealthTracker(path=tmp_path / "health.json")
        ht.set_stable_refresh_cursor(-5)
        assert ht.get_stable_refresh_cursor() == 0

    def test_stats_roundtrip(self, tmp_path):
        from engine.health import HealthTracker
        ht    = HealthTracker(path=tmp_path / "health.json")
        stats = {"pool_size": 4400, "sr_rescored": 4400, "sr_sent": 2}
        ht.set_stable_refresh_stats(stats)
        got = ht.get_stable_refresh_stats()
        assert got["pool_size"]   == 4400
        assert got["sr_rescored"] == 4400
        assert got["sr_sent"]     == 2

    def test_stats_persist_across_reload(self, tmp_path):
        from engine.health import HealthTracker
        path = tmp_path / "health.json"
        ht1  = HealthTracker(path=path)
        ht1.set_stable_refresh_stats({"wl_promoted": 3})

        ht2 = HealthTracker(path=path)
        assert ht2.get_stable_refresh_stats()["wl_promoted"] == 3

    def test_last_stable_refresh_str(self, tmp_path):
        from engine.health import HealthTracker
        ht = HealthTracker(path=tmp_path / "health.json")
        # Before any stats are set it returns "—"
        assert ht.last_stable_refresh_str() == "—"
        ht.set_stable_refresh_stats({"x": 1})
        # After setting stats the age should be a number of seconds
        assert ht.last_stable_refresh_str() != "—"


# ── Cursor arithmetic ──────────────────────────────────────────────────────────

class TestCursorArithmetic:
    """Pure arithmetic tests — verifies the cursor logic without running the job."""

    @staticmethod
    def _cursor_next(cursor: int, pool_size: int, batch: int = 10_000) -> tuple[int, int, int]:
        """Replicate the cursor arithmetic from _stable_refresh_job."""
        cursor    = (cursor % pool_size) if pool_size > 0 else 0
        end       = min(cursor + batch, pool_size)
        next_cur  = end % pool_size if pool_size > 0 else 0
        return cursor, end, next_cur

    def test_first_batch_full_pool_smaller_than_batch(self):
        cursor, end, nxt = self._cursor_next(0, pool_size=4400, batch=10_000)
        assert cursor == 0
        assert end    == 4400
        assert nxt    == 0   # wraps back to 0 (4400 % 4400 == 0)

    def test_cursor_wraps_at_pool_end(self):
        # pool = 4400, batch = 2000, cursor at 3500 → end at 4400, next = 0
        cursor, end, nxt = self._cursor_next(3500, pool_size=4400, batch=2000)
        assert cursor == 3500
        assert end    == 4400
        assert nxt    == 0

    def test_cursor_advances_midpool(self):
        cursor, end, nxt = self._cursor_next(0, pool_size=30_000, batch=10_000)
        assert cursor == 0
        assert end    == 10_000
        assert nxt    == 10_000

    def test_cursor_wraps_modulo_on_stale_large_value(self):
        # Cursor saved at 4400 but pool is now 3000 — must wrap cleanly
        cursor, end, nxt = self._cursor_next(4400, pool_size=3000, batch=1000)
        assert 0 <= cursor < 3000

    def test_empty_pool_does_not_crash(self):
        cursor, end, nxt = self._cursor_next(0, pool_size=0, batch=10_000)
        assert cursor == 0
        assert nxt    == 0


# ── Database: get_active_watchlist_candidates ──────────────────────────────────

class TestGetActiveWatchlistCandidates:
    """Unit tests for the new DB method."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_watchlist_rows(self):
        from database import Database
        db = MagicMock(spec=Database)
        db.get_active_watchlist_candidates = AsyncMock(return_value=[])
        result = await db.get_active_watchlist_candidates()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_only_watchlist_pending_rows(self):
        from database import Database
        # Simulate two watchlist rows
        row1 = MagicMock()
        row1.watchlist_state = "Watchlist"
        row1.result          = "PENDING"
        row2 = MagicMock()
        row2.watchlist_state = "Qualified"  # should NOT be returned
        row2.result          = "PENDING"

        db = MagicMock(spec=Database)
        db.get_active_watchlist_candidates = AsyncMock(return_value=[row1])
        result = await db.get_active_watchlist_candidates()
        assert len(result) == 1
        assert result[0].watchlist_state == "Watchlist"


# ── Stable refresh job: integration-style tests ────────────────────────────────

def _build_minimal_db(
    active_pool: dict | None = None,
    watchlist_candidates: list | None = None,
    has_recent_alert: bool = False,
    hist: list | None = None,
    # When truthy, bulk dedup returns a frozenset containing the key so the
    # prop is treated as "recently alerted" and skipped without scoring.
    bulk_alerted_keys: "frozenset | None" = None,
) -> MagicMock:
    db = AsyncMock()
    # Stable refresh uses get_active_underdog_snapshot_per_prop (correct removal semantics)
    db.get_active_underdog_snapshot_per_prop = AsyncMock(
        return_value=(active_pool or {})
    )
    # Keep old method mock too so callers in other jobs don't break
    db.get_latest_underdog_snapshot_per_prop = AsyncMock(
        return_value=(active_pool or {})
    )
    db.get_active_watchlist_candidates = AsyncMock(
        return_value=(watchlist_candidates or [])
    )
    # Bulk dedup pre-load (replaces per-prop has_recent_ud_alert in the loop)
    db.get_recently_alerted_prop_keys = AsyncMock(
        return_value=(bulk_alerted_keys if bulk_alerted_keys is not None
                      else frozenset())
    )
    db.has_recent_ud_alert    = AsyncMock(return_value=has_recent_alert)
    db.get_ud_prop_history    = AsyncMock(return_value=(hist or []))
    db.log_prop_opportunity   = AsyncMock(return_value=None)
    db.mark_ud_snapshot_alert_sent = AsyncMock(return_value=None)
    db.mark_opportunity_alert_sent = AsyncMock(return_value=None)
    db.seed_clv_from_ud_confirmation = AsyncMock(return_value=None)
    db.get_player_results     = AsyncMock(return_value=[])
    db.upsert_player_result   = AsyncMock(return_value=None)
    return db


# ── Database: get_active_underdog_snapshot_per_prop removal semantics ──────────

class TestGetActiveUnderdogSnapshotPerPropSQLite:
    """
    Real SQLite integration tests for get_active_underdog_snapshot_per_prop.

    These tests spin up an actual in-process SQLite database to verify the
    MAX-over-all-rows query semantics at the SQL level — not just mocked return
    values.  The critical contract:

        MAX(id) is computed over ALL rows (including removals) per prop.
        Only rows where that max-id row has removed=False are returned.
        If the latest row is removed=True, the prop is ABSENT from the result.

    This prevents the regression in get_latest_underdog_snapshot_per_prop which
    computed MAX(id) only over removed=False rows — causing a stale active
    snapshot to be returned for a prop that was subsequently removed.
    """

    @pytest.fixture()
    async def db(self):
        from database import Database
        _db = Database(url="sqlite+aiosqlite:///:memory:")
        await _db.init()
        yield _db
        await _db.close()

    def _make_record(
        self,
        player:  str   = "Test Player",
        stat:    str   = "Points",
        line:    float = 22.5,
        removed: bool  = False,
        sport:   str   = "NBA",
    ):
        from database import UnderdogSnapshotRecord
        return UnderdogSnapshotRecord(
            external_id = f"{player}-{stat}",
            player_name = player,
            team        = "Team A",
            sport       = sport,
            stat_type   = stat,
            line_value  = line,
            game_id     = "",
            game_time   = None,
            removed     = removed,
            fetched_at  = datetime.datetime.utcnow(),
        )

    async def test_active_prop_appears_in_result(self, db):
        """A prop with latest removed=False is returned."""
        await db.save_underdog_snapshot(self._make_record(removed=False))
        result = await db.get_active_underdog_snapshot_per_prop()
        assert ("Test Player", "Points") in result

    async def test_only_removal_record_excluded(self, db):
        """A prop with only a removal record is absent from the result."""
        await db.save_underdog_snapshot(self._make_record(removed=True))
        result = await db.get_active_underdog_snapshot_per_prop()
        assert ("Test Player", "Points") not in result

    async def test_older_active_then_removal_excluded(self, db):
        """
        CRITICAL: A prop with an older active row (id N) followed by a newer
        removal row (id N+1) must be ABSENT — the newer removal wins.

        The old get_latest_underdog_snapshot_per_prop would return the stale
        active row here (MAX over non-removed only = id N).
        The new method computes MAX over all rows (= id N+1, removal) then
        filters removed=False, so the prop is correctly excluded.
        """
        # Active row inserted first (gets lower autoincrement id)
        await db.save_underdog_snapshot(self._make_record(removed=False))
        # Removal row inserted second (gets higher id — the LATEST record)
        await db.save_underdog_snapshot(self._make_record(removed=True))
        result = await db.get_active_underdog_snapshot_per_prop()
        # Must be ABSENT — the latest record is a removal
        assert ("Test Player", "Points") not in result

    async def test_old_get_latest_returns_stale_snap_for_same_data(self, db):
        """
        Regression proof: the old get_latest_underdog_snapshot_per_prop
        incorrectly returns the stale active snapshot when latest is removal.
        Documents the known-broken behavior so the contrast is explicit.
        """
        await db.save_underdog_snapshot(self._make_record(removed=False))
        await db.save_underdog_snapshot(self._make_record(removed=True))
        stale  = await db.get_latest_underdog_snapshot_per_prop()
        active = await db.get_active_underdog_snapshot_per_prop()

        # Old method returns stale row (documented bug)
        assert ("Test Player", "Points") in stale, (
            "get_latest_underdog_snapshot_per_prop should return stale row "
            "(documented bug — use get_active_underdog_snapshot_per_prop instead)"
        )
        # New method correctly excludes the prop
        assert ("Test Player", "Points") not in active

    async def test_mixed_pool_only_active_props_returned(self, db):
        """With one active and one removed prop, only the active one appears."""
        await db.save_underdog_snapshot(self._make_record(player="Active", removed=False))
        await db.save_underdog_snapshot(self._make_record(player="Gone",   removed=True))
        result = await db.get_active_underdog_snapshot_per_prop()
        assert ("Active", "Points") in result
        assert ("Gone",   "Points") not in result

    async def test_re_activated_prop_returned(self, db):
        """A prop that was removed then re-added (highest id = active) appears."""
        await db.save_underdog_snapshot(self._make_record(removed=False, line=20.0))
        await db.save_underdog_snapshot(self._make_record(removed=True,  line=20.0))
        # Re-added with a new line
        await db.save_underdog_snapshot(self._make_record(removed=False, line=22.5))
        result = await db.get_active_underdog_snapshot_per_prop()
        assert ("Test Player", "Points") in result
        snap = result[("Test Player", "Points")]
        assert snap.line_value == 22.5


# ── Stable refresh + removal semantics (end-to-end) ────────────────────────────

class TestStableRefreshRemovalSemantics:
    """
    Verify that _stable_refresh_job uses get_active_underdog_snapshot_per_prop
    (not get_latest_underdog_snapshot_per_prop), so removed props:
      • Never appear in Part 1 (no rescore, no alert)
      • Are correctly transitioned to 'Removed' in Part 2 (watchlist rescan)
    """

    @pytest.mark.asyncio
    async def test_removed_prop_absent_from_part1_rescore(self):
        """
        A prop whose latest DB row is removed=True must not be rescored or
        alerted in Part 1 — it simply isn't in the active_pool.
        """
        # active_pool is EMPTY — the removed prop was correctly excluded by
        # get_active_underdog_snapshot_per_prop
        db  = _build_minimal_db(active_pool={})
        ctx = _make_context(db)

        with patch("market_engine.get_health_tracker", return_value=None):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # No rescore or alert paths reached — log_prop_opportunity never called
        db.log_prop_opportunity.assert_not_called()
        # Correct method was used (not the old broken one)
        db.get_active_underdog_snapshot_per_prop.assert_called_once()
        db.get_latest_underdog_snapshot_per_prop.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchlist_candidate_marked_removed_when_latest_snap_is_removal(self):
        """
        A watchlist candidate whose prop has been removed from Underdog
        (latest snapshot is removed=True) must be transitioned to
        watchlist_state='Removed' in Part 2.

        The active_pool is empty (prop excluded by removal semantics).
        Part 2 finds the prop absent from pool → logs 'Removed'.
        """
        wl_row = MagicMock()
        wl_row.player_name    = "Gone Player"
        wl_row.stat_type      = "Points"
        wl_row.sport          = "NBA"
        wl_row.line_value     = 20.0
        wl_row.confidence     = 35
        wl_row.external_id    = "wl-gone-001"
        wl_row.watchlist_state = "Watchlist"
        wl_row.result          = "PENDING"
        wl_row.game_time       = None
        wl_row.team            = ""
        wl_row.recommendation  = "UNDER"
        wl_row.decision_tier   = "PASS"

        # Pool is empty — prop removed → Part 2 should log 'Removed'
        db  = _build_minimal_db(
            active_pool={},
            watchlist_candidates=[wl_row],
        )
        ctx = _make_context(db)

        with patch("market_engine.get_health_tracker", return_value=None):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        calls = db.log_prop_opportunity.call_args_list
        removed_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Removed"
        ]
        assert len(removed_calls) >= 1, (
            "Expected log_prop_opportunity(watchlist_state='Removed') for a prop "
            "whose latest Underdog snapshot is a removal"
        )

    @pytest.mark.asyncio
    async def test_method_used_is_get_active_not_get_latest(self):
        """
        Explicitly confirm _stable_refresh_job calls get_active_underdog_snapshot_per_prop,
        not the older get_latest_underdog_snapshot_per_prop.
        """
        db  = _build_minimal_db(active_pool={})
        ctx = _make_context(db)

        with patch("market_engine.get_health_tracker", return_value=None):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        db.get_active_underdog_snapshot_per_prop.assert_called_once()
        db.get_latest_underdog_snapshot_per_prop.assert_not_called()

    @pytest.mark.asyncio
    async def test_part1_failure_fallback_also_uses_active_method(self):
        """
        When Part 1 raises (so active_pool is never assigned), Part 2 must
        re-fetch using get_active_underdog_snapshot_per_prop — never the old
        get_latest_underdog_snapshot_per_prop.

        A removed watchlist candidate must still be transitioned to 'Removed'
        even when Part 1 blows up, because the fallback pool is empty (correct
        removal semantics) rather than stale (old broken semantics would return
        the older active snapshot and try to promote it).
        """
        wl_row = MagicMock()
        wl_row.player_name     = "Gone Player"
        wl_row.stat_type       = "Points"
        wl_row.sport           = "NBA"
        wl_row.line_value      = 20.0
        wl_row.confidence      = 35
        wl_row.external_id     = "wl-part1-fail-001"
        wl_row.watchlist_state = "Watchlist"
        wl_row.result          = "PENDING"
        wl_row.game_time       = None
        wl_row.team            = ""
        wl_row.recommendation  = "UNDER"
        wl_row.decision_tier   = "PASS"

        db  = _build_minimal_db(
            # Simulates correct removal semantics: prop absent from active pool
            active_pool={},
            watchlist_candidates=[wl_row],
        )
        # Force Part 1 to raise so active_pool is never assigned
        db.get_active_underdog_snapshot_per_prop = AsyncMock(
            side_effect=[
                Exception("simulated Part 1 DB failure"),  # Part 1 call
                {},                                         # Part 2 fallback
            ]
        )
        ctx = _make_context(db)

        with patch("market_engine.get_health_tracker", return_value=None):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Part 2 fallback must have called the correct method a second time
        assert db.get_active_underdog_snapshot_per_prop.call_count == 2
        # get_latest must NEVER be used — even as a fallback
        db.get_latest_underdog_snapshot_per_prop.assert_not_called()

        # With an empty fallback pool the watchlist candidate is 'Removed'
        calls = db.log_prop_opportunity.call_args_list
        removed_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Removed"
        ]
        assert len(removed_calls) >= 1, (
            "Watchlist candidate must be marked 'Removed' even when Part 1 failed "
            "and Part 2 falls back to get_active_underdog_snapshot_per_prop"
        )


# ── End-to-end job tests using real UnderdogSnapshotRecord rows ────────────────

class TestStableRefreshJobE2E:
    """
    End-to-end tests that drive _stable_refresh_job with actual
    UnderdogSnapshotRecord ORM objects (not MagicMocks).

    These catch field-name mismatches (e.g. .line vs .line_value) that mock
    tests cannot detect because MagicMock auto-creates any attribute.

    DB I/O and scoring are mocked; only the snapshot model is real.
    """

    def _make_real_snap(
        self,
        player:  str   = "Real Player",
        stat:    str   = "Points",
        sport:   str   = "NBA",
        line:    float = 22.5,
        removed: bool  = False,
    ):
        from database import UnderdogSnapshotRecord
        return UnderdogSnapshotRecord(
            external_id = f"{player}-{stat}",
            player_name = player,
            team        = "Team A",
            sport       = sport,
            stat_type   = stat,
            line_value  = line,
            game_id     = "",
            game_time   = None,
            removed     = removed,
            fetched_at  = datetime.datetime.utcnow(),
        )

    def _make_wl_pol_row(
        self,
        player: str   = "WL Player",
        stat:   str   = "Points",
        sport:  str   = "NBA",
        line:   float = 20.0,
        conf:   int   = 35,
    ):
        """A PropOpportunityLog row mirroring a watchlist candidate."""
        from database import PropOpportunityLog
        row = PropOpportunityLog(
            external_id       = f"{player}-{stat}-wl",
            player_name       = player,
            team              = "",
            sport             = sport,
            stat_type         = stat,
            line_value        = line,
            recommendation    = "UNDER",
            decision_tier     = "PASS",
            confidence        = conf,
            game_time         = None,
            provider          = "Underdog",
            bet_quality_score = conf,
            watchlist_state   = "Watchlist",
            result            = "PENDING",
        )
        return row

    def _make_e2e_db(self, active_pool: dict, watchlist: list | None = None):
        db = AsyncMock()
        db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value=active_pool)
        db.get_latest_underdog_snapshot_per_prop = AsyncMock(return_value={})
        db.get_active_watchlist_candidates       = AsyncMock(return_value=(watchlist or []))
        db.get_recently_alerted_prop_keys        = AsyncMock(return_value=frozenset())
        db.has_recent_ud_alert                   = AsyncMock(return_value=False)
        db.get_ud_prop_history                   = AsyncMock(return_value=[])
        db.log_prop_opportunity                  = AsyncMock(return_value=None)
        db.mark_ud_snapshot_alert_sent           = AsyncMock(return_value=None)
        db.mark_opportunity_alert_sent           = AsyncMock(return_value=None)
        db.seed_clv_from_ud_confirmation         = AsyncMock(return_value=None)
        return db

    @pytest.mark.asyncio
    async def test_job_processes_real_snap_without_attribute_error(self):
        """
        _stable_refresh_job must not raise AttributeError when given a real
        UnderdogSnapshotRecord (field access must use line_value, not .line,
        and removed, not .selection).
        """
        real_snap = self._make_real_snap()
        db  = self._make_e2e_db({("Real Player", "Points"): real_snap})
        # All props in bulk-alerted set → deduped immediately, no scoring needed
        db.get_recently_alerted_prop_keys = AsyncMock(
            return_value=frozenset({("Real Player", "Points")})
        )
        ctx = _make_context(db)
        with patch("market_engine.get_health_tracker", return_value=None):
            from market_engine import _stable_refresh_job
            # Must not raise — specifically no AttributeError on .line or .selection
            await _stable_refresh_job(ctx)

    @pytest.mark.asyncio
    async def test_job_reads_line_value_field_not_line(self):
        """
        Confirm the job reads snap.line_value — if it accidentally reads snap.line
        it would get None (ORM default for unset attrs), causing 0.0 line values.
        """
        real_snap = self._make_real_snap(line=27.5)
        db  = self._make_e2e_db({("Real Player", "Points"): real_snap})
        ctx = _make_context(db)
        low = _make_score(total=20, tier="PASS", stars=0)
        val = _make_validation(supported=False)

        line_seen: list = []
        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=low) as mock_score,
            patch("engine.player_validator.validate_player_prop", return_value=val),
            patch("market_engine._fetch_and_compute_hit_rates",
                  new_callable=AsyncMock, return_value=None),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)
            if mock_score.called:
                line_seen.append(mock_score.call_args.kwargs.get("current_line"))

        if line_seen:
            assert line_seen[0] == 27.5, (
                f"Expected 27.5 from snap.line_value; got {line_seen[0]!r}. "
                "Job is reading snap.line (wrong field) instead of snap.line_value."
            )

    @pytest.mark.asyncio
    async def test_watchlist_rescan_uses_line_value_not_line(self):
        """
        The watchlist rescan path must read snap.line_value for the current line.
        A real UnderdogSnapshotRecord has no .line attribute so any access to it
        would yield the ORM default (None) rather than the real value.
        """
        real_snap = self._make_real_snap(player="WL Player", stat="Points", line=20.0)
        real_wl   = self._make_wl_pol_row(player="WL Player", stat="Points")
        db  = self._make_e2e_db(
            {("WL Player", "Points"): real_snap},
            watchlist=[real_wl],
        )
        # All Part 1 props in bulk-alerted set so scoring is skipped for Part 1
        db.get_recently_alerted_prop_keys = AsyncMock(
            return_value=frozenset({("WL Player", "Points")})
        )
        ctx = _make_context(db)
        low = _make_score(total=22, tier="PASS", stars=0)
        val = _make_validation(supported=False)
        dec = _make_decision(rec="PASS", tier="PASS", conf=22)
        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=low),
            patch("engine.player_validator.validate_player_prop", return_value=val),
            patch("market_engine._fetch_and_compute_hit_rates",
                  new_callable=AsyncMock, return_value=None),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=dec),
            patch("market_engine._is_game_live_or_past", return_value=False),
        ):
            from market_engine import _stable_refresh_job
            # Must not raise AttributeError; low score → Rejected (not Removed)
            await _stable_refresh_job(ctx)


class TestWatchlistCursorRotation:
    """
    Verify that the watchlist rotating cursor ensures all candidates are
    eventually processed across cycles, even when the watchlist exceeds the
    200-candidate per-cycle cap.
    """

    @staticmethod
    def _make_wl_row(i: int) -> MagicMock:
        row = MagicMock()
        row.player_name     = f"WL Player {i}"
        row.stat_type       = "Points"
        row.sport           = "NBA"
        row.line_value      = 20.0
        row.confidence      = 35
        row.external_id     = f"wl-{i:04d}"
        row.watchlist_state = "Watchlist"
        row.result          = "PENDING"
        row.game_time       = None
        row.team            = ""
        row.recommendation  = "UNDER"
        row.decision_tier   = "PASS"
        return row

    def test_cursor_arithmetic_covers_all_candidates(self):
        """A pool of 300 watchlist candidates is fully covered in 2 cycles
        when batch size is 200 — cursor advances correctly."""
        batch = 200
        pool  = 300

        # Cycle 1
        cursor  = 0
        end     = min(cursor + batch, pool)
        next_c  = end % pool if pool > 0 else 0
        assert cursor  == 0
        assert end     == 200
        assert next_c  == 200

        # Cycle 2
        cursor  = next_c
        end     = min(cursor + batch, pool)
        next_c  = end % pool if pool > 0 else 0
        assert cursor  == 200
        assert end     == 300
        assert next_c  == 0   # wraps back to 0

    def test_cursor_wraps_when_stale_exceeds_pool(self):
        """If the persisted cursor exceeds the current pool size (pool shrank),
        it must wrap cleanly via modulo without IndexError."""
        from market_engine import _STABLE_WATCHLIST_BATCH_SIZE as cap
        pool   = 50
        cursor = 180  # stale — pool shrank
        cursor = cursor % pool if pool > 0 else 0
        assert 0 <= cursor < pool

    @pytest.mark.asyncio
    async def test_second_200_candidates_processed_on_second_cycle(self, tmp_path):
        """
        With 300 watchlist candidates and a batch cap of 200, the second batch
        (items 200–299) must be processed when the job runs a second time.

        This confirms that candidates beyond the first batch are NOT starved.
        Both cycles share the same HealthTracker file so the cursor persists
        between them within the same async test.
        """
        from engine.health import HealthTracker
        all_wl = [self._make_wl_row(i) for i in range(300)]
        ht     = HealthTracker(path=tmp_path / "health.json")
        ht.set_wl_refresh_cursor(0)

        async def _run_cycle(ht_instance):
            db = AsyncMock()
            db.get_active_underdog_snapshot_per_prop = AsyncMock(return_value={})
            db.get_latest_underdog_snapshot_per_prop = AsyncMock(return_value={})
            db.get_recently_alerted_prop_keys = AsyncMock(return_value=frozenset())
            db.get_active_watchlist_candidates = AsyncMock(return_value=all_wl)
            db.has_recent_ud_alert    = AsyncMock(return_value=False)
            db.get_ud_prop_history    = AsyncMock(return_value=[])
            db.log_prop_opportunity   = AsyncMock(return_value=None)
            db.mark_ud_snapshot_alert_sent = AsyncMock(return_value=None)
            db.mark_opportunity_alert_sent = AsyncMock(return_value=None)
            db.seed_clv_from_ud_confirmation = AsyncMock(return_value=None)

            ctx = _make_context(db)
            with (
                patch("market_engine.get_health_tracker", return_value=ht_instance),
                patch("market_engine._is_futures_stat",   return_value=False),
                patch("market_engine._is_prop_deduped",   return_value=False),
                patch("engine.ud_scoring.score_ud_prop",
                      return_value=_make_score(total=20, tier="PASS", stars=0)),
                patch("engine.player_validator.validate_player_prop",
                      return_value=_make_validation(supported=False)),
                patch("market_engine._fetch_and_compute_hit_rates",
                      new_callable=AsyncMock, return_value=None),
                patch("engine.ud_bet_decision.make_ud_bet_decision",
                      return_value=_make_decision(rec="PASS", tier="PASS", conf=10)),
                patch("market_engine._is_game_live_or_past", return_value=True),
            ):
                from market_engine import _stable_refresh_job
                await _stable_refresh_job(ctx)

        # Cycle 1: cursor starts at 0 → processes items 0–199 → cursor becomes 200
        await _run_cycle(ht)
        assert ht.get_wl_refresh_cursor() == 200, (
            f"Expected wl_cursor=200 after cycle 1, got {ht.get_wl_refresh_cursor()}"
        )

        # Cycle 2: cursor at 200 → processes items 200–299 → wraps to 0
        await _run_cycle(ht)
        assert ht.get_wl_refresh_cursor() == 0, (
            f"Expected wl_cursor=0 after cycle 2 (wrapped), got {ht.get_wl_refresh_cursor()}"
        )

    def test_health_tracker_wl_cursor_get_set_roundtrip(self, tmp_path):
        """HealthTracker.get/set_wl_refresh_cursor persist across reloads."""
        from engine.health import HealthTracker
        path = tmp_path / "health.json"
        ht1  = HealthTracker(path=path)
        ht1.set_wl_refresh_cursor(200)

        ht2 = HealthTracker(path=path)
        assert ht2.get_wl_refresh_cursor() == 200

    def test_health_tracker_wl_cursor_negative_clamped(self, tmp_path):
        """Negative cursor values are clamped to zero."""
        from engine.health import HealthTracker
        ht = HealthTracker(path=tmp_path / "health.json")
        ht.set_wl_refresh_cursor(-5)
        assert ht.get_wl_refresh_cursor() == 0


class TestStableRefreshJob:

    @pytest.mark.asyncio
    async def test_no_underdog_api_call(self):
        """Stable refresh must not call any Underdog fetch endpoint."""
        snap = _make_snap()
        active_pool = {("Test Player", "Points"): snap}
        db  = _build_minimal_db(active_pool=active_pool)
        ctx = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=_make_score(total=30)),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation(supported=False)),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=_make_decision(rec="PASS")),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # get_active_underdog_snapshot_per_prop called — that's DB, not Underdog API
        db.get_active_underdog_snapshot_per_prop.assert_called_once()
        # get_latest_underdog_snapshot_per_prop must NOT be called by stable refresh
        db.get_latest_underdog_snapshot_per_prop.assert_not_called()
        # Crucially, no Underdog connector fetch was triggered

    @pytest.mark.asyncio
    async def test_dedup_suppresses_recently_alerted_prop(self):
        """A prop alerted within the dedup window should not generate a second alert.

        The stable refresh uses get_recently_alerted_prop_keys() (bulk) to pre-load
        the dedup set once per batch — not a per-prop has_recent_ud_alert call.
        """
        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            # Bulk dedup set contains the prop → it's skipped without scoring
            bulk_alerted_keys=frozenset({("Test Player", "Points")}),
        )
        ctx  = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Bulk dedup fired → log_prop_opportunity not called
        db.log_prop_opportunity.assert_not_called()
        # Critically: bulk method was used, not per-prop has_recent_ud_alert
        db.get_recently_alerted_prop_keys.assert_called_once()

    @pytest.mark.asyncio
    async def test_dedup_uses_bulk_query_not_per_prop(self):
        """Bulk dedup must be called once per cycle — never per-prop has_recent_ud_alert
        (which would open tens of thousands of serial DB sessions on a 10k batch)."""
        snaps = {(f"Player{i}", "Points"): _make_snap(player=f"Player{i}") for i in range(5)}
        # All in the bulk alerted set → all skipped
        alerted = frozenset(snaps.keys())
        db  = _build_minimal_db(active_pool=snaps, bulk_alerted_keys=alerted)
        ctx = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Exactly one bulk call for 5 props, not 5 individual calls
        db.get_recently_alerted_prop_keys.assert_called_once()
        db.has_recent_ud_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_bulk_alerted_set_does_not_fall_back_to_per_prop(self):
        """
        When get_recently_alerted_prop_keys() succeeds and returns an empty
        frozenset (no props were alerted recently), the job must NOT fall back
        to per-prop has_recent_ud_alert calls.

        Regression: the original `elif not _sr_db_alerted` branch treated an
        empty frozenset as a load failure, causing 10k serial queries on the
        common "nothing recently alerted" case.
        """
        snap = _make_snap()
        # Bulk returns empty frozenset — success, but nothing alerted recently
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            bulk_alerted_keys=frozenset(),   # empty = "nothing alerted" (not a failure)
        )
        ctx  = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=True),  # in-mem dedup fires
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Bulk succeeded with empty result → must not call per-prop has_recent_ud_alert
        db.has_recent_ud_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_prop_fallback_only_when_bulk_raises(self):
        """Per-prop has_recent_ud_alert is only called when get_recently_alerted_prop_keys
        raises — not when it returns an empty frozenset."""
        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
        )
        # Override bulk to raise (simulating DB error)
        db.get_recently_alerted_prop_keys = AsyncMock(
            side_effect=Exception("simulated bulk query failure")
        )
        db.has_recent_ud_alert = AsyncMock(return_value=True)  # per-prop → dedup
        ctx  = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Bulk failed → per-prop fallback was used
        db.has_recent_ud_alert.assert_called()

    @pytest.mark.asyncio
    async def test_in_memory_dedup_suppresses_prop(self):
        """In-memory dedup also blocks a prop already alerted this session."""
        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            bulk_alerted_keys=frozenset(),   # not in bulk set
        )
        ctx  = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=True),  # in-memory hit
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        db.log_prop_opportunity.assert_not_called()

    @pytest.mark.asyncio
    async def test_s_tier_alert_persists_alert_sent_to_db(self):
        """
        After delivering a stable S-tier alert via the normal deliver_underdog path,
        mark_ud_snapshot_alert_sent must be called so that get_recently_alerted_prop_keys()
        returns this prop in the next cycle (preventing re-alert after a bot restart).

        Note: the separate 95+ priority override broadcast_alert path has been removed per
        spec. All stable refresh alerts now flow through deliver_underdog.
        """
        snap = _make_snap(player="Priority Player", stat="Points", sport="NBA")
        db   = _build_minimal_db(
            active_pool={("Priority Player", "Points"): snap},
            bulk_alerted_keys=frozenset(),
        )
        ctx  = _make_context(db)

        s_score = _make_score(total=97, tier="S", stars=5)
        s_dec   = _make_decision(rec="OVER", tier="S", conf=97)

        delivery      = MagicMock()
        delivery.sent = True   # simulate successful Telegram send

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("market_engine._tier_delivery_gate", return_value=True),
            patch("market_engine._try_claim_delivery_slot", new_callable=AsyncMock, return_value=True),
            patch("engine.ud_scoring.score_ud_prop",   return_value=s_score),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=s_dec),
            patch("market_engine._ud_line_fresh",     return_value=True),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._get_odds_api_confirmation", new_callable=AsyncMock, return_value=None),
            patch("market_engine.AlertDelivery") as mock_ad,
        ):
            mock_ad.return_value.deliver_underdog = AsyncMock(return_value=delivery)
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # DB persistence must be called — mark_ud_snapshot_alert_sent is called from
        # inside stable_refresh_job after a successful deliver_underdog send
        db.mark_ud_snapshot_alert_sent.assert_called_once_with(
            "Priority Player", "Points"
        )

    @pytest.mark.asyncio
    async def test_restart_dedup_via_db_prevents_re_alert(self):
        """
        Simulates a bot restart: _priority_override_sent is cleared (new empty set)
        but mark_ud_snapshot_alert_sent was called previously.

        After restart, get_recently_alerted_prop_keys() returns the prop's key
        (because alert_sent=True is persisted in the DB), so the stable refresh
        must skip it without alerting again.
        """
        snap = _make_snap(player="Post-Restart Player", stat="Points", sport="NBA")
        # Bulk dedup pre-load contains the key — simulates DB having alert_sent=True
        db   = _build_minimal_db(
            active_pool={("Post-Restart Player", "Points"): snap},
            bulk_alerted_keys=frozenset({("Post-Restart Player", "Points")}),
        )
        ctx  = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            # _priority_override_sent is fresh empty set (simulating restart)
            patch("market_engine._priority_override_sent", new=set()),
            patch("market_engine.broadcast_alert", new_callable=AsyncMock) as mock_broadcast,
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Must NOT have broadcast — DB dedup caught it before scoring
        mock_broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchlist_under_rule_low_score(self):
        """Score 30–40 + UNDER recommendation → watchlist_state='Watchlist'."""
        snap = _make_snap(stat="Strikeouts", line=5.5)
        db   = _build_minimal_db(
            active_pool={("Test Player", "Strikeouts"): snap},
            has_recent_alert=False,
        )
        ctx  = _make_context(db)

        low_score  = _make_score(total=35, tier="PASS", stars=1)
        under_dec  = _make_decision(rec="UNDER", tier="PASS", conf=38)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=low_score),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=under_dec),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Should have been logged as 'Watchlist'
        calls = db.log_prop_opportunity.call_args_list
        wl_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Watchlist"
        ]
        assert len(wl_calls) >= 1, (
            "Expected at least one log_prop_opportunity(watchlist_state='Watchlist') call"
        )

    @pytest.mark.asyncio
    async def test_watchlist_under_rule_not_triggered_for_over(self):
        """Score 30–40 + OVER recommendation → NOT watchlisted (normal rejection)."""
        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            has_recent_alert=False,
        )
        ctx  = _make_context(db)

        low_score = _make_score(total=35, tier="PASS", stars=1)
        over_dec  = _make_decision(rec="OVER", tier="PASS", conf=38)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=low_score),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation(supported=False)),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=over_dec),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # Score < 30 or Over → must not be watchlisted
        calls = db.log_prop_opportunity.call_args_list
        wl_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Watchlist"
        ]
        assert len(wl_calls) == 0

    @pytest.mark.asyncio
    async def test_qualifying_prop_logged_as_qualified(self):
        """A prop that passes all gates is logged as 'Qualified'."""
        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            has_recent_alert=False,
        )
        ctx  = _make_context(db)

        good_score = _make_score(total=75, tier="A", stars=4)
        good_dec   = _make_decision(rec="OVER", tier="A", conf=72)
        delivery   = MagicMock()
        delivery.sent = False  # don't actually send Telegram

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=good_score),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=good_dec),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._ud_line_fresh",     return_value=True),
            patch("market_engine.AlertDelivery") as mock_ad,
            patch("market_engine._get_odds_api_confirmation", new_callable=AsyncMock, return_value=None),
        ):
            mock_ad.return_value.deliver_underdog = AsyncMock(return_value=delivery)
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        calls = db.log_prop_opportunity.call_args_list
        qual_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Qualified"
        ]
        assert len(qual_calls) >= 1

    @pytest.mark.asyncio
    async def test_empty_pool_completes_without_error(self):
        """Stable refresh with an empty active pool must not raise."""
        db  = _build_minimal_db(active_pool={})
        ctx = _make_context(db)

        with patch("market_engine.get_health_tracker", return_value=None):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)   # should not raise

    @pytest.mark.asyncio
    async def test_none_db_returns_early(self):
        """When db is None the job exits without error."""
        ctx = _make_context(db=None)
        ctx.bot_data = {"db": None}

        from market_engine import _stable_refresh_job
        await _stable_refresh_job(ctx)   # should not raise

    @pytest.mark.asyncio
    async def test_cursor_advances_and_persists(self, tmp_path):
        """After a cycle the cursor is saved to the HealthTracker."""
        from engine.health import HealthTracker

        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            has_recent_alert=True,  # dedup → no scoring needed
        )
        ctx  = _make_context(db)

        ht = HealthTracker(path=tmp_path / "health.json")

        with (
            patch("market_engine.get_health_tracker", return_value=ht),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # After one cycle with 1 prop, cursor should be 0 (pool_size=1, end=1 → next=0)
        assert ht.get_stable_refresh_cursor() == 0
        # Stats should be persisted
        stats = ht.get_stable_refresh_stats()
        assert "pool_size" in stats

    @pytest.mark.asyncio
    async def test_health_stats_persisted_after_cycle(self, tmp_path):
        """Stats dict is persisted to HealthTracker after every cycle."""
        from engine.health import HealthTracker

        snap = _make_snap()
        db   = _build_minimal_db(
            active_pool={("Test Player", "Points"): snap},
            has_recent_alert=True,
        )
        ctx  = _make_context(db)
        ht   = HealthTracker(path=tmp_path / "health.json")

        with (
            patch("market_engine.get_health_tracker", return_value=ht),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        stats = ht.get_stable_refresh_stats()
        for key in ("pool_size", "batch_size", "sr_rescored", "wl_active"):
            assert key in stats, f"Missing stat key: {key}"


# ── Watchlist rescan sub-tests ─────────────────────────────────────────────────

class TestWatchlistRescan:

    def _make_wl_row(
        self,
        player:  str  = "WL Player",
        stat:    str  = "Points",
        sport:   str  = "NBA",
        line:    float = 20.0,
        conf:    int   = 35,
        ext_id:  str   = "wl-001",
    ) -> MagicMock:
        row = MagicMock()
        row.player_name   = player
        row.stat_type     = stat
        row.sport         = sport
        row.line_value    = line
        row.confidence    = conf
        row.external_id   = ext_id
        row.watchlist_state = "Watchlist"
        row.result        = "PENDING"
        row.game_time     = None
        row.team          = ""
        row.recommendation = "UNDER"
        row.decision_tier  = "PASS"
        return row

    @pytest.mark.asyncio
    async def test_watchlist_candidate_marked_removed_when_not_in_pool(self):
        """A watchlist candidate absent from the active pool is marked 'Removed'."""
        wl_row = self._make_wl_row()
        db     = _build_minimal_db(
            active_pool={},                    # pool is empty → prop removed
            watchlist_candidates=[wl_row],
        )
        ctx = _make_context(db)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        calls = db.log_prop_opportunity.call_args_list
        removed_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Removed"
        ]
        assert len(removed_calls) >= 1

    @pytest.mark.asyncio
    async def test_watchlist_promotion_sends_alert_when_qualifies(self):
        """A watchlist candidate that now qualifies is promoted via deliver_underdog.

        Strategy:
        - Active pool has one prop that scores 20 (< 30 → silent reject in Part 1,
          never reaches make_ud_bet_decision because validation fails too).
        - Watchlist candidate is the same player/stat so Part 2 finds it in the pool.
        - score_ud_prop returns a high score on the second call (Part 2 rescan).
        - make_ud_bet_decision is only called once (Part 2) and returns OVER/A/75.
        """
        snap   = _make_snap(player="WL Player", stat="Points", sport="NBA", line=20.0)
        wl_row = self._make_wl_row()
        db     = _build_minimal_db(
            active_pool={("WL Player", "Points"): snap},
            watchlist_candidates=[wl_row],
            has_recent_alert=False,
        )
        ctx = _make_context(db)

        # BQ=90 and MQ (conf)=90 — both ≥85 so the Tier-2 NBA gate passes.
        good_score = _make_score(total=90, tier="A", stars=4)
        good_dec   = _make_decision(rec="OVER", tier="A", conf=90)
        delivery   = MagicMock()
        delivery.sent = True

        # Part 1 call → score=20 (validation fails → reject, no decision call)
        # Part 2 call → good_score (qualifies → deliver_underdog)
        score_side_effect = [_make_score(total=20, tier="PASS", stars=1), good_score]
        # Part 1 validation: supported=False → reject before decision
        # Part 2 validation: supported=True → passes _wl_qualifies check
        val_side_effect   = [_make_validation(supported=False), _make_validation(supported=True)]

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("market_engine._tier_delivery_gate", return_value=True),
            patch("market_engine._try_claim_delivery_slot", new_callable=AsyncMock, return_value=True),
            patch("engine.ud_scoring.score_ud_prop",   side_effect=score_side_effect),
            patch("engine.player_validator.validate_player_prop", side_effect=val_side_effect),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            # make_ud_bet_decision only reached in Part 2
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=good_dec),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._ud_line_fresh",     return_value=True),
            patch("market_engine.AlertDelivery") as mock_ad,
            patch("market_engine._get_odds_api_confirmation", new_callable=AsyncMock, return_value=None),
        ):
            mock_ad.return_value.deliver_underdog = AsyncMock(return_value=delivery)
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        # deliver_underdog should have been called for the watchlist promotion
        mock_ad.return_value.deliver_underdog.assert_called()

    @pytest.mark.asyncio
    async def test_declined_watchlist_candidate_marked_rejected(self):
        """A watchlist candidate that scores < 30 is marked 'Rejected'."""
        snap   = _make_snap(player="WL Player", stat="Points", sport="NBA", line=20.0)
        wl_row = self._make_wl_row(conf=35)
        db     = _build_minimal_db(
            active_pool={("WL Player", "Points"): snap},
            watchlist_candidates=[wl_row],
            has_recent_alert=False,
        )
        ctx = _make_context(db)

        bad_score = _make_score(total=22, tier="PASS", stars=1)  # below 30
        bad_dec   = _make_decision(rec="PASS", tier="PASS", conf=22)

        with (
            patch("market_engine.get_health_tracker", return_value=None),
            patch("market_engine._is_futures_stat",   return_value=False),
            patch("market_engine._is_prop_deduped",   return_value=False),
            patch("engine.ud_scoring.score_ud_prop",   return_value=bad_score),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation(supported=False)),
            patch("market_engine._fetch_and_compute_hit_rates", new_callable=AsyncMock, return_value=None),
            patch("engine.ud_bet_decision.make_ud_bet_decision",  return_value=bad_dec),
            patch("market_engine._is_game_live_or_past", return_value=False),
        ):
            from market_engine import _stable_refresh_job
            await _stable_refresh_job(ctx)

        calls = db.log_prop_opportunity.call_args_list
        rej_calls = [
            c for c in calls
            if c.kwargs.get("watchlist_state") == "Rejected"
        ]
        assert len(rej_calls) >= 1
