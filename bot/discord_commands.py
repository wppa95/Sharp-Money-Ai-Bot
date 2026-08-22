"""Best-effort Discord command gateway for the Sharp Money Bot.

This module is deliberately an adapter only.  It does not score props, poll
providers, or own any bot state; command requests are forwarded to the
existing Telegram command handlers with a small response bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from html import unescape
import re

from config import config

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
DISCORD_MAX_MESSAGE = 2000

# These are read-only visibility commands, except /slip's existing journal
# subcommands, whose behavior intentionally remains the Telegram behavior.
DISCORD_COMMANDS = frozenset({
    "funnel", "status", "picks", "slip", "dashboard", "alerts",
    "performance", "grade", "backtest", "help",
})


def discord_plain_text(message: str) -> str:
    """Convert the existing Telegram HTML output to Discord-safe text."""
    text = re.sub(r"<br\s*/?>", "\n", message, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip() or "No output."


def discord_chunks(message: str, max_len: int = DISCORD_MAX_MESSAGE) -> list[str]:
    """Split output without exceeding Discord's 2,000-character limit."""
    text = discord_plain_text(message)
    chunks: list[str] = []
    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len + 1)
        if cut < max_len // 2:
            cut = text.rfind(" ", 0, max_len + 1)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks or ["No output."]


class DiscordResponseMessage:
    """Telegram-like message object used by existing command handlers."""

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send

    async def reply_text(self, text: str, **_: Any) -> None:
        for chunk in discord_chunks(text):
            await self._send(chunk)


class DiscordCommandAdapter:
    """Route Discord text commands to the established command handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Awaitable[None]]] = {}
        self._load_handlers()

    def _load_handlers(self) -> None:
        from commands import (
            cmd_alerts, cmd_backtest, cmd_dashboard, cmd_funnel, cmd_grade,
            cmd_help, cmd_performance, cmd_picks, cmd_slip, cmd_status,
        )

        self._handlers = {
            "funnel": cmd_funnel,
            "status": cmd_status,
            "picks": cmd_picks,
            "slip": cmd_slip,
            "dashboard": cmd_dashboard,
            "alerts": cmd_alerts,
            "performance": cmd_performance,
            "grade": cmd_grade,
            "backtest": cmd_backtest,
            "help": cmd_help,
        }

    async def dispatch(
        self,
        content: str,
        *,
        user_id: int,
        guild_id: str,
        channel_id: str,
        send: Callable[[str], Awaitable[None]],
    ) -> bool:
        """Dispatch one Discord message; return whether it was a command."""
        try:
            parts = shlex.split(content.strip())
        except ValueError:
            parts = content.strip().split()
        if not parts or not parts[0].startswith("/"):
            return False
        command = parts[0][1:].split("@", 1)[0].lower()
        handler = self._handlers.get(command)
        if handler is None:
            return False

        update = SimpleNamespace(
            source="discord",
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=channel_id),
            message=DiscordResponseMessage(send),
        )
        context = SimpleNamespace(
            args=parts[1:],
            bot_data={},
            user_data={},
            chat_data={},
        )
        try:
            await handler(update, context)
        except Exception:
            # A Discord request can never bring down Telegram or the scheduler.
            logger.exception(
                "Discord command failed: /%s (guild=%s channel=%s)",
                command, guild_id, channel_id,
            )
            try:
                await send("⚠️ Discord command failed. Check bot logs.")
            except Exception:
                pass
        return True


class DiscordGateway:
    """Minimal Discord Gateway client with reconnect and heartbeat support."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._session: Any = None
        self._seq: Optional[int] = None
        self._adapter = DiscordCommandAdapter()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="discord_commands")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        if not config.discord_configured:
            logger.info("Discord command interface disabled (required variables absent)")
            return
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self._session = session
                delay = 5
                while not self._stop.is_set():
                    try:
                        await self._connect_once()
                        delay = 5
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("Discord command gateway disconnected (non-fatal): %s", exc)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Discord command interface stopped (non-fatal): %s", exc)
        finally:
            self._session = None

    async def _connect_once(self) -> None:
        async with self._session.ws_connect(DISCORD_GATEWAY, heartbeat=None) as ws:
            hello = await ws.receive_json()
            if hello.get("op") != 10:
                raise RuntimeError("Discord gateway did not send HELLO")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000
            await ws.send_json({
                "op": 2,
                "d": {
                    "token": config.DISCORD_BOT_TOKEN,
                    "intents": 33281,  # guilds + guild messages + message content
                    "properties": {"os": "linux", "browser": "sharp-money-bot", "device": "sharp-money-bot"},
                },
            })
            heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for msg in ws:
                    if msg.type == 1:
                        await self._handle_event(ws, msg.json())
                    elif msg.type in (8, 257):
                        raise RuntimeError(f"Discord gateway closed: {msg.type}")
                    elif msg.type in (9, 256):
                        raise RuntimeError("Discord gateway requested reconnect")
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send_json({"op": 1, "d": self._seq})

    async def _handle_event(self, ws: Any, payload: dict[str, Any]) -> None:
        if payload.get("s") is not None:
            self._seq = payload["s"]
        if payload.get("t") != "MESSAGE_CREATE":
            return
        data = payload.get("d") or {}
        if data.get("author", {}).get("bot"):
            return
        guild_id = str(data.get("guild_id") or "")
        channel_id = str(data.get("channel_id") or "")
        if guild_id != str(config.DISCORD_GUILD_ID):
            return
        if channel_id != str(config.DISCORD_CHANNEL_ID):
            return

        async def send(text: str) -> None:
            await self._send_message(channel_id, text)

        await self._adapter.dispatch(
            data.get("content", ""),
            user_id=int(data.get("author", {}).get("id", 0)),
            guild_id=guild_id,
            channel_id=channel_id,
            send=send,
        )

    async def _send_message(self, channel_id: str, content: str) -> None:
        if not self._session:
            return
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
        async with self._session.post(url, headers=headers, json={"content": content}) as response:
            if response.status not in (200, 201):
                logger.warning("Discord command response failed: HTTP %s", response.status)


_gateway = DiscordGateway()


def start_discord_commands() -> None:
    """Start the optional command listener without affecting bot jobs."""
    _gateway.start()


async def stop_discord_commands() -> None:
    await _gateway.stop()