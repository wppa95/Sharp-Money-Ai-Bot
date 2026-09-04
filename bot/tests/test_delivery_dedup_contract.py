"""Focused delivery-only deduplication contract tests."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import market_engine
from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted


@pytest.fixture(autouse=True)
def clear_delivery_dedup():
    market_engine._prop_market_alerted.clear()
    yield
    market_engine._prop_market_alerted.clear()


def _identity(**overrides):
    value = {
        "provider": "Underdog",
        "source_id": "source-1",
        "event_id": "event-1",
        "direction": "OVER",
    }
    value.update(overrides)
    return value


def _dedup_kwargs():
    from config import config

    return {
        "dedup_window_seconds": config.UD_ALERT_DEDUP_WINDOW,
        "min_line_change": config.MIN_UNDERDOG_LINE_CHANGE,
        "now_ts": time.time(),
    }


def _full_pool_candidate():
    """Build one candidate shared by the standing and full-pool paths."""
    snap = SimpleNamespace(
        external_id="projection-1",
        id=1,
        event="event-1",
        game_id="",
        team="Team A",
        game_time=None,
        game_status=None,
        provider="Underdog",
    )
    return {
        "provider": "Underdog",
        "player": "Player",
        "sport": "NHL",
        "stat_type": "Shots",
        "line_val": 2.5,
        "snap": snap,
        "decision": SimpleNamespace(recommendation="OVER", decision_tier="A"),
        "score": SimpleNamespace(total=90),
        "validation": SimpleNamespace(has_supporting_data=True),
        "market_quality": SimpleNamespace(score=90),
        "market_pressure": None,
        "pp_line": None,
        "event_id": "event-1",
    }


async def _deliver_standing_candidate(candidate):
    """Exercise the standing queue's claim → Telegram delivery contract."""
    snap = candidate["snap"]
    player = candidate["player"]
    sport = candidate["sport"]
    stat = candidate["stat_type"]
    line = candidate["line_val"]
    decision = candidate["decision"]
    identity = {
        "provider": candidate["provider"],
        "source_id": str(snap.external_id),
        "event_id": candidate["event_id"],
        "direction": decision.recommendation,
    }
    if not await market_engine._try_claim_delivery_slot(
        player, sport, stat, line, **identity
    ):
        return SimpleNamespace(sent=False), False

    try:
        result = await market_engine.AlertDelivery(
            None, object(), ["chat-1"]
        ).deliver_underdog(
            player_name=player,
            team=snap.team,
            sport=sport,
            stat_type=stat,
            old_line=line,
            new_line=line,
            score=candidate["score"],
            validation=candidate["validation"],
            decision=decision,
            market_quality=candidate["market_quality"],
            market_pressure=candidate["market_pressure"],
            standing=True,
            source=candidate["provider"],
            source_id=identity["source_id"],
            event_id=identity["event_id"],
            delivery_path="standing",
        )
    except Exception:
        market_engine._release_prop_delivery_claim(
            market_engine._prop_market_alerted,
            player,
            sport,
            stat,
            line,
            **identity,
        )
        return SimpleNamespace(sent=False), False

    if not result.sent:
        market_engine._release_prop_delivery_claim(
            market_engine._prop_market_alerted,
            player,
            sport,
            stat,
            line,
            **identity,
        )
        return result, False
    return result, True


def test_exact_provider_owned_opportunity_is_suppressed():
    alerted = {}
    _record_prop_alerted(
        alerted, "Player", "MLB", "Hits", 0.5, **_identity()
    )

    assert _is_prop_deduped(
        alerted, "Player", "MLB", "Hits", 0.5,
        **_dedup_kwargs(), **_identity()
    )


@pytest.mark.parametrize(
    "identity_change",
    [
        {"provider": "PrizePicks"},
        {"source_id": "source-2"},
        {"event_id": "event-2"},
        {"direction": "UNDER"},
    ],
)
def test_meaningful_identity_change_is_not_cross_suppressed(identity_change):
    alerted = {}
    _record_prop_alerted(
        alerted, "Player", "MLB", "Hits", 0.5, **_identity()
    )

    assert not _is_prop_deduped(
        alerted, "Player", "MLB", "Hits", 0.5,
        **_dedup_kwargs(), **_identity(**identity_change)
    )


def test_meaningful_line_change_is_not_suppressed():
    alerted = {}
    _record_prop_alerted(
        alerted, "Player", "MLB", "Hits", 0.5, **_identity()
    )

    assert not _is_prop_deduped(
        alerted, "Player", "MLB", "Hits", 1.5,
        **_dedup_kwargs(), **_identity()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_count", [10, 50, 100])
async def test_repeated_claims_produce_one_delivery_slot(repeat_count):
    claims = [
        await market_engine._try_claim_delivery_slot(
            "Player",
            "MLB",
            "Hits",
            0.5,
            **_identity(),
        )
        for _ in range(repeat_count)
    ]

    assert sum(claims) == 1


@pytest.mark.asyncio
async def test_failed_claim_can_be_released_for_a_retry():
    identity = _identity()
    assert await market_engine._try_claim_delivery_slot(
        "Player", "MLB", "Hits", 0.5, **identity
    )

    market_engine._release_prop_delivery_claim(
        market_engine._prop_market_alerted,
        "Player",
        "MLB",
        "Hits",
        0.5,
        **identity,
    )

    assert await market_engine._try_claim_delivery_slot(
        "Player", "MLB", "Hits", 0.5, **identity
    )


@pytest.mark.asyncio
async def test_standing_and_full_pool_delivery_race_sends_one_telegram_alert():
    """The standing queue and FPR cannot both deliver one candidate."""
    candidate = _full_pool_candidate()
    delivery_started = asyncio.Event()
    allow_delivery = asyncio.Event()
    deliveries = []

    class DeliverySpy:
        async def deliver_underdog(self, **kwargs):
            deliveries.append(kwargs)
            delivery_started.set()
            await allow_delivery.wait()
            return SimpleNamespace(sent=True)

    delivery = DeliverySpy()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(market_engine, "AlertDelivery", lambda *args: delivery)

        standing_task = asyncio.create_task(
            _deliver_standing_candidate(candidate)
        )
        await delivery_started.wait()

        # Keep the standing Telegram call in flight while the competing path
        # tries the same identity. The claim was made before network I/O.
        full_pool_task = asyncio.create_task(
            market_engine._deliver_full_pool_candidate(
                candidate, db=None, bot=object(), chat_ids=["chat-1"]
            )
        )
        full_pool_result, full_pool_claimed = await full_pool_task
        allow_delivery.set()
        standing_result, standing_claimed = await standing_task

    assert standing_claimed is True
    assert standing_result.sent is True
    assert full_pool_claimed is False
    assert full_pool_result.sent is False
    assert len(deliveries) == 1
    assert deliveries[0]["delivery_path"] == "standing"


@pytest.mark.asyncio
async def test_failed_delivery_releases_claim_for_one_retry_then_dedups():
    """A failed Telegram attempt is retryable, but the successful retry is not duplicated."""
    candidate = _full_pool_candidate()
    deliveries = []
    outcomes = [RuntimeError("telegram unavailable"), SimpleNamespace(sent=True)]

    class DeliverySpy:
        async def deliver_underdog(self, **kwargs):
            deliveries.append(kwargs)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    delivery = DeliverySpy()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(market_engine, "AlertDelivery", lambda *args: delivery)

        failed_result, failed_claimed = (
            await market_engine._deliver_full_pool_candidate(
                candidate, db=None, bot=object(), chat_ids=["chat-1"]
            )
        )
        retry_result, retry_claimed = await _deliver_standing_candidate(candidate)
        duplicate_result, duplicate_claimed = (
            await market_engine._deliver_full_pool_candidate(
                candidate, db=None, bot=object(), chat_ids=["chat-1"]
            )
        )

    assert failed_result.sent is False
    assert failed_claimed is False
    assert retry_result.sent is True
    assert retry_claimed is True
    assert duplicate_result.sent is False
    assert duplicate_claimed is False
    assert len(deliveries) == 2
    assert sum(result["delivery_path"] == "standing" for result in deliveries) == 1


def test_multiple_alternate_lines_remain_distinct_opportunities():
    alerted = {}
    _record_prop_alerted(
        alerted, "Player", "MLB", "Hits", 0.5,
        **_identity(source_id="projection-low"),
    )
    _record_prop_alerted(
        alerted, "Player", "MLB", "Hits", 1.5,
        **_identity(source_id="projection-high"),
    )

    assert len(alerted) == 2
    assert not _is_prop_deduped(
        alerted, "Player", "MLB", "Hits", 1.5,
        **_dedup_kwargs(), **_identity(source_id="projection-low")
    )
    assert _is_prop_deduped(
        alerted, "Player", "MLB", "Hits", 1.5,
        **_dedup_kwargs(), **_identity(source_id="projection-high")
    )