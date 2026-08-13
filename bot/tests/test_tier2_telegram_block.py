"""
test_tier2_telegram_block.py

Regression tests for the temporary Tier 2 Telegram delivery block.

Spec:
  - NBA / MLB / NFL props are scanned, scored, stored, and ranked normally.
  - Telegram delivery is SUPPRESSED for those sports.
  - All other (Tier 1) sports are delivered as before.
  - The block must cover every deliver_underdog() path:
      delivery queue, stable refresh, watchlist, full-pool rescan.
  - No 8/2 cap restored, no BQ/MQ thresholds changed, no scanning reduced.
"""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me


# ── Helpers ────────────────────────────────────────────────────────────────────

def _snap(player: str = "Test Player", stat: str = "Points",
          line: float = 20.0, sport: str = "NBA", *,
          removed: bool = False) -> MagicMock:
    s = MagicMock()
    s.sportsbook = "Underdog"
    s.player     = player
    s.sport      = sport
    s.line       = line
    s.team       = "TeamA"
    s.event      = "game-001"
    s.game_time  = None
    s.is_pickem  = True
    s.selection  = (
        f"[REMOVED] {player} {stat} {line}" if removed else f"{player} {stat} {line}"
    )
    return s


def _make_db() -> MagicMock:
    db = MagicMock()
    db.get_known_underdog_prop_keys           = AsyncMock(return_value=set())
    db.get_latest_underdog_snapshot_per_prop  = AsyncMock(return_value={})
    db.count_today_underdog_alerts            = AsyncMock(return_value=0)
    db.save_underdog_snapshot                 = AsyncMock()
    db.save_underdog_snapshots_bulk           = AsyncMock()
    db.get_ud_prop_history                    = AsyncMock(return_value=[])
    db.log_prop_opportunity                   = AsyncMock()
    db.log_prop_candidate                     = AsyncMock()
    db.log_prop_candidate_batch               = AsyncMock()
    db.get_recent_alerted_props_for_dedup     = AsyncMock(return_value=[])
    db.sync_underdog_snapshots_to_prop_history= AsyncMock(return_value=None)
    db.update_prop_lifecycle_state            = AsyncMock()
    db.set_stable_refresh_stats               = AsyncMock()
    db.mark_ud_snapshot_alert_sent            = AsyncMock()
    db.mark_opportunity_alert_sent            = AsyncMock()
    db.get_active_underdog_snapshots          = AsyncMock(return_value={})
    db.get_recently_alerted_prop_keys         = AsyncMock(return_value=set())
    db.get_prop_line_history                  = AsyncMock(return_value=[])
    db.update_scan_cycle_log                  = AsyncMock()
    db.record_scan_cycle                      = AsyncMock()
    return db


def _make_context(db) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {"db": db}
    ctx.bot      = MagicMock()
    return ctx


async def _run_job(snaps, db, *, deliver_result=None) -> MagicMock:
    """Run underdog_job with mocked registry and delivery. Returns the delivery mock."""
    from alerts import DeliveryResult
    if deliver_result is None:
        deliver_result = DeliveryResult(sent=True, recipients_sent=1)

    registry = MagicMock()
    registry.fetch_pickem = AsyncMock(return_value=snaps)
    ctx = _make_context(db)

    with patch.object(me, "_registry", registry):
        with patch.object(me, "_cold_start_done", True):
            with patch("market_engine.AlertDelivery") as mock_cls:
                mock_delivery = MagicMock()
                mock_delivery.deliver_underdog = AsyncMock(return_value=deliver_result)
                mock_cls.return_value = mock_delivery
                with patch("market_engine.broadcast_alert",
                           new_callable=AsyncMock,
                           return_value={"sent": 1, "failed": 0}):
                    await me.underdog_job(ctx)
    return mock_delivery


# ── Unit: _is_tier2_sport ──────────────────────────────────────────────────────

class TestIsTier2Sport:
    """_is_tier2_sport must correctly classify Tier 1 vs Tier 2."""

    def test_nba_is_tier2(self):
        assert me._is_tier2_sport("NBA") is True

    def test_mlb_is_tier2(self):
        assert me._is_tier2_sport("MLB") is True

    def test_nfl_is_tier2(self):
        assert me._is_tier2_sport("NFL") is True

    def test_nba_lowercase_is_tier2(self):
        assert me._is_tier2_sport("nba") is True

    def test_mlb_mixed_case_is_tier2(self):
        assert me._is_tier2_sport("MlB") is True

    def test_nhl_is_tier1(self):
        assert me._is_tier2_sport("NHL") is False

    def test_wnba_is_tier1(self):
        assert me._is_tier2_sport("WNBA") is False

    def test_soccer_is_tier1(self):
        assert me._is_tier2_sport("Soccer") is False

    def test_cs_is_tier1(self):
        assert me._is_tier2_sport("CS2") is False

    def test_tennis_is_tier1(self):
        assert me._is_tier2_sport("Tennis") is False

    def test_empty_string_is_tier1(self):
        assert me._is_tier2_sport("") is False

    def test_none_handled_safely(self):
        assert me._is_tier2_sport(None) is False


# ── Source-code audit: every deliver_underdog path has the block ───────────────

class TestSourceAudit:
    """
    Verify each deliver_underdog() call site is preceded by a Tier 2 block
    comment in the market_engine source.
    """

    def _src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "market_engine.py").read_text()

    def _assert_guard_before(self, src: str, anchor: str, label: str,
                              window: int = 1500) -> None:
        """Assert _is_tier2_sport guard call appears before anchor within `window` chars."""
        idx = src.find(anchor)
        assert idx != -1, (
            f"{label}: anchor '{anchor}' not found in market_engine.py"
        )
        window_src = src[max(0, idx - window): idx]
        assert "_is_tier2_sport" in window_src, (
            f"{label}: _is_tier2_sport guard not found within {window} chars "
            f"before '{anchor}' — this delivery path may bypass the Tier 2 block."
        )

    def test_delivery_queue_path_has_block(self):
        src = self._src()
        self._assert_guard_before(
            src,
            "await _dq_delivery.deliver_underdog(",
            "delivery-queue",
        )

    def test_stable_refresh_path_has_block(self):
        src = self._src()
        self._assert_guard_before(
            src,
            "await _sr_delivery.deliver_underdog(",
            "stable-refresh",
        )

    def test_watchlist_path_has_block(self):
        src = self._src()
        self._assert_guard_before(
            src,
            "await _wl_delivery.deliver_underdog(",
            "watchlist",
        )

    def test_fpr_path_has_block(self):
        src = self._src()
        self._assert_guard_before(
            src,
            "await _fpr_delivery.deliver_underdog(",
            "full-pool-rescan",
        )

    def test_block_uses_is_tier2_sport_helper(self):
        """_is_tier2_sport must appear in the engine at least 4 times (one per path)."""
        src = self._src()
        count = src.count("_is_tier2_sport")
        assert count >= 4, (
            f"Expected _is_tier2_sport() to appear ≥4 times in market_engine.py; found {count}"
        )

    def test_block_comment_count_matches_delivery_paths(self):
        """There must be at least 4 Tier 2 block comments (one per delivery path)."""
        src = self._src()
        count = src.count("Tier 2 Telegram block")
        assert count >= 4, (
            f"Expected ≥4 'Tier 2 Telegram block' comments; found {count}"
        )


# ── Integration: delivery blocked for Tier 2 ─────────────────────────────────

class TestTier2DeliveryBlocked:
    """
    Run underdog_job with qualifying-looking Tier 2 props and assert
    deliver_underdog is NEVER called. Props must still be stored.
    """

    @pytest.mark.asyncio
    async def test_nba_prop_blocked(self):
        """NBA: deliver_underdog must not be called, but bulk storage must happen."""
        db = _make_db()
        delivery = await _run_job([_snap(sport="NBA")], db)
        delivery.deliver_underdog.assert_not_called()
        db.save_underdog_snapshots_bulk.assert_called()

    @pytest.mark.asyncio
    async def test_mlb_prop_blocked(self):
        """MLB: deliver_underdog must not be called."""
        db = _make_db()
        delivery = await _run_job([_snap(sport="MLB", stat="Hits", line=0.5)], db)
        delivery.deliver_underdog.assert_not_called()

    @pytest.mark.asyncio
    async def test_nfl_prop_blocked(self):
        """NFL: deliver_underdog must not be called."""
        db = _make_db()
        delivery = await _run_job(
            [_snap(sport="NFL", stat="Passing Yards", line=225.5)], db
        )
        delivery.deliver_underdog.assert_not_called()

    @pytest.mark.asyncio
    async def test_nba_lowercase_blocked(self):
        """Lowercase 'nba' must also be blocked."""
        db = _make_db()
        delivery = await _run_job([_snap(sport="nba")], db)
        delivery.deliver_underdog.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier2_prop_still_stored(self):
        """Tier 2 prop must still be saved to DB (monitoring unchanged)."""
        db = _make_db()
        await _run_job([_snap(sport="NBA")], db)
        db.save_underdog_snapshots_bulk.assert_called()

    @pytest.mark.asyncio
    async def test_multiple_tier2_all_blocked(self):
        """Multiple Tier 2 props across NBA/MLB/NFL — none reach Telegram."""
        db = _make_db()
        snaps = [
            _snap("LeBron James",    "Points",        20.5, sport="NBA"),
            _snap("Aaron Judge",     "Home Runs",      0.5, sport="MLB"),
            _snap("Patrick Mahomes", "Passing Yards", 250.5, sport="NFL"),
        ]
        delivery = await _run_job(snaps, db)
        delivery.deliver_underdog.assert_not_called()


# ── Tier 1 sports: delivery helper returns False (not blocked) ────────────────

class TestTier1DeliveryUnchanged:
    """
    Tier 1 props (non-NBA/MLB/NFL) must NOT be blocked by _is_tier2_sport.
    We verify the helper, since the full integration delivery depends on scoring.
    """

    def test_nhl_not_blocked(self):
        assert me._is_tier2_sport("NHL") is False

    def test_wnba_not_blocked(self):
        assert me._is_tier2_sport("WNBA") is False

    def test_soccer_not_blocked(self):
        assert me._is_tier2_sport("Soccer") is False

    def test_tennis_not_blocked(self):
        assert me._is_tier2_sport("Tennis") is False

    def test_cs2_not_blocked(self):
        assert me._is_tier2_sport("CS2") is False

    def test_esports_not_blocked(self):
        assert me._is_tier2_sport("LOL") is False

    def test_dota_not_blocked(self):
        assert me._is_tier2_sport("DOTA") is False


# ── Source: scanning / scoring / storage not removed ──────────────────────────

class TestScanningUnchanged:
    """Verify scanning, scoring, and storage code paths are intact."""

    def _src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "market_engine.py").read_text()

    def test_score_ud_prop_still_called(self):
        src = self._src()
        assert "score_ud_prop" in src

    def test_validate_player_prop_still_called(self):
        src = self._src()
        assert "validate_player_prop" in src

    def test_make_ud_bet_decision_still_called(self):
        src = self._src()
        assert "make_ud_bet_decision" in src

    def test_save_underdog_snapshots_bulk_still_called(self):
        src = self._src()
        assert "save_underdog_snapshots_bulk" in src

    def test_stable_refresh_job_still_present(self):
        src = self._src()
        assert "_stable_refresh_job" in src

    def test_watchlist_processing_still_present(self):
        src = self._src()
        assert "_wl_qualifies" in src

    def test_fpr_still_present(self):
        src = self._src()
        assert "_full_pool_rescan_job" in src

    def test_tier2_sports_set_unchanged(self):
        """_TIER2_SPORTS must still contain exactly NBA, MLB, NFL."""
        assert me._TIER2_SPORTS == frozenset({"NBA", "MLB", "NFL"})


# ── No bypass paths ───────────────────────────────────────────────────────────

class TestNoBypasses:
    """Confirm the expected number of deliver_underdog call sites."""

    def _src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "market_engine.py").read_text()

    def test_all_deliver_underdog_calls_counted(self):
        """There must be exactly 4 deliver_underdog() await calls in market_engine.py."""
        src = self._src()
        calls = (
            src.count("await _dq_delivery.deliver_underdog(")
            + src.count("await _sr_delivery.deliver_underdog(")
            + src.count("await _wl_delivery.deliver_underdog(")
            + src.count("await _fpr_delivery.deliver_underdog(")
        )
        assert calls == 4, (
            f"Expected exactly 4 deliver_underdog() await calls in market_engine.py, "
            f"found {calls}. A new path may have been added without a Tier 2 block."
        )

    def test_tier2_block_count_matches_delivery_paths(self):
        """Number of Tier 2 block comments must be ≥ 4 (one per delivery path)."""
        src = self._src()
        block_count = src.count("Tier 2 Telegram block")
        assert block_count >= 4, (
            f"Expected ≥4 Tier 2 block markers, found {block_count}."
        )
