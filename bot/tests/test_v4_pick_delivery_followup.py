"""Pick-to-delivery regressions; all transport and remote responses are fixtures."""
from dataclasses import replace
from datetime import datetime, timedelta
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

import alerts
import market_engine
from database import Database, TelegramDeliveryAudit, PropOpportunityLog
from engine import line_change_notifications as movement
from engine.delivery_evidence import optional_sport_evidence_is_usable
from engine.player_props_pipeline import CanonicalPlayerProp
from engine.timing import is_game_alertable
from engine.evidence_memory import clear, get_player_results, put_player_results
from player_history_job import _hydrate_completed_history_cache
from providers.propline import PropLineFetchResult


@pytest.fixture()
async def db():
    instance = Database("sqlite+aiosqlite:///:memory:")
    await instance.init()
    yield instance
    await movement.reset()
    await instance.close()
    clear()


def prop(provider="Underdog", line=10.5, **kwargs):
    fields = dict(
        provider=provider, player_name="Fixture Player", sport="DOTA", team="TST",
        stat_type="Fantasy Points in Games 1+2", line_value=line,
        game_time=datetime.utcnow() + timedelta(days=3),
        external_id="own-prop", game_id="own-event", line_variant="standard",
        provider_status="scheduled", fetched_at=datetime.utcnow(), raw=None,
        available_directions=("OVER", "UNDER"),
    )
    fields.update(kwargs)
    return CanonicalPlayerProp(**fields)


@pytest.mark.parametrize("hours", [0.05, 3, 25, 72, 168])
def test_immediate_timing_has_no_calendar_or_hours_wait(hours):
    assert is_game_alertable(
        datetime.utcnow() + timedelta(hours=hours), min_minutes=0,
        max_minutes=None, urgent_edge=0, edge=0, require_known=True,
    )[0]


@pytest.mark.parametrize("game_time,status", [
    (None, None), (datetime.utcnow() - timedelta(minutes=1), "scheduled"),
    (datetime.utcnow() + timedelta(days=3), "final"),
    (datetime.utcnow() + timedelta(days=3), "live"),
])
def test_immediate_timing_still_rejects_unsafe_events(game_time, status):
    assert not is_game_alertable(
        game_time, game_status=status, min_minutes=0, max_minutes=None,
        urgent_edge=0, edge=0, require_known=True,
    )[0]


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_actual_sender_blocks_later_reporting_day(db, monkeypatch, provider):
    current = prop(provider)
    record = dict(
        provider=provider, external_id=current.external_id,
        player_name=current.player_name, team=current.team, sport=current.sport,
        stat_type=current.stat_type, line_value=current.line_value,
        game_time=current.game_time, game_id=current.game_id,
    )
    await db.track_prop_records([record])
    await db.log_prop_opportunity(
        **record, recommendation="OVER", confidence=80, decision_tier="A",
    )
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    monkeypatch.setattr(alerts, "send_discord_alert", AsyncMock(return_value=False))
    monkeypatch.setattr("alert_scope_filter.check", lambda _: NS(allowed=True))
    result = await alerts.AlertDelivery(db, object(), [123]).deliver_underdog(
        player_name=current.player_name, team=current.team, sport=current.sport,
        stat_type=current.stat_type, old_line=current.line_value,
        new_line=current.line_value, game_time=current.game_time,
        strict_pregame=True, fetched_at=current.fetched_at,
        decision=NS(recommendation="OVER"), validation=None,
        source=provider, source_id=current.external_id, event_id=current.game_id,
    )
    assert not result.sent and result.filtered
    assert "Central reporting day" in result.filtered_reason
    send.assert_not_awaited()


def test_short_verified_optional_history_does_not_impose_five_games():
    data = NS(data_source="primary", n_real_games=1, has_supporting_data=True)
    assert optional_sport_evidence_is_usable("DOTA", data)
    assert optional_sport_evidence_is_usable("TENNIS", None)
    assert not market_engine._has_delivery_evidence(
        sport="MLB", validation=data, decision=NS(recommendation="OVER"),
    )


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_line_changes_are_distinct_source_owned_deduplicated_notifications(
    db, monkeypatch, provider,
):
    await movement.reset()
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    before = prop(provider)
    after = replace(before, line_value=11.5, fetched_at=before.fetched_at + timedelta(seconds=1))
    movement.observe_persisted_batch(db, object(), [123], [before, after, after])
    await movement._worker
    assert send.await_count == 1
    message = send.call_args.args[2]
    assert "<b>LINE CHANGE</b>" in message
    assert f"<b>Platform:</b> {provider}" in message
    assert "<b>Previous line:</b> 10.5" in message
    assert "<b>Current line:</b> 11.5" in message
    assert "not a new bet pick" in message
    async with db.session() as session:
        rows = list((await session.scalars(select(TelegramDeliveryAudit))).all())
    assert len(rows) == 1 and rows[0].alert_kind == "LINE_CHANGE"
    assert rows[0].delivery_key
    # Restart the transient observer and replay the same verified transition.
    await movement.reset()
    movement.observe_persisted_batch(db, object(), [123], [before, after])
    await movement._worker
    assert send.await_count == 1
    state = await db.get_telegram_delivery_state(
        provider=provider, external_id=before.external_id, stat_type=before.stat_type,
    )
    assert state == dict(tracked=False, directional=False, already_delivered=False)


async def test_movement_does_not_cross_events_platforms_or_variants(db, monkeypatch):
    await movement.reset()
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    before = prop()
    for current in [
        before, replace(before, line_value=11.5, provider="PrizePicks"),
        replace(before, line_value=12.5, game_id="different-event"),
        replace(before, line_value=13.5, line_variant="goblin"),
        replace(before, line_value=14.5, removed=True),
        replace(before, line_value=15.5, game_time=datetime.utcnow()-timedelta(hours=1)),
    ]:
        movement.observe_persisted_batch(db, object(), [123], [current])
    assert movement._worker is None
    send.assert_not_awaited()


async def test_pending_movement_coalesces_without_unbounded_tasks(db, monkeypatch):
    await movement.reset()
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    before = prop()
    movement.observe_persisted_batch(db, object(), [123], [before])
    for i in range(1, 20):
        movement.observe_persisted_batch(db, object(), [123], [
            replace(before, line_value=10.5+i, fetched_at=before.fetched_at+timedelta(seconds=i))
        ])
    assert len(movement._pending) == 1
    task = movement._worker
    await task
    assert movement._worker is task
    assert send.await_count == 1
    assert "<b>Previous line:</b> 10.5" in send.call_args.args[2]
    assert "<b>Current line:</b> 29.5" in send.call_args.args[2]


async def test_coalesced_reversal_does_not_notify_an_unchanged_line(db, monkeypatch):
    await movement.reset()
    send = AsyncMock()
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    before = prop()
    movement.observe_persisted_batch(db, object(), [123], [
        before,
        replace(before, line_value=11.5, fetched_at=before.fetched_at+timedelta(seconds=1)),
        replace(before, fetched_at=before.fetched_at+timedelta(seconds=2)),
    ])
    if movement._worker:
        await movement._worker
    send.assert_not_awaited()


async def test_notification_cannot_overwrite_existing_pick_line_or_direction(db, monkeypatch):
    await movement.reset()
    before = prop()
    await db.log_prop_opportunity(
        external_id=before.external_id, player_name=before.player_name,
        team=before.team, sport=before.sport, stat_type=before.stat_type,
        line_value=before.line_value, recommendation="UNDER", decision_tier="A",
        confidence=80, game_time=before.game_time, game_id=before.game_id,
        provider=before.provider,
    )
    send = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", send)
    after = replace(before, line_value=11.5, fetched_at=before.fetched_at+timedelta(seconds=1))
    movement.observe_persisted_batch(db, object(), [123], [before, after])
    await movement._worker
    async with db.session() as session:
        row = await session.scalar(select(PropOpportunityLog))
        assert row.line_value == 10.5 and row.recommendation == "UNDER"
        assert not row.alert_sent


async def test_prizepicks_runs_the_same_decision_fallback_without_hit_windows(db, monkeypatch):
    monkeypatch.setattr(market_engine, "_propline_provider", NS(is_available=lambda: True))
    monkeypatch.setattr(market_engine, "_prizepicks_processing_lock", None)
    monkeypatch.setattr(market_engine, "_fetch_and_compute_hit_rates", AsyncMock(return_value=None))
    decide = Mock(return_value=NS(
        recommendation="PASS", confidence=0, reason="No verified direction",
        tier="PASS",
    ))
    monkeypatch.setattr("engine.ud_bet_decision.make_ud_bet_decision", decide)
    await market_engine._run_prizepicks_independent_cycle(
        NS(bot=object(), bot_data={"db": db}),
        _fetched_props=[prop("PrizePicks")], _fetched_persisted=True,
    )
    decide.assert_called_once()
    assert decide.call_args.kwargs["hit_rates"] is None


async def test_complete_batch_evaluates_before_next_paced_request(monkeypatch):
    evaluation_started = asyncio.Event()
    release_second_request = asyncio.Event()
    saved = []
    current = prop("PrizePicks")

    async def fetch(sport, bookmaker):
        if bookmaker == "underdog":
            await evaluation_started.wait()
            await release_second_request.wait()
            return PropLineFetchResult([], datetime.utcnow(), succeeded=True, complete=True)
        return PropLineFetchResult([current], datetime.utcnow(), succeeded=True, complete=True)

    async def persist(db, props, provider):
        saved.extend(props)
        return len(props)

    async def evaluate(context, **kwargs):
        assert saved and kwargs["_fetched_persisted"]
        assert all(p.provider == "PrizePicks" for p in kwargs["_fetched_props"])
        evaluation_started.set()

    monkeypatch.setattr(market_engine, "_propline_provider",
                        NS(is_available=lambda: True, fetch_props_for_sport_result=fetch))
    for name in ("_propline_request_lock", "_prizepicks_cycle_task"):
        monkeypatch.setattr(market_engine, name, None)
    for name in ("_propline_request_cursor", "_propline_bookmaker_cursor"):
        monkeypatch.setattr(market_engine, name, 0)
    for name in ("_propline_pending_props", "_propline_empty_sport_until", "_propline_empty_feed_streak"):
        monkeypatch.setattr(market_engine, name, {})
    monkeypatch.setattr(market_engine, "config",
                        NS(propline_bookmakers=["prizepicks", "underdog"], allowed_user_ids=[]))
    monkeypatch.setattr(market_engine, "_get_prizepicks_scan_sports", AsyncMock(return_value=("DOTA",)))
    monkeypatch.setattr(market_engine, "get_health_monitor", lambda: None)
    monkeypatch.setattr(market_engine, "get_health_tracker", lambda: None)
    monkeypatch.setattr(market_engine, "_queue_propline_history_targets", lambda *a, **k: 0)
    monkeypatch.setattr(market_engine, "_persist_propline_observations", persist)
    monkeypatch.setattr(market_engine, "_run_prizepicks_independent_cycle", evaluate)
    task = asyncio.create_task(market_engine.propline_request_scan_job(NS(bot=object(), bot_data={"db": object()})))
    try:
        await asyncio.wait_for(evaluation_started.wait(), timeout=2)
        assert not task.done(), "evaluation must not wait for the second provider"
        first_worker = market_engine._prizepicks_cycle_task
        market_engine._start_prizepicks_handoff(NS(bot=object()))
        assert market_engine._prizepicks_cycle_task is first_worker
    finally:
        release_second_request.set()
        await asyncio.wait_for(task, timeout=2)
        if market_engine._prizepicks_cycle_task:
            await market_engine._prizepicks_cycle_task


async def test_durable_dota_history_survives_short_background_refresh(db):
    clear()
    target = ("Dota Fixture", "DOTA", "Fantasy Points in Games 1+2")
    records = [
        NS(player_name=target[0], sport=target[1], stat_type=target[2],
           game_date=(datetime.utcnow()-timedelta(days=i+1)).date().isoformat(),
           actual_value=120+i, source="opendota_series", game_id=f"game-{i}", opponent="Opp")
        for i in range(13)
    ]
    await db.upsert_player_results_bulk(records)
    put_player_results(*target, records[:2])
    assert len(get_player_results(*target)) == 2
    await _hydrate_completed_history_cache(db, [target])
    assert len(get_player_results(*target)) == 13
    results = market_engine._candidate_player_results(db, *target)
    assert len(results) == 13
    hits = await market_engine._fetch_and_compute_hit_rates(
        db, *target, 105.6, allow_remote=False, preloaded_results=results,
    )
    assert hits.l5.over_count == 5 and hits.l10.over_count == 10
