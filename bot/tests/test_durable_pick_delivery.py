"""Regression tests for durable, paced individual-pick delivery."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter
from sqlalchemy import select

import alerts
from database import (
    CandidateLifecycleRecord,
    Database,
    PropLineHistory,
    PropOpportunityLog,
    TelegramDeliveryAudit,
    TelegramPickDelivery,
    TelegramPickDeliveryAttempt,
    TelegramPickDeliveryRuntime,
    UnderdogSnapshotRecord,
    telegram_pick_queue_key,
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
    line_variant: str = "",
    game_time: datetime | None = None,
    fetched_at: datetime | None = None,
    rank: int = 1,
    ranking_scope: str | None = None,
) -> dict:
    now = datetime.utcnow()
    return {
        "pick_key": telegram_pick_queue_key(
            provider=provider,
            source_id=source_id,
            event_id=event_id,
            player_name=player,
            sport=sport,
            stat_type=stat,
            line_variant=line_variant,
            line_value=line,
            direction=direction,
        ),
        "provider": provider,
        "source_id": source_id,
        "event_id": event_id,
        "player_name": player,
        "sport": sport,
        "stat_type": stat,
        "line_variant": line_variant,
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


async def _seed_pp_history(db: Database, item: dict, *, line_variant=None):
    variant = (
        str(item.get("line_variant") or "")
        if line_variant is None else str(line_variant or "")
    )
    async with db.session() as session:
        session.add(
            PropLineHistory(
                provider="PrizePicks",
                product="Player Props",
                sport=item["sport"],
                player_name=item["player_name"],
                team="",
                stat_type=item["stat_type"],
                line_value=item["line_value"],
                game_time=item["game_time"],
                provider_status="scheduled",
                external_id=item["source_id"],
                game_id=item["event_id"],
                line_variant=variant,
                fetched_at=item["source_fetched_at"],
                first_seen=item["source_fetched_at"],
                last_seen=item["source_fetched_at"],
                change_count=0,
                removed=False,
                lifecycle_state="ACTIVE",
            )
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        "source_id", "event_id", "player_name", "sport",
        "stat_type", "line_value", "direction", "line_variant",
    ],
)
async def test_prizepicks_queue_rejects_non_exact_source_identity(tmp_path, mismatch):
    db = await _make_db(tmp_path, f"pp_identity_{mismatch}.sqlite3")
    try:
        original = _item(
            provider="PrizePicks",
            sport="NBA",
            source_id="pp-source",
            event_id="pp-event",
            player="Sample Player",
            stat="Points",
            line=19.5,
            line_variant="standard",
        )
        await _seed_pp_history(db, original)
        await _seed_opportunity(db, original)

        altered = {
            "source_id": "other-source",
            "event_id": "other-event",
            "player_name": "Different Player",
            "sport": "NHL",
            "stat_type": "Goals",
            "line_value": 20.5,
            "direction": "UNDER",
            "line_variant": "alternate",
        }[mismatch]
        changes = {}
        if mismatch == "player_name":
            changes["player"] = altered
        elif mismatch == "sport":
            changes["sport"] = altered
        elif mismatch == "stat_type":
            changes["stat"] = altered
        elif mismatch == "line_value":
            changes["line"] = altered
        elif mismatch == "direction":
            changes["direction"] = altered
        elif mismatch == "line_variant":
            changes["line_variant"] = altered
        elif mismatch == "source_id":
            changes["source_id"] = altered
        else:
            changes["event_id"] = altered
        candidate = _item(
            provider="PrizePicks",
            sport=changes.get("sport", original["sport"]),
            source_id=changes.get("source_id", original["source_id"]),
            event_id=changes.get("event_id", original["event_id"]),
            player=changes.get("player", original["player_name"]),
            stat=changes.get("stat", original["stat_type"]),
            line=changes.get("line", original["line_value"]),
            direction=changes.get("direction", original["direction"]),
            line_variant=changes.get("line_variant", original["line_variant"]),
        )
        with pytest.raises(ValueError, match="exact PropLine source line"):
            await db.enqueue_telegram_pick_deliveries([candidate])
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_prizepicks_queue_and_success_audit_advance_exact_lifecycle(tmp_path):
    db = await _make_db(tmp_path, "pp_lifecycle.sqlite3")
    try:
        evaluation_id = "a" * 32
        candidate = _item(
            provider="PrizePicks",
            sport="NBA",
            source_id="pp-source",
            event_id="pp-event",
            player="Sample Player",
            stat="Points",
            line=19.5,
            line_variant="standard",
        )
        await _seed_pp_history(db, candidate)
        await _seed_opportunity(db, candidate)
        evidence = json.loads(candidate["evidence_snapshot_json"])
        evidence["candidate_lifecycle_id"] = evaluation_id
        candidate["evidence_snapshot_json"] = json.dumps(evidence)
        await db.record_candidate_lifecycle_batch([{
            "evaluation_id": evaluation_id,
            "provider": candidate["provider"],
            "source_id": candidate["source_id"],
            "event_id": candidate["event_id"],
            "player_name": candidate["player_name"],
            "sport": candidate["sport"],
            "stat_type": candidate["stat_type"],
            "line_variant": candidate["line_variant"],
            "line_value": candidate["line_value"],
            "direction": candidate["direction"],
            "delivery_path": "prizepicks",
            "qualification_json": {"passed_collection_gates": True},
            "evidence_snapshot_json": evidence,
        }])
        async with db.session() as session:
            lifecycle = await session.get(CandidateLifecycleRecord, evaluation_id)
            original_identity = lifecycle.pick_identity_key
            assert lifecycle.disposition == "directional_decision"

        queued_ids = await db.enqueue_telegram_pick_deliveries([candidate])
        assert len(queued_ids) == 1
        async with db.session() as session:
            lifecycle = await session.get(CandidateLifecycleRecord, evaluation_id)
            queue_row = await session.scalar(
                select(TelegramPickDelivery).where(
                    TelegramPickDelivery.id == queued_ids[0]
                )
            )
            assert lifecycle.disposition == "queue_admitted"
            assert lifecycle.pick_identity_key == original_identity
            assert queue_row.provider == "PrizePicks"
            assert queue_row.source_id == "pp-source"
            assert queue_row.line_value == 19.5
            assert queue_row.direction == "OVER"
            assert queue_row.line_variant == "standard"

        recorded = await db.record_telegram_play_delivery(
            provider="PrizePicks",
            delivery_path="prizepicks",
            player_name=candidate["player_name"],
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=candidate["source_id"],
            event_id=candidate["event_id"],
            line_value=candidate["line_value"],
            recipients_sent=1,
            recipients_failed=0,
            evidence_snapshot=evidence,
            direction=candidate["direction"],
            line_variant=candidate["line_variant"],
        )
        assert recorded is True
        async with db.session() as session:
            lifecycle = await session.get(CandidateLifecycleRecord, evaluation_id)
            assert lifecycle.disposition == "delivered"
            assert lifecycle.pick_identity_key == original_identity
            assert lifecycle.delivered_at is not None
            audit = await session.scalar(
                select(TelegramDeliveryAudit).where(
                    TelegramDeliveryAudit.provider == "PrizePicks",
                    TelegramDeliveryAudit.external_id == "pp-source",
                    TelegramDeliveryAudit.line_value == 19.5,
                )
            )
            assert audit is not None
            assert json.loads(audit.evidence_snapshot_json)["line_variant"] == "standard"
    finally:
        await _close_db(db)


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
        for candidate in candidates:
            if candidate["provider"] == "PrizePicks":
                await _seed_pp_history(db, candidate)
                await _seed_opportunity(db, candidate)
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
async def test_candidate_rank_ages_out_so_old_qualified_work_cannot_starve(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        aged = _item(source_id="aged-rank-8", rank=8)
        aged["qualified_at"] = now - timedelta(seconds=90)
        fresh_best = _item(source_id="fresh-rank-1", rank=1)
        fresh_best["qualified_at"] = now
        fresh_next = _item(source_id="fresh-rank-2", rank=2)
        fresh_next["qualified_at"] = now
        await db.enqueue_telegram_pick_deliveries(
            [aged, fresh_best, fresh_next]
        )

        first = await db.claim_next_telegram_pick_delivery(
            now=now + timedelta(seconds=1),
            pacing_seconds=0,
        )
        assert first["source_id"] == "aged-rank-8"
        await db.finish_telegram_pick_delivery(
            delivery_id=first["id"],
            attempt_id=first["attempt_id"],
            status="sent",
            now=now + timedelta(seconds=1),
        )

        second = await db.claim_next_telegram_pick_delivery(
            now=now + timedelta(seconds=2),
            pacing_seconds=0,
        )
        assert second["source_id"] == "fresh-rank-1"
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
        await _close_db(db)
        db = await _make_db(tmp_path)
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
@pytest.mark.parametrize(
    ("prior_direction", "candidate_direction", "expected_valid"),
    [
        ("UNDER", "OVER", True),
        ("OVER", "UNDER", True),
        ("OVER", "OVER", False),
    ],
)
async def test_prior_success_dedup_is_side_specific(
    tmp_path, prior_direction, candidate_direction, expected_valid
):
    db = await _make_db(tmp_path)
    try:
        candidate = _item(direction=candidate_direction)
        await _seed_underdog(db, candidate)
        await _seed_opportunity(db, candidate)
        await db.log_telegram_delivery(
            provider="Underdog",
            alert_kind="ACTIONABLE_PICK",
            delivery_path="underdog",
            player_name=candidate["player_name"],
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=candidate["source_id"],
            event_id=candidate["event_id"],
            line_value=candidate["line_value"],
            direction=prior_direction,
            line_variant="",
            evidence_snapshot={
                "direction": prior_direction,
                "line_variant": "",
            },
            outcome="sent",
            recipients_sent=1,
        )

        result = await db.validate_telegram_pick_source(candidate)
        assert result["valid"] is expected_valid
        if not expected_valid:
            assert result["reason"] == "already_confirmed_delivered"
    finally:
        await _close_db(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_variant", "expected_valid"),
    [("alternate", True), ("standard", False)],
)
async def test_prizepicks_prior_success_dedup_matches_line_variant(
    tmp_path, prior_variant, expected_valid
):
    db = await _make_db(tmp_path)
    try:
        candidate = _item(
            provider="PrizePicks",
            sport="NBA",
            source_id="pp-source",
            event_id="pp-event",
            stat="Points",
            line=19.5,
        )
        candidate["line_variant"] = "standard"
        await _seed_pp_history(db, candidate)
        await _seed_opportunity(db, candidate)
        await db.log_telegram_delivery(
            provider="PrizePicks",
            alert_kind="ACTIONABLE_PICK",
            delivery_path="prizepicks",
            player_name=candidate["player_name"],
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=candidate["source_id"],
            event_id=candidate["event_id"],
            line_value=candidate["line_value"],
            direction=candidate["direction"],
            line_variant=prior_variant,
            evidence_snapshot={
                "direction": candidate["direction"],
                "line_variant": prior_variant,
            },
            outcome="sent",
            recipients_sent=1,
        )

        result = await db.validate_telegram_pick_source(candidate)
        assert result["valid"] is expected_valid
        if not expected_valid:
            assert result["reason"] == "already_confirmed_delivered"
    finally:
        await _close_db(db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mismatch", "provider"),
    [
        ("player", "Underdog"),
        ("line", "Underdog"),
        ("direction", "Underdog"),
        ("source", "Underdog"),
        ("event", "Underdog"),
        ("line_variant", "PrizePicks"),
    ],
)
async def test_successful_audit_requires_complete_pick_identity(
    tmp_path, mismatch, provider
):
    db = await _make_db(tmp_path)
    try:
        candidate = _item(provider=provider)
        if provider == "PrizePicks":
            candidate.update(
                sport="NBA",
                source_id="pp-source",
                event_id="pp-event",
                stat_type="Points",
                line_value=19.5,
                line_variant="standard",
            )
        await _seed_opportunity(db, candidate)
        if provider == "PrizePicks":
            await _seed_pp_history(db, candidate)

        audit_player = candidate["player_name"]
        audit_line = candidate["line_value"]
        audit_direction = candidate["direction"]
        audit_source = candidate["source_id"]
        audit_event = candidate["event_id"]
        audit_variant = candidate.get("line_variant", "")
        if mismatch == "player":
            audit_player = "Different Player"
        elif mismatch == "line":
            audit_line += 1.0
        elif mismatch == "direction":
            audit_direction = "UNDER"
        elif mismatch == "source":
            audit_source = "different-source"
        elif mismatch == "event":
            audit_event = "different-event"
        elif mismatch == "line_variant":
            audit_variant = "alternate"

        await db.log_telegram_delivery(
            provider=provider,
            alert_kind="ACTIONABLE_PICK",
            delivery_path=provider.casefold(),
            player_name=audit_player,
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=audit_source,
            event_id=audit_event,
            line_value=audit_line,
            direction=audit_direction,
            line_variant=audit_variant,
            evidence_snapshot={
                "direction": audit_direction,
                "line_variant": audit_variant,
            },
            outcome="sent",
            recipients_sent=1,
        )

        assert await db.count_actionable_pick_records() == 0
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_exact_prizepicks_success_audit_matches_source_variant(tmp_path):
    db = await _make_db(tmp_path)
    try:
        candidate = _item(
            provider="PrizePicks",
            sport="NBA",
            source_id="pp-source",
            event_id="pp-event",
            stat="Points",
            line=19.5,
        )
        candidate["line_variant"] = "standard"
        await _seed_pp_history(db, candidate)
        await _seed_opportunity(db, candidate)

        recorded = await db.record_telegram_play_delivery(
            provider="PrizePicks",
            delivery_path="prizepicks",
            player_name=candidate["player_name"],
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=candidate["source_id"],
            event_id=candidate["event_id"],
            line_value=candidate["line_value"],
            recipients_sent=1,
            recipients_failed=0,
            evidence_snapshot={"direction": "OVER", "line_variant": "standard"},
            direction="OVER",
            line_variant="standard",
        )

        assert recorded is True
        assert await db.count_actionable_pick_records() == 1
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_actionable_pick_reporting_deduplicates_exact_legacy_and_current_audits(
    tmp_path,
):
    db = await _make_db(tmp_path)
    try:
        candidate = _item()
        await _seed_opportunity(db, candidate)
        async with db.session() as session:
            session.add(
                TelegramDeliveryAudit(
                    provider=candidate["provider"],
                    alert_kind="ACTIONABLE_PICK",
                    delivery_path=candidate["delivery_path"],
                    player_name=candidate["player_name"],
                    sport=candidate["sport"],
                    stat_type=candidate["stat_type"],
                    external_id=candidate["source_id"],
                    event_id=candidate["event_id"],
                    line_value=candidate["line_value"],
                    outcome="sent",
                    recipients_sent=1,
                    recipients_failed=0,
                    attempted_at=datetime.utcnow(),
                    evidence_snapshot_json=json.dumps(
                        {"direction": candidate["direction"], "line_variant": ""}
                    ),
                )
            )
            await session.commit()

        assert await db.record_telegram_play_delivery(
            provider=candidate["provider"],
            delivery_path=candidate["delivery_path"],
            player_name=candidate["player_name"],
            sport=candidate["sport"],
            stat_type=candidate["stat_type"],
            external_id=candidate["source_id"],
            event_id=candidate["event_id"],
            line_value=candidate["line_value"],
            recipients_sent=1,
            recipients_failed=0,
            evidence_snapshot={"direction": candidate["direction"]},
            direction=candidate["direction"],
        )
        assert await db.count_actionable_pick_records() == 1
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_actionable_reports_require_exact_audit_not_alert_sent_flag(tmp_path):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        verified = _item(
            source_id="verified-source",
            event_id="verified-event",
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        flag_only = _item(
            source_id="flag-only-source",
            event_id="flag-only-event",
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        await _seed_opportunity(db, verified)
        await _seed_opportunity(db, flag_only)
        assert await db.record_telegram_play_delivery(
            provider=verified["provider"],
            delivery_path=verified["delivery_path"],
            player_name=verified["player_name"],
            sport=verified["sport"],
            stat_type=verified["stat_type"],
            external_id=verified["source_id"],
            event_id=verified["event_id"],
            line_value=verified["line_value"],
            recipients_sent=1,
            recipients_failed=0,
            evidence_snapshot={"direction": verified["direction"]},
            direction=verified["direction"],
        )
        async with db.session() as session:
            await session.execute(
                __import__("sqlalchemy").update(PropOpportunityLog)
                .where(PropOpportunityLog.external_id == verified["source_id"])
                .values(alert_sent=False, alert_sent_at=None)
            )
            await session.execute(
                __import__("sqlalchemy").update(PropOpportunityLog)
                .where(PropOpportunityLog.external_id == flag_only["source_id"])
                .values(alert_sent=True, alert_sent_at=now)
            )
            await session.commit()

        alerted = await db.get_alerted_opportunity_log(
            start_at=now - timedelta(hours=1),
            end_at=now + timedelta(minutes=1),
            limit=20,
        )
        current = await db.get_current_actionable_opportunities(limit=20)
        assert [row.external_id for row in alerted] == [verified["source_id"]]
        assert [row.external_id for row in current] == [verified["source_id"]]
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
            runtime = await session.get(TelegramPickDeliveryRuntime, 1)
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
            assert json.loads(audit.evidence_snapshot_json)["line_variant"] == ""
            assert opportunity.alert_sent is True
            assert opportunity.line_value == candidate["line_value"]
            assert runtime.next_send_at <= (
                attempt.attempted_at + timedelta(seconds=3.21)
            )
        assert await db.count_actionable_pick_records() == 1
    finally:
        await _close_db(db)


@pytest.mark.asyncio
async def test_partial_broadcast_persists_global_retry_after_across_restart(
    tmp_path, monkeypatch
):
    db = await _make_db(tmp_path)
    try:
        now = datetime.utcnow()
        first_pick = _item(
            source_id="first-source",
            event_id="first-event",
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        second_pick = _item(
            source_id="second-source",
            event_id="second-event",
            game_time=now + timedelta(hours=2),
            fetched_at=now,
        )
        await _seed_underdog(db, first_pick)
        await _seed_opportunity(db, first_pick)
        await db.enqueue_telegram_pick_deliveries([first_pick, second_pick])
        monkeypatch.setenv("ALLOWED_USER_IDS", "101,202")

        class PartialBot:
            def __init__(self):
                self.calls = []
                self.failed_once = False

            async def send_message(self, *, chat_id, **_kwargs):
                self.calls.append(chat_id)
                if chat_id == 202 and not self.failed_once:
                    self.failed_once = True
                    raise RetryAfter(1)
                return SimpleNamespace(message_id=1)

        bot = PartialBot()
        context = SimpleNamespace(bot_data={"db": db}, bot=bot)
        original_claim = db.claim_next_telegram_pick_delivery

        async def claim_without_test_pacing(**kwargs):
            return await original_claim(pacing_seconds=0, **kwargs)

        monkeypatch.setattr(
            db,
            "claim_next_telegram_pick_delivery",
            claim_without_test_pacing,
        )
        await alerts.telegram_pick_delivery_job(context)

        assert bot.calls == [202, 101]
        async with db.session() as session:
            deliveries = (
                await session.execute(select(TelegramPickDelivery))
            ).scalars().all()
            first_row = next(row for row in deliveries if row.source_id == "first-source")
            second_row = next(row for row in deliveries if row.source_id == "second-source")
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
            runtime = await session.get(TelegramPickDeliveryRuntime, 1)
            assert first_row.status == "pending"
            assert second_row.status == "pending"
            assert attempt.status == "retry_scheduled"
            assert attempt.exception_class == "RetryAfter"
            assert attempt.retry_after_seconds == 1
            assert attempt.recipients_sent == 1
            assert attempt.recipients_failed == 1
            assert "only recipients without" in attempt.reason
            recipient_results = json.loads(attempt.recipient_results_json)
            assert recipient_results["sent"] == [
                alerts._telegram_recipient_delivery_key(101)
            ]
            assert recipient_results["failed"][0]["recipient_key"] == (
                alerts._telegram_recipient_delivery_key(202)
            )
            assert json.loads(first_row.sent_recipient_keys_json) == [
                alerts._telegram_recipient_delivery_key(101)
            ]
            assert audit.outcome == "sent"
            assert audit.recipients_sent == 1
            assert audit.recipients_failed == 1
            completed_at = attempt.completed_at
            assert runtime.next_send_at >= completed_at + timedelta(seconds=1)

        await _close_db(db)
        db = await _make_db(tmp_path)
        assert await db.claim_next_telegram_pick_delivery(
            now=completed_at + timedelta(milliseconds=900), pacing_seconds=0
        ) is None
        original_claim = db.claim_next_telegram_pick_delivery

        async def reopened_claim_without_test_pacing(**kwargs):
            return await original_claim(pacing_seconds=0, **kwargs)

        monkeypatch.setattr(
            db,
            "claim_next_telegram_pick_delivery",
            reopened_claim_without_test_pacing,
        )
        await asyncio.sleep(1.1)
        await alerts.telegram_pick_delivery_job(
            SimpleNamespace(bot_data={"db": db}, bot=bot)
        )
        assert bot.calls == [202, 101, 202]
        async with db.session() as session:
            first_row = (
                await session.execute(
                    select(TelegramPickDelivery).where(
                        TelegramPickDelivery.source_id == "first-source"
                    )
                )
            ).scalar_one()
            attempts = (
                await session.execute(
                    select(TelegramPickDeliveryAttempt)
                    .where(TelegramPickDeliveryAttempt.delivery_id == first_row.id)
                    .order_by(TelegramPickDeliveryAttempt.attempted_at)
                )
            ).scalars().all()
            assert first_row.status == "sent"
            assert len(attempts) == 2
            assert attempts[1].recipients_sent == 1
            assert attempts[1].recipients_failed == 0
            assert json.loads(attempts[1].recipient_results_json)["sent"] == [
                alerts._telegram_recipient_delivery_key(202)
            ]
            assert json.loads(first_row.sent_recipient_keys_json) == sorted(
                [
                    alerts._telegram_recipient_delivery_key(101),
                    alerts._telegram_recipient_delivery_key(202),
                ]
            )
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
        "player_name": "Sample Player",
        "sport": "NHL",
        "stat_type": "Goals",
        "line_value": 1.5,
        "line_variant": "",
    }
    over = _telegram_delivery_key(**identity, direction="OVER")
    under = _telegram_delivery_key(**identity, direction="UNDER")
    legacy = _telegram_delivery_key(**identity)
    other_player = _telegram_delivery_key(
        **{**identity, "player_name": "Other Player"},
        direction="OVER",
    )

    assert over is not None
    assert under is not None
    assert over != under
    assert other_player is not None
    assert other_player != over
    assert legacy is None
