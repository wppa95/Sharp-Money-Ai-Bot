"""Focused regressions for Telegram command reliability and provider reporting."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import commands
from alerts import format_help_message
from engine.dashboard import DashboardReport, DailyTrend, SportPerf


def _update_and_context(args: list[str] | None = None):
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    return update, context


def _mock_status_db() -> MagicMock:
    db = MagicMock()
    db.count_steam_records = AsyncMock(return_value=0)
    db.count_ev_records = AsyncMock(return_value=0)
    db.count_odds_records = AsyncMock(return_value=0)
    db.count_today_actionable_alerts = AsyncMock(return_value=0)
    db.count_prop_line_history = AsyncMock(
        side_effect=lambda provider=None: {
            None: 30,
            "Underdog": 20,
            "PrizePicks": 10,
        }[provider]
    )
    return db


def test_help_lists_every_registered_command_and_no_unregistered_restarts():
    main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    registered = set(re.findall(r'CommandHandler\("([^"]+)"', main_source))
    help_text = format_help_message()

    assert registered
    assert all(f"/{command}" in help_text for command in registered)
    assert "/restarts" not in help_text


def test_every_registered_command_resolves_to_an_async_handler():
    main_source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    registrations = re.findall(
        r'CommandHandler\("([^"]+)",\s*(cmd_[a-z_]+)\)',
        main_source,
    )

    assert len(registrations) == 34
    for command, handler_name in registrations:
        handler = getattr(commands, handler_name, None)
        assert handler is not None, f"/{command} has no {handler_name}"
        assert inspect.iscoroutinefunction(handler), f"/{command} handler is not async"


def test_canonical_provider_roles_are_exact():
    lines = commands._primary_player_prop_provider_lines()

    assert lines == [
        "  🟢 <b>Underdog</b> — Primary Player Props",
        "  🟢 <b>PrizePicks</b> — PropLine — Primary Player Props",
        "  🔴 <b>DraftKings/FanDuel</b> — Disabled",
    ]


@pytest.mark.asyncio
async def test_dashboard_times_out_without_holding_command_worker():
    update, context = _update_and_context()

    async def never_finishes(_db):
        await asyncio.sleep(1)

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", MagicMock()),
        patch("commands._DASHBOARD_TIMEOUT_SECONDS", 0.01),
        patch("engine.dashboard.DashboardEngine.gather", side_effect=never_finishes),
    ):
        await asyncio.wait_for(commands.cmd_dashboard(update, context), timeout=0.2)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert messages[0] == "⏳ Gathering stats…"
    assert any("taking too long" in message for message in messages)


@pytest.mark.asyncio
async def test_status_shows_provider_roles_and_separate_history_counts():
    update, context = _update_and_context()
    db = _mock_status_db()

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._season_checker", None),
    ):
        await commands.cmd_status(update, context)

    message = update.message.reply_text.await_args.args[0]
    assert "🟢 <b>Underdog</b> — Primary Player Props" in message
    assert "🟢 <b>PrizePicks</b> — PropLine — Primary Player Props" in message
    assert "🔴 <b>DraftKings/FanDuel</b> — Disabled" in message
    assert "Underdog: 20" in message
    assert "PrizePicks (PropLine): 10" in message


@pytest.mark.asyncio
async def test_dashboard_activity_section_keeps_provider_counts_separate():
    update, context = _update_and_context()
    db = MagicMock()
    db.count_underdog_records = AsyncMock(return_value=100)
    db.count_prop_line_history = AsyncMock(
        side_effect=lambda provider=None: {
            "Underdog": 70,
            "PrizePicks": 30,
        }[provider]
    )
    db.get_recent_tracked_props = AsyncMock(return_value=[])
    report = DashboardReport()

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("engine.dashboard.DashboardEngine.gather", AsyncMock(return_value=report)),
    ):
        await commands.cmd_dashboard(update, context)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    activity = messages[-1]
    assert "Underdog snapshots:  <code>100</code>" in activity
    assert "PrizePicks (PropLine) history: <code>30</code>" in activity
    assert "🐶 <b>Underdog</b>" in activity
    assert "🟣 <b>PrizePicks</b>" in activity


def test_dashboard_report_does_not_merge_provider_breakdowns():
    report = DashboardReport(
        total_ud_alerts=12,
        total_pp_alerts=7,
        today_ud_alerts=2,
        today_pp_alerts=3,
        by_sport=[SportPerf(sport="MLB", ud_count=5, pp_count=4, ev_count=1)],
        daily_trend=[
            DailyTrend(date_str="08/28", ud_count=2, pp_count=3, ev_count=1)
        ],
    )

    message = report.to_telegram()

    assert "Underdog activity: <b>12</b>  ·  today: <b>2</b>" in message
    assert "PrizePicks activity: <b>7</b>  ·  today: <b>3</b>" in message
    assert "UD <code>  5</code> · PP <code>  4</code> · EV <code>  1</code>" in message
    assert "UD <code>2</code> · PP <code>3</code> · EV <code>1</code>" in message


@pytest.mark.asyncio
async def test_funnel_renders_provider_pipeline_separately():
    update, context = _update_and_context()
    db = MagicMock()
    db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
    db.get_funnel_summary = AsyncMock(
        return_value={"tracked": 0, "by_sport": []}
    )
    db.get_telegram_reconciliation = AsyncMock(
        return_value={
            "by_provider": {
                "Underdog": {
                    "observed": 100,
                    "evaluated": 80,
                    "actionable_persisted": 6,
                    "audit_delivered_alerts": 2,
                },
                "PrizePicks": {
                    "observed": 40,
                    "evaluated": 30,
                    "actionable_persisted": 4,
                    "audit_delivered_alerts": 1,
                },
            }
        }
    )

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
    ):
        await commands.cmd_funnel(update, context)

    message = update.message.reply_text.await_args.args[0]
    assert "<b>Underdog</b>" in message
    assert "Observed: <code>100</code>  ·  Evaluated: <code>80</code>" in message
    assert "<b>PrizePicks</b>" in message
    assert "Observed: <code>40</code>  ·  Evaluated: <code>30</code>" in message


@pytest.mark.asyncio
async def test_alert_history_preserves_uppercase_provider_pick_labels():
    update, context = _update_and_context()
    rows = [
        SimpleNamespace(
            provider="Underdog",
            line_value=1.5,
            alert_sent_at=None,
            recommendation="OVER",
            decision_tier="S",
            player_name="Player One",
            stat_type="Hits",
            sport="MLB",
        ),
        SimpleNamespace(
            provider="PrizePicks",
            line_value=2.5,
            alert_sent_at=None,
            recommendation="UNDER",
            decision_tier="A",
            player_name="Player Two",
            stat_type="Runs",
            sport="MLB",
        ),
    ]
    db = MagicMock()
    db.get_alerted_opportunity_log = AsyncMock(return_value=rows)

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
    ):
        await commands.cmd_alerts(update, context)

    message = update.message.reply_text.await_args.args[0]
    assert "<b>UNDERDOG PICK</b>" in message
    assert "<b>PRIZEPICKS PICK</b>" in message