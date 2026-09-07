"""Integration regressions for the durable Telegram command catalog."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update

import commands
import main
from database import Database, TelegramCommand


def _application_double() -> SimpleNamespace:
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    return SimpleNamespace(bot=bot)


@pytest.mark.asyncio
async def test_disabled_command_stays_disabled_across_restart_and_surfaces_match(
    tmp_path,
):
    """SQLite state drives both Telegram's menu and the /help response."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'commands.db'}"

    first_db = Database(database_url)
    await first_db.init()
    await first_db.ensure_telegram_commands(main.TELEGRAM_COMMAND_CATALOG)
    async with first_db.session() as session:
        await session.execute(
            update(TelegramCommand)
            .where(TelegramCommand.command == "alerts")
            .values(enabled=False)
        )
        await session.commit()
    await first_db.close()

    # A fresh Database instance represents the bot after restart.  Reseeding
    # must update metadata without restoring an operator-disabled command.
    restarted_db = Database(database_url)
    await restarted_db.init()
    application = _application_double()
    rows, seeded = await main.sync_telegram_command_catalog(
        application,
        restarted_db,
    )

    enabled_names = {row.command for row in rows}
    menu_names = {
        command.command
        for command in application.bot.set_my_commands.await_args.args[0]
    }
    assert seeded == len(main.TELEGRAM_COMMAND_CATALOG)
    assert "alerts" not in enabled_names
    assert menu_names == enabled_names
    assert {
        row.command
        for row in await restarted_db.get_telegram_commands(enabled_only=False)
    } == {
        definition["command"]
        for definition in main.TELEGRAM_COMMAND_CATALOG
    }

    update_obj = MagicMock()
    update_obj.effective_user.id = 1
    update_obj.message.reply_text = AsyncMock()
    context = SimpleNamespace(bot_data={"db": restarted_db})
    with patch("commands._check_allowed", return_value=True):
        await commands.cmd_help(update_obj, context)

    help_text = update_obj.message.reply_text.await_args.args[0]
    help_names = {
        line.split(" — ", 1)[0].strip()[1:]
        for line in help_text.splitlines()
        if line.strip().startswith("/")
    }
    assert help_names == enabled_names
    assert "/alerts" not in help_text

    # Disabling presentation does not unregister or replace the callback.
    registered = dict(main.TELEGRAM_COMMAND_HANDLERS)
    assert "alerts" in registered
    assert registered["alerts"] is commands.cmd_alerts

    await restarted_db.close()


@pytest.mark.asyncio
async def test_unavailable_catalog_falls_back_without_blocking_startup():
    """A transient catalog failure still leaves a usable static menu."""
    db = MagicMock()
    db.ensure_telegram_commands = AsyncMock(
        side_effect=RuntimeError("database is locked")
    )
    application = _application_double()

    rows, seeded = await main.sync_telegram_command_catalog(application, db)

    assert rows == []
    assert seeded == 0
    fallback_commands = application.bot.set_my_commands.await_args.args[0]
    assert {
        command.command for command in fallback_commands
    } == {
        definition["command"]
        for definition in main.TELEGRAM_COMMAND_CATALOG
    }