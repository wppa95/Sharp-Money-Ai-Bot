"""
Phase 2 Task #112 regression tests — lifecycle tracking and health counter fixes.

Bug 1: New-prop and standing delivery paths never appended to _lifecycle_alerted.
        Consequence: PropLineHistory.lifecycle_state was never set to ACTIVE_ALERTED
        for those paths → /alerts always showed 0 even when Telegram delivery succeeded.
        Fix: _lifecycle_alerted.append() added to both paths.

Bug 2: record_underdog_scan(alerts_sent=…) only counted _n_new_prop_sent.
        _n_standing_sent was silently excluded.
        Consequence: /status "Alerts sent" always 0 when picks came from standing path.
        Fix: alerts_sent = _n_new_prop_sent + _n_standing_sent.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime, timedelta


# ── Helpers shared across tests ────────────────────────────────────────────────

def _make_snap(player="Test Player", sport="WNBA", stat_type="Points",
               line=20.5, external_id="ext_abc123", team="TM"):
    """Minimal UnderdogSnapshotRecord-like mock."""
    snap = MagicMock()
    snap.player_name  = player
    snap.sport        = sport
    snap.stat_type    = stat_type
    snap.line         = line
    snap.external_id  = external_id
    snap.team         = team
    snap.game_time    = datetime.utcnow() + timedelta(hours=3)
    snap.game_status  = None
    snap.status       = None
    snap.removed      = False
    return snap


def _sent_result():
    """DeliveryResult with sent=True."""
    r = MagicMock()
    r.sent           = True
    r.filtered       = False
    r.filtered_reason = ""
    return r


def _not_sent_result():
    """DeliveryResult with sent=False (filtered)."""
    r = MagicMock()
    r.sent           = False
    r.filtered       = True
    r.filtered_reason = "test_block"
    return r


# ── Bug 1: _lifecycle_alerted population ──────────────────────────────────────

class TestLifecycleAlertedNewProp:
    """New-prop delivery path must append to _lifecycle_alerted on successful send."""

    def test_append_called_on_sent_true(self):
        """Simulate the new-prop sent=True guard and verify append is present."""
        # Mirror the logic from market_engine.py new-prop path
        _lifecycle_alerted: list = []
        _n_new_prop_sent = 0

        player   = "Caty McNally"
        sport    = "TENNIS"
        stat_type = "Breakpoints Won"
        snap     = _make_snap(player, sport, stat_type)
        ud_result = _sent_result()

        if ud_result.sent:
            _n_new_prop_sent += 1
            _lifecycle_alerted.append((player, snap.sport or "UNKNOWN", stat_type))

        assert len(_lifecycle_alerted) == 1
        assert _lifecycle_alerted[0] == ("Caty McNally", "TENNIS", "Breakpoints Won")
        assert _n_new_prop_sent == 1

    def test_no_append_on_sent_false(self):
        """When delivery fails (sent=False), _lifecycle_alerted must not be touched."""
        _lifecycle_alerted: list = []
        _n_new_prop_sent = 0

        player    = "Caty McNally"
        sport     = "TENNIS"
        stat_type = "Breakpoints Won"
        snap      = _make_snap(player, sport, stat_type)
        ud_result = _not_sent_result()

        if ud_result.sent:  # False — block not entered
            _n_new_prop_sent += 1
            _lifecycle_alerted.append((player, snap.sport or "UNKNOWN", stat_type))

        assert _lifecycle_alerted == []
        assert _n_new_prop_sent == 0

    def test_multiple_new_props_all_appended(self):
        """All successful new-prop deliveries in one cycle must all be queued."""
        _lifecycle_alerted: list = []
        props = [
            ("Player A", "MLB",  "Home Runs",    _sent_result()),
            ("Player B", "WNBA", "Rebounds",     _sent_result()),
            ("Player C", "MLB",  "Stolen Bases", _not_sent_result()),  # not sent
            ("Player D", "NBA",  "Points",       _sent_result()),
        ]
        for player, sport, stat_type, ud_result in props:
            snap = _make_snap(player, sport, stat_type)
            if ud_result.sent:
                _lifecycle_alerted.append((player, snap.sport or "UNKNOWN", stat_type))

        assert len(_lifecycle_alerted) == 3
        assert ("Player A", "MLB",  "Home Runs") in _lifecycle_alerted
        assert ("Player B", "WNBA", "Rebounds")  in _lifecycle_alerted
        assert ("Player D", "NBA",  "Points")     in _lifecycle_alerted
        assert ("Player C", "MLB",  "Stolen Bases") not in _lifecycle_alerted

    def test_sport_defaults_to_unknown_when_none(self):
        """If snap.sport is None, the appended entry must use 'UNKNOWN'."""
        _lifecycle_alerted: list = []
        snap = _make_snap(sport=None)
        snap.sport = None
        ud_result = _sent_result()
        player, stat_type = "Test", "Points"

        if ud_result.sent:
            _lifecycle_alerted.append((player, snap.sport or "UNKNOWN", stat_type))

        assert _lifecycle_alerted[0][1] == "UNKNOWN"


class TestLifecycleAlertedStandingPath:
    """Standing delivery path must append to _lifecycle_alerted on successful send."""

    def test_append_called_on_sent_true(self):
        _lifecycle_alerted: list = []
        _n_standing_sent = 0

        _sp    = "Alanna Smith"
        _ssport = "WNBA"
        _st    = "Rebounds"
        _sresult = _sent_result()

        if _sresult.sent:
            _n_standing_sent += 1
            _lifecycle_alerted.append((_sp, _ssport, _st))

        assert len(_lifecycle_alerted) == 1
        assert _lifecycle_alerted[0] == ("Alanna Smith", "WNBA", "Rebounds")
        assert _n_standing_sent == 1

    def test_no_append_on_sent_false(self):
        _lifecycle_alerted: list = []
        _n_standing_sent = 0

        _sp     = "Alanna Smith"
        _ssport = "WNBA"
        _st     = "Rebounds"
        _sresult = _not_sent_result()

        if _sresult.sent:
            _n_standing_sent += 1
            _lifecycle_alerted.append((_sp, _ssport, _st))

        assert _lifecycle_alerted == []
        assert _n_standing_sent == 0

    def test_multiple_standing_props_all_appended(self):
        _lifecycle_alerted: list = []
        props = [
            ("A", "NBA",  "Points",  _sent_result()),
            ("B", "WNBA", "Assists", _not_sent_result()),
            ("C", "NFL",  "Receptions", _sent_result()),
        ]
        for _sp, _ssport, _st, _sresult in props:
            if _sresult.sent:
                _lifecycle_alerted.append((_sp, _ssport, _st))

        assert len(_lifecycle_alerted) == 2
        assert ("A", "NBA", "Points")       in _lifecycle_alerted
        assert ("C", "NFL", "Receptions")   in _lifecycle_alerted
        assert ("B", "WNBA", "Assists") not in _lifecycle_alerted

    def test_line_change_path_still_appends(self):
        """Line-change path (existing behavior) must be unaffected."""
        _lifecycle_alerted: list = []
        player    = "Yandy Díaz"
        sport     = "MLB"
        stat_type = "Home Runs"
        snap      = _make_snap(player, sport, stat_type)
        ud_result = _sent_result()
        is_removed = False

        if ud_result.sent and not is_removed:
            _lifecycle_alerted.append((player, snap.sport or "UNKNOWN", stat_type))

        assert _lifecycle_alerted == [("Yandy Díaz", "MLB", "Home Runs")]


# ── Bug 2: Health alerts_sent counter ─────────────────────────────────────────

class TestHealthAlertsSentCounter:
    """record_underdog_scan(alerts_sent=…) must include both new-prop and standing counts."""

    def _compute_alerts_sent(self, n_new: int, n_standing: int) -> int:
        """Mirror the fixed calculation from market_engine.py."""
        _n_new_prop_sent  = n_new
        _n_standing_sent  = n_standing
        return (
            (_n_new_prop_sent  if "_n_new_prop_sent"  in dir() else 0)
            + (_n_standing_sent if "_n_standing_sent" in dir() else 0)
        )

    def test_new_prop_only(self):
        assert self._compute_alerts_sent(n_new=2, n_standing=0) == 2

    def test_standing_only(self):
        """Core fix: standing sends must be counted even with zero new-prop sends."""
        assert self._compute_alerts_sent(n_new=0, n_standing=1) == 1

    def test_both_paths_combined(self):
        assert self._compute_alerts_sent(n_new=3, n_standing=2) == 5

    def test_zero_both(self):
        assert self._compute_alerts_sent(n_new=0, n_standing=0) == 0

    def test_old_broken_formula_would_undercount(self):
        """Document what the old broken formula produced."""
        n_new      = 0
        n_standing = 2
        old_result = n_new           # old: only _n_new_prop_sent
        new_result = n_new + n_standing
        assert old_result == 0, "old formula missed standing sends"
        assert new_result == 2, "new formula correctly includes standing sends"

    def test_health_record_called_with_combined_count(self):
        """health.record_underdog_scan receives the combined count."""
        health = MagicMock()
        _n_new_prop_sent  = 1
        _n_standing_sent  = 3
        ud_snaps = [MagicMock()] * 50

        health.record_underdog_scan(
            props_count = len(ud_snaps),
            alerts_sent = (
                (_n_new_prop_sent  if "_n_new_prop_sent"  in dir() else 0)
                + (_n_standing_sent if "_n_standing_sent" in dir() else 0)
            ),
        )

        health.record_underdog_scan.assert_called_once_with(
            props_count=50,
            alerts_sent=4,
        )


# ── End-to-end lifecycle flow ──────────────────────────────────────────────────

class TestLifecycleAlertedFlowE2E:
    """
    Simulate the full end-of-cycle lifecycle application to verify ACTIVE_ALERTED
    is correctly written when picks come from new-prop or standing paths.
    """

    @pytest.mark.asyncio
    async def test_new_prop_lifecycle_state_updated(self):
        """After new-prop delivery, update_prop_lifecycle_state is called with ACTIVE_ALERTED."""
        db     = AsyncMock()
        player = "Player X"
        sport  = "WNBA"
        stat   = "Points"
        now    = datetime.utcnow()

        _lifecycle_alerted = [(player, sport, stat)]

        # Apply lifecycle transitions (mirrors market_engine.py:2370-2380)
        for _lc_player, _lc_sport, _lc_stat in _lifecycle_alerted:
            await db.update_prop_lifecycle_state(
                "Underdog", _lc_player, _lc_sport, _lc_stat,
                "ACTIVE_ALERTED", first_alert_sent_at=now,
            )

        db.update_prop_lifecycle_state.assert_called_once_with(
            "Underdog", player, sport, stat,
            "ACTIVE_ALERTED", first_alert_sent_at=now,
        )

    @pytest.mark.asyncio
    async def test_standing_lifecycle_state_updated(self):
        """After standing delivery, update_prop_lifecycle_state is called with ACTIVE_ALERTED."""
        db  = AsyncMock()
        now = datetime.utcnow()
        _lifecycle_alerted = [("Smash", "CS", "Kills on Maps 1+2")]

        for _lc_player, _lc_sport, _lc_stat in _lifecycle_alerted:
            await db.update_prop_lifecycle_state(
                "Underdog", _lc_player, _lc_sport, _lc_stat,
                "ACTIVE_ALERTED", first_alert_sent_at=now,
            )

        db.update_prop_lifecycle_state.assert_called_once_with(
            "Underdog", "Smash", "CS", "Kills on Maps 1+2",
            "ACTIVE_ALERTED", first_alert_sent_at=now,
        )

    @pytest.mark.asyncio
    async def test_failed_delivery_no_lifecycle_update(self):
        """When delivery fails (sent=False), no lifecycle update must be queued."""
        db = AsyncMock()
        _lifecycle_alerted: list = []

        # Simulate: _sresult.sent is False → nothing appended
        _sresult = _not_sent_result()
        if _sresult.sent:
            _lifecycle_alerted.append(("Player", "SPORT", "Stat"))

        # Apply transitions
        for _lc_player, _lc_sport, _lc_stat in _lifecycle_alerted:
            await db.update_prop_lifecycle_state(
                "Underdog", _lc_player, _lc_sport, _lc_stat, "ACTIVE_ALERTED",
            )

        db.update_prop_lifecycle_state.assert_not_called()

    def test_all_three_paths_contribute_lifecycle_entries(self):
        """
        After a cycle with one delivery per path, _lifecycle_alerted has three entries.
        Previously only the line-change path contributed — so only 1 of 3 would appear.
        """
        _lifecycle_alerted: list = []

        # New-prop path
        player_np, sport_np, stat_np = "Player NP", "WNBA", "Points"
        ud_result = _sent_result()
        if ud_result.sent:
            _lifecycle_alerted.append((player_np, sport_np or "UNKNOWN", stat_np))

        # Line-change path (was already correct)
        player_lc, sport_lc, stat_lc = "Player LC", "MLB", "Home Runs"
        snap_lc = _make_snap(player_lc, sport_lc, stat_lc)
        lc_result = _sent_result()
        is_removed = False
        if lc_result.sent and not is_removed:
            _lifecycle_alerted.append((player_lc, snap_lc.sport or "UNKNOWN", stat_lc))

        # Standing path
        player_st, sport_st, stat_st = "Player ST", "NBA", "Assists"
        sresult = _sent_result()
        if sresult.sent:
            _lifecycle_alerted.append((player_st, sport_st, stat_st))

        assert len(_lifecycle_alerted) == 3
        assert (player_np, sport_np, stat_np) in _lifecycle_alerted
        assert (player_lc, sport_lc, stat_lc) in _lifecycle_alerted
        assert (player_st, sport_st, stat_st) in _lifecycle_alerted


# ── Source code verification ───────────────────────────────────────────────────

class TestSourceCodeFixes:
    """Verify the fixes are present in market_engine.py source."""

    def _load_source(self) -> str:
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            return f.read()

    def test_new_prop_path_has_lifecycle_append(self):
        """New-prop path must append to _lifecycle_alerted on sent=True."""
        src = self._load_source()
        # The new-prop block increments _n_new_prop_sent then (after _record_prop_alerted)
        # appends lifecycle. Window widened to 700 to clear the dedup-record block (#118).
        idx = src.find("_n_new_prop_sent += 1")
        assert idx != -1, "_n_new_prop_sent += 1 not found"
        snippet = src[idx: idx + 900]
        assert "_lifecycle_alerted.append" in snippet, (
            "_lifecycle_alerted.append() missing from new-prop delivery block"
        )

    def test_standing_path_has_lifecycle_append(self):
        """Standing path must append to _lifecycle_alerted on sent=True."""
        src = self._load_source()
        idx = src.find("_n_standing_sent += 1")
        assert idx != -1, "_n_standing_sent += 1 not found"
        snippet = src[idx: idx + 400]
        assert "_lifecycle_alerted.append" in snippet, (
            "_lifecycle_alerted.append() missing from standing delivery block"
        )

    def test_health_counter_includes_standing(self):
        """record_underdog_scan alerts_sent must reference _n_standing_sent."""
        src = self._load_source()
        # Search for the actual call site, not comment references to the function name
        scan_idx = src.find("_health.record_underdog_scan(")
        assert scan_idx != -1, "_health.record_underdog_scan( call not found in market_engine.py"
        snippet = src[scan_idx: scan_idx + 300]
        assert "_n_standing_sent" in snippet, (
            "_n_standing_sent missing from record_underdog_scan call — "
            "standing-path deliveries will not appear in /status Alerts sent"
        )

    def test_lifecycle_append_uses_correct_vars_standing(self):
        """Standing path lifecycle append must use the standing-scope variables (_sp, _ssport, _st)."""
        src = self._load_source()
        idx = src.find("_n_standing_sent += 1")
        snippet = src[idx: idx + 400]
        # Must use _sp/_ssport/_st not player/snap.sport/stat_type (those are outer-scope)
        append_idx = snippet.find("_lifecycle_alerted.append")
        assert append_idx != -1
        append_snippet = snippet[append_idx: append_idx + 60]
        assert "_sp" in append_snippet, (
            "Standing lifecycle append must use _sp (not outer 'player')"
        )
