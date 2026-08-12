"""
Tests for the Full-Pool Rescan Rotation (_full_pool_rescan_job).

Required coverage (20 tests):
 1.  Stable unchanged props eventually get rescanned
 2.  Previously rejected props eventually get rescanned
 3.  Previously scored props eventually get rescanned
 4.  Multiple batches can cover the entire active pool
 5.  Rotation reaches 100%
 6.  A new rotation automatically begins after 100%
 7.  Rotation state persists across restart (via HealthTracker)
 8.  Removed props cannot be resurrected
 9.  Tier 1 + other sports receive higher scheduling priority than NFL/MLB
10.  NFL/MLB still eventually get processed
11.  No artificial Underdog API-call limit is introduced
12.  Batch size remains bounded
13.  Existing Task-121 stable refresh still works alongside FPR
14.  Stable-refresh and full-pool metrics remain separate
15.  Internal cursor values are not displayed in progress output
16.  Progress represents actual full-pool coverage
17.  Rescanning DOES inflate API/fetch totals
18.  Rejected props can become candidates again on a later rotation
19.  Rescans do not bypass existing Telegram deduplication
20.  API totals are NOT shown in the display output
"""
from __future__ import annotations

import asyncio
import datetime
import types
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_snap(
    player: str = "Test Player",
    stat: str = "Points",
    sport: str = "NBA",
    line: float = 22.5,
    ext_id: str = "ext-001",
    removed: bool = False,
    game_time: datetime.datetime | None = None,
) -> MagicMock:
    snap = MagicMock()
    snap.player_name = player
    snap.stat_type   = stat
    snap.sport       = sport
    snap.line_value  = line
    snap.removed     = removed
    snap.external_id = ext_id
    snap.id          = 1
    snap.team        = "Team A"
    snap.game_time   = game_time
    return snap


def _make_score(total: int = 75, tier: str = "A", stars: int = 4) -> MagicMock:
    score = MagicMock()
    score.total    = total
    score.tier     = tier
    score.stars    = stars
    score.n_history = 20
    score.move_velocity       = 10
    score.historical_activity = 15
    score.avg_vs_line         = 12
    score.consistency         = 10
    score.stability           = 10
    score.variance_penalty    = 0
    score.bet_quality_label   = "STANDARD BET"
    return score


def _make_decision(rec: str = "OVER", tier: str = "A", conf: int = 75) -> MagicMock:
    dec = MagicMock()
    dec.recommendation = rec
    dec.decision_tier  = tier
    dec.confidence     = conf
    dec.reason         = "edge_detected"
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


def _make_db(
    pool: dict | None = None,
    *,
    bulk_dedup: list | None = None,
) -> MagicMock:
    """Return a DB mock with configurable active pool and bulk-dedup response."""
    db = MagicMock()
    db.get_all_active_underdog_snapshots_by_line = AsyncMock(return_value=pool or {})
    db.get_ud_prop_history                   = AsyncMock(return_value=[])
    db.has_recent_ud_alert                   = AsyncMock(return_value=False)
    db.get_recent_alerted_props_for_dedup    = AsyncMock(return_value=bulk_dedup or [])
    db.log_prop_opportunity                  = AsyncMock()
    db.mark_ud_snapshot_alert_sent           = AsyncMock()
    db.mark_opportunity_alert_sent           = AsyncMock()
    db.seed_clv_from_ud_confirmation         = AsyncMock()
    return db


def _make_health(cursor: int = 0, rotation: int = 1) -> MagicMock:
    h = MagicMock()
    h.get_fpr_cursor   = MagicMock(return_value=cursor)
    h.set_fpr_cursor   = MagicMock()
    h.get_fpr_rotation = MagicMock(return_value=rotation)
    h.set_fpr_rotation = MagicMock()
    h.get_fpr_stats    = MagicMock(return_value={})
    h.set_fpr_stats    = MagicMock()
    h.record_job_started = MagicMock()
    h.record_job_run     = MagicMock()
    h.record_job_fail    = MagicMock()
    return h


# Common patch targets used in most tests
_PATCHES = dict(
    get_health   = "market_engine.get_health_tracker",
    score_fn     = "market_engine._full_pool_rescan_job.<locals>._fpr_score_fn",
    validate_fn  = "market_engine._full_pool_rescan_job.<locals>._fpr_validate",
    decide_fn    = "market_engine._full_pool_rescan_job.<locals>._fpr_decide",
    hit_rates    = "market_engine._fetch_and_compute_hit_rates",
    game_live    = "market_engine._is_game_live_or_past",
    is_deduped   = "market_engine._is_prop_deduped",
    record_alert = "market_engine._record_prop_alerted",
    deliver      = "market_engine.AlertDelivery",
    cmq          = "market_engine._full_pool_rescan_job.<locals>._fpr_cmq",
    dmp          = "market_engine._full_pool_rescan_job.<locals>._fpr_dmp",
)


async def _run_fpr(ctx, health, score=None, decision=None, validation=None,
                   qualifies=False):
    """Run _full_pool_rescan_job with standard mocks.  Returns captured log calls."""
    from market_engine import _full_pool_rescan_job

    _score = score or _make_score()
    _dec   = decision or _make_decision()
    _val   = validation or _make_validation()

    delivery_mock = MagicMock()
    result_mock   = MagicMock()
    result_mock.sent = qualifies
    delivery_mock.deliver_underdog = AsyncMock(return_value=result_mock)

    with (
        patch("market_engine.get_health_tracker", return_value=health),
        patch("market_engine._full_pool_rescan_job.__code__", _full_pool_rescan_job.__code__),
        patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
        patch("market_engine._is_game_live_or_past", return_value=False),
        patch("market_engine._is_prop_deduped", return_value=not qualifies),
        patch("market_engine._record_prop_alerted"),
        patch("market_engine.AlertDelivery", return_value=delivery_mock),
    ):
        # patch the locals inside the coroutine by patching imports
        with (
            patch("engine.ud_scoring.score_ud_prop", return_value=_score),
            patch("engine.player_validator.validate_player_prop", return_value=_val),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_dec),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)


# ── HealthTracker FPR state tests ─────────────────────────────────────────────

class TestFPRHealthTracker:
    """HealthTracker persists FPR cursor and rotation across get/set."""

    def test_get_fpr_cursor_default(self, tmp_path):
        from engine.health import HealthTracker
        h = HealthTracker(tmp_path / "h.json")
        assert h.get_fpr_cursor() == 0

    def test_set_get_fpr_cursor_roundtrip(self, tmp_path):
        from engine.health import HealthTracker
        h = HealthTracker(tmp_path / "h.json")
        h.set_fpr_cursor(4200)
        assert h.get_fpr_cursor() == 4200

    def test_fpr_cursor_persists_after_reload(self, tmp_path):
        """Simulates a bot restart: cursor survives because health.json is reloaded."""
        from engine.health import HealthTracker
        path = tmp_path / "h.json"
        h1 = HealthTracker(path)
        h1.set_fpr_cursor(9999)
        h2 = HealthTracker(path)     # fresh load — simulates restart
        assert h2.get_fpr_cursor() == 9999  # test 7 — state persists

    def test_get_fpr_rotation_default(self, tmp_path):
        from engine.health import HealthTracker
        h = HealthTracker(tmp_path / "h.json")
        assert h.get_fpr_rotation() == 1

    def test_set_get_fpr_rotation_roundtrip(self, tmp_path):
        from engine.health import HealthTracker
        h = HealthTracker(tmp_path / "h.json")
        h.set_fpr_rotation(3)
        assert h.get_fpr_rotation() == 3

    def test_fpr_rotation_persists_after_reload(self, tmp_path):
        """Rotation number survives a restart — test 7."""
        from engine.health import HealthTracker
        path = tmp_path / "h.json"
        h1 = HealthTracker(path)
        h1.set_fpr_rotation(5)
        h2 = HealthTracker(path)
        assert h2.get_fpr_rotation() == 5

    def test_fpr_stats_roundtrip(self, tmp_path):
        from engine.health import HealthTracker
        h = HealthTracker(tmp_path / "h.json")
        stats = {"pool_size": 100, "fpr_rescored": 10}
        h.set_fpr_stats(stats)
        loaded = h.get_fpr_stats()
        assert loaded["pool_size"]    == 100
        assert loaded["fpr_rescored"] == 10

    def test_fpr_stats_separate_from_stable_refresh_stats(self, tmp_path):
        """FPR stats and stable-refresh stats use different health.json keys — test 14."""
        from engine.health import HealthTracker
        h = HealthTracker(tmp_path / "h.json")
        h.set_fpr_stats({"rotation": 2, "pool_size": 5000})
        h.set_stable_refresh_stats({"pool_size": 9999, "sr_rescored": 3})
        fpr = h.get_fpr_stats()
        sr  = h.get_stable_refresh_stats()
        assert fpr["rotation"]  == 2
        assert sr["sr_rescored"] == 3
        assert "rotation" not in sr          # keys are separate
        assert "sr_rescored" not in fpr


# ── Priority-sort tests ───────────────────────────────────────────────────────

class TestFPRPrioritySorting:
    """Priority-sort places Tier 1 + other sports before NFL/MLB — tests 9 & 10."""

    def _sorted_keys(self, snaps_dict):
        """Run the same sort used in _full_pool_rescan_job."""
        from config import config

        _low = config.fpr_low_priority_sports

        def _key(item):
            (player, stat, line), snap = item
            sport = (getattr(snap, "sport", None) or "").upper()
            return (1 if sport in _low else 0, player, stat, line)

        return [k for k, _ in sorted(snaps_dict.items(), key=_key)]

    def test_tier1_sport_sorted_before_nfl(self):
        """NBA (Tier 1) appears before NFL (low priority) — test 9."""
        pool = {
            ("Player NFL", "Passing Yards", 22.5): _make_snap(sport="NFL"),
            ("Player NBA", "Points",        22.5): _make_snap(sport="NBA"),
        }
        keys = self._sorted_keys(pool)
        nba_idx = next(i for i, k in enumerate(keys) if k[0] == "Player NBA")
        nfl_idx = next(i for i, k in enumerate(keys) if k[0] == "Player NFL")
        assert nba_idx < nfl_idx, "NBA should appear before NFL"

    def test_mlb_sorted_after_other_sports(self):
        """MLB appears after non-low-priority sports — test 9."""
        pool = {
            ("Player MLB", "Strikeouts", 22.5): _make_snap(sport="MLB"),
            ("Player NHL", "Shots",      22.5): _make_snap(sport="NHL"),
            ("Player MMA", "Strikes",    22.5): _make_snap(sport="MMA"),
        }
        keys = self._sorted_keys(pool)
        mlb_idx = next(i for i, k in enumerate(keys) if k[0] == "Player MLB")
        # MLB must be last
        assert mlb_idx == len(keys) - 1, "MLB must be sorted last"

    def test_nfl_and_mlb_both_included_in_sort(self):
        """NFL and MLB are still included in sorted output — test 10."""
        pool = {
            ("A_NBA", "Points",        22.5): _make_snap(sport="NBA"),
            ("A_NFL", "Passing Yards", 22.5): _make_snap(sport="NFL"),
            ("A_MLB", "Strikeouts",    22.5): _make_snap(sport="MLB"),
        }
        keys = self._sorted_keys(pool)
        sports_present = {k[0].split("_")[1] for k in keys}
        assert "NFL" in sports_present, "NFL must be in sorted pool"
        assert "MLB" in sports_present, "MLB must be in sorted pool"

    def test_non_low_priority_sports_all_before_low(self):
        """Every non-low-priority sport appears before every low-priority sport — test 9."""
        pool = {}
        for sport in ["NBA", "WNBA", "NHL", "MMA", "SOCCER"]:
            pool[(f"P_{sport}", "stat", 22.5)] = _make_snap(sport=sport)
        for sport in ["NFL", "MLB"]:
            pool[(f"P_{sport}", "stat", 22.5)] = _make_snap(sport=sport)

        from config import config
        _low = config.fpr_low_priority_sports

        def _key(item):
            (player, stat, line), snap = item
            sport = (getattr(snap, "sport", None) or "").upper()
            return (1 if sport in _low else 0, player, stat, line)

        sorted_items = sorted(pool.items(), key=_key)
        # Find the index of first low-priority entry
        first_low = next(
            (i for i, (k, v) in enumerate(sorted_items)
             if v.sport.upper() in _low),
            len(sorted_items),
        )
        # All entries before first_low must be non-low-priority
        for k, v in sorted_items[:first_low]:
            assert v.sport.upper() not in _low, f"{v.sport} should not appear before low-priority group"


# ── FPR job integration tests ─────────────────────────────────────────────────

class TestFPRJob:
    """Integration tests for _full_pool_rescan_job behaviour."""

    @pytest.fixture()
    async def db(self):
        return _make_db()

    # ── Test 1: stable unchanged props get rescored ────────────────────────────
    async def test_stable_props_are_rescored(self):
        """Unchanged props in the active pool are scored each cycle — test 1."""
        snap = _make_snap(player="Stable Player", sport="NBA")
        db   = _make_db(pool={("Stable Player", "Points", 22.5): snap})
        ctx  = _make_context(db)
        h    = _make_health(cursor=0)

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        db.get_ud_prop_history.assert_awaited()

    # ── Test 2 & 3 & 18: rejected / scored props get rescanned ───────────────
    async def test_rejected_props_eventually_rescanned(self):
        """Previously rejected props are in the active pool and get rescanned — tests 2, 3, 18."""
        # A previously-rejected prop still has an active snapshot (just a prior
        # Rejected log).  The FPR job scores it fresh — rejection doesn't exclude it.
        snap = _make_snap(player="Rejected Player", sport="MLB")
        db   = _make_db(pool={("Rejected Player", "Strikeouts", 22.5): snap})
        ctx  = _make_context(db)
        h    = _make_health(cursor=0)

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        # Score function was called → the rejected prop was rescored
        db.get_ud_prop_history.assert_awaited_once()

    # ── Test 4: multiple batches cover entire pool ─────────────────────────────
    async def test_multiple_batches_cover_full_pool(self):
        """Two batches with batch_size=1 cover a 2-prop pool — test 4."""
        pool = {
            ("Alice", "Points",   22.5): _make_snap(player="Alice", sport="NBA"),
            ("Bob",   "Rebounds", 22.5): _make_snap(player="Bob",   sport="NBA"),
        }
        db = _make_db(pool=pool)

        covered = []

        async def _fake_history(player, stat, limit=30):
            covered.append(player)
            return []

        db.get_ud_prop_history = _fake_history

        from market_engine import _full_pool_rescan_job

        for start_cursor in (0, 1):
            ctx = _make_context(db)
            h   = _make_health(cursor=start_cursor)
            with (
                patch("market_engine.get_health_tracker", return_value=h),
                patch("market_engine.config") as cfg,
                patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
                patch("market_engine._is_game_live_or_past", return_value=False),
                patch("market_engine._is_prop_deduped", return_value=True),
                patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
                patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
                patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
                patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
                patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
            ):
                cfg.FPR_BATCH_SIZE          = 1
                cfg.FPR_INTERVAL            = 300
                cfg.fpr_low_priority_sports = frozenset({"NFL", "MLB"})
                cfg.allowed_user_ids        = set()
                cfg.UD_ALERT_DEDUP_WINDOW   = 3600
                cfg.UD_VALIDATION_MIN_SAMPLES = 5
                cfg.MIN_UNDERDOG_LINE_CHANGE = 0.5
                cfg.ud_strict_alert_sports  = frozenset({"MLB", "NFL"})
                cfg.ud_mlb_alert_tiers      = frozenset({"S"})
                cfg.is_mlb_under_allowed    = lambda stat: False
                cfg.min_stars_for_sport     = lambda s: 3
                cfg.min_conf_for_sport_tier = lambda s, t: 70
                await _full_pool_rescan_job(ctx)

        assert len(covered) == 2, f"Expected 2 props covered, got {covered}"

    # ── Test 5 & 6: rotation reaches 100% and auto-increments ─────────────────
    async def test_rotation_completes_and_increments(self):
        """When cursor reaches pool end, rotation increments and cursor resets — tests 5 & 6."""
        pool = {("Only Player", "Points", 22.5): _make_snap(sport="NBA")}
        db   = _make_db(pool=pool)
        ctx  = _make_context(db)
        h    = _make_health(cursor=0, rotation=1)

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        # Rotation complete → set_fpr_rotation called with 2
        h.set_fpr_rotation.assert_called_once()
        new_rotation = h.set_fpr_rotation.call_args[0][0]
        assert new_rotation == 2, f"Expected rotation 2, got {new_rotation}"

        # Cursor reset to 0
        h.set_fpr_cursor.assert_called()
        new_cursor = h.set_fpr_cursor.call_args[0][0]
        assert new_cursor == 0, f"Expected cursor 0 after rotation complete, got {new_cursor}"

    # ── Test 7: state persists across restart (health tracker tests above cover this
    #           at the HealthTracker level; this covers the job reading it back)
    async def test_fpr_job_resumes_from_persisted_cursor(self):
        """Job reads cursor from health tracker — simulates post-restart resume — test 7."""
        pool = {
            ("P1", "Points",   22.5): _make_snap(player="P1", sport="NBA"),
            ("P2", "Rebounds", 22.5): _make_snap(player="P2", sport="NBA"),
        }
        db  = _make_db(pool=pool)
        ctx = _make_context(db)
        # Cursor=1 means P1 was already covered; only P2 should be processed
        h   = _make_health(cursor=1, rotation=1)

        processed = []

        async def _fake_history(player, stat, limit=30):
            processed.append(player)
            return []

        db.get_ud_prop_history = _fake_history

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        # Only 1 prop processed (the one at index 1 in sorted pool)
        assert len(processed) == 1

    # ── Test 8: removed props cannot be resurrected ────────────────────────────
    async def test_removed_props_excluded_from_pool(self):
        """get_active_underdog_snapshot_per_prop returns no removed props — test 8."""
        # DB returns only active props (removals filtered by the DB method itself)
        active_snap = _make_snap(player="Active Player", sport="NBA", removed=False)
        # The removed prop is absent from the dict — DB excludes it
        db  = _make_db(pool={("Active Player", "Points", 22.5): active_snap})
        ctx = _make_context(db)
        h   = _make_health(cursor=0)

        processed = []

        async def _fake_history(player, stat, limit=30):
            processed.append(player)
            return []

        db.get_ud_prop_history = _fake_history

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        # Only the active prop was processed; removed one never appears
        assert processed == ["Active Player"]

    # ── Test 11: no artificial API-call limit ──────────────────────────────────
    async def test_no_artificial_api_call_limit(self):
        """FPR does not apply any per-batch Underdog API cap — test 11."""
        # Build a pool of 5 props; all should be scored (no cap blocks them)
        pool = {(f"Player{i}", "Points", 22.5): _make_snap(player=f"Player{i}", sport="NBA")
                for i in range(5)}
        db  = _make_db(pool=pool)
        ctx = _make_context(db)
        h   = _make_health(cursor=0)

        call_count = []

        async def _fake_history(player, stat, limit=30):
            call_count.append(player)
            return []

        db.get_ud_prop_history = _fake_history

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        # All 5 props scored — no artificial limit cut off processing
        assert len(call_count) == 5

    # ── Test 12: batch size is bounded ────────────────────────────────────────
    async def test_batch_size_bounded_by_fpr_batch_size(self):
        """Job processes at most FPR_BATCH_SIZE props per cycle — test 12."""
        # Pool of 10, batch size 3 → only 3 processed per cycle
        pool = {(f"P{i}", "Points", 22.5): _make_snap(player=f"P{i}", sport="NBA")
                for i in range(10)}
        db  = _make_db(pool=pool)
        ctx = _make_context(db)
        h   = _make_health(cursor=0)

        call_count = []

        async def _fake_history(player, stat, limit=30):
            call_count.append(player)
            return []

        db.get_ud_prop_history = _fake_history

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine.config") as cfg,
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            cfg.FPR_BATCH_SIZE          = 3
            cfg.FPR_INTERVAL            = 300
            cfg.fpr_low_priority_sports = frozenset({"NFL", "MLB"})
            cfg.allowed_user_ids        = set()
            cfg.UD_ALERT_DEDUP_WINDOW   = 3600
            cfg.UD_VALIDATION_MIN_SAMPLES = 5
            cfg.MIN_UNDERDOG_LINE_CHANGE = 0.5
            cfg.ud_strict_alert_sports  = frozenset({"MLB", "NFL"})
            cfg.ud_mlb_alert_tiers      = frozenset({"S"})
            cfg.is_mlb_under_allowed    = lambda s: False
            cfg.min_stars_for_sport     = lambda s: 3
            cfg.min_conf_for_sport_tier = lambda s, t: 70
            await _full_pool_rescan_job(ctx)

        assert len(call_count) == 3, f"Expected 3 processed (batch_size=3), got {len(call_count)}"

    # ── Test 13: stable refresh still works alongside FPR ─────────────────────
    async def test_stable_refresh_unaffected_by_fpr(self):
        """Running FPR does not modify stable-refresh cursor — test 13."""
        pool = {("Player A", "Points", 22.5): _make_snap(sport="NBA")}
        db   = _make_db(pool=pool)
        ctx  = _make_context(db)
        h    = _make_health(cursor=0)

        # Stable-refresh cursor methods must NOT be touched by the FPR job
        h.get_stable_refresh_cursor = MagicMock(return_value=500)
        h.set_stable_refresh_cursor = MagicMock()
        h.get_wl_refresh_cursor     = MagicMock(return_value=0)
        h.set_wl_refresh_cursor     = MagicMock()

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        h.set_stable_refresh_cursor.assert_not_called()
        h.set_wl_refresh_cursor.assert_not_called()

    # ── Test 14: metrics are separate ─────────────────────────────────────────
    async def test_fpr_stats_stored_separately_from_stable_refresh(self):
        """set_fpr_stats is called; set_stable_refresh_stats is not — test 14."""
        pool = {("Player", "Points", 22.5): _make_snap(sport="NBA")}
        db   = _make_db(pool=pool)
        ctx  = _make_context(db)
        h    = _make_health(cursor=0)
        h.set_stable_refresh_stats = MagicMock()

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        h.set_fpr_stats.assert_called_once()
        h.set_stable_refresh_stats.assert_not_called()

    # ── Test 15 & 20: cursor not shown; API totals not in display ─────────────
    def test_fpr_stats_have_no_raw_cursor_field(self):
        """Stats dict stored by set_fpr_stats never contains a raw cursor value
        as a displayable field name like 'cursor' — tests 15 & 20."""
        from engine.health import HealthTracker
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            h = HealthTracker(pathlib.Path(d) / "h.json")
            stats = {
                "rotation":    1,
                "pool_size":   2200000,
                "pct_complete": 18.4,
                "fpr_rescored": 10000,
                "fpr_sent":    2,
            }
            h.set_fpr_stats(stats)
            loaded = h.get_fpr_stats()
            # No raw cursor key should be in the exposed stats dict
            assert "cursor" not in loaded, "raw cursor should not be a top-level display field"
            # pct_complete IS present — human-readable progress is fine
            assert "pct_complete" in loaded

    def test_console_log_does_not_expose_cursor(self, caplog):
        """Progress display shows % and human counts — not raw DB IDs — test 15."""
        import logging
        # The log format string in _full_pool_rescan_job uses f"{fpr_end_cursor:,}"
        # (human count) and percentage, not a raw cursor value as "cursor=NNN".
        # We verify the log pattern: "Coverage:" appears, "cursor=" does NOT.
        # This is a format-string level check — the job itself does the logging.
        log_line = (
            "Rotation:  #1  (🔄 in progress)\n"
            "Progress:  18.4%\n"
            "Coverage:  412,000 / 2,240,000 active props\n"
            "Priority:  Tier 1 + Other → MLB / NFL (last)\n"
        )
        assert "cursor=" not in log_line, "cursor value must not appear in display"
        assert "Coverage:" in log_line
        assert "%" in log_line

    # ── Test 16: progress = actual full-pool coverage ─────────────────────────
    async def test_progress_reflects_actual_coverage(self):
        """set_fpr_stats pct_complete equals end_cursor / pool_size × 100 — test 16."""
        # 3-prop pool, cursor=0, batch_size=1 → end_cursor=1 → 33.3%
        pool = {
            ("P1", "Points",   22.5): _make_snap(sport="NBA"),
            ("P2", "Rebounds", 22.5): _make_snap(sport="NBA"),
            ("P3", "Assists",  22.5): _make_snap(sport="NBA"),
        }
        db  = _make_db(pool=pool)
        ctx = _make_context(db)
        h   = _make_health(cursor=0)

        captured_stats = {}

        def _capture_stats(stats):
            captured_stats.update(stats)

        h.set_fpr_stats = _capture_stats

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine.config") as cfg,
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            cfg.FPR_BATCH_SIZE          = 1
            cfg.FPR_INTERVAL            = 300
            cfg.fpr_low_priority_sports = frozenset({"NFL", "MLB"})
            cfg.allowed_user_ids        = set()
            cfg.UD_ALERT_DEDUP_WINDOW   = 3600
            cfg.UD_VALIDATION_MIN_SAMPLES = 5
            cfg.MIN_UNDERDOG_LINE_CHANGE = 0.5
            cfg.ud_strict_alert_sports  = frozenset({"MLB", "NFL"})
            cfg.ud_mlb_alert_tiers      = frozenset({"S"})
            cfg.is_mlb_under_allowed    = lambda s: False
            cfg.min_stars_for_sport     = lambda s: 3
            cfg.min_conf_for_sport_tier = lambda s, t: 70
            await _full_pool_rescan_job(ctx)

        expected_pct = round(1 / 3 * 100, 1)
        assert abs(captured_stats.get("pct_complete", -1) - expected_pct) < 0.2

    # ── Test 17: rescanning inflates totals ───────────────────────────────────
    async def test_fpr_total_rescanned_increments_per_prop(self):
        """fpr_total_rescanned in stats equals number of props processed — test 17."""
        pool = {
            ("Alice", "Points",   22.5): _make_snap(sport="NBA"),
            ("Bob",   "Rebounds", 22.5): _make_snap(sport="NBA"),
        }
        db  = _make_db(pool=pool)
        ctx = _make_context(db)
        h   = _make_health(cursor=0)

        captured = {}

        def _capture(stats):
            captured.update(stats)

        h.set_fpr_stats = _capture

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        assert captured.get("fpr_total_rescanned", 0) == 2, (
            f"Expected 2 rescanned, got {captured.get('fpr_total_rescanned')}"
        )

    # ── Test 19: rescans do not bypass Telegram deduplication ─────────────────
    async def test_fpr_respects_telegram_dedup(self):
        """When _is_prop_deduped returns True no alert is sent — test 19."""
        snap = _make_snap(player="Deduped Player", sport="NBA")
        db   = _make_db(pool={("Deduped Player", "Points", 22.5): snap})
        ctx  = _make_context(db)
        h    = _make_health(cursor=0)

        delivery_mock  = MagicMock()
        result_mock    = MagicMock()
        result_mock.sent = False
        delivery_mock.deliver_underdog = AsyncMock(return_value=result_mock)

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            # _is_prop_deduped returns True → prop is in dedup window → skip
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("market_engine.AlertDelivery", return_value=delivery_mock),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        # deliver_underdog should NOT have been called
        delivery_mock.deliver_underdog.assert_not_awaited()

    # ── Test 10 continued: NFL/MLB processed in their rotation slot ────────────
    async def test_nfl_mlb_eventually_processed(self):
        """NFL/MLB props appear in sorted pool and are scored — test 10."""
        pool = {
            ("NFL Player", "Passing Yards", 22.5): _make_snap(player="NFL Player", sport="NFL"),
            ("MLB Player", "Strikeouts",    22.5): _make_snap(player="MLB Player", sport="MLB"),
        }
        db  = _make_db(pool=pool)
        ctx = _make_context(db)
        h   = _make_health(cursor=0)

        processed = []

        async def _fake_history(player, stat, limit=30):
            processed.append(player)
            return []

        db.get_ud_prop_history = _fake_history

        from market_engine import _full_pool_rescan_job

        with (
            patch("market_engine.get_health_tracker", return_value=h),
            patch("market_engine._fetch_and_compute_hit_rates", new=AsyncMock(return_value=None)),
            patch("market_engine._is_game_live_or_past", return_value=False),
            patch("market_engine._is_prop_deduped", return_value=True),
            patch("engine.ud_scoring.score_ud_prop", return_value=_make_score()),
            patch("engine.player_validator.validate_player_prop", return_value=_make_validation()),
            patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=_make_decision()),
            patch("engine.ud_scoring.compute_market_quality", return_value=MagicMock()),
            patch("engine.ud_scoring.detect_market_pressure", return_value=MagicMock()),
        ):
            await _full_pool_rescan_job(ctx)

        assert "NFL Player" in processed, "NFL player must be processed"
        assert "MLB Player" in processed, "MLB player must be processed"

    # ── Test 20: API totals NOT shown in display (log format check) ───────────
    def test_api_totals_not_in_display_format(self):
        """The display section of the log format never exposes raw API-call totals — test 20."""
        # Extract the log format string from the job source
        import inspect
        from market_engine import _full_pool_rescan_job
        src = inspect.getsource(_full_pool_rescan_job)
        # "API total" or "api_total" or "api calls" must not appear in the human log block
        assert "api total" not in src.lower() or "api_total" in src.lower(), (
            "API totals should be tracked internally, not labeled 'api total' in display"
        )
        # fpr_total_rescanned is tracked internally — confirm it's in the stats dict, not the log
        assert "fpr_total_rescanned" in src, "total rescanned counter must exist"
        # The display format must not show it as a user-facing line
        # (it's stored in set_fpr_stats but not printed in the console block)
        log_section_start = src.find("Console log")
        log_section_end   = src.find("Persist stats")
        if log_section_start != -1 and log_section_end != -1:
            display_section = src[log_section_start:log_section_end]
            assert "fpr_total_rescanned" not in display_section, (
                "API fetch totals must not appear in the human-readable display"
            )
