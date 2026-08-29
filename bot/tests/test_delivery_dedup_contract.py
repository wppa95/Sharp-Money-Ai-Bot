"""Focused delivery-only deduplication contract tests."""

from __future__ import annotations

import time

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