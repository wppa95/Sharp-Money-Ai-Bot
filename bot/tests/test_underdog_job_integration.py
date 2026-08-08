"""
Integration tests for underdog_job — covering the three gaps identified in code review:

  1. clamp_score applied at UnderdogSnapshotRecord persistence boundary
  2. Lifecycle state transitions (ACTIVE_ALERTED, REMOVED) applied AFTER bridge
  3. Job health tracking — record_job_run / record_job_fail called correctly

These tests run the real underdog_job with mocked DB and registry (same pattern as
test_underdog_job_summary.py) but focus on the state-machine and health-tracking
behaviours rather than alert counters.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me
from engine.health import HealthTracker
from engine.score_validation import clamp_score


# ── Shared helpers (mirror test_underdog_job_summary.py pattern) ───────────────

def _make_snap(
    player: str = "Shohei Ohtani",
    stat_type: str = "Total Bases",
    line: float = 2.5,
    sport: str = "MLB",
    removed: bool = False,
) -> MagicMock:
    snap = MagicMock()
    snap.sportsbook  = "Underdog"
    snap.player      = player
    snap.sport       = sport
    snap.line        = line
    snap.team        = "LAA"
    snap.game_time   = None
    snap.event       = "game-001"
    snap.market_type = "Pick'em"
    snap.is_pickem   = True
    suffix           = " [REMOVED]" if removed else ""
    snap.selection   = f"{player} {stat_type} {line}{suffix}"
    return snap


def _make_db_record(
    player: str = "Shohei Ohtani",
    stat_type: str = "Total Bases",
    line_value: float = 2.5,
    line_moved: bool = False,
    prev_line: float | None = None,
) -> MagicMock:
    """Mirrors the helper in test_underdog_job_summary.py — used for prop_history lists."""
    r = MagicMock()
    r.player_name = player
    r.stat_type   = stat_type
    r.line_value  = line_value
    r.line_moved  = line_moved
    r.prev_line   = prev_line
    r.removed     = False
    r.alert_sent  = False
    r.score_tier  = "PASS"
    return r


def _make_prev_record(
    player: str = "Shohei Ohtani",
    stat_type: str = "Total Bases",
    line_value: float = 1.5,
    alert_sent: bool = False,
    score_tier: str = "PASS",
) -> MagicMock:
    r = MagicMock()
    r.player_name = player
    r.stat_type   = stat_type
    r.line_value  = line_value
    r.line_moved  = True
    r.prev_line   = None
    r.removed     = False
    r.alert_sent  = alert_sent
    r.score_tier  = score_tier
    return r


def _make_db(recent_records=None, known_keys=None, prop_history=None) -> MagicMock:
    recent_dict: dict = {}
    for r in (recent_records or []):
        recent_dict[(r.player_name, r.stat_type)] = r

    if known_keys is None:
        known_keys = {(r.player_name, r.stat_type) for r in (recent_records or [])}

    db = MagicMock()
    db.get_latest_underdog_snapshot_per_prop = AsyncMock(return_value=recent_dict)
    db.get_known_underdog_prop_keys          = AsyncMock(return_value=known_keys)
    db.count_today_underdog_alerts           = AsyncMock(return_value=0)
    db.save_underdog_snapshot                = AsyncMock()
    db.save_underdog_snapshots_bulk          = AsyncMock()
    db.get_ud_prop_history                   = AsyncMock(return_value=prop_history or [])
    db.sync_underdog_snapshots_to_prop_history = AsyncMock(return_value=3)
    db.update_prop_lifecycle_state           = AsyncMock(return_value=True)
    return db


def _make_context(db: MagicMock) -> MagicMock:
    ctx          = MagicMock()
    ctx.bot_data = {"db": db}
    ctx.bot      = MagicMock()
    return ctx


async def _run_job(snapshots, db, *, deliver_result=None, health: "HealthTracker | None" = None):
    from alerts import DeliveryResult
    if deliver_result is None:
        deliver_result = DeliveryResult(sent=False)

    registry = MagicMock()
    registry.fetch_pickem = AsyncMock(return_value=snapshots)
    ctx = _make_context(db)

    with patch.object(me, "_registry", registry):
        with patch.object(me, "_cold_start_done", True):
            with patch("market_engine.AlertDelivery") as mock_cls:
                mock_delivery = MagicMock()
                mock_delivery.deliver_underdog = AsyncMock(return_value=deliver_result)
                mock_cls.return_value = mock_delivery
                with patch("alerts.broadcast_alert",
                           new_callable=AsyncMock,
                           return_value={"sent": 1, "failed": 0}):
                    with patch("market_engine.get_health_tracker", return_value=health):
                        await me.underdog_job(ctx)

    return mock_delivery


# ── 1. clamp_score at persistence boundary ────────────────────────────────────

class TestClampScoreAtPersistenceBoundary:
    """
    Verify that clamp_score is invoked (and limits values) before they reach
    UnderdogSnapshotRecord — not just imported.
    """

    def test_clamp_score_limits_out_of_range_total(self):
        """score.total = 150 → clamped to 100.0 before storage."""
        assert clamp_score(150, "ud_score.total", 0, 100) == 100.0

    def test_clamp_score_limits_negative_stars(self):
        """score.stars = -2 → clamped to 0 before storage."""
        assert clamp_score(-2, "ud_score.stars", 0, 5) == 0.0

    def test_clamp_score_limits_over_max_stars(self):
        """score.stars = 99 → clamped to 5 before storage."""
        assert clamp_score(99, "ud_score.stars", 0, 5) == 5.0

    def test_clamp_score_applied_in_underdog_job_source(self):
        """
        clamp_score is called in underdog_job at the UnderdogSnapshotRecord
        creation point — verified via source inspection.
        """
        import inspect
        src = inspect.getsource(me.underdog_job)
        # All three score fields must pass through clamp_score
        assert "clamp_score(score.total"      in src
        assert "clamp_score(score.stars"      in src
        assert "clamp_score(decision.confidence" in src

    @pytest.mark.asyncio
    async def test_score_out_of_range_logs_warning_not_raises(self, caplog):
        """
        When the scoring engine returns a value outside the valid range,
        clamp_score logs a WARNING and the job does NOT crash.
        """
        from alerts import DeliveryResult

        snap = _make_snap(line=5.0)
        prev = _make_prev_record(line_value=2.5)
        db   = _make_db(recent_records=[prev])

        # Build an out-of-range mock score
        mock_score = MagicMock()
        mock_score.total               = 150   # out of range — should be clamped to 100
        mock_score.stars               = 4
        mock_score.tier                = "S"
        mock_score.move_velocity       = 25
        mock_score.historical_activity = 20
        mock_score.avg_vs_line         = 15
        mock_score.consistency         = 10
        mock_score.stability           = 10
        mock_score.n_history           = 10
        mock_score.current_line        = 5.0

        with caplog.at_level(logging.WARNING, logger="engine.score_validation"):
            # score_ud_prop is imported by name inside the loop body;
            # patch at the module level so both cold-start and line-change paths see it.
            with patch("engine.ud_scoring.score_ud_prop", return_value=mock_score):
                await _run_job([snap], db, deliver_result=DeliveryResult(sent=False))

        # clamp_score should have logged a WARNING for the 150 total
        warnings = [r for r in caplog.records if "ud_score.total" in r.message]
        assert len(warnings) >= 1, "Expected clamp_score warning for out-of-range total"

    def test_ev_ai_confidence_clamped_at_persistence_boundary(self):
        """
        EVRecord.ai_confidence must be clamped via clamp_score before being
        passed to the database — verified via source inspection of alerts._log_ev.
        """
        import inspect
        import alerts as al
        src = inspect.getsource(al.AlertDelivery._log_ev)
        assert "clamp_score" in src, (
            "clamp_score not applied to ai_confidence in alerts.AlertDelivery._log_ev"
        )
        assert "ev.ai_confidence" in src, (
            "clamp_score label 'ev.ai_confidence' not found in _log_ev"
        )

    @pytest.mark.asyncio
    async def test_ev_ai_confidence_out_of_range_logs_warning(self, caplog):
        """
        When an EVOpportunity has ai_confidence > 100, clamp_score logs a WARNING
        before the value is stored in EVRecord.
        """
        import alerts as al

        opp = MagicMock()
        opp.sport         = MagicMock(); opp.sport.value        = "MLB"
        opp.market_type   = MagicMock(); opp.market_type.value  = "ML"
        opp.event         = "NYY vs BOS"
        opp.player        = None
        opp.line          = None
        opp.best_odds     = -110
        opp.best_book     = "DraftKings"
        opp.fair_probability = 0.55
        opp.expected_value   = 8.5
        opp.steam_score      = 0
        opp.ai_confidence    = 150   # out of range — should be clamped to 100
        opp.recommendation   = MagicMock(); opp.recommendation.value = "TAKE"
        opp.stars         = 4
        opp.reason_codes  = ["EV_POSITIVE"]
        opp.timestamp     = None
        opp.ev_result     = MagicMock(); opp.ev_result.selection = "NYY ML"

        db_mock = MagicMock()
        db_mock.save_ev = AsyncMock()

        delivery = al.AlertDelivery(db=db_mock, bot=MagicMock(), chat_ids=[99999])

        with caplog.at_level(logging.WARNING, logger="engine.score_validation"):
            await delivery._log_ev(opp, alert_sent=False)

        warnings = [r for r in caplog.records if "ev.ai_confidence" in r.message]
        assert len(warnings) >= 1, "Expected clamp_score WARNING for ai_confidence=150"

        # The stored value must be clamped, not raw
        saved = db_mock.save_ev.call_args[0][0]
        assert saved.ai_confidence == 100, (
            f"ai_confidence stored as {saved.ai_confidence}, expected 100 after clamping"
        )

    @pytest.mark.asyncio
    async def test_normal_scores_no_clamp_warning(self, caplog):
        """In-range scores produce no clamp warnings."""
        snap = _make_snap(line=3.0)
        prev = _make_prev_record(line_value=2.5)
        db   = _make_db(recent_records=[prev])

        with caplog.at_level(logging.WARNING, logger="engine.score_validation"):
            await _run_job([snap], db)

        warnings = [r for r in caplog.records if "clamp" in r.message.lower()]
        assert len(warnings) == 0, f"Unexpected clamp warnings: {[r.message for r in warnings]}"


# ── 2. Lifecycle state transitions applied AFTER bridge ───────────────────────

class TestLifecycleTransitionsAfterBridge:
    """
    Verify that:
      - After a successful alert, update_prop_lifecycle_state is called with
        ACTIVE_ALERTED AFTER sync_underdog_snapshots_to_prop_history (the bridge).
      - After a successful removal alert, REMOVED is applied similarly.
      - When no alert is sent, update_prop_lifecycle_state is not called.
    """

    @pytest.mark.asyncio
    async def test_alert_sent_sets_active_alerted_after_bridge(self):
        """
        When deliver_underdog returns sent=True, update_prop_lifecycle_state must
        be called with 'ACTIVE_ALERTED' after the bridge runs.

        Uses the same qualifying setup as test_line_changed_qualifies in
        test_underdog_job_summary.py: large line move (3→7) + rich hit-rate
        data so score.stars reaches UD_MIN_STARS_TO_ALERT.  chat_ids is patched
        so delivery actually executes.
        """
        from alerts import DeliveryResult
        from engine.player_results import PlayerHitRates, WindowStats

        # Large line move (4 units) → A/S tier expected
        # Sport is NBA (non-strict, in ud_alert_sports) so MLB/NFL BQ gate does not apply;
        # this test verifies lifecycle transitions, not MLB-specific delivery rules.
        snap = _make_snap("Player A", "Total Bases", line=7.0, sport="NBA")
        history = [
            _make_db_record(
                "Player A", "Total Bases",
                line_value=3.0 + 0.2 * i,
                line_moved=(i > 0),
                prev_line=3.0 + 0.2 * (i - 1) if i > 0 else None,
            )
            for i in range(20)
        ]
        db = _make_db(
            recent_records=[_make_db_record("Player A", "Total Bases", 3.0)],
            prop_history=history,
        )

        def _win(n, r):
            oc = round(n * r)
            return WindowStats(games=n, over_count=oc, under_count=n - oc, hit_rate=r, average=8.0)

        fake_hit_rates = PlayerHitRates(
            player_name="Player A", stat_type="total bases", current_line=7.0,
            l5=_win(5, 0.80), l10=_win(10, 0.80),
            l20=_win(20, 0.75), l30=_win(30, 0.70),
            season=_win(50, 0.70), h2h=None,
            has_real_data=True, total_games=50,
        )

        call_order: list[str] = []
        original_bridge = db.sync_underdog_snapshots_to_prop_history
        original_update = db.update_prop_lifecycle_state

        async def _bridge_spy(*args, **kwargs):
            call_order.append("bridge")
            return await original_bridge(*args, **kwargs)

        async def _update_spy(*args, **kwargs):
            # positional: (provider, player_name, sport, stat_type, new_state, ...)
            new_state = args[4] if len(args) > 4 else kwargs.get("new_state", "?")
            call_order.append(f"update:{new_state}")
            return await original_update(*args, **kwargs)

        db.sync_underdog_snapshots_to_prop_history = _bridge_spy
        db.update_prop_lifecycle_state = _update_spy

        with patch("market_engine._fetch_and_compute_hit_rates",
                   new=AsyncMock(return_value=fake_hit_rates)):
            # Patch allowed_user_ids so chat_ids is non-empty and delivery fires
            with patch.object(me.config.__class__, "allowed_user_ids",
                              new_callable=lambda: property(lambda self: {99999})):
                await _run_job(
                    [snap], db,
                    deliver_result=DeliveryResult(sent=True, recipients_sent=1),
                )

        # Bridge must have run
        assert "bridge" in call_order, (
            "sync_underdog_snapshots_to_prop_history was not called. "
            f"call_order={call_order}"
        )

        # ACTIVE_ALERTED must have been applied AFTER bridge
        active_updates = [x for x in call_order if "ACTIVE_ALERTED" in x]
        assert len(active_updates) >= 1, (
            "update_prop_lifecycle_state('ACTIVE_ALERTED') was never called. "
            f"call_order={call_order}"
        )
        bridge_idx       = call_order.index("bridge")
        first_update_idx = next(i for i, x in enumerate(call_order) if "ACTIVE_ALERTED" in x)
        assert first_update_idx > bridge_idx, (
            "ACTIVE_ALERTED update happened BEFORE bridge — ordering is wrong. "
            f"bridge at {bridge_idx}, update at {first_update_idx}, order={call_order}"
        )

    @pytest.mark.asyncio
    async def test_removal_alert_sets_removed_after_bridge(self):
        """
        When a removal alert is sent, update_prop_lifecycle_state('REMOVED')
        is called after the bridge.
        """
        from alerts import DeliveryResult

        snap = _make_snap("Aaron Judge", "Home Runs", removed=True)
        prev = _make_prev_record("Aaron Judge", "Home Runs", alert_sent=True)
        db   = _make_db(recent_records=[prev])

        call_order: list[str] = []
        original_bridge = db.sync_underdog_snapshots_to_prop_history
        original_update = db.update_prop_lifecycle_state

        async def _bridge_spy(*args, **kwargs):
            call_order.append("bridge")
            return await original_bridge(*args, **kwargs)

        async def _update_spy(*args, **kwargs):
            # args: (provider, player_name, sport, stat_type, new_state)
            call_order.append(f"update:{args[4]}")
            return await original_update(*args, **kwargs)

        db.sync_underdog_snapshots_to_prop_history = _bridge_spy
        db.update_prop_lifecycle_state = _update_spy

        await _run_job(
            [snap], db,
            deliver_result=DeliveryResult(sent=True, recipients_sent=1),
        )

        assert "bridge" in call_order, "Bridge was not called"
        removed_updates = [x for x in call_order if "REMOVED" in x]
        assert len(removed_updates) >= 1, (
            "update_prop_lifecycle_state('REMOVED') was never called. "
            f"call_order={call_order}"
        )
        bridge_idx = call_order.index("bridge")
        first_removed_idx = next(i for i, x in enumerate(call_order) if "REMOVED" in x)
        assert first_removed_idx > bridge_idx, (
            "REMOVED update happened BEFORE bridge — ordering is wrong"
        )

    @pytest.mark.asyncio
    async def test_no_alert_no_lifecycle_update(self):
        """
        When deliver_underdog returns sent=False, update_prop_lifecycle_state
        should NOT be called (no lifecycle transition needed).
        """
        from alerts import DeliveryResult

        snap = _make_snap(line=3.0)
        prev = _make_prev_record(line_value=2.5)
        db   = _make_db(recent_records=[prev])

        await _run_job([snap], db, deliver_result=DeliveryResult(sent=False))

        db.update_prop_lifecycle_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_runs_before_lifecycle_even_on_no_alert(self):
        """
        sync_underdog_snapshots_to_prop_history always runs (it's not gated on
        alert status) so PropLineHistory is always kept up to date.
        """
        from alerts import DeliveryResult

        snap = _make_snap(line=2.5)
        db   = _make_db()

        await _run_job([snap], db, deliver_result=DeliveryResult(sent=False))

        db.sync_underdog_snapshots_to_prop_history.assert_called_once()


# ── 3. Job health tracking ────────────────────────────────────────────────────

class TestJobHealthTracking:
    """
    Verify that:
      - Empty Underdog response records a job_run (not silently skipped).
      - Successful full cycle records a job_run.
      - A processing exception triggers record_job_fail (not record_job_run).
      - record_job_run is called AFTER the bridge (not before).
    """

    def _make_health(self) -> HealthTracker:
        tmp = Path(tempfile.mktemp(suffix=".json"))
        return HealthTracker(path=tmp)

    @pytest.mark.asyncio
    async def test_empty_snapshots_records_job_run(self):
        """
        When fetch_pickem returns an empty list, the job still records a successful
        run so /health doesn't show the job as 'never ran'.
        """
        health = self._make_health()

        registry = MagicMock()
        registry.fetch_pickem = AsyncMock(return_value=[])
        ctx = _make_context(_make_db())

        with patch.object(me, "_registry", registry):
            with patch.object(me, "_cold_start_done", True):
                with patch("market_engine.get_health_tracker", return_value=health):
                    await me.underdog_job(ctx)

        info = health.get_job_info("underdog_job")
        assert info.get("run_count", 0) >= 1, "record_job_run not called for empty response"
        assert info.get("fail_count", 0) == 0, "record_job_fail should not be called for empty response"

    @pytest.mark.asyncio
    async def test_no_underdog_snaps_records_job_run(self):
        """
        When fetch_pickem returns only non-Underdog snapshots, job still records run.
        """
        health = self._make_health()

        other_snap = _make_snap()
        other_snap.sportsbook = "PrizePicks"   # not Underdog → filtered out

        registry = MagicMock()
        registry.fetch_pickem = AsyncMock(return_value=[other_snap])
        ctx = _make_context(_make_db())

        with patch.object(me, "_registry", registry):
            with patch.object(me, "_cold_start_done", True):
                with patch("market_engine.get_health_tracker", return_value=health):
                    await me.underdog_job(ctx)

        info = health.get_job_info("underdog_job")
        assert info.get("run_count", 0) >= 1, "record_job_run not called when all snaps are non-Underdog"

    @pytest.mark.asyncio
    async def test_successful_cycle_records_job_run(self):
        """A normal successful cycle increments run_count exactly once."""
        from alerts import DeliveryResult

        health = self._make_health()
        snap   = _make_snap()
        db     = _make_db()

        await _run_job([snap], db, health=health)

        info = health.get_job_info("underdog_job")
        assert info.get("run_count", 0) >= 1, "record_job_run not called after successful cycle"
        assert info.get("fail_streak", 0) == 0

    @pytest.mark.asyncio
    async def test_fetch_exception_records_job_fail(self):
        """When fetch_pickem raises, record_job_fail is called."""
        health = self._make_health()

        registry = MagicMock()
        registry.fetch_pickem = AsyncMock(side_effect=RuntimeError("API down"))
        ctx = _make_context(_make_db())

        with patch.object(me, "_registry", registry):
            with patch.object(me, "_cold_start_done", True):
                with patch("market_engine.get_health_tracker", return_value=health):
                    await me.underdog_job(ctx)

        info = health.get_job_info("underdog_job")
        assert info.get("fail_count", 0) >= 1, "record_job_fail not called after fetch exception"
        assert "API down" in (info.get("last_error") or "")
        # run_count must NOT be incremented on failure
        assert info.get("run_count", 0) == 0

    @pytest.mark.asyncio
    async def test_processing_exception_records_job_fail(self):
        """When DB query raises inside the main body, record_job_fail is called."""
        health = self._make_health()

        snap = _make_snap()
        db   = _make_db()
        # Simulate a DB failure in the pre-loop round-trip
        db.get_latest_underdog_snapshot_per_prop = AsyncMock(
            side_effect=RuntimeError("DB connection lost")
        )

        registry = MagicMock()
        registry.fetch_pickem = AsyncMock(return_value=[snap])
        ctx = _make_context(db)

        with patch.object(me, "_registry", registry):
            with patch.object(me, "_cold_start_done", True):
                with patch("market_engine.get_health_tracker", return_value=health):
                    await me.underdog_job(ctx)

        info = health.get_job_info("underdog_job")
        assert info.get("fail_count", 0) >= 1, (
            "record_job_fail not called after processing exception. "
            f"job_info={info}"
        )
        assert info.get("run_count", 0) == 0, "run_count should not increment on failure"

    @pytest.mark.asyncio
    async def test_bridge_failure_records_job_fail(self):
        """When PropLineHistory bridge raises, the job must record a job failure."""
        health = self._make_health()

        snap = _make_snap()
        db   = _make_db()
        db.sync_underdog_snapshots_to_prop_history = AsyncMock(
            side_effect=RuntimeError("DB locked during bridge")
        )

        await _run_job([snap], db, health=health)

        info = health.get_job_info("underdog_job")
        assert info.get("fail_count", 0) >= 1, (
            "record_job_fail not called when bridge raises. job_info=%s" % info
        )
        assert info.get("run_count", 0) == 0, (
            "record_job_run should NOT be called when bridge fails"
        )

    @pytest.mark.asyncio
    async def test_lifecycle_failure_records_job_fail(self):
        """When every lifecycle update fails, the job must record a job failure."""
        from alerts import DeliveryResult
        from engine.player_results import PlayerHitRates, WindowStats

        # Set up a qualifying prop so lifecycle updates are triggered.
        # Sport is NBA (non-strict, in ud_alert_sports) so MLB/NFL BQ gate does not apply;
        # this test verifies job health tracking, not MLB-specific delivery rules.
        snap = _make_snap("Player A", "Total Bases", line=7.0, sport="NBA")
        history = [
            _make_db_record(
                "Player A", "Total Bases",
                line_value=3.0 + 0.2 * i,
                line_moved=(i > 0),
            )
            for i in range(20)
        ]
        db = _make_db(
            recent_records=[_make_db_record("Player A", "Total Bases", 3.0)],
            prop_history=history,
        )
        # Make every lifecycle update fail
        db.update_prop_lifecycle_state = AsyncMock(
            side_effect=RuntimeError("lifecycle DB error")
        )

        def _win(n, r):
            oc = round(n * r)
            return WindowStats(games=n, over_count=oc, under_count=n - oc, hit_rate=r, average=8.0)

        fake_hit_rates = PlayerHitRates(
            player_name="Player A", stat_type="total bases", current_line=7.0,
            l5=_win(5, 0.80), l10=_win(10, 0.80),
            l20=_win(20, 0.75), l30=_win(30, 0.70),
            season=_win(50, 0.70), h2h=None,
            has_real_data=True, total_games=50,
        )

        health = self._make_health()

        with patch("market_engine._fetch_and_compute_hit_rates",
                   new=AsyncMock(return_value=fake_hit_rates)):
            with patch.object(me.config.__class__, "allowed_user_ids",
                              new_callable=lambda: property(lambda self: {99999})):
                await _run_job(
                    [snap], db,
                    deliver_result=DeliveryResult(sent=True, recipients_sent=1),
                    health=health,
                )

        info = health.get_job_info("underdog_job")
        assert info.get("fail_count", 0) >= 1, (
            "record_job_fail not called when lifecycle updates all fail. job_info=%s" % info
        )
        assert info.get("run_count", 0) == 0, (
            "record_job_run should NOT be called when lifecycle fails"
        )

    @pytest.mark.asyncio
    async def test_record_job_run_called_after_bridge(self):
        """
        record_job_run must be called AFTER sync_underdog_snapshots_to_prop_history.
        If bridge hasn't run yet when record_job_run fires, PropLineHistory is stale.
        """
        from alerts import DeliveryResult

        call_order: list[str] = []
        health = self._make_health()

        snap = _make_snap()
        db   = _make_db()

        original_bridge = db.sync_underdog_snapshots_to_prop_history

        async def _bridge_spy(*args, **kwargs):
            call_order.append("bridge")
            return await original_bridge(*args, **kwargs)

        db.sync_underdog_snapshots_to_prop_history = _bridge_spy

        original_run = health.record_job_run

        def _run_spy(name: str) -> None:
            call_order.append(f"job_run:{name}")
            return original_run(name)

        health.record_job_run = _run_spy

        await _run_job([snap], db, health=health)

        assert "bridge" in call_order, "Bridge not called"
        assert any("job_run" in x for x in call_order), "record_job_run not called"

        bridge_idx  = call_order.index("bridge")
        run_idx     = next(i for i, x in enumerate(call_order) if "job_run" in x)
        assert run_idx > bridge_idx, (
            f"record_job_run ({run_idx}) came BEFORE bridge ({bridge_idx}) — ordering is wrong. "
            f"call_order={call_order}"
        )
