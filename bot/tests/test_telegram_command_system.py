"""Regression coverage for the live Telegram command registration boundary."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import main
import pytest
from httpx import ReadError
from telegram.error import Conflict, NetworkError
from commands import (
    cmd_grade,
    cmd_learning,
    cmd_rollups,
)


AUTHORITATIVE_COMMANDS = {
    "picks",
    "slip",
    "dashboard",
    "alerts",
    "funnel",
    "reconcile",
    "grade",
    "grades",
    "player",
    "slipstats",
    "analyze",
    "market",
    "steam",
    "ev",
    "clv",
    "performance",
    "backtest",
    "stats",
    "calibration",
    "tracking",
    "analyst",
    "rollups",
    "refinement",
    "start",
    "status",
    "system",
    "health",
    "providers",
    "config",
    "blocks",
    "block",
    "backfill",
    "testalert",
    "help",
}


class RecordingApplication:
    """Small application double that records the actual registration calls."""

    def __init__(self) -> None:
        self.handlers = []
        self.error_handlers = []

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler) -> None:
        self.error_handlers.append(handler)


def _registered_map(application: RecordingApplication) -> dict[str, object]:
    return {
        next(iter(handler.commands)): handler.callback
        for handler in application.handlers
    }


def test_authoritative_command_surface_is_registered_once():
    application = RecordingApplication()

    returned = main.register_command_handlers(application)
    registered = _registered_map(application)

    assert returned is application
    assert AUTHORITATIVE_COMMANDS <= registered.keys()
    assert len(registered) == 35
    assert len(application.handlers) == len(registered)
    assert application.error_handlers == [main.error_handler]


def test_aliases_share_the_exact_production_callbacks():
    application = RecordingApplication()
    main.register_command_handlers(application)
    registered = _registered_map(application)

    assert registered["grade"] is cmd_grade
    assert registered["grades"] is cmd_grade
    assert registered["rollups"] is cmd_rollups
    assert registered["learning"] is cmd_learning


def test_every_registered_callback_is_async_and_command_handlers_are_real():
    application = RecordingApplication()
    main.register_command_handlers(application)

    assert all(type(handler).__name__ == "CommandHandler" for handler in application.handlers)
    assert all(inspect.iscoroutinefunction(handler.callback) for handler in application.handlers)
    assert all(handler.commands for handler in application.handlers)


def test_build_application_uses_the_same_registered_surface(monkeypatch):
    monkeypatch.setattr(main.config, "TELEGRAM_BOT_TOKEN", "123456:regression-test-token")

    application = main.build_application()
    registered = {
        command
        for handler in application.handlers[0]
        for command in handler.commands
    }

    assert AUTHORITATIVE_COMMANDS <= registered
    assert "grades" in registered
    assert "learning" in registered
    assert len(registered) == 35
    assert len(application.error_handlers) == 1


def test_registration_does_not_require_database_or_engine_initialization():
    application = RecordingApplication()

    main.register_command_handlers(application)

    assert all(handler.callback for handler in application.handlers)


@pytest.mark.asyncio
async def test_telegram_conflict_stops_duplicate_polling_instance():
    class ApplicationDouble:
        stopped = False

        def stop_running(self) -> None:
            self.stopped = True

    application = ApplicationDouble()
    context = SimpleNamespace(
        error=Conflict("terminated by another getUpdates request"),
        application=application,
    )

    await main.error_handler(None, context)

    assert application.stopped is True


@pytest.mark.asyncio
async def test_wrapped_telegram_read_error_is_transient(caplog):
    transport_error = ReadError("connection reset")
    wrapped = NetworkError(str(transport_error))
    wrapped.__cause__ = transport_error
    context = SimpleNamespace(error=wrapped, application=None)

    with caplog.at_level(logging.WARNING, logger="commands"):
        await main.error_handler(None, context)

    assert "Telegram HTTP read dropped" in caplog.text
    assert "Update None caused error" not in caplog.text