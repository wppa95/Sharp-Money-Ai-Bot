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
        assert "3-leg section" in first_message

        slips[2] = _slip(2, line=19.5)
        await commands.auto_slip_notifications_job(context)
        assert bot.send_message.await_count == 2
        changed_message = bot.send_message.await_args.kwargs["text"]
        assert "2-leg section" in changed_message
        assert "3-leg section" not in changed_message
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