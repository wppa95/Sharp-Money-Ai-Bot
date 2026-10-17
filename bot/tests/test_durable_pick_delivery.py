"""Regression tests for durable, paced individual-pick delivery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter
from sqlalchemy import select

import alerts
from database import (
    Database,
    PropLineHistory,
    PropOpportunityLog,
    TelegramDeliveryAudit,
    TelegramPickDelivery,
    TelegramPickDeliveryAttempt,
    UnderdogSnapshotRecord,
)


def _qualification(direction: str = "OVER") -> str:
    return json.dumps(
        {
            "qualified": True,
            "direction_and_evidence": {"passed": True, "direction": direction},
            "originating_platform_direction": {
                "passed": True,
                "available_directions": [direction],
            },
        }
    )


def _evidence(direction: str = "OVER") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "direction": direction,
            "line": 1.5,
            "l10_games": 10,
            "l10_hit_rate": 0.7,
            "evidence_timestamp": datetime.utcnow().isoformat(),
        }
    )


def _item(
    *,
    provider: str = "Underdog",
    sport: str = "NHL",
    source_id: str = "source-1",
    event_id: str = "event-1",
    player: str = "Sample Player",
    stat: str = "Goals",
    line: float = 1.5,
    direction: str = "OVER",
    game_time: datetime | None = None,
    fetched_at: datetime | None = None,
    rank: int = 1,
    ranking_scope: str | None = None,
) -> dict:
    now = datetime.utcnow()
    return {
        "pick_key": (
            f"{provider}:{source_id}:{event_id}:{player}:{sport}:{stat}:"
            f"{line}:{direction}"
        ),
        "provider": provider,
        "source_id": source_id,
        "event_id": event_id,
        "player_name": player,
        "sport": sport,
        "stat_type": stat,
        "line_variant": "",
        "line_value": line,
        "direction": direction,
        "delivery_path": provider.lower(),
        "game_time": game_time or now + timedelta(hours=2),
        "game_status": "scheduled",
        "source_fetched_at": fetched_at or now,
        "qualification_json": _qualification(direction),
        "evidence_snapshot_json": _evidence(direction),
        "candidate_rank": rank,
        "ranking_scope": ranking_scope or f"sport:{sport}",
        "message_html": f"<b>{player} — {direction} {line} {stat}</b>",
    }


async def _make_db(tmp_path, name: str = "delivery.sqlite3") -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / name}")
    await db.init()
    return db


async def _close_db(db: Database) -> None:
    await db._engine.dispose()


async def _seed_underdog(db: Database, item: dict, *, line: float | None = None):
    async with db.session() as session:
        session.add(
            UnderdogSnapshotRecord(
                external_id=item["source_id"],
                player_name=item["player_name"],
                team="",
                sport=item["sport"],
                stat_type=item["stat_type"],
                line_value=item["line_value"] if line is None else line,
                game_id=item["event_id"],
                game_time=item["game_time"],
                provider_status=item["game_status"],
                removed=False,
                fetched_at=item["source_fetched_at"],
            )
        )
        await session.commit()


async def _seed_opportunity(
    db: Database,
    item: dict,
    *,
    direction: str | None = None,
    line: float | None = None,
):
    from database import _stored_opportunity_external_id

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(
            PropOpportunityLog(
                external_id=_stored_opportunity_external_id(
                    item["source_id"], item["provider"]
                ),
                player_name=item["player_name"],
                team="",
                sport=item["sport"],
                stat_type=item["stat_type"],
                line_value=item["line_value"] if line is None else line,
                recommendation=direction or item["direction"],
                decision_tier="A",
                confidence=80,
                game_time=item["game_time"],
                game_id=item["event_id"],
                provider=item["provider"],
                detected_at=now,
            )
        )
        await session.commit()


class _FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def send_message(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=self.calls)


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_fair_across_platform_and_sport(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        candidates = [
            _item(provider="Underdog", sport="NHL", source_id="ud-1"),
            _item(provider="PrizePicks", sport="NHL", source_id="pp-1"),
            _item(provider="Underdog", sport="NBA", source_id="ud-2"),
        ]
        candidates = [
            {**item, "source_fetched_at": now, "game_time": now + timedelta(hours=2)}
            for item in candidates
        ]
        ids = await db.enqueue_telegram_pick_deliveries(candidates + [candidates[0]])
        claim_start = datetime.utcnow() + timedelta(seconds=1)
        assert ids[0] == ids[3]

        claimed = []
        for index in range(3):
            timestamp = claim_start + timedelta(seconds=index)
            item = await db.claim_next_telegram_pick_delivery(
                now=timestamp, pacing_seconds=0
            )
            assert item is not None
            claimed.append((item["provider"], item["sport"]))
            await db.finish_telegram_pick_delivery(
                delivery_id=item["id"],
                attempt_id=item["attempt_id"],
                status="sent",
                now=timestamp,
            )
        assert claimed == [
            ("Underdog", "NHL"),
            ("PrizePicks", "NHL"),
            ("Underdog", "NBA"),
        ]
        async with db.session() as session:
            rows = (await session.execute(
                __import__("sqlalchemy").select(TelegramPickDelivery)
            )).scalars().all()
            assert len(rows) == 3
            assert {row.status for row in rows} == {"sent"}
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_pending_delivery_survives_database_reopen(tmp_path):
    first_db = await _make_db(tmp_path, "restart.sqlite3")
    candidate = _item()
    await first_db.enqueue_telegram_pick_deliveries([candidate])
    await _close_db(first_db)

    reopened = await _make_db(tmp_path, "restart.sqlite3")
    try:
        claim = await reopened.claim_next_telegram_pick_delivery(
            now=datetime.utcnow() + timedelta(seconds=2),
            pacing_seconds=0,
        )
        assert claim is not None
        assert claim["pick_key"] == candidate["pick_key"]
        async with reopened.session() as session:
            row = await session.get(TelegramPickDelivery, claim["id"])
            assert row.status == "sending"
    finally:
        await _close_db(reopened)


@pytest.mark.asyncio
async def test_retry_after_is_persisted_and_not_retried_before_cooldown(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.enqueue_telegram_pick_deliveries([_item()])
        now = datetime.utcnow() + timedelta(seconds=1)
        first = await db.claim_next_telegram_pick_delivery(
            now=now, pacing_seconds=0
        )
        assert first is not None
        await db.finish_telegram_pick_delivery(
            delivery_id=first["id"],
            attempt_id=first["attempt_id"],
            status="retry_scheduled",
            retry_after_seconds=45,
            exception_class="RetryAfter",
            reason="server cooldown",
            now=now,
        )
        assert await db.claim_next_telegram_pick_delivery(
            now=now + timedelta(seconds=44), pacing_seconds=0
        ) is None
        second = await db.claim_next_telegram_pick_delivery(
            now=now + timedelta(seconds=45), pacing_seconds=0
        )
        assert second is not None
        assert second["attempt_id"] != first["attempt_id"]
        async with db.session() as session:
            delivery_row = await session.get(TelegramPickDelivery, first["id"])
            attempts = (await session.execute(
                __import__("sqlalchemy").select(TelegramPickDeliveryAttempt)
            )).scalars().all()
            assert delivery_row.attempt_count == 2
            assert [attempt.status for attempt in attempts] == [
                "retry_scheduled",
                "sending",
            ]
            assert attempts[0].retry_after_seconds == 45
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_interrupted_send_is_terminal_to_prevent_duplicate_delivery(tmp_path):
    db = await _make_db(tmp_path)
    try:
        await db.enqueue_telegram_pick_deliveries([_item()])
        now = datetime.utcnow() + timedelta(seconds=1)
        first = await db.claim_next_telegram_pick_delivery(
            now=now, lease_seconds=20, pacing_seconds=0
        )
        assert first is not None
        assert await db.claim_next_telegram_pick_delivery(
            now=now + timedelta(seconds=31), lease_seconds=20, pacing_seconds=0
        ) is None
        async with db.session() as session:
            row = await session.get(TelegramPickDelivery, first["id"])
            attempt = await session.get(
                TelegramPickDeliveryAttempt, first["attempt_id"]
            )
            assert row.status == "failed"
            assert row.final_reason == "process_interrupted_unknown"
            assert attempt.status == "failed"
            assert attempt.exception_class == "ProcessInterrupted"
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_source_line_mismatch_invalidates_without_telegram_send(
    tmp_path, monkeypatch
):
    db = await _make_db(tmp_path)
    try:
        candidate = _item(line=1.5)
        await _seed_underdog(db, candidate, line=2.5)
        await db.enqueue_telegram_pick_deliveries([candidate])
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        bot = _FakeBot()

        await alerts.telegram_pick_delivery_job(
            SimpleNamespace(bot_data={"db": db}, bot=bot)
        )

        assert bot.calls == 0
        async with db.session() as session:
            row = (await session.execute(
                __import__("sqlalchemy").select(TelegramPickDelivery)
            )).scalars().one()
            attempt = (await session.execute(
                __import__("sqlalchemy").select(TelegramPickDeliveryAttempt)
            )).scalars().one()
            assert row.status == "invalidated"
            assert "line" in row.final_reason
            assert attempt.status == "invalidated"
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_changed_directional_opportunity_invalidates_pending_pick(
    tmp_path, monkeypatch
):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        candidate = _item(
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        await _seed_underdog(db, candidate)
        await _seed_opportunity(db, candidate, direction="UNDER")
        await db.enqueue_telegram_pick_deliveries([candidate])
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        bot = _FakeBot()

        await alerts.telegram_pick_delivery_job(
            SimpleNamespace(bot_data={"db": db}, bot=bot)
        )

        assert bot.calls == 0
        async with db.session() as session:
            row = (await session.execute(
                select(TelegramPickDelivery)
            )).scalars().one()
            attempt = (await session.execute(
                select(TelegramPickDeliveryAttempt)
            )).scalars().one()
            assert row.status == "invalidated"
            assert row.final_reason == "qualified_line_or_direction_changed"
            assert attempt.status == "invalidated"
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_expired_reporting_day_is_not_sent(tmp_path, monkeypatch):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        candidate = _item(
            game_time=now + timedelta(days=2),
            fetched_at=now,
        )
        await _seed_underdog(db, candidate)
        await _seed_opportunity(db, candidate)
        await db.enqueue_telegram_pick_deliveries([candidate])
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        bot = _FakeBot()

        await alerts.telegram_pick_delivery_job(
            SimpleNamespace(bot_data={"db": db}, bot=bot)
        )

        assert bot.calls == 0
        async with db.session() as session:
            row = (await session.execute(
                select(TelegramPickDelivery)
            )).scalars().one()
            assert row.status == "expired"
            assert "reporting day" in row.final_reason
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_worker_persists_retry_after_exception_and_evidence(
    tmp_path, monkeypatch
):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        candidate = _item(
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        await _seed_underdog(db, candidate)
        await _seed_opportunity(db, candidate)
        await db.enqueue_telegram_pick_deliveries([candidate])
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        bot = _FakeBot(error=RetryAfter(45))

        await alerts.telegram_pick_delivery_job(
            SimpleNamespace(bot_data={"db": db}, bot=bot)
        )

        assert bot.calls == 1
        async with db.session() as session:
            row = (await session.execute(
                __import__("sqlalchemy").select(TelegramPickDelivery)
            )).scalars().one()
            attempt = (await session.execute(
                __import__("sqlalchemy").select(TelegramPickDeliveryAttempt)
            )).scalars().one()
            assert row.status == "pending"
            assert row.next_attempt_at is not None
            assert attempt.status == "retry_scheduled"
            assert attempt.exception_class == "RetryAfter"
            assert attempt.retry_after_seconds == 45
            assert json.loads(attempt.evidence_snapshot_json)["l10_games"] == 10
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_confirmed_send_is_audited_and_never_sent_twice(tmp_path, monkeypatch):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        candidate = _item(
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        await _seed_underdog(db, candidate)
        async with db.session() as session:
            session.add(
                PropOpportunityLog(
                    external_id=candidate["source_id"],
                    player_name=candidate["player_name"],
                    team="",
                    sport=candidate["sport"],
                    stat_type=candidate["stat_type"],
                    line_value=candidate["line_value"],
                    recommendation=candidate["direction"],
                    decision_tier="A",
                    confidence=80,
                    game_time=candidate["game_time"],
                    game_id=candidate["event_id"],
                    provider=candidate["provider"],
                    detected_at=now,
                    alert_sent=False,
                )
            )
            await session.commit()
        await db.enqueue_telegram_pick_deliveries([candidate])
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        bot = _FakeBot()
        context = SimpleNamespace(bot_data={"db": db}, bot=bot)

        await alerts.telegram_pick_delivery_job(context)
        await db.enqueue_telegram_pick_deliveries([candidate])
        await alerts.telegram_pick_delivery_job(context)

        assert bot.calls == 1
        async with db.session() as session:
            delivery_row = (
                await session.execute(select(TelegramPickDelivery))
            ).scalars().one()
            attempt = (
                await session.execute(select(TelegramPickDeliveryAttempt))
            ).scalars().one()
            audit = (
                await session.execute(
                    select(TelegramDeliveryAudit).where(
                        TelegramDeliveryAudit.alert_kind == "ACTIONABLE_PICK"
                    )
                )
            ).scalars().one()
            opportunity = (
                await session.execute(select(PropOpportunityLog))
            ).scalars().one()
            assert delivery_row.status == "sent"
            assert attempt.status == "sent"
            assert audit.outcome == "sent"
            assert audit.line_value == candidate["line_value"]
            assert json.loads(audit.evidence_snapshot_json)["direction"] == "OVER"
            assert opportunity.alert_sent is True
            assert opportunity.line_value == candidate["line_value"]
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_permanent_telegram_rejection_is_terminal_and_logged(
    tmp_path, monkeypatch
):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        candidate = _item(
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        await _seed_underdog(db, candidate)
        await _seed_opportunity(db, candidate)
        await db.enqueue_telegram_pick_deliveries([candidate])
        monkeypatch.setenv("ALLOWED_USER_IDS", "12345")
        bot = _FakeBot(error=BadRequest("invalid message"))
        context = SimpleNamespace(bot_data={"db": db}, bot=bot)

        await alerts.telegram_pick_delivery_job(context)
        await alerts.telegram_pick_delivery_job(context)

        assert bot.calls == 1
        async with db.session() as session:
            delivery_row = (
                await session.execute(select(TelegramPickDelivery))
            ).scalars().one()
            attempt = (
                await session.execute(select(TelegramPickDeliveryAttempt))
            ).scalars().one()
            assert delivery_row.status == "failed"
            assert delivery_row.last_error_class == "BadRequest"
            assert attempt.status == "failed"
            assert attempt.exception_class == "BadRequest"
            assert json.loads(attempt.evidence_snapshot_json)["l10_games"] == 10
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_alert_sent_requires_an_exact_successful_delivery_audit(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        async with db.session() as session:
            session.add(
                PropOpportunityLog(
                    external_id="ud-source",
                    player_name="Sample Player",
                    team="",
                    sport="NHL",
                    stat_type="Goals",
                    line_value=1.5,
                    recommendation="OVER",
                    decision_tier="A",
                    confidence=80,
                    game_time=now + timedelta(hours=2),
                    game_id="ud-event",
                    provider="Underdog",
                    detected_at=now,
                    alert_sent=False,
                )
            )
            await session.commit()

        assert await db.mark_opportunity_alert_sent(
            "ud-source", "Goals", provider="Underdog"
        ) == 0

        confirmed = await db.record_telegram_play_delivery(
            provider="Underdog",
            delivery_path="underdog",
            player_name="Sample Player",
            sport="NHL",
            stat_type="Goals",
            external_id="ud-source",
            event_id="ud-event",
            line_value=1.5,
            recipients_sent=1,
            recipients_failed=0,
            evidence_snapshot={"direction": "OVER"},
            direction="OVER",
        )
        assert confirmed is True
        assert await db.mark_opportunity_alert_sent(
            "ud-source", "Goals", provider="Underdog"
        ) == 1
        async with db.session() as session:
            row = (await session.execute(
                __import__("sqlalchemy").select(PropOpportunityLog)
            )).scalars().one()
            assert row.alert_sent is True
            assert row.alert_sent_at is not None
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_unqualified_candidate_does_not_enter_delivery_queue(monkeypatch):
    now = datetime.utcnow()
    db = SimpleNamespace()
    delivery = alerts.AlertDelivery(db, _FakeBot(), [12345])
    result = await delivery.deliver_underdog(
        player_name="Sample Player",
        team="",
        sport="NHL",
        stat_type="Goals",
        old_line=1.5,
        new_line=1.5,
        game_time=now + timedelta(hours=2),
        game_status="scheduled",
        fetched_at=now,
        score=SimpleNamespace(total=80),
        validation=SimpleNamespace(),
        decision=SimpleNamespace(recommendation="PASS", confidence=80),
        standing=True,
        strict_pregame=True,
        source="Underdog",
        source_id="source-1",
        event_id="event-1",
        defer_delivery=True,
    )
    assert result.filtered is True
    assert result.queued is False
    assert "qualification" in (result.filtered_reason or "").lower() or (
        "direction" in (result.filtered_reason or "").lower()
    )


@pytest.mark.asyncio
async def test_send_alert_paces_chat_and_honors_retry_after(monkeypatch):
    monkeypatch.setattr(alerts, "_TELEGRAM_MIN_CHAT_INTERVAL_SECONDS", 0.02)
    times = []

    class TimedBot:
        async def send_message(self, **_kwargs):
            import time
            times.append(time.monotonic())
            return SimpleNamespace(message_id=len(times))

    bot = TimedBot()
    await alerts.send_alert_detailed(bot, 98765, "first")
    await alerts.send_alert_detailed(bot, 98765, "second")
    assert len(times) == 2
    assert times[1] - times[0] >= 0.015

    blocked = _FakeBot(error=RetryAfter(30))
    first = await alerts.send_alert_detailed(blocked, 98766, "rate limited")
    second = await alerts.send_alert_detailed(blocked, 98766, "cooldown")
    assert first.retry_after_seconds == 30
    assert second.retry_after_seconds is not None
    assert blocked.calls == 1


@pytest.mark.asyncio
async def test_propline_history_is_its_own_provider_record(tmp_path):
    db = await _make_db(tmp_path)
    try:
        candidate = _item(
            provider="PrizePicks",
            source_id="pp-source",
            event_id="pp-event",
            sport="NBA",
            stat="Points",
            line=19.5,
        )
        candidate["line_variant"] = "standard"
        await _seed_opportunity(db, candidate)
        async with db.session() as session:
            session.add(
                PropLineHistory(
                    provider="PrizePicks",
                    product="PLAYER_PROPS",
                    sport="NBA",
                    player_name="Sample Player",
                    team="",
                    stat_type="Points",
                    line_value=19.5,
                    game_time=candidate["game_time"],
                    provider_status="scheduled",
                    external_id="pp-source",
                    game_id="pp-event",
                    line_variant="standard",
                    fetched_at=candidate["source_fetched_at"],
                    first_seen=candidate["source_fetched_at"],
                    last_seen=candidate["source_fetched_at"],
                    change_count=0,
                    removed=False,
                    lifecycle_state="ACTIVE",
                )
            )
            await session.commit()
        result = await db.validate_telegram_pick_source(candidate)
        assert result["valid"] is True
        assert result["provider"] == "PrizePicks"
        assert result["line_value"] == 19.5
        assert result["event_id"] == "pp-event"
        assert result["line_variant"] == "standard"
        mismatched_variant = {**candidate, "line_variant": "alternate"}
        assert (
            await db.validate_telegram_pick_source(mismatched_variant)
        )["valid"] is False
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_confirmed_transport_without_direction_does_not_mark_sent(tmp_path):
    db = await _make_db(tmp_path)
    try:
        candidate = _item()
        await _seed_opportunity(db, candidate)

        recorded = await db.record_telegram_play_delivery(
            provider=candidate["provider"],
            delivery_path="standing",
            player_name=candidate["player_name"],
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=candidate["source_id"],
            event_id=candidate["event_id"],
            line_value=candidate["line_value"],
            recipients_sent=1,
            recipients_failed=0,
        )

        assert recorded is False
        async with db.session() as session:
            opportunity = (
                await session.execute(select(PropOpportunityLog))
            ).scalars().one()
            audit = await session.scalar(select(TelegramDeliveryAudit))
            assert opportunity.alert_sent is False
            assert audit is None
    finally:
        await _close_db(db)


def test_actionable_pick_audit_identity_includes_side():
    from database import _telegram_delivery_key

    identity = {
        "provider": "Underdog",
        "alert_kind": "ACTIONABLE_PICK",
        "delivery_path": "standing",
        "external_id": "source-1",
        "event_id": "event-1",
        "stat_type": "Goals",
        "line_value": 1.5,
    }
    over = _telegram_delivery_key(**identity, direction="OVER")
    under = _telegram_delivery_key(**identity, direction="UNDER")
    legacy = _telegram_delivery_key(**identity)

    assert over is not None
    assert under is not None
    assert over != under
    assert legacy is not None
    assert legacy not in {over, under}
