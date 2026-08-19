"""
test_tier2_telegram_block.py

Regression tests for the temporary Tier 2 Telegram delivery block.

Spec:
  - NBA / MLB / NFL props are scanned, scored, stored, and ranked normally.
  - Telegram delivery is SUPPRESSED for those sports via:
      (a) _is_tier2_sport guard at all 4 deliver_underdog() call sites
      (b) FINAL backstop inside deliver_underdog() in alerts.py
      (c) Guards on every direct broadcast_alert() call with a sport field
  - All other (Tier 1) sports are delivered as before.
  - No 8/2 cap restored, no BQ/MQ thresholds changed, no scanning reduced.
"""
from __future__ import annotations

import asyncio
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

    @pytest.mark.parametrize("sport", ["NCAA", "NCAAF", "CFB"])
    def test_college_football_aliases_are_suppressed(self, sport):
        assert me._is_tier2_sport(sport) is True

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


# ── Unit: deliver_underdog() final backstop in alerts.py ─────────────────────

class TestDeliverUnderdogBackstop:
    """
    deliver_underdog() must return sent=False for any Tier 2 sport,
    regardless of which call site invoked it.
    """

    def _make_delivery(self):
        from alerts import AlertDelivery, DeliveryResult
        db  = MagicMock()
        db.count_today_underdog_alerts = AsyncMock(return_value=0)
        bot = MagicMock()
        return AlertDelivery(db=db, bot=bot, chat_ids=[12345])

    @pytest.mark.asyncio
    async def test_nba_sport_returns_not_sent(self):
        """Direct deliver_underdog() call with sport='NBA' must return sent=False."""
        delivery = self._make_delivery()
        result = await delivery.deliver_underdog(
            player_name="LeBron James", team="LAL", sport="NBA",
            stat_type="Points", old_line=20.0, new_line=21.5,
        )
        assert result.sent is False
        assert "NBA" in result.filtered_reason or "Tier 2" in result.filtered_reason

    @pytest.mark.asyncio
    async def test_mlb_sport_returns_not_sent(self):
        """Direct deliver_underdog() call with sport='MLB' must return sent=False."""
        delivery = self._make_delivery()
        result = await delivery.deliver_underdog(
            player_name="Aaron Judge", team="NYY", sport="MLB",
            stat_type="Home Runs", old_line=0.5, new_line=0.5,
        )
        assert result.sent is False

    @pytest.mark.asyncio
    async def test_nfl_sport_returns_not_sent(self):
        """Direct deliver_underdog() call with sport='NFL' must return sent=False."""
        delivery = self._make_delivery()
        result = await delivery.deliver_underdog(
            player_name="Patrick Mahomes", team="KC", sport="NFL",
            stat_type="Passing Yards", old_line=250.5, new_line=255.5,
        )
        assert result.sent is False

    @pytest.mark.parametrize("sport", ["NCAA", "NCAAF", "CFB"])
    @pytest.mark.asyncio
    async def test_college_football_alias_returns_not_sent(self, sport):
        delivery = self._make_delivery()
        result = await delivery.deliver_underdog(
            player_name="College Player", team="COL", sport=sport,
            stat_type="Passing Yards", old_line=250.5, new_line=255.5,
        )
        assert result.sent is False

    @pytest.mark.asyncio
    async def test_nba_lowercase_returns_not_sent(self):
        """Lowercase sport='nba' must also be blocked."""
        delivery = self._make_delivery()
        result = await delivery.deliver_underdog(
            player_name="Test Player", team="TM", sport="nba",
            stat_type="Points", old_line=20.0, new_line=20.0,
        )
        assert result.sent is False

    def test_tier1_nhl_not_blocked_by_constant(self):
        """Sport='NHL' must NOT be in the _TIER2_SPORTS_BLOCK constant."""
        import alerts as alerts_mod
        assert "NHL" not in alerts_mod._TIER2_SPORTS_BLOCK
        assert "WNBA" not in alerts_mod._TIER2_SPORTS_BLOCK
        assert "Soccer" not in alerts_mod._TIER2_SPORTS_BLOCK

    def test_tier2_constant_contains_correct_sports(self):
        """Existing blocks remain, with college-football aliases added."""
        import alerts as alerts_mod
        assert alerts_mod._TIER2_SPORTS_BLOCK == frozenset({
            "NBA", "MLB", "NFL", "NCAA", "NCAAF", "CFB",
        })

    @pytest.mark.asyncio
    async def test_backstop_prevents_send_message_for_nba(self):
        """bot.send_message must never be called for NBA even when all other gates pass."""
        from alerts import AlertDelivery
        db  = MagicMock()
        db.count_today_underdog_alerts = AsyncMock(return_value=0)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        delivery = AlertDelivery(db=db, bot=bot, chat_ids=[12345])
        await delivery.deliver_underdog(
            player_name="Test Player", team="TM", sport="NBA",
            stat_type="Points", old_line=20.0, new_line=21.5,
        )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_backstop_prevents_send_message_for_mlb(self):
        """bot.send_message must never be called for MLB."""
        from alerts import AlertDelivery
        db  = MagicMock()
        db.count_today_underdog_alerts = AsyncMock(return_value=0)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        delivery = AlertDelivery(db=db, bot=bot, chat_ids=[12345])
        await delivery.deliver_underdog(
            player_name="Test Player", team="TM", sport="MLB",
            stat_type="Hits", old_line=0.5, new_line=0.5,
        )
        bot.send_message.assert_not_called()


# ── Source-code audit: every deliver_underdog path has the guard ───────────────

class TestSourceAudit:
    """
    Verify:
      1. alerts.py has the final backstop inside deliver_underdog()
      2. market_engine.py: all 4 deliver_underdog() call sites have the guard
      3. market_engine.py: all direct broadcast_alert() calls with sport have the guard
      4. engine/player_prop_market.py: direct broadcast_alert() call has the guard
    """

    def _me_src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "market_engine.py").read_text()

    def _alerts_src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "alerts.py").read_text()

    def _ppm_src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "engine" / "player_prop_market.py").read_text()

    def _assert_guard_before(self, src: str, anchor: str, label: str,
                              marker: str = "_is_tier2_sport",
                              window: int = 1500) -> None:
        idx = src.find(anchor)
        assert idx != -1, f"{label}: anchor '{anchor}' not found"
        window_src = src[max(0, idx - window): idx]
        assert marker in window_src, (
            f"{label}: '{marker}' not found within {window} chars before '{anchor}'"
        )

    # ── alerts.py final backstop ─────────────────────────────────────────────

    def test_alerts_py_has_final_backstop(self):
        """alerts.py deliver_underdog() must contain the Tier 2 final backstop."""
        src = self._alerts_src()
        assert "FINAL Tier 2 Telegram backstop" in src or "T2_BLOCK" in src, (
            "alerts.py deliver_underdog() is missing the final Tier 2 backstop block."
        )

    def test_alerts_py_backstop_uses_sport_param(self):
        """The backstop must gate on the sport parameter."""
        src = self._alerts_src()
        # Check that NBA/MLB/NFL are explicitly listed in the backstop set
        assert '"NBA"' in src and '"MLB"' in src and '"NFL"' in src

    def test_alerts_py_backstop_returns_delivery_result(self):
        """The backstop must return a DeliveryResult (not raise)."""
        src = self._alerts_src()
        assert "filtered_reason" in src and "Tier 2 sport blocked" in src

    # ── market_engine.py deliver_underdog call sites ────────────────────────

    def test_delivery_queue_path_has_block(self):
        src = self._me_src()
        self._assert_guard_before(src, "await _dq_delivery.deliver_underdog(", "delivery-queue")

    def test_stable_refresh_path_has_block(self):
        src = self._me_src()
        self._assert_guard_before(src, "await _sr_delivery.deliver_underdog(", "stable-refresh")

    def test_watchlist_path_has_block(self):
        src = self._me_src()
        self._assert_guard_before(src, "await _wl_delivery.deliver_underdog(", "watchlist")

    def test_fpr_path_has_block(self):
        src = self._me_src()
        self._assert_guard_before(src, "await _fpr_delivery.deliver_underdog(", "fpr")

    # ── market_engine.py direct broadcast_alert() call sites ────────────────

    def test_inefficiency_broadcast_has_tier2_block(self):
        """format_inefficiency_alert broadcast must be preceded by Tier 2 guard."""
        src = self._me_src()
        self._assert_guard_before(
            src, "format_inefficiency_alert(ineff, cr)", "inefficiency-broadcast",
            marker="Tier 2 Telegram block",
        )

    def test_steam_broadcast_has_tier2_block(self):
        """format_steam_multibook_alert broadcast must be preceded by Tier 2 guard."""
        src = self._me_src()
        self._assert_guard_before(
            src, "format_steam_multibook_alert(", "steam-broadcast",
            marker="Tier 2 Telegram block",
        )

    def test_clv_broadcast_has_tier2_block(self):
        """format_clv_opportunity_alert broadcast must be preceded by Tier 2 guard."""
        src = self._me_src()
        self._assert_guard_before(
            src, "format_clv_opportunity_alert(opp)", "clv-broadcast",
            marker="Tier 2 Telegram block",
        )

    # ── engine/player_prop_market.py ────────────────────────────────────────

    def test_player_prop_market_broadcast_has_tier2_block(self):
        """player_prop_market broadcast_alert must be preceded by Tier 2 guard."""
        src = self._ppm_src()
        self._assert_guard_before(
            src, "await broadcast_alert(bot, chat_ids, message)", "player-prop-market",
            marker="Tier 2 Telegram block",
            window=400,
        )

    # ── Counts ───────────────────────────────────────────────────────────────

    def test_block_uses_is_tier2_sport_helper_4_times(self):
        """_is_tier2_sport must appear ≥4 times in market_engine.py."""
        src = self._me_src()
        count = src.count("_is_tier2_sport")
        assert count >= 4, f"Expected ≥4 occurrences; found {count}"

    def test_block_comment_count_matches_delivery_paths(self):
        """There must be ≥4 'Tier 2 Telegram block' markers in market_engine.py."""
        src = self._me_src()
        count = src.count("Tier 2 Telegram block")
        assert count >= 4, f"Expected ≥4 block markers; found {count}"

    def test_all_deliver_underdog_calls_counted(self):
        """Exactly 4 named deliver_underdog() await calls in market_engine.py."""
        src = self._me_src()
        calls = (
            src.count("await _dq_delivery.deliver_underdog(")
            + src.count("await _sr_delivery.deliver_underdog(")
            + src.count("await _wl_delivery.deliver_underdog(")
            + src.count("await _fpr_delivery.deliver_underdog(")
        )
        assert calls == 4, f"Expected exactly 4; found {calls}"


# ── Integration: delivery blocked for Tier 2 via underdog_job ────────────────

class TestTier2DeliveryBlocked:
    """
    Run underdog_job with Tier 2 props; deliver_underdog must never be called.
    Props must still be stored (scanning/storage unchanged).
    """

    @pytest.mark.asyncio
    async def test_nba_prop_blocked(self):
        db = _make_db()
        delivery = await _run_job([_snap(sport="NBA")], db)
        delivery.deliver_underdog.assert_not_called()
        db.save_underdog_snapshots_bulk.assert_called()

    @pytest.mark.asyncio
    async def test_mlb_prop_blocked(self):
        db = _make_db()
        delivery = await _run_job([_snap(sport="MLB", stat="Hits", line=0.5)], db)
        delivery.deliver_underdog.assert_not_called()

    @pytest.mark.asyncio
    async def test_nfl_prop_blocked(self):
        db = _make_db()
        delivery = await _run_job(
            [_snap(sport="NFL", stat="Passing Yards", line=225.5)], db
        )
        delivery.deliver_underdog.assert_not_called()

    @pytest.mark.asyncio
    async def test_nba_lowercase_blocked(self):
        db = _make_db()
        delivery = await _run_job([_snap(sport="nba")], db)
        delivery.deliver_underdog.assert_not_called()

    @pytest.mark.asyncio
    async def test_tier2_prop_still_stored(self):
        db = _make_db()
        await _run_job([_snap(sport="NBA")], db)
        db.save_underdog_snapshots_bulk.assert_called()

    @pytest.mark.asyncio
    async def test_multiple_tier2_all_blocked(self):
        db = _make_db()
        snaps = [
            _snap("LeBron James",    "Points",        20.5, sport="NBA"),
            _snap("Aaron Judge",     "Home Runs",      0.5, sport="MLB"),
            _snap("Patrick Mahomes", "Passing Yards", 250.5, sport="NFL"),
        ]
        delivery = await _run_job(snaps, db)
        delivery.deliver_underdog.assert_not_called()


# ── Tier 1 sports not blocked ─────────────────────────────────────────────────

class TestTier1DeliveryUnchanged:
    def test_nhl_not_blocked(self):       assert me._is_tier2_sport("NHL")   is False
    def test_wnba_not_blocked(self):      assert me._is_tier2_sport("WNBA")  is False
    def test_soccer_not_blocked(self):    assert me._is_tier2_sport("Soccer")is False
    def test_tennis_not_blocked(self):    assert me._is_tier2_sport("Tennis")is False
    def test_cs2_not_blocked(self):       assert me._is_tier2_sport("CS2")   is False
    def test_lol_not_blocked(self):       assert me._is_tier2_sport("LOL")   is False
    def test_dota_not_blocked(self):      assert me._is_tier2_sport("DOTA")  is False


# ── Scanning / scoring / storage code paths unchanged ────────────────────────

class TestScanningUnchanged:
    def _src(self) -> str:
        import pathlib
        return (pathlib.Path(__file__).parent.parent / "market_engine.py").read_text()

    def test_score_ud_prop_still_called(self):
        assert "score_ud_prop" in self._src()

    def test_validate_player_prop_still_called(self):
        assert "validate_player_prop" in self._src()

    def test_make_ud_bet_decision_still_called(self):
        assert "make_ud_bet_decision" in self._src()

    def test_save_underdog_snapshots_bulk_still_called(self):
        assert "save_underdog_snapshots_bulk" in self._src()

    def test_stable_refresh_job_still_present(self):
        assert "_stable_refresh_job" in self._src()

    def test_watchlist_processing_still_present(self):
        assert "_wl_qualifies" in self._src()

    def test_fpr_still_present(self):
        assert "_full_pool_rescan_job" in self._src()

    def test_tier2_sports_set_unchanged(self):
        """Existing Tier 2 sports remain, with football aliases suppressed."""
        assert me._TIER2_SPORTS == frozenset({
            "NBA", "MLB", "NFL", "NCAA", "NCAAF", "CFB",
        })


# ── Restart / recovery cannot replay Tier 2 alerts ───────────────────────────

class TestRestartRecovery:
    """
    Verify that the final backstop in deliver_underdog() catches any replay
    that might occur after a bot restart (e.g., stable refresh re-evaluating
    a previously alerted Tier 2 prop).
    """

    @pytest.mark.asyncio
    async def test_restart_nba_replay_blocked(self):
        """An NBA prop re-evaluated after restart must still be blocked."""
        from alerts import AlertDelivery
        db  = MagicMock()
        db.count_today_underdog_alerts = AsyncMock(return_value=0)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        delivery = AlertDelivery(db=db, bot=bot, chat_ids=[12345])
        # Simulate a prop that would have been alerted before restart
        result = await delivery.deliver_underdog(
            player_name="Replay Player", team="TM", sport="NBA",
            stat_type="Points", old_line=20.0, new_line=20.0,
            new_prop=False, standing=True,
        )
        assert result.sent is False
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_mlb_replay_blocked(self):
        """An MLB prop re-evaluated after restart must still be blocked."""
        from alerts import AlertDelivery
        db  = MagicMock()
        db.count_today_underdog_alerts = AsyncMock(return_value=0)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        delivery = AlertDelivery(db=db, bot=bot, chat_ids=[12345])
        result = await delivery.deliver_underdog(
            player_name="Replay Player", team="TM", sport="MLB",
            stat_type="Hits", old_line=0.5, new_line=0.5,
        )
        assert result.sent is False
        bot.send_message.assert_not_called()
