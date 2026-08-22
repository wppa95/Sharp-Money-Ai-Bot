"""Focused tests for the best-effort Discord alert companion."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alerts import send_discord_alert
from config import config


@pytest.mark.asyncio
async def test_discord_send_uses_configured_channel_without_logging_token():
    response = MagicMock(status=200)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = response
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    timeout = MagicMock()

    with (
        patch.object(config, "DISCORD_BOT_TOKEN", "secret-token"),
        patch.object(config, "DISCORD_GUILD_ID", "guild"),
        patch.object(config, "DISCORD_CHANNEL_ID", "channel"),
        patch("aiohttp.ClientSession", return_value=session),
        patch("aiohttp.ClientTimeout", return_value=timeout),
    ):
        assert await send_discord_alert("<b>Pick</b> &amp; OVER") is True

    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args[0].endswith("/channels/channel/messages")
    assert kwargs["headers"]["Authorization"] == "Bot secret-token"
    assert kwargs["json"] == {"content": "Pick & OVER"}


@pytest.mark.asyncio
async def test_discord_failure_is_non_fatal():
    with (
        patch.object(config, "DISCORD_BOT_TOKEN", "token"),
        patch.object(config, "DISCORD_GUILD_ID", "guild"),
        patch.object(config, "DISCORD_CHANNEL_ID", "channel"),
        patch("aiohttp.ClientSession", side_effect=RuntimeError("offline")),
    ):
        assert await send_discord_alert("alert") is False