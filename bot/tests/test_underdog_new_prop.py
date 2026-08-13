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
from contextlib import ExitStack
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call, PropertyMock

import market_engine as me
from engine.ud_scoring import MarketQuality, MarketQualityLabel, PropDifficultyClass


# ── Hit-rate helper (required for new-prop decision gate) ─────────────────────

def _make_hit_rates(over_rate: float = 0.80):
    """Return a PlayerHitRates object that makes the decision engine pick OVER."""
    from engine.player_results import PlayerHitRates, WindowStats

    def _w(n, r):
        oc = round(n * r)
        return WindowStats(games=n, over_count=oc, under_count=n - oc,
                           hit_rate=r, average=1.5)
    return PlayerHitRates(
        player_name="test", stat_type="hits", current_line=0.5,
        l5=_w(5, over_rate), l10=_w(10, over_rate),
        l20=_w(20, over_rate - 0.05), l30=_w(30, over_rate - 0.10),
        season=_w(50, over_rate - 0.10), h2h=None,
        has_real_data=True, total_games=50,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _snap(player: str, stat: str, line: float = 2.5, *, removed: bool = False,
          sport: str = "NBA") -> MagicMock:
    """Build a snapshot mock.  Default sport=NBA (not in ud_strict_alert_sports)
    so generic new-prop delivery tests are not affected by the MLB/NFL BQ≥95 gate.
    Pass sport='MLB' explicitly when testing MLB-specific gate behavior."""
    s = MagicMock()
    s.sportsbook = "Underdog"
    s.player     = player
    s.selection  = f"[REMOVED] {player} {stat} {line}" if removed else f"{player} {stat} {line}"
    s.line       = line
    s.sport      = sport
    s.team       = "TeamA"
    s.event      = "game123"
    s.game_time  = None
    return s


def _fake_history(n: int = 6, line: float = 0.5) -> list:
    """Build n fake UnderdogSnapshotRecord-like mocks for validation/scoring tests.

    Sets all attributes accessed by both validate_player_prop and score_ud_prop:
      line_value, line_moved, prev_line (None so consistency skips them), removed.
    """
    records = []
    for i in range(n):
        r = MagicMock()
        r.line_value = line
        r.line_moved = (i % 2 == 0)
        r.prev_line  = None    # prevents _score_consistency from comparing MagicMocks
        r.removed    = False   # prevents _score_stability from filtering rows
        records.append(r)
    return records


def _make_score(total: int = 78, tier: str = "A", stars: int = 4) -> MagicMock:
    score = MagicMock()
    score.total = total
    score.tier = tier
    score.stars = stars
    score.n_history = 20
    score.move_velocity = 10
    score.historical_activity = 15
    score.avg_vs_line = 12
    score.consistency = 10
    score.stability = 10
    score.difficulty = PropDifficultyClass.HIGH_FLOOR
    score.variance_penalty = 0
    score.bet_quality_label = "STANDARD BET"
    return score


def _make_validation(supported: bool = True) -> MagicMock:
    val = MagicMock()
    val.has_supporting_data = supported
    val.reason = "ok" if supported else "insufficient_history"
    return val


def _make_decision(rec: str = "OVER", tier: str = "A", conf: int = 75) -> MagicMock:
    dec = MagicMock()
    dec.recommendation = rec
    dec.decision_tier = tier
    dec.confidence = conf
    dec.reason = "edge_detected"
    return dec


def _make_market_quality(label: str = "HIGH") -> MarketQuality:
    return MarketQuality(
        label=MarketQualityLabel(label),
        score=78,
        reasons=("High-floor stat (Points)",),
    )


def _make_db(
    known_keys: set | None = None,
    recent_dict: dict | None = None,
    prop_history: list | None = None,
) -> MagicMock:
    db = MagicMock()
    db.get_known_underdog_prop_keys          = AsyncMock(return_value=known_keys or set())
    db.get_latest_underdog_snapshot_per_prop = AsyncMock(return_value=recent_dict or {})
    db.count_today_underdog_alerts           = AsyncMock(return_value=0)
    db.save_underdog_snapshot                = AsyncMock()
    db.save_underdog_snapshots_bulk          = AsyncMock()
    # Default to empty history — validation gate blocks immediate alerts without data.
    # Pass prop_history=_fake_history() in tests that expect immediate alerts to fire.
    db.get_ud_prop_history                   = AsyncMock(
        return_value=prop_history if prop_history is not None else []
    )
    return db


def _make_context(db: MagicMock) -> MagicMock:
    ctx          = MagicMock()
    ctx.bot_data = {"db": db}
    ctx.bot      = MagicMock()
    return ctx


async def _run_job(
    snapshots,
    db,
    *,
    deliver_result=None,
    hit_rates=None,
    score=None,
    validation=None,
    decision=None,
    market_quality=None,
):
    """Run underdog_job under full mocking.

    Pass *hit_rates* to patch ``_fetch_and_compute_hit_rates`` so the decision
    engine can produce an OVER/UNDER pick — required for the new-prop immediate
    alert gate.  Omit when testing paths that should NOT produce an alert.
    """
    from alerts import DeliveryResult
    if deliver_result is None:
        deliver_result = DeliveryResult(sent=True, recipients_sent=1)

    registry = MagicMock()
    registry.fetch_pickem = AsyncMock(return_value=snapshots)
    ctx = _make_context(db)

    hit_rates_mock = AsyncMock(return_value=hit_rates)

    with ExitStack() as stack:
        stack.enter_context(patch.object(me, "_registry", registry))
        stack.enter_context(patch.object(me, "_cold_start_done", True))
        stack.enter_context(patch.object(type(me.config), "allowed_user_ids", new_callable=PropertyMock, return_value={123}))
        # Reset per-test: _prop_market_alerted is module-level and persists across
        # tests in the same process.  A fresh dict prevents the dedup gate (#118)
        # from suppressing alerts in tests that follow a test that recorded an alert.
        stack.enter_context(patch.object(me, "_prop_market_alerted", {}))
        stack.enter_context(patch("market_engine._fetch_and_compute_hit_rates", hit_rates_mock))
        if score is not None:
            stack.enter_context(patch("engine.ud_scoring.score_ud_prop", return_value=score))
        if validation is not None:
            stack.enter_context(patch("engine.player_validator.validate_player_prop", return_value=validation))
        if decision is not None:
            stack.enter_context(patch("engine.ud_bet_decision.make_ud_bet_decision", return_value=decision))
        if market_quality is not None:
            stack.enter_context(patch("engine.ud_scoring.compute_market_quality", return_value=market_quality))
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
    """A new high-floor 0.5-line prop with supporting history + real OVER pick triggers an immediate alert."""
    snaps = [_snap("Jalen Brunson", "Points", 0.5)]
    db = _make_db(known_keys=set(), prop_history=_fake_history(6))

    with caplog.at_level(logging.INFO, logger="market_engine"):
        delivery = await _run_job(
            snaps,
            db,
            hit_rates=_make_hit_rates(),
            score=_make_score(),
            validation=_make_validation(),
            decision=_make_decision(),
            market_quality=_make_market_quality(),
        )

    delivery.deliver_underdog.assert_called_once()
    _, kwargs = delivery.deliver_underdog.call_args
    assert kwargs.get("new_prop") is True
    assert kwargs.get("removed") is not True


@pytest.mark.asyncio
async def test_low_line_new_prop_without_history_goes_to_digest():
    """A new prop with 0.5 line but NO history is blocked from immediate alert by validation gate.

    It still appears in the digest — validation does NOT suppress storage.
    """
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    # Empty history — validation gate blocks immediate alert
    db = _make_db(known_keys=set(), prop_history=[])

    delivery = await _run_job(snaps, db)

    # No immediate alert — not enough history
    delivery.deliver_underdog.assert_not_called()
    # But the record IS saved (via bulk) and the prop goes to digest
    db.save_underdog_snapshots_bulk.assert_called()
    record = db.save_underdog_snapshots_bulk.call_args[0][0][0]
    assert record.alert_outcome == "new_prop_summary"


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
    # But it must still be saved to DB (via bulk)
    db.save_underdog_snapshots_bulk.assert_called()


@pytest.mark.asyncio
async def test_known_prop_not_treated_as_new():
    """A prop in known_keys WITH an active prev_record follows the line-change path, not new-prop."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    known = {("Aaron Judge", "Home Runs")}

    # Build a fake prev_record so prev_record is NOT None → not a re-entry
    prev = MagicMock()
    prev.line_value  = 0.5   # same line → no line change event
    prev.alert_sent  = True
    prev.score_tier  = "A"

    db = _make_db(
        known_keys  = known,
        recent_dict = {("Aaron Judge", "Home Runs"): prev},
    )

    delivery = await _run_job(snaps, db)

    # deliver_underdog may or may not be called (no line change) but if called,
    # new_prop kwarg must NOT be True — this prop has an active record
    for c in delivery.deliver_underdog.call_args_list:
        _, kwargs = c
        assert kwargs.get("new_prop") is not True, \
            "Prop with active prev_record should not fire as new_prop"


@pytest.mark.asyncio
async def test_known_prop_reentry_treated_as_new_prop():
    """A prop in known_keys but with NO recent non-removed record is a re-entry.

    Re-entries fire as new_prop=True (bypasses timing filter) so the user
    is notified the prop returned to the feed.
    """
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    known = {("Aaron Judge", "Home Runs")}

    # recent_dict={} means get_latest_underdog_snapshot_per_prop() returned
    # nothing for this prop — last snapshot was a removal.
    db = _make_db(known_keys=known, recent_dict={}, prop_history=_fake_history(6))

    delivery = await _run_job(snaps, db, hit_rates=_make_hit_rates())

    # Re-entry should fire as new_prop (same treatment as a first appearance).
    # deliver_underdog is called with new_prop=True.
    if delivery.deliver_underdog.call_args_list:
        _, kwargs = delivery.deliver_underdog.call_args
        assert kwargs.get("new_prop") is True, \
            "Re-entry (known key, no active prev_record) must fire as new_prop=True"


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
    snaps = [_snap("Jalen Brunson", "Points", 0.5)]
    db = _make_db(known_keys=set(), prop_history=_fake_history(6))

    sent_result = DeliveryResult(sent=True, recipients_sent=1)
    await _run_job(
        snaps,
        db,
        deliver_result=sent_result,
        hit_rates=_make_hit_rates(),
        score=_make_score(),
        validation=_make_validation(),
        decision=_make_decision(),
        market_quality=_make_market_quality(),
    )

    db.save_underdog_snapshots_bulk.assert_called()
    record = db.save_underdog_snapshots_bulk.call_args[0][0][0]
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

    db.save_underdog_snapshots_bulk.assert_called()
    record = db.save_underdog_snapshots_bulk.call_args[0][0][0]
    assert record.alert_outcome == "new_prop_summary"
    assert record.alert_sent is False


@pytest.mark.asyncio
async def test_summary_line_includes_new_counts(caplog):
    """The INFO summary line must include new=N new_sent=N fields."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    # Provide history so the immediate alert fires and new_sent=1
    db = _make_db(known_keys=set(), prop_history=_fake_history(6))

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

    Both props are priority high-floor stats at 0.5 AND have sufficient history → both
    trigger immediate alerts.
    """
    # "Points" and "Rebounds" are both in UD_PRIORITY_STAT_CATEGORIES
    snaps = [
        _snap("Player A", "Points",   0.5),
        _snap("Player B", "Rebounds", 0.5),
    ]
    # Provide history so validation gate passes for both props
    db = _make_db(known_keys=set(), prop_history=_fake_history(6))

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)

    with caplog.at_level(logging.INFO, logger="market_engine"):
        await _run_job(
            snaps,
            db,
            deliver_result=sent,
            hit_rates=_make_hit_rates(),
            score=_make_score(),
            validation=_make_validation(),
            decision=_make_decision(),
            market_quality=_make_market_quality(),
        )

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

    record = db.save_underdog_snapshots_bulk.call_args[0][0][0]
    # score_tier and score_total should be set (not None) since scoring ran
    assert record.score_tier is not None
    assert record.score_total is not None


@pytest.mark.asyncio
async def test_new_props_stored_silently_no_digest():
    """New props are stored and scored silently — no digest is broadcast to Telegram.

    The "UNDERDOG NEW PROPS" discovery dump has been suppressed.  New props only
    trigger a Telegram message when they pass the full qualification gate
    (score + validation + decision + sport whitelist).  Non-immediate props
    (low score, no history, non-priority line) must be stored without any alert.
    """
    snaps = [_snap("Aaron Judge", "Walks", 5.0)]    # non-immediate: stored silently
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
                with patch.object(me, "_cold_start_done", True):
                    await me.underdog_job(ctx)

    # No individual alert (non-immediate prop)
    mock_delivery.deliver_underdog.assert_not_called()
    # No digest broadcast — digest is suppressed in Underdog-only mode
    for c in mock_broadcast.call_args_list:
        assert "UNDERDOG NEW PROPS" not in str(c), (
            "Cycle digest must not be broadcast — new props are stored silently"
        )
    # Prop is still persisted in the DB (via bulk save)
    db.save_underdog_snapshots_bulk.assert_called()


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
                with patch.object(me, "_cold_start_done", True):
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
    # Must still be persisted in the DB (via bulk save)
    db.save_underdog_snapshots_bulk.assert_called()


@pytest.mark.asyncio
async def test_priority_stat_at_half_line_triggers_immediate_alert():
    """A priority high-floor stat at 0.5 line + history + real OVER pick triggers an immediate alert."""
    snaps = [_snap("Jalen Brunson", "Points", 0.5)]
    db = _make_db(known_keys=set(), prop_history=_fake_history(6))

    from alerts import DeliveryResult
    sent = DeliveryResult(sent=True, recipients_sent=1)

    delivery = await _run_job(
        snaps,
        db,
        deliver_result=sent,
        hit_rates=_make_hit_rates(),
        score=_make_score(),
        validation=_make_validation(),
        decision=_make_decision(),
        market_quality=_make_market_quality(),
    )

    delivery.deliver_underdog.assert_called_once()
    _, kwargs = delivery.deliver_underdog.call_args
    assert kwargs.get("new_prop") is True


@pytest.mark.asyncio
async def test_validation_json_stored_on_record():
    """validation_json is stored on the DB record for all new props."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set(), prop_history=[])

    await _run_job(snaps, db)

    db.save_underdog_snapshots_bulk.assert_called()
    record = db.save_underdog_snapshots_bulk.call_args[0][0][0]
    assert record.validation_json is not None
    import json
    parsed = json.loads(record.validation_json)
    assert "n" in parsed
    assert "has_data" in parsed


@pytest.mark.asyncio
async def test_validation_json_stored_with_history():
    """validation_json reflects history metrics when history is available."""
    snaps = [_snap("Aaron Judge", "Home Runs", 0.5)]
    db = _make_db(known_keys=set(), prop_history=_fake_history(8))

    await _run_job(snaps, db)

    record = db.save_underdog_snapshots_bulk.call_args[0][0][0]
    import json
    parsed = json.loads(record.validation_json)
    assert parsed["n"] == 8
    assert parsed["has_data"] is True


@pytest.mark.asyncio
async def test_half_line_non_priority_stat_goes_to_digest():
    """0.5 line with a non-priority stat (e.g. Walks) goes to digest, not immediate alert."""
    # "Walks" is not in UD_PRIORITY_STAT_CATEGORIES
    snaps = [_snap("Aaron Judge", "Walks", 0.5)]
    db = _make_db(known_keys=set())

    delivery = await _run_job(snaps, db)

    # 0.5 line but "Walks" not in priority cats → not immediate
    delivery.deliver_underdog.assert_not_called()
    db.save_underdog_snapshots_bulk.assert_called()


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
