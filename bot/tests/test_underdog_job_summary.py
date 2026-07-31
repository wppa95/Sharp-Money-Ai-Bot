"""
Tests for the INFO-level summary line emitted at the end of underdog_job.

Scenarios:
  - No pick'em snapshots returned → no summary line (early return)
  - Only removed props → fetched=N scored=0 S=0 A=0 B=0 PASS=0 qualified=0 removed=N
  - Line-changed prop that scores PASS → scored=1 PASS=1 qualified=0
  - Line-changed prop that scores A/S-tier with real OVER/UNDER pick → qualified=1
  - Mixed batch (removed + PASS-scored + unchanged) → all counters correct
  - Exactly one summary line emitted per run
  - All eight fields present in the message
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import market_engine as me


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_snap(
    player: str = "Test Player",
    stat_type: str = "Hits",
    line: float = 2.5,
    sport: str = "MLB",
    removed: bool = False,
) -> MagicMock:
    snap = MagicMock()
    snap.sportsbook  = "Underdog"
    snap.player      = player
    snap.sport       = sport
    snap.line        = line
    snap.team        = "team-uuid"
    snap.game_time   = None
    snap.event       = "game-001"
    snap.market_type = "Pick'em"
    snap.is_pickem   = True
    suffix           = " [REMOVED]" if removed else ""
    snap.selection   = f"{player} {stat_type} {line}{suffix}"
    return snap


def _make_db_record(
    player: str = "Test Player",
    stat_type: str = "Hits",
    line_value: float = 2.5,
    line_moved: bool = False,
    prev_line: float | None = None,
) -> MagicMock:
    r = MagicMock()
    r.player_name = player
    r.stat_type   = stat_type
    r.line_value  = line_value
    r.line_moved  = line_moved
    r.prev_line   = prev_line
    r.removed     = False
    return r


def _make_db(
    recent_records: list | None = None,
    today_alerts: int = 0,
    prop_history: list | None = None,
    known_keys: set | None = None,
) -> MagicMock:
    # Build the dict that get_latest_underdog_snapshot_per_prop() returns.
    # Mirrors the real method: one entry per (player_name, stat_type), last wins.
    recent_dict: dict = {}
    for r in (recent_records or []):
        key = (r.player_name, r.stat_type)
        recent_dict[key] = r

    # By default, treat all props that have previous records as "known"
    # so they follow the line-change path rather than the new-prop path.
    if known_keys is None:
        known_keys = {(r.player_name, r.stat_type) for r in (recent_records or [])}

    db = MagicMock()
    db.get_latest_underdog_snapshot_per_prop = AsyncMock(return_value=recent_dict)
    db.get_known_underdog_prop_keys          = AsyncMock(return_value=known_keys)
    db.count_today_underdog_alerts           = AsyncMock(return_value=today_alerts)
    db.save_underdog_snapshot                = AsyncMock()
    db.save_underdog_snapshots_bulk          = AsyncMock()
    db.get_ud_prop_history                   = AsyncMock(return_value=prop_history or [])
    return db


def _make_context(db: MagicMock) -> MagicMock:
    ctx          = MagicMock()
    ctx.bot_data = {"db": db}
    ctx.bot      = MagicMock()
    return ctx


async def _run_job(snapshots, db, *, deliver_result=None):
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
                # The cycle digest is dispatched via broadcast_alert directly (not through
                # AlertDelivery), so we patch it here to prevent real Telegram calls.
                with patch("alerts.broadcast_alert",
                           new_callable=AsyncMock,
                           return_value={"sent": 1, "failed": 0}):
                    await me.underdog_job(ctx)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_snapshots_no_summary(caplog):
    registry = MagicMock()
    registry.fetch_pickem = AsyncMock(return_value=[])
    ctx = _make_context(_make_db())

    with caplog.at_level(logging.INFO, logger="market_engine"):
        with patch.object(me, "_registry", registry):
            with patch.object(me, "_cold_start_done", True):
                await me.underdog_job(ctx)

    summaries = [r for r in caplog.records if "underdog_job: fetched=" in r.message]
    assert len(summaries) == 0


@pytest.mark.asyncio
async def test_only_removed_props(caplog):
    from alerts import DeliveryResult
    snaps = [
        _make_snap("Player A", "Hits", 2.5, removed=True),
        _make_snap("Player B", "Runs", 0.5, removed=True),
    ]
    db = _make_db()

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(snaps, db, deliver_result=DeliveryResult(sent=True, recipients_sent=1))

    summary = next(r for r in caplog.records if "underdog_job: fetched=" in r.message)
    assert "fetched=2"   in summary.message
    assert "scored=0"    in summary.message
    assert "S=0"         in summary.message
    assert "A=0"         in summary.message
    assert "B=0"         in summary.message
    assert "PASS=0"      in summary.message
    assert "qualified=0" in summary.message
    assert "removed=2"   in summary.message


@pytest.mark.asyncio
async def test_line_changed_scores_pass(caplog):
    # Line moved 2.5 → 3.0 but no history → scores ~28 → PASS
    snap = _make_snap("Player A", "Hits", 3.0)
    db   = _make_db(
        recent_records=[_make_db_record("Player A", "Hits", 2.5)],
        prop_history=[],
    )

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job([snap], db)

    summary = next(r for r in caplog.records if "underdog_job: fetched=" in r.message)
    assert "fetched=1"   in summary.message
    assert "scored=1"    in summary.message
    assert "PASS=1"      in summary.message
    assert "qualified=0" in summary.message
    assert "removed=0"   in summary.message


@pytest.mark.asyncio
async def test_line_changed_qualifies(caplog):
    from alerts import DeliveryResult
    from engine.player_results import PlayerHitRates, WindowStats

    # Large move (4 units) + rich consistent history → A/S tier.
    # _fetch_and_compute_hit_rates is patched to return real-looking hit_rates
    # so the decision engine can produce an OVER pick (required to qualify).
    snap = _make_snap("Player A", "Hits", 7.0, sport="MLB")
    history = [
        _make_db_record(
            "Player A", "Hits",
            line_value=3.0 + 0.2 * i,
            line_moved=(i > 0),
            prev_line=3.0 + 0.2 * (i - 1) if i > 0 else None,
        )
        for i in range(20)
    ]
    db = _make_db(
        recent_records=[_make_db_record("Player A", "Hits", 3.0)],
        prop_history=history,
    )

    # Build hit_rates that will produce an OVER pick (all windows ~80%).
    def _win(n, r):
        oc = round(n * r)
        return WindowStats(games=n, over_count=oc, under_count=n-oc, hit_rate=r, average=8.0)

    fake_hit_rates = PlayerHitRates(
        player_name="Player A", stat_type="hits", current_line=7.0,
        l5=_win(5, 0.80), l10=_win(10, 0.80),
        l20=_win(20, 0.75), l30=_win(30, 0.70),
        season=_win(50, 0.70), h2h=None,
        has_real_data=True, total_games=50,
    )

    with caplog.at_level(logging.INFO, logger="market_engine"):
        with patch("market_engine._fetch_and_compute_hit_rates",
                   new=AsyncMock(return_value=fake_hit_rates)):
            await _run_job(
                [snap], db,
                deliver_result=DeliveryResult(sent=True, recipients_sent=1),
            )

    summary = next(r for r in caplog.records if "underdog_job: fetched=" in r.message)
    assert "fetched=1" in summary.message
    assert "scored=1"  in summary.message
    assert "removed=0" in summary.message
    assert int(summary.message.split("scored=")[1].split()[0])    == 1
    assert int(summary.message.split("qualified=")[1].split()[0]) == 1


@pytest.mark.asyncio
async def test_mixed_batch_counts(caplog):
    # 1 removed + 1 line-change (no history → PASS) + 1 unchanged
    snaps = [
        _make_snap("Player A", "Hits", 2.5, removed=True),
        _make_snap("Player B", "Runs", 3.0),
        _make_snap("Player C", "RBIs", 1.5),
    ]
    db = _make_db(
        recent_records=[
            _make_db_record("Player B", "Runs", 2.5),
            _make_db_record("Player C", "RBIs", 1.5),  # same → no change
        ],
        prop_history=[],
    )

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(snaps, db)

    summary = next(r for r in caplog.records if "underdog_job: fetched=" in r.message)
    assert "fetched=3"   in summary.message
    assert "removed=1"   in summary.message
    assert "scored=1"    in summary.message
    assert "PASS=1"      in summary.message
    assert "qualified=0" in summary.message


@pytest.mark.asyncio
async def test_exactly_one_summary_line(caplog):
    snaps = [_make_snap()]
    db    = _make_db()

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(snaps, db)

    summaries = [r for r in caplog.records if "underdog_job: fetched=" in r.message]
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_all_fields_present(caplog):
    snaps = [_make_snap()]
    db    = _make_db()

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(snaps, db)

    summary = next(r for r in caplog.records if "underdog_job: fetched=" in r.message)
    for field in ("fetched=", "scored=", "S=", "A=", "B=", "PASS=", "qualified=", "removed="):
        assert field in summary.message, f"field '{field}' missing from summary"
