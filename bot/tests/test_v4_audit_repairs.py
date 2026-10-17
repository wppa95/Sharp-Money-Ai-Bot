"""V4 audit regressions. No external requests or production databases."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import commands
from engine.dashboard import DashboardReport, DashboardEngine
from engine.offering_coverage import coverage
from engine.player_props_pipeline import canonicalize_player_props
from engine.sport_policy import canonical_sport_key


def test_primary_provider_reporting_includes_both_distinct_sources():
    text = "\n".join(commands._primary_player_prop_provider_lines())
    assert "PrizePicks" in text and "primary" in text.lower()
    assert "Underdog: primary" in text and "secondary" not in text.lower()


def test_dashboard_removes_requested_sections_and_preserves_provider_populations():
    r = DashboardReport(
        tracking_by_sport={"NHL": {"active": 3}},
        recent_tracked_props=[dict(player_name="Example", sport="NHL",
                                   stat_type="Hits", line_value=1.5)],
        provider_pipeline={
            "PrizePicks": {"observed": 11, "active": 7, "directional": 3,
                          "delivered": 1, "pending": 2, "resolved": 1},
            "Underdog": {"observed": 22, "active": 9},
        },
    )
    text = r.to_telegram()
    assert "By current sport" not in text
    assert "Recently Tracked Props" not in text
    assert "PrizePicks" in text and "11" in text
    assert "Underdog" in text and "22" in text
    assert text.index("PrizePicks") < text.index("Underdog")


@pytest.mark.parametrize("alias,expected", [("CS2", "CS"), ("Counter-Strike 2", "CS"),
                                           ("Dota 2", "DOTA"), ("DOTA2", "DOTA")])
def test_sport_aliases_have_one_canonical_identity(alias, expected):
    assert canonical_sport_key(alias) == expected
    coverage.__init__()
    canonicalize_player_props("PrizePicks", [NS(
        sport=expected, player_name="Example", stat_type="Kills", line_value=10.5,
        external_id="id", game_id="event", game_time=datetime.utcnow()+timedelta(days=1),
        fetched_at=datetime.utcnow(),
    )])
    assert any("Kills" in line for line in coverage.report(alias, "PrizePicks"))


@pytest.mark.asyncio
async def test_analyst_reads_delivered_prizepicks_without_a_live_underdog_snapshot(monkeypatch):
    monkeypatch.setattr(commands, "_check_allowed", lambda _: True)
    db = MagicMock()
    db.get_alerted_opportunity_log = AsyncMock(return_value=[NS(
        provider="PrizePicks", external_id="PrizePicks:pp-id", game_id="event",
        player_name="PP Example", sport="NHL", stat_type="Shots on Goal",
        line_value=2.5, recommendation="UNDER", explanation="Completed game evidence",
        alert_sent=True, validation_json=None, analyst_payload_json=None,
        analyst_prediction=None, analyst_grade=None, analyst_model_version=None,
        game_time=datetime.utcnow()-timedelta(days=1),
    )])
    db.get_recent_underdog_snapshots = AsyncMock(return_value=[])
    monkeypatch.setattr(commands, "_db", db)
    update = NS(message=NS(reply_text=AsyncMock()))
    await commands.cmd_analyst(update, NS(args=[]))
    text = "\n".join(call.args[0] for call in update.message.reply_text.call_args_list)
    assert "PP Example" in text and "PrizePicks" in text and "UNDER" in text
    assert "No recent alerted picks" not in text
    db.get_recent_underdog_snapshots.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracking_does_not_hide_prizepicks(monkeypatch):
    monkeypatch.setattr(commands, "_check_allowed", lambda _: True)
    monkeypatch.setattr(commands, "_db", NS(get_tracking_summary=AsyncMock(return_value={
        "historical_observations": 30, "active_tracked": 7,
        "historical_observations_by_provider": {"PrizePicks": 10, "Underdog": 20},
        "active_by_provider": {"PrizePicks": 3, "Underdog": 4},
    })))
    update = NS(message=NS(reply_text=AsyncMock()))
    await commands.cmd_tracking(update, NS(args=[]))
    assert "PrizePicks" in update.message.reply_text.call_args.args[0]


@pytest.mark.parametrize("values,line,direction,hits,pushes", [
    ([16,29,6,6,4],10.5,"UNDER",3,0),
    ([1,0,0,0,0],0.5,"UNDER",4,0),
    ([1,2,3,4,5],3,"UNDER",2,1),
    ([1,2,3,4,5],3,"OVER",2,1),
])
def test_shared_directional_counts_match_actual_completed_games(values,line,direction,hits,pushes):
    from engine.player_results import compute_hit_rates, directional_window_counts
    rows = [NS(actual_value=v, game_date=f"2026-09-{25-i:02}", opponent="B")
            for i,v in enumerate(values)]
    hr = compute_hit_rates(rows,line)
    assert directional_window_counts(hr.l5,direction) == (hits,5,pushes)
    assert directional_window_counts(None,direction) is None
    assert directional_window_counts({"n":5,"o":2},"UNDER") is None


@pytest.mark.parametrize("scope,raw_key,value", [
    ("Kills on Maps 1+2","kills_maps_1_2",27),
    ("Kills on Maps 1+2+3","kills_maps_1_2_3",40),
])
def test_completed_cs_history_retains_and_matches_exact_map_scope(scope,raw_key,value):
    from player_history_job import _completed_propline_game_rows
    from providers.propline import PropLineReadOnlyTarget
    target = PropLineReadOnlyTarget(
        family="player_games",official_sport="esports",canonical_sport="CS",
        player_name="Example",market=scope)
    payload=NS(payload={"games":[{
        "game_id":"game", "status":"Final","game_date":"2026-09-20T20:00:00Z",
        "stats":{"kills_maps_1_2":27,"kills_maps_1_2_3":40},
    }]})
    rows=_completed_propline_game_rows(target,payload)
    assert len(rows)==1 and rows[0].actual_value==value
    assert rows[0].stat_type==scope.lower()
    payload.payload["games"][0]["stats"].pop(raw_key)
    assert _completed_propline_game_rows(target,payload)==[]


@pytest.fixture
async def evidence_db(tmp_path):
    from database import Database
    db=Database(f"sqlite+aiosqlite:///{tmp_path/'audit.db'}")
    await db.init()
    yield db
    await db._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_ids,expected",[
    (["a","a","a"],6), (["a","b","a"],None), (["","",""],None),
])
async def test_composite_grading_requires_one_original_event(evidence_db,event_ids,expected):
    for stat,event,value in zip(("hits","runs","rbis"),event_ids,(3,2,1)):
        await evidence_db.upsert_player_result(NS(
            player_name="Example",sport="MLB",stat_type=stat,game_date="2026-09-20",
            actual_value=value,opponent="B",source="mlb_stats_api",game_id=event))
    row=await evidence_db.get_game_result_for_grading(
        "Example","MLB","hits + runs + rbis","2026-09-20",game_id="a",provider="PrizePicks")
    opp=NS(id=12,player_name="Example",sport="MLB",stat_type="hits + runs + rbis",
           game_time=datetime(2026,9,20,20),game_id="a",provider="PrizePicks",team="A")
    bulk=await evidence_db.get_game_results_for_grading_bulk([opp])
    if expected is None:
        assert row is None
        assert 12 not in bulk
    else:
        assert row.actual_value==expected and row.game_id=="a"
        assert bulk[12][0].actual_value==expected and bulk[12][0].game_id=="a"


@pytest.mark.asyncio
async def test_completed_evidence_aliases_persist_and_read_without_migrating(evidence_db):
    await evidence_db.upsert_player_result(NS(
        player_name="Example",sport="CS2",stat_type="kills on maps 1+2",
        game_date="2026-09-20",actual_value=27,opponent="B",
        source="propline_player_games",game_id="a"))
    for alias in ("CS","CS2"):
        rows=await evidence_db.get_player_results("Example",alias,"kills on maps 1+2")
        assert len(rows)==1 and rows[0].sport=="CS"
        key=("Example",alias,"kills on maps 1+2")
        assert len((await evidence_db.get_player_results_bulk([key]))[key])==1
    assert await evidence_db.get_player_results("Example","CS","kills on maps 1+2+3")==[]
    opp=NS(id=12,player_name="Example",sport="CS2",stat_type="kills on maps 1+2",
           game_time=datetime(2026,9,20,20),game_id="a",provider="PrizePicks",team="A")
    assert (await evidence_db.get_game_results_for_grading_bulk([opp]))[12][0].actual_value==27


@pytest.mark.asyncio
async def test_last_write_telemetry_only_advances_after_successful_write_commits(evidence_db,monkeypatch):
    from sqlalchemy import text
    import engine.health
    tracker=MagicMock()
    monkeypatch.setattr(engine.health,"get_health_tracker",lambda:tracker)
    async with evidence_db.session() as s:
        await s.execute(text("SELECT 1"))
        await s.commit()
    tracker.record_database_write.assert_not_called()
    async with evidence_db.session() as s:
        await s.execute(text("UPDATE player_game_results SET source='x'"))
        await s.commit()
    tracker.record_database_write.assert_not_called()
    async with evidence_db.session() as s:
        await s.execute(text("UPDATE player_game_results SET source='x'"))
        await s.rollback()
    tracker.record_database_write.assert_not_called()
    await evidence_db.upsert_player_result(NS(
        player_name="Example",sport="MLB",stat_type="hits",game_date="2026-09-20",
        actual_value=1,opponent="B",source="mlb_stats_api",game_id="a"))
    tracker.record_database_write.assert_called_once_with("database_commit")


@pytest.mark.asyncio
async def test_delivered_analysis_retains_audit_direction_and_line_after_current_row_changes(evidence_db):
    from database import PropOpportunityLog
    async with evidence_db.session() as s:
        s.add(PropOpportunityLog(
            provider="PrizePicks",external_id="pp-source",player_name="Example",
            team="A",sport="NHL",stat_type="goals",line_value=1.5,
            recommendation="OVER",decision_tier="B",confidence=0,game_id="game",
            game_time=datetime.utcnow()-timedelta(days=1),alert_sent=True,
            result="MISS",actual_value=0))
        await s.commit()
    await evidence_db.record_telegram_play_delivery(
        provider="PrizePicks",delivery_path="prizepicks",player_name="Example",
        sport="NHL",stat_type="goals",external_id="pp-source",event_id="game",
        line_value=0.5,recipients_sent=1,recipients_failed=0,
        evidence_snapshot={"direction":"UNDER","line":0.5})
    rows=await evidence_db.get_alerted_opportunity_log()
    assert len(rows)==1
    assert (rows[0].provider,rows[0].line_value,rows[0].recommendation,rows[0].result)==(
        "PrizePicks",0.5,"UNDER","HIT")


@pytest.mark.asyncio
async def test_failed_feed_does_not_discard_another_completed_persisted_pp_batch(monkeypatch):
    import market_engine as me
    monkeypatch.setattr(me,"_propline_pending_props",{})
    monkeypatch.setattr(me,"_propline_empty_sport_until",{})
    monkeypatch.setattr(me,"_propline_empty_feed_streak",{})
    monkeypatch.setattr(me,"_propline_request_lock",None)
    monkeypatch.setattr(me,"_propline_request_cursor",0)
    monkeypatch.setattr(me,"_PROPLINE_SPORTS_PER_CYCLE",2)
    monkeypatch.setattr(me,"_get_prizepicks_scan_sports",AsyncMock(return_value=("NHL","NBA")))
    item=NS(external_id="pp",game_id="e",player_name="Example",sport="NHL",
            stat_type="shots on goal",line_value=2.5)
    provider=NS(is_available=lambda:True,backoff_remaining=0,client=None,
        fetch_props_for_sport_result=AsyncMock(side_effect=[
            NS(props=[item],succeeded=True,complete=True,error="",failure_kind=""),
            NS(props=[],succeeded=False,complete=False,error="timeout",failure_kind="timeout"),
        ]))
    monkeypatch.setattr(me,"_propline_provider",provider)
    monkeypatch.setattr(type(me.config),"propline_bookmakers",property(lambda _:["prizepicks"]))
    monkeypatch.setattr(me,"_persist_propline_observations",AsyncMock(return_value=1))
    monkeypatch.setattr(me,"_cache_propline_evidence",lambda *a,**k:None)
    monkeypatch.setattr(me,"_queue_propline_history_targets",lambda *a,**k:0)
    assert not await me.propline_request_scan_job(NS(bot_data={"db":object()}))
    assert list(me._propline_pending_props.values())==[item]


def test_current_picks_retains_independent_prizepicks_direction_and_source_line():
    from commands import _select_directional_provider_rows
    shared=dict(player_name="Example",sport="NHL",stat_type="goals",
                game_id="game",external_id="source",fetched_at=datetime.utcnow())
    ud=NS(**shared,provider="Underdog",bet_recommendation="OVER",line_value=0.5)
    pp=NS(**shared,provider="PrizePicks",bet_recommendation="UNDER",line_value=1.5)
    result=_select_directional_provider_rows([ud],[pp],{},10)
    assert {(provider,row.line_value,row.bet_recommendation) for provider,row in result}=={
        ("Underdog",0.5,"OVER"),("PrizePicks",1.5,"UNDER")}


@pytest.mark.asyncio
async def test_autoflushed_orm_write_records_commit_not_select(evidence_db,monkeypatch):
    from database import PlayerGameResult
    from sqlalchemy import select
    import engine.health
    tracker=MagicMock()
    monkeypatch.setattr(engine.health,"get_health_tracker",lambda:tracker)
    async with evidence_db.session() as s:
        s.add(PlayerGameResult(
            player_name="Example",sport="NHL",stat_type="goals",
            game_date="2026-09-20",game_id="game",source="nhl_stats_api",actual_value=1))
        await s.execute(select(PlayerGameResult))
        tracker.record_database_write.assert_not_called()
        await s.commit()
    tracker.record_database_write.assert_called_once_with("database_commit")


@pytest.mark.asyncio
async def test_failed_commit_never_advances_database_write_marker(evidence_db,monkeypatch):
    from sqlalchemy import text
    import engine.health
    tracker=MagicMock()
    monkeypatch.setattr(engine.health,"get_health_tracker",lambda:tracker)
    async with evidence_db.session() as s:
        await s.execute(text(
            "INSERT INTO player_game_results "
            "(player_name,sport,stat_type,game_date,game_id,source,actual_value,fetched_at) "
            "VALUES ('Example','NHL','goals','2026-09-20','game','nhl_stats_api',1,CURRENT_TIMESTAMP)"
        ))
        with monkeypatch.context() as m:
            m.setattr(s._session,"commit",AsyncMock(side_effect=RuntimeError("commit failed")))
            with pytest.raises(RuntimeError,match="commit failed"):
                await s.commit()
        await s.rollback()
    tracker.record_database_write.assert_not_called()
