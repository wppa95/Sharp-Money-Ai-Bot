"""Final same-day Central policy, source-owned durable reminders and history."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import alerts
import database
import reporting
from alerts_multiplatform import _format_game_time_et
from database import Database, TelegramDeliveryAudit
from engine import game_reminders as reminders
from engine import timing
from engine.delivery_evidence import has_qualifying_directional_pick
from engine.player_props_pipeline import CanonicalPlayerProp
from engine.strongest_pick import is_strong_actionable_pick
from engine.player_results import compute_hit_rates
from engine.ud_bet_decision import make_ud_bet_decision
from providers.propline import _parse_datetime
from connectors.underdog import _parse_event_time
import market_engine


@pytest.fixture()
def clock(monkeypatch):
    current = [datetime(2026, 10, 16, 15, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return current[0].replace(tzinfo=None)

    monkeypatch.setattr(timing, "datetime", Clock)
    monkeypatch.setattr(database, "datetime", Clock)
    monkeypatch.setattr(reporting, "reporting_now", lambda: current[0])
    return current


@pytest.fixture()
async def db():
    instance = Database("sqlite+aiosqlite:///:memory:")
    await instance.init()
    reminders.reset()
    yield instance
    reminders.reset()
    await instance.close()


def quote(clock, provider="Underdog", **changes):
    values = dict(
        provider=provider, player_name="Fixture", sport="DOTA", team="TST",
        stat_type="Kills", line_value=10.5,
        game_time=(clock[0] + timedelta(hours=8)).replace(tzinfo=None),
        external_id="source-prop", game_id="source-event", line_variant="standard",
        provider_status="scheduled", fetched_at=datetime.utcnow(), raw=None,
        available_directions=("OVER", "UNDER"),
    )
    values.update(changes)
    return CanonicalPlayerProp(**values)


async def store_pick(db, q, *, sent_at, display_event=None):
    fields = dict(
        provider=q.provider, external_id=q.external_id, player_name=q.player_name,
        sport=q.sport, team=q.team, stat_type=q.stat_type, line_value=q.line_value,
        game_time=q.game_time, game_id=q.game_id,
    )
    await db.track_prop_records([fields])
    await db.log_prop_opportunity(
        **fields, recommendation="OVER", confidence=70, decision_tier="B",
    )
    assert await db.record_telegram_play_delivery(
        provider=q.provider, delivery_path="new", player_name=q.player_name,
        sport=q.sport, stat_type=q.stat_type, external_id=q.external_id,
        event_id=display_event or q.game_id, line_value=q.line_value, recipients_sent=1,
        recipients_failed=0, attempted_at=sent_at.replace(tzinfo=None),
        evidence_snapshot=alerts._reminder_snapshot(
            q.game_time, "OVER", q.line_variant, q.game_id,
        ),
    )


async def audit_rows(db):
    async with db.session() as session:
        return list((await session.scalars(
            select(TelegramDeliveryAudit).order_by(TelegramDeliveryAudit.id)
        )).all())


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
@pytest.mark.parametrize("hours", [1, 8])
async def test_today_pick_immediate_without_wait(db, clock, monkeypatch, provider, hours):
    q = quote(clock, provider, game_time=(clock[0]+timedelta(hours=hours)).replace(tzinfo=None))
    await db.track_prop_records([dict(
        provider=q.provider, external_id=q.external_id, player_name=q.player_name,
        sport=q.sport, team=q.team, stat_type=q.stat_type, line_value=q.line_value,
        game_time=q.game_time, game_id=q.game_id,
    )])
    await db.log_prop_opportunity(
        provider=q.provider, external_id=q.external_id, player_name=q.player_name,
        sport=q.sport, team=q.team, stat_type=q.stat_type, line_value=q.line_value,
        game_time=q.game_time, game_id=q.game_id,
        recommendation="OVER", confidence=70, decision_tier="B",
    )
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    monkeypatch.setattr(alerts, "send_discord_alert", AsyncMock(return_value=False))
    monkeypatch.setattr("alert_scope_filter.check", lambda _: NS(allowed=True))
    result = await alerts.AlertDelivery(db, object(), [123]).deliver_underdog(
        player_name=q.player_name, team=q.team, sport=q.sport, stat_type=q.stat_type,
        old_line=q.line_value, new_line=q.line_value, game_time=q.game_time,
        strict_pregame=True, fetched_at=q.fetched_at,
        decision=NS(recommendation="OVER"), source=q.provider,
        source_id=q.external_id, event_id=q.game_id,
    )
    assert result.sent and send.await_count == 1
    records = await audit_rows(db)
    assert [r.alert_kind for r in records] == ["ACTIONABLE_PICK", "GAME_REMINDER"]
    assert records[1].outcome == ("pending" if hours > 2 else "elapsed")
    data = json.loads(records[1].evidence_snapshot_json)
    assert datetime.fromisoformat(data["due_utc"]) == q.game_time - timedelta(hours=2)


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_tomorrow_blocked_until_central_midnight(db, clock, monkeypatch, provider):
    q = quote(clock, provider, game_time=datetime(2026, 10, 17, 17))
    sender = alerts.AlertDelivery(db, object(), [123])
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    monkeypatch.setattr("alert_scope_filter.check", lambda _: NS(allowed=True))
    result = await sender.deliver_underdog(
        player_name=q.player_name, team=q.team, sport=q.sport, stat_type=q.stat_type,
        old_line=q.line_value, new_line=q.line_value, game_time=q.game_time,
        strict_pregame=True, decision=NS(recommendation="OVER"),
        source=provider, source_id=q.external_id, event_id=q.game_id,
    )
    assert result.filtered and "Central" in result.filtered_reason
    send.assert_not_awaited()
    clock[0] = datetime(2026, 10, 17, 4, 59, 59, tzinfo=timezone.utc)
    assert not reporting.is_current_reporting_day(q.game_time)
    clock[0] += timedelta(seconds=1)
    assert reporting.is_current_reporting_day(q.game_time)


@pytest.mark.parametrize("raw,label", [
    ("2026-07-15T19:00:00Z", "Jul 15, 2:00 PM CDT"),
    ("2026-01-15T20:00:00Z", "Jan 15, 2:00 PM CST"),
    ("2026-07-15T15:00:00-04:00", "Jul 15, 2:00 PM CDT"),
    ("2026-03-08T07:59:00Z", "Mar 8, 1:59 AM CST"),
    ("2026-03-08T08:00:00Z", "Mar 8, 3:00 AM CDT"),
    ("2026-11-01T06:30:00Z", "Nov 1, 1:30 AM CDT"),
    ("2026-11-01T07:30:00Z", "Nov 1, 1:30 AM CST"),
])
def test_provider_utc_and_offsets_display_central(raw, label):
    for parse in (_parse_datetime, _parse_event_time):
        assert _format_game_time_et(parse(raw)) == label


@pytest.mark.parametrize("sport", ["MLB", "NFL"])
@pytest.mark.parametrize("count,allowed", [(0, False), (5, False), (6, True), (10, True)])
def test_six_real_games_not_six_windows(sport, count, allowed):
    data = NS(data_source="primary", n_real_games=count, has_supporting_data=count > 0)
    decision = NS(recommendation="UNDER", l5_games=5, l5_under=4, l20_games=None,
                  l30_games=None, h2h_games=None)
    assert has_qualifying_directional_pick(
        decision, sport=sport, evidence=data,
    ) is allowed
    assert market_engine._has_delivery_evidence(
        sport=sport, validation=data, decision=decision,
    ) is allowed
    assert alerts._has_strong_underdog_evidence(
        sport=sport, validation=data, decision=decision,
    ) is allowed
    assert is_strong_actionable_pick(
        dict(sport=sport, decision=decision, validation=data)
    ) is allowed


@pytest.mark.parametrize("sport", ["NBA", "WNBA", "NHL", "DOTA", "CS", "TENNIS"])
def test_optional_history_still_needs_supported_direction(sport):
    for direction, allowed in [("OVER", True), ("UNDER", True), ("PASS", False), ("", False)]:
        d = NS(recommendation=direction)
        assert market_engine._has_delivery_evidence(sport=sport, decision=d) is allowed
        assert alerts._has_strong_underdog_evidence(sport=sport, decision=d) is allowed


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_exact_due_reminder_restart_dedup_and_original_immutability(
    db, clock, monkeypatch, provider,
):
    q = quote(clock, provider)
    await store_pick(db, q, sent_at=clock[0])
    # Replay cannot add another reminder, even through a different delivery path.
    await store_pick(db, q, sent_at=clock[0])
    records = await audit_rows(db)
    original = records[0].evidence_snapshot_json
    assert len(records) == 2
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    clock[0] += timedelta(hours=6)
    reminders.observe([q])
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    assert send.await_count == 1
    body = send.call_args.args[2]
    assert "<b>GAME REMINDER</b>" in body and "4:00 PM CDT" in body
    assert "6:00 PM CDT" in body and provider in body and reminders.FOOTER in body
    reminders.reset()
    reminders.observe([q])
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    assert send.await_count == 1
    after = await audit_rows(db)
    assert len(after) == 2 and after[1].outcome == "sent"
    assert after[0].evidence_snapshot_json == original
    assert after[0].line_value == 10.5 and after[0].alert_kind == "ACTIONABLE_PICK"


async def test_initial_pick_inside_two_hours_records_elapsed_not_late_reminder(db, clock, monkeypatch):
    q = quote(clock, game_time=(clock[0] + timedelta(minutes=90)).replace(tzinfo=None))
    await store_pick(db, q, sent_at=clock[0])
    records = await audit_rows(db)
    assert records[1].outcome == "elapsed"
    assert json.loads(records[1].evidence_snapshot_json)["reason"] == "due_time_already_passed"
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    reminders.observe([q])
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    send.assert_not_awaited()


@pytest.mark.parametrize("change", [
    {"removed": True}, {"provider_status": "live"},
    {"line_value": 11.5}, {"available_directions": ("UNDER",)},
])
async def test_invalid_selection_cancels_without_mutating_play(db, clock, monkeypatch, change):
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    reminders.observe([replace(q, **change)])
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    records = await audit_rows(db)
    assert records[1].outcome == "cancelled"
    assert records[0].line_value == 10.5
    send.assert_not_awaited()


async def test_reschedule_updates_due_not_original_pick(db, clock, monkeypatch):
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    later = replace(q, game_time=q.game_time + timedelta(hours=1))
    reminders.observe([later])
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    record = (await audit_rows(db))[1]
    data = json.loads(record.evidence_snapshot_json)
    assert record.outcome == "pending"
    assert datetime.fromisoformat(data["due_utc"]) == q.game_time - timedelta(hours=1)


async def test_missing_fresh_quote_and_started_event_never_send(db, clock, monkeypatch):
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    clock[0] += timedelta(hours=6)
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    assert (await audit_rows(db))[1].outcome == "pending"
    clock[0] += timedelta(hours=2)
    reminders.observe([q])
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    assert (await audit_rows(db))[1].outcome == "cancelled"
    send.assert_not_awaited()


async def test_uncertain_claim_survives_restart_without_duplicate_transport(db, clock, monkeypatch):
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    row = (await audit_rows(db))[1]
    assert await db.claim_game_reminder(row.id)
    assert not await db.claim_game_reminder(row.id)
    reminders.reset()
    reminders.observe([q])
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    clock[0] += timedelta(hours=6)
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    send.assert_not_awaited()


@pytest.mark.parametrize("sport,count", [("NBA", 4), ("DOTA", 1)])
def test_short_real_history_can_establish_direction_with_existing_rate_thresholds(sport, count):
    results = [NS(
        player_name="Fixture", stat_type="Points", actual_value=20,
        game_date=f"2026-10-{16-i:02d}", opponent="OTHER", source="primary",
    ) for i in range(count)]
    rates = compute_hit_rates(results, 10.5)
    decision = make_ud_bet_decision(
        score=None, validation=None, hit_rates=rates, current_line=10.5, sport=sport,
    )
    assert decision.recommendation == "OVER"
    assert rates.total_games == count
    assert rates.l20 is None and rates.l30 is None


@pytest.mark.parametrize("start,due_label,start_label", [
    ("2026-03-08T09:30:00+00:00", "1:30 AM CST", "4:30 AM CDT"),
    ("2026-11-01T07:30:00+00:00", "12:30 AM CDT", "1:30 AM CST"),
])
def test_reminder_two_real_hours_across_dst(start, due_label, start_label):
    instant = datetime.fromisoformat(start)
    audit = NS(
        evidence_snapshot_json=json.dumps(alerts._reminder_snapshot(instant, "OVER", "standard")),
        provider="Underdog", external_id="prop", event_id="event",
        player_name="Fixture", sport="DOTA", stat_type="Kills", line_value=10.5,
        delivery_key="original",
    )
    record = reminders.build_record(audit, (instant - timedelta(hours=5)).replace(tzinfo=None))
    data = json.loads(record["evidence_snapshot_json"])
    assert datetime.fromisoformat(data["game_time_utc"]) - datetime.fromisoformat(data["due_utc"]) == timedelta(hours=2)
    body = reminders.format_reminder(NS(**record), data)
    assert due_label in body and start_label in body


async def test_movement_reminder_and_pick_have_independent_immutable_records(db, clock, monkeypatch):
    from engine import line_change_notifications as movement
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    await movement.reset()
    movement.observe_persisted_batch(db, object(), [123], [q])
    changed = replace(q, line_value=11.5, fetched_at=q.fetched_at+timedelta(seconds=1))
    movement.observe_persisted_batch(db, object(), [123], [changed])
    await movement._worker
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    records = await audit_rows(db)
    assert [r.alert_kind for r in records] == ["ACTIONABLE_PICK", "GAME_REMINDER", "LINE_CHANGE"]
    assert records[0].line_value == 10.5 and records[0].outcome == "sent"
    assert records[1].outcome == "cancelled"
    assert records[2].line_value == 11.5
    assert send.await_count == 1
    assert "<b>LINE CHANGE</b>" in send.call_args.args[2]
    await movement.reset()


async def test_other_platform_quote_cannot_authorize_reminder(db, clock, monkeypatch):
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    reminders.observe([replace(q, provider="PrizePicks")])
    clock[0] += timedelta(hours=6)
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    assert (await audit_rows(db))[1].outcome == "pending"
    send.assert_not_awaited()


@pytest.mark.parametrize("date,hours", [
    (datetime(2026, 3, 8, 17, tzinfo=timezone.utc), 23),
    (datetime(2026, 11, 1, 17, tzinfo=timezone.utc), 25),
])
def test_reporting_day_bounds_respect_dst(date, hours):
    start, end = reporting.current_reporting_day_bounds(now=date)
    assert end - start == timedelta(hours=hours)


async def test_complete_snapshot_absence_cancels_only_its_own_route(db, clock, monkeypatch):
    q = quote(clock)
    await store_pick(db, q, sent_at=clock[0])
    reminders.observe([q], scope=("native",))
    reminders.observe([], scope=("propline", "Underdog:DOTA"))
    assert not reminders._unavailable
    reminders.observe([], scope=("native",))
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    assert (await audit_rows(db))[1].outcome == "cancelled"
    send.assert_not_awaited()


def test_ambiguous_or_missing_reminder_timestamp_is_not_fabricated():
    for data in ({}, {"direction": "OVER", "game_time_utc": "not a timestamp"}):
        assert reminders.build_record(
            NS(evidence_snapshot_json=json.dumps(data)), datetime.utcnow(),
        ) is None


async def test_verified_provider_event_id_can_differ_from_immutable_audit_label(db, clock, monkeypatch):
    q = quote(clock, game_id="272866")
    await store_pick(db, q, sent_at=clock[0], display_event="Verified Team A vs Team B")
    reminders.observe([q])
    clock[0] += timedelta(hours=6)
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    await reminders.run_due_reminders(db, object(), [123], now=clock[0])
    rows = await audit_rows(db)
    assert send.await_count == 1
    assert rows[0].event_id == rows[1].event_id == "Verified Team A vs Team B"
    assert json.loads(rows[1].evidence_snapshot_json)["source_event_id"] == "272866"
    assert rows[1].outcome == "sent"
