"""
Tests for the new-prop detection path in underdog_job.

Covers:
  - Low-line props (≤ UD_NEW_PROP_LOW_LINE_THRESHOLD) trigger a new-prop alert
  - High-line props without a score gate do NOT trigger
  - Props already in known_keys are NOT treated as new
  - deliver_underdog is called with new_prop=True (not removed=True)
  - alert_outcome is "new_prop_sent" / "new_prop_skipped" in the DB record
  - Summary line includes new=N new_sent=N fields
  - format_underdog_new_prop_alert produces expected content
  - New-prop delivery bypasses the timing filter
  - Removal props are never treated as new props
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

import market_engine as me


# ── Helpers ────────────────────────────────────────────────────────────────────

def _snap(player: str, stat: str, line: float = 2.5, *, removed: bool = False) -> MagicMock:
    s = MagicMock()
    s.sportsbook = "Underdog"
    s.player     = player
    s.selection  = f"[REMOVED] {player} {stat} {line}" if removed else f"{player} {stat} {line}"
    s.line       = line
    s.sport      = "MLB"
    s.team       = "TeamA"
    s.event      = "game123"
    s.game_time  = None
    return s


def _make_db(
    known_keys: set | None = None,
    recent_dict: dict | None = None,
) -> MagicMock:
    db = MagicMock()
    db.get_known_underdog_prop_keys          = AsyncMock(return_value=known_keys or set())
    db.get_latest_underdog_snapshot_per_prop = AsyncMock(return_value=recent_dict or {})
    db.count_today_underdog_alerts           = AsyncMock(return_value=0)
    db.save_underdog_snapshot                = AsyncMock()
    db.get_ud_prop_history                   = AsyncMock(return_value=[])
    return db


def _make_context(db: MagicMock) -> MagicMock:
    ctx          = MagicMock()
    ctx.bot_data = {"db": db}
    ctx.bot      = MagicMock()
    return ctx


async def _run_job(snapshots, db, *, deliver_result=None):
    from alerts import DeliveryResult
    if deliver_result is None:
        deliver_result = DeliveryResult(sent=True, recipients_sent=1)

    registry = MagicMock()
    registry.fetch_pickem = AsyncMock(return_value=snapshots)
    ctx = _make_context(db)

    with patch.object(me, "_registry", registry):
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
            return mock_delivery


# ── New-prop detection ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_low_line_new_prop_triggers_alert(caplog):
    """A new prop with a 0.5 line must call deliver_underdog(new_prop=True)."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set())          # empty DB — all props are new

    with caplog.at_level(logging.INFO, logger="market_engine"):
        delivery = await _run_job(snaps, db)

    delivery.deliver_underdog.assert_called_once()
    _, kwargs = delivery.deliver_underdog.call_args
    assert kwargs.get("new_prop") is True
    assert kwargs.get("removed") is not True


@pytest.mark.asyncio
async def test_high_line_new_prop_not_immediately_alerted():
    """A new prop above the immediate threshold and not a priority stat is NOT individually alerted.

    It must still be included in the cycle batch (and saved to DB).
    """
    from config import config
    # "Walks" is deliberately not in UD_PRIORITY_STAT_CATEGORIES defaults
    snaps = [_snap("Aaron Judge", "Walks", 99.0)]
    db = _make_db(known_keys=set())

    from alerts import DeliveryResult
    not_sent = DeliveryResult(sent=False)

    # Verify: line 99.0 > 0.5 immediate threshold, stars=0 < gate, "Walks" not in priority
    delivery = await _run_job(snaps, db, deliver_result=not_sent)

    # deliver_underdog should NOT be called — prop goes to cycle summary instead
    delivery.deliver_underdog.assert_not_called()
    # But it must still be saved to DB
    db.save_underdog_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_known_prop_not_treated_as_new():
    """A prop already in known_keys follows the normal line-change path, not new-prop."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    known = {("Aaron Judge", "Home Runs")}
    db = _make_db(known_keys=known, recent_dict={})

    delivery = await _run_job(snaps, db)

    # deliver_underdog may or may not be called (no line change) but if called,
    # new_prop kwarg must NOT be True
    for c in delivery.deliver_underdog.call_args_list:
        _, kwargs = c
        assert kwargs.get("new_prop") is not True


@pytest.mark.asyncio
async def test_removal_never_treated_as_new():
    """Removal notices are never treated as new-prop regardless of known_keys."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5, removed=True)]
    db = _make_db(known_keys=set())   # empty known_keys — but it's a removal

    delivery = await _run_job(snaps, db)

    # If deliver_underdog was called, it must be with removed=True, not new_prop=True
    for c in delivery.deliver_underdog.call_args_list:
        _, kwargs = c
        assert kwargs.get("new_prop") is not True
        assert kwargs.get("removed") is True


@pytest.mark.asyncio
async def test_new_prop_outcome_stored_as_new_prop_sent(caplog):
    """When a new-prop alert is sent, alert_outcome='new_prop_sent' in the DB record."""
    from alerts import DeliveryResult
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set())

    sent_result = DeliveryResult(sent=True, recipients_sent=1)
    await _run_job(snaps, db, deliver_result=sent_result)

    db.save_underdog_snapshot.assert_called_once()
    record = db.save_underdog_snapshot.call_args[0][0]
    assert record.alert_outcome == "new_prop_sent"
    assert record.alert_sent is True


@pytest.mark.asyncio
async def test_non_immediate_new_prop_outcome_is_summary():
    """A non-immediate new prop (high line, no score, non-priority stat) gets outcome='new_prop_summary'.

    It must NOT get outcome='new_prop_skipped' — it IS included in the cycle digest.
    """
    # "Walks" is not in the priority stat list
    snaps = [_snap("Aaron Judge", "Walks", 99.0)]
    db = _make_db(known_keys=set())

    await _run_job(snaps, db)

    db.save_underdog_snapshot.assert_called_once()
    record = db.save_underdog_snapshot.call_args[0][0]
    assert record.alert_outcome == "new_prop_summary"
    assert record.alert_sent is False


@pytest.mark.asyncio
async def test_summary_line_includes_new_counts(caplog):
    """The INFO summary line must include new=N new_sent=N fields."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set())

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(snaps, db, deliver_result=sent)

    summary = next(
        (r.message for r in caplog.records if "fetched=" in r.message), None
    )
    assert summary is not None, "No summary line emitted"
    assert "new=" in summary
    assert "new_sent=" in summary


@pytest.mark.asyncio
async def test_new_prop_sent_increments_new_sent_counter(caplog):
    """new_sent counter equals number of immediate new-prop alerts delivered.

    Both props are priority stats at 0.5 → both trigger immediate alerts.
    """
    # "Home Runs" and "Strikeouts" are both in UD_PRIORITY_STAT_CATEGORIES
    snaps = [
        _snap("Player A", "Strikeouts", 0.5),
        _snap("Player B", "Home Runs",  0.5),
    ]
    db = _make_db(known_keys=set())

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(snaps, db, deliver_result=sent)

    summary = next(r.message for r in caplog.records if "fetched=" in r.message)
    assert "new=2" in summary
    assert "new_sent=2" in summary


@pytest.mark.asyncio
async def test_new_prop_score_stored_on_record():
    """Score fields are populated even for new props (n_history=0)."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set())

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)
    await _run_job(snaps, db, deliver_result=sent)

    record = db.save_underdog_snapshot.call_args[0][0]
    # score_tier and score_total should be set (not None) since scoring ran
    assert record.score_tier is not None
    assert record.score_total is not None


@pytest.mark.asyncio
async def test_cycle_digest_sent_when_new_props_detected():
    """broadcast_alert must be called with the cycle digest after the loop when new props exist."""
    snaps = [_snap("Aaron Judge", "Walks", 5.0)]    # non-immediate: goes to digest only
    db = _make_db(known_keys=set())

    with patch("alerts.broadcast_alert", new_callable=AsyncMock,
               return_value={"sent": 1, "failed": 0}) as mock_broadcast:
        with patch("market_engine.AlertDelivery") as mock_cls:
            mock_delivery = MagicMock()
            mock_delivery.deliver_underdog = AsyncMock()
            mock_cls.return_value = mock_delivery

            registry = MagicMock()
            registry.fetch_pickem = AsyncMock(return_value=snaps)
            ctx = MagicMock()
            ctx.bot_data = {"db": db}
            ctx.bot = MagicMock()

            with patch.object(me, "_registry", registry):
                await me.underdog_job(ctx)

    # broadcast_alert should have been called for the digest (no individual alert)
    mock_delivery.deliver_underdog.assert_not_called()   # non-immediate → no individual
    assert mock_broadcast.call_count >= 1
    # The digest message must mention "UNDERDOG NEW PROPS"
    digest_call = next(
        (c for c in mock_broadcast.call_args_list
         if "UNDERDOG NEW PROPS" in str(c)),
        None,
    )
    assert digest_call is not None, "Cycle digest not sent"


@pytest.mark.asyncio
async def test_no_digest_when_no_new_props():
    """No cycle digest is sent when all props are already known."""
    snaps = [_snap("Aaron Judge", "Walks", 5.0)]
    # All props in known_keys → nothing is new
    db = _make_db(known_keys={("Aaron Judge", "Walks")})

    with patch("alerts.broadcast_alert", new_callable=AsyncMock,
               return_value={"sent": 1, "failed": 0}) as mock_broadcast:
        with patch("market_engine.AlertDelivery") as mock_cls:
            mock_delivery = MagicMock()
            mock_delivery.deliver_underdog = AsyncMock()
            mock_cls.return_value = mock_delivery

            registry = MagicMock()
            registry.fetch_pickem = AsyncMock(return_value=snaps)
            ctx = MagicMock()
            ctx.bot_data = {"db": db}
            ctx.bot = MagicMock()

            with patch.object(me, "_registry", registry):
                await me.underdog_job(ctx)

    # No digest — prop is known, no new props detected
    for c in mock_broadcast.call_args_list:
        assert "UNDERDOG NEW PROPS" not in str(c)


@pytest.mark.asyncio
async def test_priority_stat_high_line_goes_to_digest_not_immediate():
    """A priority stat at a high line (> 0.5) does NOT trigger an immediate alert — digest only."""
    from config import config
    # "Home Runs" is in the default UD_PRIORITY_STAT_CATEGORIES
    snaps = [_snap("Aaron Judge", "Home Runs", 5.0)]
    db = _make_db(known_keys=set())

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)

    delivery = await _run_job(snaps, db, deliver_result=sent)

    # High line + no history → stars=0 < gate AND line > 0.5 → not immediate
    delivery.deliver_underdog.assert_not_called()
    # Must still be batched into the digest
    db.save_underdog_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_priority_stat_at_half_line_triggers_immediate_alert():
    """A priority stat AT 0.5 line DOES trigger an immediate individual alert."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set())

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)

    delivery = await _run_job(snaps, db, deliver_result=sent)

    delivery.deliver_underdog.assert_called_once()
    _, kwargs = delivery.deliver_underdog.call_args
    assert kwargs.get("new_prop") is True


@pytest.mark.asyncio
async def test_half_line_non_priority_stat_goes_to_digest():
    """0.5 line with a non-priority stat (e.g. Walks) goes to digest, not immediate alert."""
    # "Walks" is not in UD_PRIORITY_STAT_CATEGORIES
    snaps = [_snap("Aaron Judge", "Walks", 0.5)]
    db = _make_db(known_keys=set())

    delivery = await _run_job(snaps, db)

    # 0.5 line but "Walks" not in priority cats → not immediate
    delivery.deliver_underdog.assert_not_called()
    db.save_underdog_snapshot.assert_called_once()


# ── Format function ────────────────────────────────────────────────────────────

def test_format_new_prop_alert_contains_required_fields():
    """format_underdog_new_prop_alert includes player, stat, line, and header."""
    from alerts_multiplatform import format_underdog_new_prop_alert
    msg = format_underdog_new_prop_alert(
        player_name="Aaron Judge",
        team="NYY",
        sport="MLB",
        stat_type="Home Runs",
        line_value=0.5,
        game_time=None,
        score=None,
        low_line_threshold=1.0,
    )
    assert "UNDERDOG PROP LIVE" in msg
    assert "Aaron Judge" in msg
    assert "Home Runs" in msg
    assert "0.5" in msg
    assert "New prop detected" in msg
    assert "Low starting line" in msg


def test_format_new_prop_no_low_line_reason_when_above_threshold():
    """Low-line reason is omitted when line is above the threshold."""
    from alerts_multiplatform import format_underdog_new_prop_alert
    msg = format_underdog_new_prop_alert(
        player_name="Aaron Judge",
        team="NYY",
        sport="MLB",
        stat_type="Hits",
        line_value=5.0,
        game_time=None,
        score=None,
        low_line_threshold=1.0,
    )
    assert "Low starting line" not in msg


def test_format_new_prop_includes_grade_when_score_present():
    """When a UDPropScore is passed, the grade block appears in the message."""
    from alerts_multiplatform import format_underdog_new_prop_alert

    score = MagicMock()
    score.tier          = "A"
    score.stars         = 4
    score.total         = 80.0
    score.stars_display = "★★★★☆"
    score.n_history     = 0

    msg = format_underdog_new_prop_alert(
        player_name="Shohei Ohtani",
        team="LAD",
        sport="MLB",
        stat_type="Strikeouts",
        line_value=0.5,
        game_time=None,
        score=score,
        low_line_threshold=1.0,
    )
    assert "Grade" in msg
    assert "A" in msg
    assert "80" in msg


# ── deliver_underdog: new_prop bypasses timing filter ─────────────────────────

@pytest.mark.asyncio
async def test_new_prop_bypasses_timing_filter():
    """new_prop=True must skip the game timing filter."""
    from alerts import AlertDelivery
    from alert_scope_filter import FilterResult

    db   = MagicMock()
    db.count_today_underdog_alerts = AsyncMock(return_value=0)
    bot  = AsyncMock()
    delivery = AlertDelivery(db, bot, [123])

    timing_called = []

    def _timing(*a, **kw):
        timing_called.append(True)
        return (False, "game in progress")  # would block if called

    with (
        patch("alert_scope_filter.check", return_value=FilterResult(allowed=True)),
        patch("alerts_multiplatform.format_underdog_new_prop_alert", return_value="<msg>"),
        patch("alerts.broadcast_alert", new_callable=AsyncMock,
              return_value={"sent": 1, "failed": 0}),
        patch("engine.timing.is_game_alertable", side_effect=_timing),
    ):
        result = await delivery.deliver_underdog(
            "Aaron Judge", "NYY", "MLB", "Home Runs",
            0.5, 0.5,
            game_time=None,
            new_prop=True,
        )

    assert result.sent is True
    assert len(timing_called) == 0, "Timing filter must not be called for new_prop alerts"
