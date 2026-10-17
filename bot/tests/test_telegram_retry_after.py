from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import RetryAfter

import alerts


@pytest.mark.asyncio
async def test_send_alert_honors_retry_after_per_recipient(monkeypatch, caplog):
    now = [100.0]
    monkeypatch.setattr(alerts, "_telegram_clock", lambda: now[0])
    monkeypatch.setattr(alerts, "_telegram_retry_after_until", {})

    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[RetryAfter(45), None, None]
    )

    # The API's rejection is definitive; don't immediately retry this message.
    assert not await alerts.send_alert(bot, "recipient-a", "pick")
    bot.send_message.assert_awaited_once()

    # Suppress subsequent sends to that recipient for Telegram's retry window.
    assert not await alerts.send_alert(bot, "recipient-a", "next pick")
    bot.send_message.assert_awaited_once()

    # A different recipient retains its independent delivery path.
    assert await alerts.send_alert(bot, "recipient-b", "pick")
    assert bot.send_message.await_count == 2

    now[0] += 46
    assert await alerts.send_alert(bot, "recipient-a", "freshly revalidated pick")
    assert bot.send_message.await_count == 3

    # Transport logs must not reveal recipient identifiers.
    assert "recipient-a" not in caplog.text
    assert "recipient-b" not in caplog.text


@pytest.mark.asyncio
async def test_retry_after_deadline_only_extends(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(alerts, "_telegram_clock", lambda: now[0])
    monkeypatch.setattr(alerts, "_telegram_retry_after_until", {})

    alerts._record_telegram_retry_after("recipient", 30)
    alerts._record_telegram_retry_after("recipient", 10)
    assert alerts._telegram_retry_after_remaining("recipient") == 30

    alerts._record_telegram_retry_after("recipient", 50)
    assert alerts._telegram_retry_after_remaining("recipient") == 50

    now[0] += 51
    assert alerts._telegram_retry_after_remaining("recipient") == 0
    assert "recipient" not in alerts._telegram_retry_after_until
