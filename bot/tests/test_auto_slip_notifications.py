from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import hashlib


def _slip(size: int, line: float = 18.5):
    return SimpleNamespace(
        legs=[
            SimpleNamespace(
                player_name=f"Player {index}",
                stat_type="Points",
                sport="NBA",
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


@pytest.mark.asyncio
async def test_auto_slips_send_once_and_reoffer_changed_combinations(monkeypatch):
    from database import Database
    import commands

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    monkeypatch.setattr(commands, "_db", db)
    monkeypatch.setenv("ALLOWED_USER_IDS", "777")

    slips = {2: _slip(2), 3: _slip(3)}

    async def fake_cmd_slip(_update, context):
        context._slip_build_capture.update(slips=slips, candidate_count=3)

    monkeypatch.setattr(commands, "cmd_slip", fake_cmd_slip)
    monkeypatch.setattr(
        commands,
        "_render_slip_section",
        lambda size, _slip, label="": [f"{size}-leg section"],
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(application=SimpleNamespace(bot=bot))

    try:
        await commands.auto_slip_notifications_job(context)
        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 1
        first_message = bot.send_message.await_args.kwargs["text"]
        assert "2-leg section" in first_message
        assert "3-leg section" not in first_message
        assert "Today's qualifying two-pick" in first_message
        assert "Research only" not in first_message

        slips[2] = _slip(2, line=19.5)
        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 2
        changed_message = bot.send_message.await_args.kwargs["text"]
        assert "2-leg section" in changed_message
        assert "3-leg section" not in changed_message
        assert "Research only" not in changed_message
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_telegram_send_releases_slip_claim_for_retry(monkeypatch):
    from database import Database
    import commands

    db = Database("sqlite+aiosqlite:///:memory:")
    await db.init()
    monkeypatch.setattr(commands, "_db", db)
    monkeypatch.setenv("ALLOWED_USER_IDS", "777")
    slips = {2: _slip(2)}

    async def fake_cmd_slip(_update, context):
        context._slip_build_capture.update(slips=slips, candidate_count=2)

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
            f"777:{commands._auto_slip_signature(2, slips[2])}".encode("utf-8")
        ).hexdigest()
        assert await db.claim_auto_slip_notification(
            signature, 2
        ) is False
    finally:
        await db.close()


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