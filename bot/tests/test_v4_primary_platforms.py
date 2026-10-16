"""Equal primary-platform grading with shared, identity-checked evidence."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import commands
import main
import market_engine
from database import Database, PropOpportunityLog, is_gradeable_player_result_for_event
from providers import player_history, player_stats


@pytest.mark.parametrize("source", [
    "propline_event_stats", "propline_player_games",
    "propline_event_results", "propline_history",
])
async def test_shared_result_grades_both_platforms_without_merging_lines(
    source, monkeypatch,
):
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    now = datetime.utcnow()
    fetch_stats = AsyncMock(side_effect=AssertionError("stored evidence must win"))
    fetch_event = AsyncMock(side_effect=AssertionError("unnecessary PropLine call"))
    fetch_identity = AsyncMock(side_effect=AssertionError("unnecessary PropLine call"))
    monkeypatch.setattr(main, "get_health_tracker", lambda: None)
    monkeypatch.setattr(player_stats.PlayerStatsProvider, "fetch_results", fetch_stats)
    monkeypatch.setattr(player_history, "get_player_history_provider", lambda: SimpleNamespace(
        fetch_resolved_event_results=fetch_event,
        fetch_resolved_results=fetch_identity,
    ))
    try:
        for provider in ("Underdog", "PrizePicks"):
            for line in (19.0, 20.0, 21.0):
                await db.log_prop_opportunity(
                    external_id=f"{provider}-{line}",
                    player_name="Shared Player", team="TEAM", sport="MLB",
                    stat_type="Hits", line_value=line, recommendation="OVER",
                    decision_tier="A", confidence=80,
                    game_time=now - timedelta(hours=6), game_id="verified-event",
                    provider=provider,
                )
        await db.upsert_player_result(SimpleNamespace(
            player_name="Shared Player", sport="MLB", stat_type="Hits",
            game_date=(now - timedelta(hours=6)).strftime("%Y-%m-%d"),
            actual_value=20.0, opponent="OPP", source=source,
            game_id="verified-event",
        ))
        await main._grade_opportunities_job(SimpleNamespace(bot_data={"db": db}))
        async with db.session() as session:
            rows = (await session.execute(select(PropOpportunityLog))).scalars().all()
        assert len(rows) == 6
        for provider in ("Underdog", "PrizePicks"):
            owned = [row for row in rows if row.provider == provider]
            assert {row.line_value: row.result for row in owned} == {
                19.0: "HIT", 20.0: "PUSH", 21.0: "MISS",
            }
            assert all(row.game_id == "verified-event" for row in owned)
            assert all(row.grade_result_source == source for row in owned)
        fetch_stats.assert_not_awaited()
        fetch_event.assert_not_awaited()
        fetch_identity.assert_not_awaited()
    finally:
        await db.close()


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
@pytest.mark.parametrize("source", ["propline_event_stats", "propline_player_games"])
def test_shared_final_stats_still_require_exact_event(provider, source):
    assert is_gradeable_player_result_for_event(provider, source, "game", "game")
    assert not is_gradeable_player_result_for_event(provider, source, "game", "other")
    assert not is_gradeable_player_result_for_event(provider, source, "", "game")
    assert not is_gradeable_player_result_for_event(provider, source, "game", "")


@pytest.mark.parametrize("args", [[], ["2"], ["3"], ["6"], ["create"], ["add", "Player"]])
async def test_slip_routes_cannot_create_or_build_entries(args, monkeypatch):
    monkeypatch.setattr(commands, "_check_allowed", lambda _: True)
    monkeypatch.setattr(commands, "_db", SimpleNamespace())
    journal = AsyncMock()
    monkeypatch.setattr(commands, "_cmd_slip_journal", journal)
    reply = AsyncMock()
    await commands.cmd_slip(
        SimpleNamespace(message=SimpleNamespace(reply_text=reply)),
        SimpleNamespace(args=args),
    )
    journal.assert_not_awaited()
    assert "Multi-pick slips are disabled" in reply.await_args.args[0]


async def test_every_prizepicks_line_can_read_local_evidence_beyond_250(monkeypatch):
    props = [
        SimpleNamespace(
            player_name=f"Player {i}", sport="TENNIS", stat_type="Aces",
            line_value=3.5, external_id=f"pp-{i}", event_id=f"event-{i}",
            game_time=datetime.utcnow() + timedelta(hours=1),
            fetched_at=datetime.utcnow(),
        )
        for i in range(251)
    ]
    db = SimpleNamespace(
        get_prop_line_history=AsyncMock(return_value=[]),
        track_prop_records=AsyncMock(),
        log_prop_candidates_bulk=AsyncMock(),
    )
    local_evidence = AsyncMock(return_value=None)
    monkeypatch.setattr(market_engine, "_prizepicks_processing_lock", None)
    monkeypatch.setattr(market_engine, "_propline_provider", SimpleNamespace(
        is_available=lambda: True,
    ))
    monkeypatch.setattr(market_engine, "_load_line_movement_context", AsyncMock(return_value={}))
    monkeypatch.setattr(market_engine, "_fetch_and_compute_hit_rates", local_evidence)
    monkeypatch.setattr(player_history, "get_player_history_provider", lambda: SimpleNamespace(
        get_snapshot=AsyncMock(return_value=None),
    ))
    await market_engine._run_prizepicks_independent_cycle(
        SimpleNamespace(bot_data={"db": db}, bot=SimpleNamespace()),
        _fetched_props=props, _fetched_persisted=True,
    )
    assert local_evidence.await_count == 251
    assert all(
        call.kwargs["allow_remote"] is False
        and call.kwargs["source_provider"] == "PrizePicks"
        for call in local_evidence.await_args_list
    )


async def test_disabled_auto_slips_never_build_claim_or_send(monkeypatch):
    build = AsyncMock()
    db = AsyncMock()
    bot = AsyncMock()
    monkeypatch.setattr(commands, "cmd_slip", build)
    monkeypatch.setattr(commands, "_db", db)
    await commands.auto_slip_notifications_job(
        SimpleNamespace(application=SimpleNamespace(bot=bot))
    )
    build.assert_not_awaited()
    assert db.mock_calls == []
    assert bot.mock_calls == []
