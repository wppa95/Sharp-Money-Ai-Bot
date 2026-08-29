"""Regression tests for source-to-Telegram alert reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
async def test_reconciliation_flags_replayed_audit_identity(db):
    now = datetime.utcnow()
    for _ in range(2):
        await db.log_telegram_delivery(
            provider="Underdog",
            delivery_path="standing",
            player_name="Same Player",
            sport="NFL",
            stat_type="Passing Yards",
            external_id="same-1",
            line_value=250.5,
            outcome="sent",
            recipients_sent=1,
            attempted_at=now,
        )

    report = await db.get_telegram_reconciliation(since_hours=24)

    assert report["duplicate_delivery_events"] == 1
    assert report["by_provider"]["Underdog"]["audit_delivered_alerts"] == 2


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

    limiter = MagicMock()
    limiter.check.return_value = SimpleNamespace(allowed=True)
    db = MagicMock()
    db.log_telegram_delivery = AsyncMock()
    with (
        patch.object(market_engine, "get_rate_limiter", return_value=limiter),
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
    limiter.record_qualified.assert_called_once()
    limiter.record_sent.assert_called_once()
    broadcast.assert_awaited_once()
    db.log_telegram_delivery.assert_awaited_once()
    assert db.log_telegram_delivery.await_args.kwargs["recipients_sent"] == 2
    assert db.log_telegram_delivery.await_args.kwargs["outcome"] == "sent"


@pytest.mark.asyncio
async def test_consensus_broadcast_rate_limit_skips_network_and_is_audited():
    import market_engine

    limiter = MagicMock()
    limiter.check.return_value = SimpleNamespace(
        allowed=False,
        reason="rate-limited",
    )
    db = MagicMock()
    db.log_telegram_delivery = AsyncMock()
    with (
        patch.object(market_engine, "get_rate_limiter", return_value=limiter),
        patch.object(market_engine, "broadcast_alert", new_callable=AsyncMock) as broadcast,
    ):
        counts = await market_engine._broadcast_consensus_alert(
            db=db,
            bot=MagicMock(),
            chat_ids=[1],
            message="test",
            sport="NBA",
            alert_kind="STEAM",
        )

    assert counts == {"sent": 0, "failed": 0, "rate_limited": True}
    broadcast.assert_not_awaited()
    limiter.record_sent.assert_not_called()
    db.log_telegram_delivery.assert_awaited_once()
    assert db.log_telegram_delivery.await_args.kwargs["outcome"] == "rate_limited"