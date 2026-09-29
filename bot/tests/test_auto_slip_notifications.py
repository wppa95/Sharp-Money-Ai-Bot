from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import hashlib


def _slip(size: int, line: float = 18.5, sport: str = "NBA"):
    return SimpleNamespace(
        legs=[
            SimpleNamespace(
                player_name=f"Player {index}",
                stat_type="Points",
                sport=sport,
                team=f"Team {index}",
                game_time=datetime(2026, 9, 28, 19, 0 + index),
                game_description="",
                confidence=None,
                tier=None,
                best_edge=None,
                best_side="OVER",
                pp_line_value=line + index,
                prev_line=None,
                opening_line=None,
                sportsbook="Underdog",
                external_id=f"event-{size}-{index}",
                game_id=f"game-{size}-{index}",
                comp=None,
            )
            for index in range(size)
        ],
        correlation_warnings=[],
        excluded=[],
    )


def test_slip_candidate_order_uses_directional_evidence_per_sport():
    from commands import _order_slip_candidates_by_sport

    def entry(name, sport, direction, over_rate, samples):
        candidate = SimpleNamespace(
            player_name=name,
            sport=sport,
            stat_type="Points",
            external_id=name,
            game_time=datetime(2026, 9, 28, 19, 0),
            best_side=direction,
        )
        prop = SimpleNamespace(sport=sport)
        history = (
            SimpleNamespace(
                season=SimpleNamespace(games=samples, hit_rate=over_rate),
                total_games=samples,
            )
            if samples
            else None
        )
        return candidate, prop, history

    entries = [
        entry("NBA weaker", "NBA", "OVER", 0.75, 20),
        entry("Tennis direction-only", "TENNIS", "UNDER", None, 0),
        entry("WNBA strong under", "WNBA", "UNDER", 0.20, 20),
        entry("NBA stronger", "NBA", "OVER", 0.90, 20),
    ]

    ordered = _order_slip_candidates_by_sport(entries)

    assert [candidate.player_name for candidate in ordered] == [
        "NBA stronger",
        "Tennis direction-only",
        "WNBA strong under",
        "NBA weaker",
    ]


@pytest.mark.asyncio
async def test_auto_slip_notification_claim_is_durable_and_idempotent():
    from database import Database

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    try:
        signature = "a" * 64
        assert await db.claim_auto_slip_notification(signature, 2) is True
        assert await db.claim_auto_slip_notification(signature, 2) is False
        assert await db.mark_auto_slip_notification_sent(signature) is True
        assert await db.claim_auto_slip_notification(signature, 2) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_slip_results_use_official_event_rows_and_deduplicate_recipients():
    from database import Database, PlayerGameResult

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    legs = [
        {
            "provider": "Underdog",
            "external_id": "prop-over",
            "game_id": "event-over",
            "player_name": "Over Player",
            "sport": "NBA",
            "stat_type": "points",
            "line_value": 18.5,
            "direction": "OVER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        },
        {
            "provider": "Underdog",
            "external_id": "prop-under",
            "game_id": "event-under",
            "player_name": "Under Player",
            "sport": "NBA",
            "stat_type": "rebounds",
            "line_value": 8.5,
            "direction": "UNDER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        },
    ]
    try:
        for signature in ("a" * 64, "b" * 64):
            assert await db.claim_auto_slip_notification(
                signature,
                2,
                combination_signature="c" * 64,
                legs=legs,
            )
            assert await db.mark_auto_slip_notification_sent(signature)

        async with db.session() as session:
            session.add_all(
                [
                    PlayerGameResult(
                        player_name="Over Player",
                        sport="NBA",
                        stat_type="points",
                        game_date="2026-09-28",
                        actual_value=22.0,
                        source="propline_event_results",
                        game_id="event-over",
                    ),
                    PlayerGameResult(
                        player_name="Under Player",
                        sport="NBA",
                        stat_type="rebounds",
                        game_date="2026-09-28",
                        actual_value=8.5,
                        source="propline_history",
                        game_id="event-under",
                    ),
                ]
            )
            await session.commit()

        assert await db.grade_auto_slip_notification_legs() == 4
        history = await db.get_auto_slip_notification_history(limit=8)
        assert len(history) == 1
        assert history[0]["result"] == "PUSH"
        assert [leg["result"] for leg in history[0]["legs"]] == ["HIT", "PUSH"]

        stats = await db.get_auto_slip_notification_stats()
        assert stats["total_slips"] == 1
        assert stats["by_size"][2]["push"] == 1
        assert stats["untracked_deliveries"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_slip_result_reports_miss_with_directional_under_hit():
    from database import Database, PlayerGameResult

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    legs = [
        {
            "provider": "Underdog",
            "external_id": "prop-under-win",
            "game_id": "event-under-win",
            "player_name": "Under Winner",
            "sport": "NBA",
            "stat_type": "rebounds",
            "line_value": 8.5,
            "direction": "UNDER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        },
        {
            "provider": "Underdog",
            "external_id": "prop-over-loss",
            "game_id": "event-over-loss",
            "player_name": "Over Loser",
            "sport": "NBA",
            "stat_type": "points",
            "line_value": 20.5,
            "direction": "OVER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        },
    ]
    try:
        assert await db.claim_auto_slip_notification(
            "f" * 64,
            2,
            combination_signature="1" * 64,
            legs=legs,
        )
        assert await db.mark_auto_slip_notification_sent("f" * 64)
        async with db.session() as session:
            session.add_all(
                [
                    PlayerGameResult(
                        player_name="Under Winner",
                        sport="NBA",
                        stat_type="rebounds",
                        game_date="2026-09-28",
                        actual_value=7.0,
                        source="propline_event_results",
                        game_id="event-under-win",
                    ),
                    PlayerGameResult(
                        player_name="Over Loser",
                        sport="NBA",
                        stat_type="points",
                        game_date="2026-09-28",
                        actual_value=19.0,
                        source="propline_history",
                        game_id="event-over-loss",
                    ),
                ]
            )
            await session.commit()

        assert await db.grade_auto_slip_notification_legs() == 2
        history = await db.get_auto_slip_notification_history(limit=8)
        assert history[0]["result"] == "MISS"
        assert [leg["result"] for leg in history[0]["legs"]] == ["HIT", "MISS"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_slip_does_not_grade_untrusted_or_wrong_event_rows():
    from database import Database, PlayerGameResult

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    legs = [
        {
            "provider": "Underdog",
            "external_id": "prop-a",
            "game_id": "expected-event",
            "player_name": "Player A",
            "sport": "NBA",
            "stat_type": "points",
            "line_value": 10.5,
            "direction": "OVER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        }
    ]
    try:
        assert await db.claim_auto_slip_notification(
            "d" * 64,
            2,
            combination_signature="e" * 64,
            legs=legs,
        )
        assert await db.mark_auto_slip_notification_sent("d" * 64)
        async with db.session() as session:
            session.add_all(
                [
                    PlayerGameResult(
                        player_name="Player A",
                        sport="NBA",
                        stat_type="points",
                        game_date="2026-09-28",
                        actual_value=20.0,
                        source="propline_event_results",
                        game_id="different-event",
                    ),
                    PlayerGameResult(
                        player_name="Player A",
                        sport="NBA",
                        stat_type="points",
                        game_date="2026-09-28",
                        actual_value=20.0,
                        source="api",
                        game_id="expected-event",
                    ),
                ]
            )
            await session.commit()

        assert await db.grade_auto_slip_notification_legs() == 0
        history = await db.get_auto_slip_notification_history(limit=8)
        assert history[0]["result"] == "PENDING"
        assert history[0]["legs"][0]["result"] == "PENDING"
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("sport", "stat_type"),
    [
        ("TENNIS", "aces"),
        ("NBA", "points"),
        ("MLB", "hits"),
        ("CS", "kills"),
    ],
)
@pytest.mark.asyncio
async def test_auto_slip_grades_final_player_game_stats_for_exact_event(
    sport: str, stat_type: str
):
    from database import Database, PlayerGameResult

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    legs = [
        {
            "provider": "Underdog",
            "external_id": f"{sport}-over",
            "game_id": f"{sport}-event-over",
            "player_name": f"{sport} Over Player",
            "sport": sport,
            "stat_type": stat_type,
            "line_value": 10.5,
            "direction": "OVER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        },
        {
            "provider": "Underdog",
            "external_id": f"{sport}-under",
            "game_id": f"{sport}-event-under",
            "player_name": f"{sport} Under Player",
            "sport": sport,
            "stat_type": stat_type,
            "line_value": 8.5,
            "direction": "UNDER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        },
    ]
    try:
        assert await db.claim_auto_slip_notification(
            f"{sport}-notification",
            2,
            combination_signature=f"{sport}-combination",
            legs=legs,
        )
        assert await db.mark_auto_slip_notification_sent(f"{sport}-notification")
        async with db.session() as session:
            session.add_all(
                [
                    PlayerGameResult(
                        player_name=f"{sport} Over Player",
                        sport=sport,
                        stat_type=stat_type,
                        game_date="2026-09-28",
                        actual_value=12.0,
                        source="propline_player_games",
                        game_id=f"{sport}-event-over",
                    ),
                    PlayerGameResult(
                        player_name=f"{sport} Under Player",
                        sport=sport,
                        stat_type=stat_type,
                        game_date="2026-09-28",
                        actual_value=7.0,
                        source="propline_player_games",
                        game_id=f"{sport}-event-under",
                    ),
                ]
            )
            await session.commit()

        assert await db.grade_auto_slip_notification_legs() == 2
        history = await db.get_auto_slip_notification_history(limit=1)
        assert [leg["result"] for leg in history[0]["legs"]] == ["HIT", "HIT"]
        assert all(
            leg["grade_result_source"] == "propline_player_games"
            for leg in history[0]["legs"]
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_slip_player_game_stats_require_saved_event_identity():
    from database import Database, PlayerGameResult

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    legs = [
        {
            "provider": "Underdog",
            "external_id": "no-event-id-prop",
            "game_id": "",
            "player_name": "No Event ID Player",
            "sport": "TENNIS",
            "stat_type": "aces",
            "line_value": 10.5,
            "direction": "OVER",
            "game_time": datetime(2026, 9, 28, 19, 0),
        }
    ]
    try:
        assert await db.claim_auto_slip_notification(
            "no-event-id-notification",
            2,
            combination_signature="no-event-id-combination",
            legs=legs,
        )
        assert await db.mark_auto_slip_notification_sent("no-event-id-notification")
        await db.upsert_player_result(
            PlayerGameResult(
                player_name="No Event ID Player",
                sport="TENNIS",
                stat_type="aces",
                game_date="2026-09-28",
                actual_value=12.0,
                source="propline_player_games",
                game_id="completed-event",
            )
        )

        assert await db.grade_auto_slip_notification_legs() == 0
        history = await db.get_auto_slip_notification_history(limit=1)
        assert history[0]["legs"][0]["result"] == "PENDING"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_slips_send_once_and_reoffer_changed_combinations(monkeypatch):
    from database import Database
    import commands

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    monkeypatch.setattr(commands, "_db", db)
    monkeypatch.setenv("ALLOWED_USER_IDS", "777")

    slips_by_sport = {
        "CS": _slip(2, sport="CS"),
        "TENNIS": _slip(2, sport="TENNIS"),
    }

    async def fake_cmd_slip(_update, context):
        context._slip_build_capture.update(
            slips_by_sport=slips_by_sport,
            candidate_count=4,
            candidate_counts_by_sport={"CS": 2, "TENNIS": 2},
        )

    monkeypatch.setattr(commands, "cmd_slip", fake_cmd_slip)
    monkeypatch.setattr(
        commands,
        "_render_slip_section",
        lambda size, _slip, label="": [f"{label} {size}-leg section"],
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(application=SimpleNamespace(bot=bot))

    try:
        await commands.auto_slip_notifications_job(context)
        first_message = bot.send_message.await_args.kwargs["text"]
        assert bot.send_message.await_count == 1
        assert "CS 2-leg section" in first_message
        assert "TENNIS 2-leg section" not in first_message
        assert "Today's qualifying two-pick slip" in first_message

        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 2
        second_message = bot.send_message.await_args.kwargs["text"]
        assert "CS 2-leg section" not in second_message
        assert "TENNIS 2-leg section" in second_message

        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 2

        slips_by_sport["TENNIS"] = _slip(
            2, line=19.5, sport="TENNIS"
        )
        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 3
        changed_message = bot.send_message.await_args.kwargs["text"]
        assert "TENNIS 2-leg section" in changed_message
        assert "CS 2-leg section" not in changed_message
    finally:
        await db.close()


def test_automatic_slips_require_a_known_start_within_two_hours(monkeypatch):
    from datetime import datetime, timedelta, timezone
    import commands

    now = datetime(2026, 9, 29, 12, 0)
    monkeypatch.setattr(commands.config, "ALERT_WINDOW_MAX_MINUTES", 120)

    assert commands._auto_slip_event_in_window(
        now + timedelta(minutes=119), "scheduled", now=now
    )
    assert not commands._auto_slip_event_in_window(
        now + timedelta(hours=12), "scheduled", now=now
    )
    assert not commands._auto_slip_event_in_window(
        now - timedelta(minutes=1), "scheduled", now=now
    )
    assert not commands._auto_slip_event_in_window(
        None, "scheduled", now=now
    )
    # Aware provider instants are normalized to UTC before comparing.
    assert commands._auto_slip_event_in_window(
        datetime(
            2026, 9, 29, 8, 0,
            tzinfo=timezone(timedelta(hours=-5)),
        ),
        "scheduled",
        now=now,
    )


@pytest.mark.asyncio
async def test_failed_telegram_send_releases_slip_claim_for_retry(monkeypatch):
    from database import Database
    import commands

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    monkeypatch.setattr(commands, "_db", db)
    monkeypatch.setenv("ALLOWED_USER_IDS", "777")
    slips_by_sport = {"NBA": _slip(2)}

    async def fake_cmd_slip(_update, context):
        context._slip_build_capture.update(
            slips_by_sport=slips_by_sport,
            candidate_count=2,
            candidate_counts_by_sport={"NBA": 2},
        )

    monkeypatch.setattr(commands, "cmd_slip", fake_cmd_slip)
    monkeypatch.setattr(
        commands,
        "_render_slip_section",
        lambda size, _slip, label="": [f"{size}-leg section"],
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[RuntimeError("temporary Telegram error"), None]
        )
    )
    context = SimpleNamespace(application=SimpleNamespace(bot=bot))

    try:
        await commands.auto_slip_notifications_job(context)
        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 2
        signature = hashlib.sha256(
            f"777:{commands._auto_slip_signature(2, slips_by_sport['NBA'])}".encode(
                "utf-8"
            )
        ).hexdigest()
        assert await db.claim_auto_slip_notification(
            signature, 2
        ) is False
    finally:
        await db.close()


def test_auto_slips_are_built_separately_for_each_sport():
    from commands import _build_auto_slips_by_sport

    candidates = _slip(2, sport="CS").legs + _slip(
        2, sport="TENNIS"
    ).legs
    slips, counts = _build_auto_slips_by_sport(candidates)

    assert counts == {"CS": 2, "TENNIS": 2}
    assert set(slips) == {"CS", "TENNIS"}
    for sport, slip in slips.items():
        assert len(slip.legs) == 2
        assert {leg.sport for leg in slip.legs} == {sport}


def test_auto_slip_signature_ignores_leg_order_but_tracks_line_changes():
    from commands import _auto_slip_signature

    slip = _slip(2)
    reversed_slip = SimpleNamespace(
        legs=list(reversed(slip.legs)),
        correlation_warnings=[],
        excluded=[],
    )
    assert _auto_slip_signature(2, slip) == _auto_slip_signature(
        2, reversed_slip
    )

    changed = _slip(2, line=19.5)
    assert _auto_slip_signature(2, slip) != _auto_slip_signature(2, changed)


@pytest.mark.asyncio
async def test_slip_journal_and_slipstats_show_auto_slip_outcomes(monkeypatch):
    import commands

    db = SimpleNamespace(
        get_slip_journal_history=AsyncMock(return_value=[]),
        grade_auto_slip_notification_legs=AsyncMock(return_value=2),
        get_auto_slip_notification_history=AsyncMock(
            return_value=[
                {
                    "slip_size": 2,
                    "sent_at": datetime(2026, 9, 28, 20, 0),
                    "result": "PUSH",
                    "legs": [
                        {
                            "player_name": "Player A",
                            "direction": "OVER",
                            "line_value": 10.5,
                            "stat_type": "Points",
                            "result": "HIT",
                            "actual_value": 12.0,
                        },
                        {
                            "player_name": "Player B",
                            "direction": "UNDER",
                            "line_value": 8.5,
                            "stat_type": "Rebounds",
                            "result": "PUSH",
                            "actual_value": 8.5,
                        },
                    ],
                }
            ]
        ),
        get_auto_slip_notification_stats=AsyncMock(
            return_value={
                "by_size": {
                    2: {
                        "hit": 0,
                        "miss": 0,
                        "push": 1,
                        "pending": 0,
                        "void": 0,
                        "total": 1,
                    }
                },
                "total_slips": 1,
                "untracked_deliveries": 0,
            }
        ),
        get_slip_journal_stats=AsyncMock(
            return_value={
                "total_slips": 0,
                "by_size": {},
                "total_staked": 0.0,
                "total_payout": 0.0,
                "overall_roi": None,
            }
        ),
        get_pick_accuracy_by_sport=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(commands, "_db", db)
    monkeypatch.setattr(commands, "_check_allowed", lambda _update: True)

    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock())
    )
    context = SimpleNamespace(args=[])

    await commands._cmd_slip_journal(update, context, ["journal"])
    journal_text = update.message.reply_text.await_args.args[0]
    assert "Auto-sent combinations" in journal_text
    assert "PUSH" in journal_text
    assert "Player A" in journal_text
    assert "actual 8.5" in journal_text

    update.message.reply_text.reset_mock()
    await commands.cmd_slipstats(update, context)
    stats_text = update.message.reply_text.await_args.args[0]
    assert "AUTO-SENT SLIPS" in stats_text
    assert "1P" in stats_text
    assert "HIT = every leg hit" in stats_text