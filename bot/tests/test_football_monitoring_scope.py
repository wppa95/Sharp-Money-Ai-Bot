"""Regression coverage for football monitoring and evidence-backed delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connectors.underdog import UnderdogConnector, UnderdogProjection
import alerts
from alerts import (
    is_football_delivery_blocked,
    is_telegram_suppressed_sport,
    send_alert_detailed,
    telegram_pick_delivery_job,
)
from engine.sport_policy import (
    is_disallowed_football_sport,
    is_football_related_sport,
)
from providers.propline import PropLineFetchResult, PropLinePrizePicksProvider
from providers.prop_provider import PlayerProp
from providers.underdog_provider import UnderdogProvider


@pytest.mark.parametrize(
    "sport",
    [
        "NFL",
        "NCAAF",
        "CFB",
        "FCS",
        "CFL",
        "AFL",
        "AFLW",
        "football_nfl",
        "americanfootball_nfl",
        "college_football",
        "gridiron",
        "aussie_rules_afl",
        "rugby_union",
        "UFL",
        "USFL",
        "XFL",
        "NCAA Football",
        "arena football",
        "indoor football",
    ],
)
def test_football_aliases_are_classified_as_gridiron_family(sport):
    assert is_football_related_sport(sport) is True


@pytest.mark.parametrize(
    "sport",
    [
        "MLB",
        "NBA",
        "WNBA",
        "NHL",
        "TENNIS",
        "CS",
        "DOTA",
        "LOL",
        "VAL",
        "SOCCER",
        "EPL",
        "MLS",
    ],
)
def test_supported_non_football_sports_remain_allowed(sport):
    assert is_football_related_sport(sport) is False
    assert is_football_delivery_blocked(sport) is False


@pytest.mark.parametrize(
    "sport",
    [
        "NFL",
        "NCAAF",
        "CFB",
        "FCS",
        "CFL",
        "AFLW",
        "americanfootball_ncaaf",
        "rugby_union",
        "indoor football",
    ],
)
def test_every_football_alias_is_blocked_at_the_telegram_send_boundary(sport):
    bot = SimpleNamespace(send_message=AsyncMock())

    outcome = asyncio.run(
        send_alert_detailed(
            bot,
            123,
            f"<b>Sport:</b> {sport}\nIndividual pick",
        )
    )

    assert outcome.sent is False
    assert outcome.exception_class == "FootballDeliveryBlocked"
    bot.send_message.assert_not_awaited()


def test_football_send_boundary_blocks_explicit_sport_even_if_text_is_ambiguous():
    bot = SimpleNamespace(send_message=AsyncMock())
    outcome = asyncio.run(
        send_alert_detailed(bot, 123, "Individual pick", sport="americanfootball_nfl")
    )
    assert outcome.sent is False
    bot.send_message.assert_not_awaited()


def test_non_football_remains_eligible_at_the_telegram_send_boundary():
    bot = SimpleNamespace(send_message=AsyncMock())
    alerts._telegram_last_send_at.clear()

    outcome = asyncio.run(
        send_alert_detailed(
            bot,
            123,
            "<b>Sport:</b> NBA\nIndividual pick",
        )
    )

    assert outcome.sent is True
    bot.send_message.assert_awaited_once()


def test_queued_football_retry_is_invalidated_before_source_validation(monkeypatch):
    from engine.offering_coverage import coverage

    item = {
        "id": 41,
        "attempt_id": "attempt-1",
        "qualification_json": "{}",
        "evidence_snapshot_json": "{}",
        "direction": "OVER",
        "sport": "americanfootball_ncaaf",
    }
    db = SimpleNamespace(
        claim_next_telegram_pick_delivery=AsyncMock(return_value=item),
        finish_telegram_pick_delivery=AsyncMock(),
        validate_telegram_pick_source=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot_data={"db": db}, bot=bot)
    monkeypatch.setattr(alerts, "config", SimpleNamespace(allowed_user_ids=[123]))
    monkeypatch.setattr(coverage, "ready_scopes", lambda: {"Underdog"})

    asyncio.run(telegram_pick_delivery_job(context))

    db.finish_telegram_pick_delivery.assert_awaited_once()
    assert db.finish_telegram_pick_delivery.await_args.kwargs["status"] == "invalidated"
    assert (
        db.finish_telegram_pick_delivery.await_args.kwargs["reason"]
        == "football_delivery_disabled"
    )
    db.validate_telegram_pick_source.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "sport",
    [
        "NFL",
        "football_nfl",
        "CFB",
        "NCAAF",
        "football_ncaaf",
        "americanfootball_ncaaf",
    ],
)
def test_nfl_and_college_football_remain_classified_as_allowed(sport):
    assert is_disallowed_football_sport(sport) is False
    assert is_telegram_suppressed_sport(sport) is False


@pytest.mark.parametrize(
    "sport",
    [
        "college_football",
        "FCS",
        "CFL",
        "AFL",
        "AFLW",
        "UFL",
        "USFL",
        "XFL",
        "rugby_union",
        "gridiron",
    ],
)
def test_other_football_family_sports_remain_suppressed(sport):
    assert is_disallowed_football_sport(sport) is True
    assert is_telegram_suppressed_sport(sport) is True


@pytest.mark.parametrize(
    "sport",
    [
        "MLB",
        "NBA",
        "WNBA",
        "NHL",
        "SOCCER",
        "EPL",
        "MLS",
        "NCAAB",
        "NCAAF",
        "TENNIS",
        "CS",
        "DOTA",
        "LOL",
        "VAL",
        "MMA",
    ],
)
def test_telegram_boundary_does_not_block_non_football_sports(sport):
    assert is_telegram_suppressed_sport(sport) is False


def _projection(external_id: str, sport: str) -> UnderdogProjection:
    return UnderdogProjection(
        external_id=external_id,
        player_name=f"{sport} Player",
        team="Example",
        sport=sport,
        stat_type="Points",
        line_value=20.5,
        game_id=f"{sport}-game",
        fetched_at=datetime.utcnow(),
    )


def test_underdog_keeps_football_for_snapshot_creation():
    connector = UnderdogConnector()
    connector._fetch_projections = AsyncMock(
        return_value=[
            _projection("nfl-1", "NFL"),
            _projection("soccer-1", "SOCCER"),
            _projection("mlb-1", "MLB"),
        ]
    )

    snapshots = asyncio.run(connector.fetch())

    assert {snapshot.sport for snapshot in snapshots} == {"SOCCER", "MLB", "NFL"}
    assert {snapshot.external_id for snapshot in snapshots} == {
        "soccer-1", "mlb-1", "nfl-1",
    }


def test_underdog_unfiltered_connector_keeps_all_provider_sports():
    sports = [
        "MLB",
        "NBA",
        "WNBA",
        "NHL",
        "NFL",
        "CFB",
        "SOCCER",
        "TENNIS",
        "CS",
        "DOTA",
        "RUGBY",
    ]
    connector = UnderdogConnector(active_sports=None)
    connector._fetch_projections = AsyncMock(
        return_value=[
            _projection(f"feed-{sport.lower()}", sport)
            for sport in sports
        ]
    )

    snapshots = asyncio.run(connector.fetch())

    assert {snapshot.sport for snapshot in snapshots} == set(sports)
    assert connector._active_sports == set()


def test_underdog_emits_removal_for_stale_football_state():
    connector = UnderdogConnector()
    nfl = _projection("nfl-1", "NFL")
    connector._previous = {"nfl-1": nfl}
    connector._last_seen = {"nfl-1"}
    connector._fetch_projections = AsyncMock(return_value=[_projection("mlb-1", "MLB")])

    snapshots = asyncio.run(connector.fetch())

    assert {snapshot.external_id for snapshot in snapshots} == {"mlb-1", "nfl-1"}
    assert any("[REMOVED]" in snapshot.selection for snapshot in snapshots)


def test_underdog_normalized_provider_keeps_football_and_other_sports():
    snapshots = [
        SimpleNamespace(
            removed=False,
            player_name="NFL Player",
            sport="NFL",
            stat_type="Rushing Yards",
            selection="NFL Player Rushing Yards 20.5",
            line=20.5,
            line_value=20.5,
            team="Example",
            game_id="nfl-game",
            game_time=None,
            fetched_at=datetime.utcnow(),
        ),
        SimpleNamespace(
            removed=False,
            player_name="Soccer Player",
            sport="SOCCER",
            stat_type="Shots",
            selection="Soccer Player Shots 2.5",
            line=2.5,
            line_value=2.5,
            team="Example",
            game_id="soccer-game",
            game_time=None,
            fetched_at=datetime.utcnow(),
        ),
    ]

    props = asyncio.run(UnderdogProvider(snapshots=snapshots).fetch_props())

    assert [prop.sport for prop in props] == ["NFL", "SOCCER"]


class _MixedPropLineClient:
    api_url = "https://authorized.example/props"
    is_configured = True

    async def fetch_prizepicks_props(self, **_kwargs):
        return PropLineFetchResult(
            props=[
                PlayerProp(
                    provider="PrizePicks",
                    sport="NFL",
                    player_name="NFL Player",
                    team="Example",
                    stat_type="rushing yards",
                    line_value=20.5,
                ),
                PlayerProp(
                    provider="PrizePicks",
                    sport="NBA",
                    player_name="NBA Player",
                    team="Example",
                    stat_type="points",
                    line_value=20.5,
                ),
            ],
            fetched_at=datetime.utcnow(),
            succeeded=True,
        )


def test_prizepicks_custom_endpoint_keeps_mixed_football_batch():
    provider = PropLinePrizePicksProvider(client=_MixedPropLineClient())

    props = asyncio.run(provider.fetch_props())

    assert [(prop.sport, prop.player_name) for prop in props] == [
        ("NFL", "NFL Player"),
        ("NBA", "NBA Player"),
    ]


def test_prizepicks_sport_scoped_football_fetch_is_supported():
    client = _MixedPropLineClient()
    client.official_sport_for = lambda sport: {
        "CFB": "americanfootball_ncaaf",
    }.get(sport)
    provider = PropLinePrizePicksProvider(client=client)

    result = asyncio.run(provider.fetch_props_for_sport_result("CFB"))

    assert result.succeeded is True
    assert result.failure_kind == ""


@pytest.mark.parametrize(
    "sport",
    ["NFL", "NCAAF", "CFB", "FCS", "CFL", "AFL", "AFLW"],
)
def test_prizepicks_normalization_accepts_football_for_persistence(sport):
    from providers.propline import normalize_propline_prizepicks_record

    record = {
        "provider": "PrizePicks",
        "market_type": "player_prop",
        "player_name": "Football Player",
        "sport": sport,
        "stat_type": "Points",
        "line": 20.5,
    }

    normalized = normalize_propline_prizepicks_record(record)
    assert normalized is not None
    assert normalized.sport == sport