"""Regression tests for source-to-Telegram alert reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _directional_decision(direction: str = "OVER") -> SimpleNamespace:
    over = 75 if direction == "OVER" else 25
    windows = ("l5", "l10", "l15", "l20", "l30", "season")
    return SimpleNamespace(
        recommendation=direction,
        confidence=75,
        **{f"{window}_games": 100 for window in windows},
        **{f"{window}_over": over for window in windows},
        **{f"{window}_under": 100 - over for window in windows},
    )


@pytest.fixture()
async def db():
    from database import Database

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_reconciliation_separates_provider_and_recipient_counts(db):
    from database import PropCandidateLog, PropOpportunityLog

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(PropCandidateLog(
            scan_ts=now,
            player_name="UD Player",
            team="UD",
            sport="MLB",
            stat_type="Hits",
            line_value=0.5,
            provider="Underdog",
            score_total=90,
            score_tier="S",
            confidence=90,
            gate_decision="ACCEPTED",
        ))
        session.add(PropOpportunityLog(
            external_id="ud-1",
            player_name="UD Player",
            team="UD",
            sport="MLB",
            stat_type="Hits",
            line_value=0.5,
            recommendation="OVER",
            decision_tier="S",
            confidence=90,
            provider="Underdog",
            game_id="game-ud-1",
            detected_at=now,
            alert_sent=True,
            alert_sent_at=now,
        ))
        session.add(PropOpportunityLog(
            external_id="pp-1",
            player_name="PP Player",
            team="PP",
            sport="NBA",
            stat_type="Points",
            line_value=20.5,
            recommendation="UNDER",
            decision_tier="A",
            confidence=80,
            provider="PrizePicks",
            detected_at=now,
            alert_sent=False,
        ))
        await session.commit()

    await db.log_telegram_delivery(
        provider="Underdog",
        delivery_path="underdog",
        player_name="UD Player",
        sport="MLB",
        stat_type="Hits",
        external_id="ud-1",
        event_id="game-ud-1",
        line_value=0.5,
        outcome="sent",
        recipients_sent=3,
        attempted_at=now,
    )
    await db.log_telegram_delivery(
        provider="PrizePicks",
        delivery_path="prizepicks",
        player_name="PP Player",
        sport="NBA",
        stat_type="Points",
        external_id="pp-1",
        line_value=20.5,
        outcome="rate_limited",
        attempted_at=now,
    )

    report = await db.get_telegram_reconciliation(
        since_hours=24,
        bucket_minutes=60,
    )
    ud = report["by_provider"]["Underdog"]
    pp = report["by_provider"]["PrizePicks"]

    assert ud["evaluated"] == 1
    assert ud["actionable_persisted"] == 1
    assert ud["delivered_alerts"] == 1
    assert ud["audit_delivered_alerts"] == 1
    assert ud["audit_recipients"] == 3
    assert pp["evaluated"] == 1
    assert pp["actionable_persisted"] == 1
    assert pp["delivered_alerts"] == 0
    assert pp["rate_limited"] == 1
    assert report["audited_deliveries"] == 1
    assert report["exact_recipient_count_available"] is True


async def _log_actionable_opportunity(
    db,
    *,
    external_id: str,
    player_name: str = "Traceable Player",
    stat_type: str = "Hits",
    provider: str = "Underdog",
    game_id: str = "",
):
    from database import PropOpportunityLog

    now = datetime.utcnow()
    opportunity_game_id = game_id or f"game-{external_id}"
    await db.track_prop_records(
        [{
            "external_id": external_id,
            "player_name": player_name,
            "team": "TEAM",
            "sport": "MLB",
            "stat_type": stat_type,
            "line_value": 0.5,
            "game_time": now + timedelta(hours=3),
            "game_id": opportunity_game_id,
            "provider": provider,
        }]
    )
    async with db.session() as session:
        session.add(PropOpportunityLog(
            external_id=external_id,
            player_name=player_name,
            team="TEAM",
            sport="MLB",
            stat_type=stat_type,
            line_value=0.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=85,
            provider=provider,
            game_id=opportunity_game_id,
            detected_at=now,
            alert_sent=False,
        ))
        await session.commit()


async def _deliver_traceable_play(db, *, source_id: str, event_id: str, path: str):
    from alerts import AlertDelivery

    delivery = AlertDelivery(db, MagicMock(), [123])
    with (
        # Isolate original source/audit linkage: these legacy mocks omit
        # start timestamps and must not create separate reminder rows.
        patch("reporting.is_current_reporting_day", return_value=True),
        patch(
            "alerts.broadcast_alert",
            new_callable=AsyncMock,
            return_value={"sent": 1, "failed": 0},
        ),
        patch("alerts.send_discord_alert", new_callable=AsyncMock, return_value=False),
    ):
        return await delivery.deliver_underdog(
            player_name="Traceable Player",
            team="TEAM",
            sport="MLB",
            stat_type="Hits",
            old_line=0.5,
            new_line=1.5,
            validation=SimpleNamespace(
                has_supporting_data=True,
                data_source="propline",
                n_real_games=0,
                n_supplemental_games=6,
            ),
            decision=_directional_decision("OVER"),
            score=SimpleNamespace(total=90, stars=4),
            source_id=source_id,
            event_id=event_id,
            delivery_path=path,
        )


@pytest.mark.asyncio
async def test_successful_play_persists_exact_source_and_event_and_marks_opportunity(db):
    import json

    from alerts import DeliveryResult
    from database import PropOpportunityLog, TelegramDeliveryAudit
    from sqlalchemy import select

    await _log_actionable_opportunity(
        db, external_id="play-source-1", game_id="game-1"
    )
    result = await _deliver_traceable_play(
        db,
        source_id="play-source-1",
        event_id="game-1",
        path="line_change",
    )

    assert isinstance(result, DeliveryResult)
    assert result.sent is True
    async with db.session() as session:
        audit = (
            await session.execute(
                select(TelegramDeliveryAudit).where(
                    TelegramDeliveryAudit.external_id == "play-source-1"
                )
            )
        ).scalar_one()
        opportunity = (
            await session.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "play-source-1"
                )
            )
        ).scalar_one()

    assert audit.event_id == "game-1"
    assert audit.delivery_path == "line_change"
    assert audit.alert_kind == "ACTIONABLE_PICK"
    assert audit.outcome == "sent"
    evidence = json.loads(audit.evidence_snapshot_json)
    assert evidence["schema_version"] == 1
    assert evidence["source"] == "propline"
    assert evidence["sample_count"] == 6
    assert opportunity.alert_sent is True
    report = await db.get_telegram_reconciliation(since_hours=24)
    ud = report["by_provider"]["Underdog"]
    assert ud["actionable_persisted"] == 1
    assert ud["delivered_alerts"] == 1
    assert ud["audit_delivered_alerts"] == 1
    assert ud["audit_recipients"] == 1
    assert report["audited_deliveries"] == 1


@pytest.mark.asyncio
async def test_successful_play_marks_only_the_exact_source_opportunity(db):
    from database import PropOpportunityLog
    from sqlalchemy import select

    await _log_actionable_opportunity(
        db, external_id="play-source-a", game_id="game-a"
    )
    await _log_actionable_opportunity(db, external_id="play-source-b")
    await _deliver_traceable_play(
        db,
        source_id="play-source-a",
        event_id="game-a",
        path="new",
    )

    async with db.session() as session:
        rows = list(
            (
                await session.execute(
                    select(PropOpportunityLog).order_by(PropOpportunityLog.external_id)
                )
            ).scalars()
        )
    assert [(row.external_id, bool(row.alert_sent)) for row in rows] == [
        ("play-source-a", True),
        ("play-source-b", False),
    ]


@pytest.mark.asyncio
async def test_failed_play_is_audited_but_not_marked_delivered(db):
    from database import PropOpportunityLog, TelegramDeliveryAudit
    from alerts import AlertDelivery
    from sqlalchemy import select

    await _log_actionable_opportunity(db, external_id="failed-play")
    delivery = AlertDelivery(db, MagicMock(), [123])
    with (
        patch("reporting.is_current_reporting_day", return_value=True),
        patch(
            "alerts.broadcast_alert",
            new_callable=AsyncMock,
            return_value={"sent": 0, "failed": 1},
        ),
        patch("alerts.send_discord_alert", new_callable=AsyncMock, return_value=False),
    ):
        result = await delivery.deliver_underdog(
            "Traceable Player", "TEAM", "MLB", "Hits", 0.5, 1.5,
            validation=SimpleNamespace(
                has_supporting_data=True,
                data_source="propline",
                n_real_games=0,
                n_supplemental_games=6,
            ),
            decision=_directional_decision("OVER"),
            score=SimpleNamespace(total=90, stars=4),
            source_id="failed-play",
            event_id="failed-game",
            delivery_path="standing",
        )

    assert result.sent is False
    async with db.session() as session:
        audit = (
            await session.execute(
                select(TelegramDeliveryAudit).where(
                    TelegramDeliveryAudit.external_id == "failed-play"
                )
            )
        ).scalar_one()
        opportunity = (
            await session.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.external_id == "failed-play"
                )
            )
        ).scalar_one()
    assert audit.outcome == "failed"
    assert opportunity.alert_sent is False
    report = await db.get_telegram_reconciliation(since_hours=24)
    assert report["audited_deliveries"] == 0


@pytest.mark.asyncio
async def test_non_play_success_is_not_counted_as_delivered_play(db):
    now = datetime.utcnow()
    await db.log_telegram_delivery(
        provider="Underdog",
        alert_kind="MARKET_MOVE",
        delivery_path="line_change",
        player_name="Observed Player",
        sport="MLB",
        stat_type="Hits",
        external_id="observed-source",
        event_id="observed-game",
        line_value=0.5,
        outcome="sent",
        recipients_sent=1,
        attempted_at=now,
    )

    report = await db.get_telegram_reconciliation(since_hours=24)
    ud = report["by_provider"]["Underdog"]
    assert ud["delivery_attempts"] == 1
    assert ud["audit_delivered_alerts"] == 0
    assert ud["audit_recipients"] == 0
    assert report["audited_deliveries"] == 0


@pytest.mark.asyncio
async def test_reconciliation_counts_prizepicks_history_as_observed_and_tracked(db):
    """Normalized PP rows must appear in provider telemetry without an alert."""
    from database import PropLineHistory

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(PropLineHistory(
            external_id="pp-history-1",
            provider="PrizePicks",
            player_name="PP Player",
            team="PP",
            sport="WNBA",
            stat_type="Points",
            line_value=18.5,
            fetched_at=now,
            removed=False,
        ))
        await session.commit()

    report = await db.get_telegram_reconciliation(since_hours=24)
    pp = report["by_provider"]["PrizePicks"]

    assert pp["observed"] == 1
    assert pp["tracked"] == 1
    assert pp["evaluated"] == 0
    assert pp["actionable_persisted"] == 0


@pytest.mark.asyncio
async def test_reconciliation_prefers_normalized_underdog_history_over_legacy_rows(db):
    from database import PropLineHistory, UnderdogSnapshotRecord

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(PropLineHistory(
            external_id="ud-history-1",
            provider="Underdog",
            player_name="UD Player",
            team="UD",
            sport="SOCCER",
            stat_type="Goals",
            line_value=0.5,
            fetched_at=now,
            removed=False,
        ))
        session.add(UnderdogSnapshotRecord(
            external_id="legacy-ud-1",
            player_name="Legacy UD Player",
            team="UD",
            sport="NBA",
            stat_type="Points",
            line_value=20.5,
            fetched_at=now,
            removed=False,
        ))
        await session.commit()

    report = await db.get_telegram_reconciliation(since_hours=24)

    assert report["by_provider"]["Underdog"]["observed"] == 1


@pytest.mark.asyncio
async def test_reconciliation_flags_replayed_audit_identity(db):
    from database import PropOpportunityLog

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(PropOpportunityLog(
            external_id="same-1",
            player_name="Same Player",
            team="UD",
            sport="NFL",
            stat_type="Passing Yards",
            line_value=250.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=80,
            provider="Underdog",
            game_id="game-same-1",
            detected_at=now,
            alert_sent=True,
            alert_sent_at=now,
        ))
        await session.commit()
    for _ in range(2):
        await db.log_telegram_delivery(
            provider="Underdog",
            delivery_path="standing",
            player_name="Same Player",
            sport="NFL",
            stat_type="Passing Yards",
            external_id="same-1",
            event_id="game-same-1",
            line_value=250.5,
            outcome="sent",
            recipients_sent=1,
            attempted_at=now,
        )

    report = await db.get_telegram_reconciliation(since_hours=24)

    # The storage-level delivery key makes the second identical successful
    # insert a no-op, complementing the in-memory delivery claim.
    assert report["duplicate_delivery_events"] == 0
    assert report["by_provider"]["Underdog"]["audit_delivered_alerts"] == 1


@pytest.mark.asyncio
async def test_incomplete_success_audit_is_unknown_not_confirmed(db):
    from database import PropOpportunityLog

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(PropOpportunityLog(
            external_id="legacy-source",
            player_name="Legacy Player",
            team="UD",
            sport="MLB",
            stat_type="Hits",
            line_value=0.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=80,
            provider="Underdog",
            game_id="game-incomplete",
            detected_at=now,
            alert_sent=True,
            alert_sent_at=now,
        ))
        await session.commit()

    await db.log_telegram_delivery(
        provider="Underdog",
        delivery_path="standing",
        player_name="Legacy Player",
        sport="MLB",
        stat_type="Hits",
        external_id="legacy-source",
        # Missing event_id makes this an ambiguous legacy record.
        line_value=0.5,
        outcome="sent",
        recipients_sent=1,
        attempted_at=now,
    )

    report = await db.get_telegram_reconciliation(since_hours=24)
    assert report["by_provider"]["Underdog"]["delivered_alerts"] == 0
    assert report["by_provider"]["Underdog"]["audit_delivered_alerts"] == 0
    assert report["unverified_historical"] >= 1
    assert await db.get_alerted_opportunity_log(since_hours=24) == []
    assert await db.get_recent_alerted_props_for_dedup(since_hours=24) == {}


@pytest.mark.asyncio
async def test_alert_history_is_newest_first_and_preserves_audit_line(db):
    from database import PropOpportunityLog

    now = datetime.utcnow()
    async with db.session() as session:
        session.add(PropOpportunityLog(
            external_id="moving-source",
            player_name="Moving Player",
            team="UD",
            sport="NBA",
            stat_type="Points",
            line_value=21.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=80,
            provider="Underdog",
            game_id="game-moving",
            detected_at=now,
            alert_sent=True,
            alert_sent_at=now,
        ))
        await session.commit()

    await db.log_telegram_delivery(
        provider="Underdog",
        delivery_path="line_change",
        player_name="Moving Player",
        sport="NBA",
        stat_type="Points",
        external_id="moving-source",
        event_id="game-moving",
        line_value=20.5,
        outcome="sent",
        recipients_sent=1,
        attempted_at=now - timedelta(minutes=2),
    )
    await db.log_telegram_delivery(
        provider="Underdog",
        delivery_path="line_change",
        player_name="Moving Player",
        sport="NBA",
        stat_type="Points",
        external_id="moving-source",
        event_id="game-moving",
        line_value=21.5,
        outcome="sent",
        recipients_sent=1,
        attempted_at=now,
    )

    rows = await db.get_alerted_opportunity_log(since_hours=24, limit=1)
    assert len(rows) == 1
    assert rows[0].line_value == 21.5
    assert rows[0].alert_sent_at == now


@pytest.mark.asyncio
async def test_reconciliation_excludes_old_rows(db):
    from database import PropOpportunityLog

    old = datetime.utcnow() - timedelta(hours=30)
    async with db.session() as session:
        session.add(PropOpportunityLog(
            external_id="old-1",
            player_name="Old Player",
            team="OLD",
            sport="MLB",
            stat_type="Hits",
            line_value=0.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=80,
            provider="Underdog",
            detected_at=old,
            alert_sent=True,
            alert_sent_at=old,
        ))
        await session.commit()

    report = await db.get_telegram_reconciliation(since_hours=24)

    assert report["by_provider"].get("Underdog", {}).get("actionable_persisted", 0) == 0
    assert report["legacy_delivery_rows"] == 0


@pytest.mark.asyncio
async def test_consensus_broadcast_audits_fanout_and_only_counts_sent():
    import market_engine

    db = MagicMock()
    db.log_telegram_delivery = AsyncMock()
    with (
        patch.object(
            market_engine,
            "broadcast_alert",
            new_callable=AsyncMock,
            return_value={"sent": 2, "failed": 1},
        ) as broadcast,
    ):
        counts = await market_engine._broadcast_consensus_alert(
            db=db,
            bot=MagicMock(),
            chat_ids=[1, 2, 3],
            message="test",
            sport="MLB",
            alert_kind="INEFFICIENCY",
        )

    assert counts["sent"] == 2
    broadcast.assert_awaited_once()
    db.log_telegram_delivery.assert_awaited_once()
    assert db.log_telegram_delivery.await_args.kwargs["recipients_sent"] == 2
    assert db.log_telegram_delivery.await_args.kwargs["outcome"] == "sent"


@pytest.mark.asyncio
async def test_consensus_broadcast_has_no_global_rate_limit():
    import market_engine

    db = MagicMock()
    db.log_telegram_delivery = AsyncMock()
    with (
        patch.object(
            market_engine,
            "broadcast_alert",
            new_callable=AsyncMock,
            return_value={"sent": 0, "failed": 1},
        ) as broadcast,
    ):
        counts = await market_engine._broadcast_consensus_alert(
            db=db,
            bot=MagicMock(),
            chat_ids=[1],
            message="test",
            sport="NBA",
            alert_kind="STEAM",
        )

    assert counts == {"sent": 0, "failed": 1, "rate_limited": False}
    broadcast.assert_awaited_once()
    db.log_telegram_delivery.assert_awaited_once()
    assert db.log_telegram_delivery.await_args.kwargs["outcome"] == "failed"