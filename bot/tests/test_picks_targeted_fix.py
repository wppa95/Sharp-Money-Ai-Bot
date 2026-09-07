"""Focused regressions for current-state and fair-provider /picks selection."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import commands
from database import Database, PropLineHistory, UnderdogSnapshotRecord
from providers.propline import normalize_propline_prizepicks_record
from providers.prop_provider import PlayerProp


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.init()
    yield database
    await database.close()


def _row(
    provider: str,
    player: str,
    direction: str | None,
    *,
    external_id: str,
    score_tier: str | None = None,
    decision_tier: str | None = None,
):
    return SimpleNamespace(
        provider=provider,
        player_name=player,
        sport="NBA",
        stat_type="Points",
        bet_recommendation=direction,
        external_id=external_id,
        game_id="game-1",
        score_tier=score_tier,
        decision_tier=decision_tier,
        watchlist_state="Rejected",
        alert_sent=False,
    )


def test_direction_is_resolved_before_fair_result_cap():
    non_directional = [
        _row("Underdog", f"No Direction {index}", None, external_id=f"none-{index}")
        for index in range(20)
    ]
    ud_over = _row("Underdog", "UD Directional", "OVER", external_id="ud-over")
    pp_under = _row("PrizePicks", "PP Directional", "UNDER", external_id="pp-under")

    selected = commands._select_directional_provider_rows(
        [*non_directional, ud_over],
        [pp_under],
        {},
        2,
    )

    assert [(provider, row.player_name) for provider, row in selected] == [
        ("Underdog", "UD Directional"),
        ("PrizePicks", "PP Directional"),
    ]


def test_live_underdog_direction_overrides_synced_value_before_cap():
    ud = _row("Underdog", "Live Direction", None, external_id="ud-live")
    selected = commands._select_directional_provider_rows(
        [ud],
        [],
        {("Live Direction", "NBA", "Points", "ud-live"): ("UNDER", 70)},
        1,
    )
    assert selected == [("Underdog", ud)]


def test_provider_identity_and_source_aware_dedup_remain_intact():
    ud = _row("Underdog", "Same Player", "OVER", external_id="ud-source")
    pp_a = _row("PrizePicks", "Same Player", "UNDER", external_id="pp-a")
    pp_b = _row("PrizePicks", "Same Player", "OVER", external_id="pp-b")

    selected = commands._select_directional_provider_rows(
        [ud],
        [pp_a, pp_b, pp_a],
        {},
        3,
    )

    assert [(provider, row.external_id) for provider, row in selected] == [
        ("Underdog", "ud-source"),
        ("PrizePicks", "pp-a"),
        ("PrizePicks", "pp-b"),
    ]


@pytest.mark.parametrize("tier", ["S", "A", "B", "C", "PASS", None])
@pytest.mark.parametrize("decision_tier", ["Tier 1", "Tier 2", "Rejected", None])
def test_retired_classification_metadata_does_not_gate_picks(tier, decision_tier):
    row = _row(
        "PrizePicks",
        "Neutral Metadata",
        "OVER",
        external_id=f"{tier}-{decision_tier}",
        score_tier=tier,
        decision_tier=decision_tier,
    )
    assert commands._select_directional_provider_rows([], [row], {}, 1) == [
        ("PrizePicks", row)
    ]


def test_pregame_state_remains_hard_and_unknown_is_not_fabricated():
    future = datetime.utcnow() + timedelta(hours=1)
    past = datetime.utcnow() - timedelta(minutes=1)

    assert commands._is_upcoming_game(future, "scheduled") is True
    assert commands._is_upcoming_game(None, "scheduled") is True
    assert commands._is_upcoming_game(future, "live") is False
    assert commands._is_upcoming_game(past, None) is False
    assert commands._is_upcoming_game(None, None) is False


def test_picks_selection_does_not_require_evidence_delivery_or_alert_state():
    row = _row("PrizePicks", "Research Row", "UNDER", external_id="research")
    del row.alert_sent
    assert not hasattr(row, "evidence")
    assert commands._select_directional_provider_rows([], [row], {}, 1) == [
        ("PrizePicks", row)
    ]
    source = inspect.getsource(commands._cmd_picks_inner)
    assert "broadcast_alert" not in source
    assert "alert_sent" not in source


def test_propline_does_not_fabricate_missing_event_state():
    prop = normalize_propline_prizepicks_record({
        "provider": "PrizePicks",
        "market_type": "player_prop",
        "id": "projection-unknown",
        "player_name": "Unknown Event",
        "sport": "NBA",
        "stat": "Points",
        "line": 20.5,
    })
    assert prop is not None
    assert prop.game_time is None
    assert prop.provider_status == ""


def test_propline_prefers_genuine_event_state_over_projection_availability():
    prop = normalize_propline_prizepicks_record({
        "provider": "PrizePicks",
        "market_type": "player_prop",
        "id": "projection-scheduled",
        "player_name": "Scheduled Event",
        "sport": "NBA",
        "stat": "Points",
        "line": 20.5,
        "status": "available",
        "event": {
            "id": "game-scheduled",
            "status": "scheduled",
        },
    })
    assert prop is not None
    assert prop.provider_status == "scheduled"


@pytest.mark.asyncio
async def test_prizepicks_current_row_preserves_real_event_metadata(db):
    fetched_at = datetime.utcnow()
    source_timestamp = fetched_at - timedelta(hours=3)
    game_time = fetched_at + timedelta(hours=2)
    prop = PlayerProp(
        provider="PrizePicks",
        sport="NBA",
        player_name="PP Event",
        team="TEAM",
        stat_type="Points",
        line_value=22.5,
        game_time=game_time,
        external_id="pp-event",
        game_id="game-pp",
        fetched_at=fetched_at,
        provider_status="scheduled",
        source_timestamp=source_timestamp,
    )

    await db.upsert_prop_line_history_batch(
        "PrizePicks",
        [prop],
        append_snapshot=True,
    )
    rows = await db.get_latest_props_for_provider("PrizePicks", since_hours=4)

    assert len(rows) == 1
    assert rows[0].external_id == "pp-event"
    assert rows[0].game_id == "game-pp"
    assert rows[0].game_time == game_time
    assert rows[0].provider_status == "scheduled"
    assert rows[0].fetched_at == fetched_at


@pytest.mark.asyncio
async def test_underdog_late_event_metadata_refreshes_current_row(db):
    first_seen = datetime.utcnow()
    future = first_seen + timedelta(hours=2)
    common = dict(
        external_id="ud-event",
        player_name="UD Event",
        team="TEAM",
        sport="NBA",
        stat_type="Points",
        line_value=21.5,
        game_id="game-ud",
        removed=False,
    )
    await db.save_underdog_snapshot(UnderdogSnapshotRecord(
        **common,
        game_time=None,
        provider_status=None,
        fetched_at=first_seen,
    ))
    await db.sync_underdog_snapshots_to_prop_history()

    await db.save_underdog_snapshot(UnderdogSnapshotRecord(
        **common,
        game_time=future,
        provider_status="scheduled",
        fetched_at=first_seen + timedelta(seconds=1),
    ))
    await db.sync_underdog_snapshots_to_prop_history()

    async with db.session() as session:
        from sqlalchemy import select
        current = (
            await session.execute(
                select(PropLineHistory)
                .where(PropLineHistory.external_id == "ud-event")
                .order_by(PropLineHistory.fetched_at.desc())
                .limit(1)
            )
        ).scalar_one()

    assert current.game_id == "game-ud"
    assert current.game_time == future
    assert current.provider_status == "scheduled"


@pytest.mark.asyncio
async def test_underdog_picks_query_preserves_distinct_source_ids(db):
    future = datetime.utcnow() + timedelta(hours=2)
    async with db.session() as session:
        session.add_all([
            PropLineHistory(
                provider="Underdog",
                sport="NBA",
                player_name="Alternate Line",
                team="TEAM",
                stat_type="Points",
                line_value=20.5,
                game_time=future,
                provider_status="scheduled",
                external_id="line-low",
                game_id="game-alt",
                fetched_at=datetime.utcnow(),
            ),
            PropLineHistory(
                provider="Underdog",
                sport="NBA",
                player_name="Alternate Line",
                team="TEAM",
                stat_type="Points",
                line_value=22.5,
                game_time=future,
                provider_status="scheduled",
                external_id="line-high",
                game_id="game-alt",
                fetched_at=datetime.utcnow(),
            ),
        ])
        await session.commit()

    rows = await db.get_top_ud_props_for_picks(
        limit=10,
        since_hours=4,
        sports=["NBA"],
    )
    assert {row.external_id for row in rows} == {"line-low", "line-high"}


@pytest.mark.asyncio
async def test_picks_requests_bounded_provider_populations(monkeypatch):
    class BoundedDB:
        def __init__(self):
            self.ud_kwargs = None
            self.pp_kwargs = None
            self.rec_identities = None

        async def get_top_ud_props_for_picks(self, **kwargs):
            self.ud_kwargs = kwargs
            return []

        async def get_latest_props_for_provider(self, provider, **kwargs):
            assert provider == "PrizePicks"
            self.pp_kwargs = kwargs
            return []

        async def get_ud_recommendations_bulk(self, identities, since_hours):
            self.rec_identities = identities
            return {}

    fake_db = BoundedDB()
    monkeypatch.setattr(commands, "_db", fake_db)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(args=[])

    await commands._cmd_picks_inner(update, context)

    assert fake_db.ud_kwargs["limit"] == 40
    assert fake_db.ud_kwargs["limit"] is not None
    assert fake_db.pp_kwargs["limit"] == 40
    assert fake_db.pp_kwargs["directional_first"] is True
    assert fake_db.rec_identities == []


@pytest.mark.asyncio
async def test_recommendation_lookup_is_source_bounded(db):
    now = datetime.utcnow()
    requested = UnderdogSnapshotRecord(
        external_id="wanted-source",
        player_name="Bounded Player",
        team="TEAM",
        sport="NBA",
        stat_type="Points",
        line_value=20.5,
        game_id="wanted-game",
        game_time=now + timedelta(hours=1),
        removed=False,
        bet_recommendation="OVER",
        bet_confidence=72,
        fetched_at=now,
    )
    unrelated = [
        UnderdogSnapshotRecord(
            external_id=f"unrelated-{index}",
            player_name=f"Unrelated {index}",
            team="TEAM",
            sport="NBA",
            stat_type="Points",
            line_value=10.5,
            game_id=f"game-{index}",
            game_time=now + timedelta(hours=1),
            removed=False,
            bet_recommendation="UNDER",
            bet_confidence=60,
            fetched_at=now,
        )
        for index in range(100)
    ]
    async with db.session() as session:
        session.add_all([requested, *unrelated])
        await session.commit()

    result = await db.get_ud_recommendations_bulk([
        ("Bounded Player", "NBA", "Points", "wanted-source"),
    ])

    assert result == {
        ("Bounded Player", "NBA", "Points", "wanted-source"): ("OVER", 72)
    }


@pytest.mark.asyncio
async def test_outer_deadline_cancels_nested_database_task():
    child_started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def database_read():
        child_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    async def report():
        return await commands._bounded(database_read(), timeout=60)

    with pytest.raises(asyncio.TimeoutError):
        await commands._bounded(report(), timeout=0.01)

    await asyncio.wait_for(child_started.wait(), timeout=0.2)
    await asyncio.wait_for(child_cancelled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_representative_bounded_picks_path_finishes_under_deadline(monkeypatch):
    now = datetime.utcnow()
    pp_rows = [
        SimpleNamespace(
            provider="PrizePicks",
            player_name=f"Player {index}",
            sport="NBA",
            stat_type="Points",
            bet_recommendation="OVER" if index % 2 == 0 else "UNDER",
            external_id=f"pp-{index}",
            game_id=f"game-{index}",
            game_time=now + timedelta(hours=2),
            provider_status="scheduled",
            line_value=20.5 + index,
            prev_line=None,
            fetched_at=now,
            team="TEAM",
                score_tier="A",
            score_confidence=None,
                bet_confidence=75,
                score_total=75,
                market_quality_score=75,
        )
        for index in range(40)
    ]

    class RepresentativeDB:
        async def get_top_ud_props_for_picks(self, **kwargs):
            assert kwargs["limit"] == 40
            return []

        async def get_latest_props_for_provider(self, provider, **kwargs):
            assert provider == "PrizePicks"
            assert kwargs["limit"] == 40
            return pp_rows

        async def get_ud_recommendations_bulk(self, identities, since_hours):
            assert identities == []
            return {}

        async def get_player_results(self, *args, **kwargs):
            return []

        async def get_all_ud_lines_for_prop(self, *args, **kwargs):
            raise AssertionError("PrizePicks rows must not query Underdog lines")

        async def get_latest_downstream_intelligence(self, *args, **kwargs):
            return {}

    monkeypatch.setattr(commands, "_db", RepresentativeDB())
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(args=[])

    await commands._bounded(
        commands._cmd_picks_inner(update, context),
        timeout=commands._COMMAND_TIMEOUT_SECONDS,
    )

    sent_text = "\n".join(
        str(call.args[0]) for call in update.message.reply_text.await_args_list
    )
    assert "Actionable Player Prop Picks" in sent_text
    assert "temporarily unavailable" not in sent_text