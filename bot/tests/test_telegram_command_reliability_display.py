"""Focused regressions for Telegram command reliability and provider reporting."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import commands
import market_engine
from alerts import format_help_message
from connectors import ConnectorRegistry
from connectors.underdog import UnderdogConnector
from engine.dashboard import DashboardReport, DailyTrend, SportPerf
from providers.base import FailureType, ProviderHealth, ProviderStatus
from providers.health_monitor import ProviderHealthMonitor


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


def _health(
    name: str,
    status: ProviderStatus = ProviderStatus.OK,
    failures: int = 0,
) -> ProviderHealth:
    return ProviderHealth(
        name=name,
        status=status,
        last_success=None,
        last_failure=None,
        consecutive_failures=failures,
        failure_type=None,
        quota_remaining=None,
        quota_used=None,
    )


def _health_monitor(
    underdog: ProviderHealth | None = None,
    prizepicks: ProviderHealth | None = None,
) -> MagicMock:
    monitor = MagicMock()
    health_by_provider = {
        "Underdog": underdog or _health("Underdog"),
        "PrizePicks": prizepicks or _health("PrizePicks"),
    }
    monitor.get_health.side_effect = health_by_provider.__getitem__
    monitor.get_all_health.return_value = health_by_provider
    return monitor


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
    lines = commands._primary_player_prop_provider_lines(_health_monitor())

    assert lines == [
        "  ⚪ <b>Underdog</b> — Primary Player Props",
        "  ⚪ <b>PrizePicks</b> — PropLine — Primary Player Props",
    ]


def test_provider_roles_show_degraded_underdog_and_disabled_prizepicks():
    monitor = _health_monitor(
        underdog=_health("Underdog", ProviderStatus.DEGRADED, failures=1),
        prizepicks=_health("PrizePicks", ProviderStatus.DISABLED),
    )

    lines = commands._primary_player_prop_provider_lines(monitor)
    runtime = commands._primary_provider_runtime_lines(monitor)

    assert lines[0] == "  🟡 <b>Underdog</b> — Primary Player Props"
    assert lines[1] == "  ⚪ <b>PrizePicks</b> — PropLine — Unavailable"
    assert "    🟡 Underdog: degraded" in runtime[1]
    assert "1 failure" in runtime[1]


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
@pytest.mark.parametrize("handler_name", ["cmd_performance", "cmd_backtest"])
async def test_slow_history_commands_report_database_not_ready(handler_name):
    update, context = _update_and_context()

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", None),
    ):
        await getattr(commands, handler_name)(update, context)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert messages == ["⚠️ Database not ready."]
    assert "NoneType" not in messages[0]


@pytest.mark.asyncio
async def test_status_shows_provider_roles_and_separate_history_counts():
    update, context = _update_and_context()
    db = _mock_status_db()

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._season_checker", None),
        patch("providers.get_health_monitor", return_value=_health_monitor()),
    ):
        await commands.cmd_status(update, context)

    message = update.message.reply_text.await_args.args[0]
    assert "⚪ <b>Underdog</b> — Primary Player Props" in message
    assert "⚪ <b>PrizePicks</b> — PropLine — Primary Player Props" in message
    assert "DraftKings/FanDuel" not in message
    assert "Underdog: 20" in message
    assert "PrizePicks (PropLine): 10" in message


@pytest.mark.asyncio
async def test_status_returns_health_when_one_db_counter_times_out():
    update, context = _update_and_context()
    db = _mock_status_db()

    async def never_finishes():
        await asyncio.sleep(1)

    db.count_today_actionable_alerts = never_finishes

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._season_checker", None),
        patch("commands._STATUS_DB_READ_TIMEOUT_SECONDS", 0.01),
        patch("providers.get_health_monitor", return_value=_health_monitor()),
    ):
        await asyncio.wait_for(commands.cmd_status(update, context), timeout=0.2)

    message = update.message.reply_text.await_args.args[0]
    assert "Sharp Money Bot" in message
    assert "Some database counters are temporarily unavailable" in message
    assert "Alerts today: unavailable" in message
    assert "Underdog: 20" in message
    assert "PrizePicks (PropLine): 10" in message


@pytest.mark.asyncio
async def test_status_survives_counter_cancellation_during_outer_deadline():
    """Reproduce inner timeout/cancellation escaping the outer /status timeout."""
    update, context = _update_and_context()
    db = _mock_status_db()

    async def cancellation_resistant_counter():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            # Simulate aiosqlite taking time to unwind a cancelled DB operation.
            await asyncio.sleep(0.05)
            return 0

    db.count_today_actionable_alerts = cancellation_resistant_counter

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._season_checker", None),
        patch("commands._STATUS_DB_READ_TIMEOUT_SECONDS", 0.01),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.02),
        patch("providers.get_health_monitor", return_value=_health_monitor()),
    ):
        await asyncio.wait_for(commands.cmd_status(update, context), timeout=0.2)

    message = update.message.reply_text.await_args.args[0]
    assert "Sharp Money Bot" in message
    assert "Some database counters are temporarily unavailable" in message
    assert "Alerts today: unavailable" in message
    assert "/status failed" not in message


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
        patch("providers.get_health_monitor", return_value=_health_monitor()),
    ):
        await commands.cmd_dashboard(update, context)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    activity = messages[-1]
    assert "Underdog snapshots:  <code>100</code>" in activity
    assert "PrizePicks (PropLine) history: <code>30</code>" in activity
    assert "🐶 <b>Underdog</b>" in activity
    assert "🟣 <b>PrizePicks</b>" in activity


@pytest.mark.asyncio
async def test_providers_command_includes_live_primary_provider_health():
    update, context = _update_and_context()
    monitor = _health_monitor(
        underdog=_health("Underdog", ProviderStatus.DEGRADED, failures=1),
        prizepicks=_health("PrizePicks", ProviderStatus.OK),
    )

    with (
        patch("commands._check_allowed", return_value=True),
        patch("providers.get_health_monitor", return_value=monitor),
    ):
        await commands.cmd_providers(update, context)

    message = update.message.reply_text.await_args.args[0]
    assert "🟡 <b>Underdog</b> — Primary Player Props" in message
    assert "⚪ <b>PrizePicks</b> — PropLine — Primary Player Props" in message
    assert "🟡 Underdog: degraded" in message
    assert "⚪ PrizePicks: awaiting first successful fetch" in message


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


@pytest.mark.asyncio
async def test_prizepicks_companion_cycle_records_success_health():
    context = SimpleNamespace(bot_data={"db": MagicMock()}, bot=MagicMock())
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.fetch_props_for_sport_result = None
    provider.fetch_props_for_sport = AsyncMock(return_value=[])
    monitor = MagicMock()

    with (
        patch.object(market_engine, "_propline_provider", provider),
        patch.object(market_engine, "get_health_monitor", return_value=monitor),
    ):
        await market_engine._run_prizepicks_independent_cycle(context)

    monitor.record_success.assert_called_once_with("PrizePicks")


@pytest.mark.asyncio
async def test_prizepicks_companion_cycle_records_failure_health():
    context = SimpleNamespace(bot_data={"db": MagicMock()}, bot=MagicMock())
    provider = MagicMock()
    provider.is_available.return_value = True
    provider.fetch_props_for_sport_result = None
    provider.fetch_props_for_sport = AsyncMock(side_effect=TimeoutError("timed out"))
    monitor = MagicMock()

    with (
        patch.object(market_engine, "_propline_provider", provider),
        patch.object(market_engine, "get_health_monitor", return_value=monitor),
    ):
        await market_engine._run_prizepicks_independent_cycle(context)

    monitor.record_failure.assert_called_once_with(
        "PrizePicks", "timed out", FailureType.TIMEOUT
    )


@pytest.mark.asyncio
async def test_real_underdog_registry_failure_and_recovery_reach_command_output():
    connector = UnderdogConnector(enabled=True)
    connector._fetch_projections = AsyncMock(
        side_effect=[RuntimeError("API down"), []]
    )
    registry = ConnectorRegistry()
    registry.register(connector)
    monitor = ProviderHealthMonitor()
    monitor.register("Underdog")
    monitor.register("PrizePicks")
    context = SimpleNamespace(bot_data={"db": MagicMock()}, bot=MagicMock())

    async def run_job():
        with (
            patch.object(market_engine, "_registry", registry),
            patch.object(market_engine, "_cold_start_done", True),
            patch.object(market_engine, "_ud_full_scan_running", False),
            patch.object(market_engine, "_propline_provider", None),
            patch.object(market_engine, "get_health_monitor", return_value=monitor),
            patch("market_engine.get_health_tracker", return_value=MagicMock()),
        ):
            await market_engine.underdog_job(context)

    async def provider_message() -> str:
        update, command_context = _update_and_context()
        with (
            patch("commands._check_allowed", return_value=True),
            patch("providers.get_health_monitor", return_value=monitor),
        ):
            await commands.cmd_providers(update, command_context)
        return update.message.reply_text.await_args.args[0]

    await run_job()
    degraded = await provider_message()
    assert monitor.get_health("Underdog").status == ProviderStatus.DEGRADED
    assert "🟡 <b>Underdog</b> — Primary Player Props" in degraded
    assert "🟡 Underdog: degraded" in degraded

    await run_job()
    recovered = await provider_message()
    assert monitor.get_health("Underdog").status == ProviderStatus.OK
    assert "🟢 <b>Underdog</b> — Primary Player Props" in recovered
    assert "🟢 Underdog: ok" in recovered


@pytest.mark.asyncio
async def test_bounded_timeout_returns_before_cancellation_cleanup_finishes():
    """A slow aiosqlite cancellation must not hold the Telegram command."""
    cleanup_finished = asyncio.Event()

    async def cancellation_resistant_report():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.08)
            cleanup_finished.set()
            return "late result"

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await commands._bounded(
            cancellation_resistant_report(),
            timeout=0.01,
        )

    elapsed = time.monotonic() - started
    assert elapsed < 0.06
    await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_stats_keeps_fast_metrics_when_one_report_read_times_out():
    update, context = _update_and_context()
    db = MagicMock()
    db.count_today_actionable_alerts = AsyncMock(return_value=4)
    db.count_underdog_records = AsyncMock(return_value=99)
    db.get_all_resolved_pp_edges = AsyncMock(return_value=[])
    db.get_tracking_summary = AsyncMock(return_value={"total": 12})

    async def slow_prizepicks_count():
        await asyncio.sleep(1)

    db.count_today_pp_alerts = slow_prizepicks_count

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
    ):
        await asyncio.wait_for(commands.cmd_stats(update, context), timeout=0.2)

    message = update.message.reply_text.await_args.args[0]
    assert "Underdog:    <b>4</b>" in message
    assert "PrizePicks (PropLine): <b>unavailable</b>" in message
    assert "Tracked opportunities: <b>12</b>" in message
    assert "99 Underdog snapshots" in message


@pytest.mark.asyncio
async def test_dashboard_keeps_provider_activity_when_one_count_read_times_out():
    update, context = _update_and_context()
    db = MagicMock()
    db.count_underdog_records = AsyncMock(return_value=100)
    db.get_recent_tracked_props = AsyncMock(return_value=[])

    async def provider_history(provider=None):
        if provider == "Underdog":
            await asyncio.sleep(1)
            return 70
        return 30

    db.count_prop_line_history = AsyncMock(side_effect=provider_history)

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("engine.dashboard.DashboardEngine.gather", AsyncMock(return_value=DashboardReport())),
        patch("providers.get_health_monitor", return_value=_health_monitor()),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
    ):
        await asyncio.wait_for(commands.cmd_dashboard(update, context), timeout=0.2)

    activity = update.message.reply_text.await_args_list[-1].args[0]
    assert "Underdog snapshots/history: <i>unavailable</i>" in activity
    assert "PrizePicks (PropLine) history: <code>30</code>" in activity
    assert "🐶 <b>Underdog</b>" in activity
    assert "🟣 <b>PrizePicks</b>" in activity


@pytest.mark.asyncio
async def test_funnel_keeps_fast_pipeline_sections_when_scan_read_times_out():
    update, context = _update_and_context()
    db = MagicMock()
    db.get_funnel_summary = AsyncMock(
        return_value={"tracked": 3, "by_sport": []}
    )
    db.get_telegram_reconciliation = AsyncMock(
        return_value={
            "by_provider": {
                "Underdog": {"observed": 10, "evaluated": 8},
                "PrizePicks": {"observed": 5, "evaluated": 4},
            }
        }
    )

    async def slow_scan_summary(*args, **kwargs):
        await asyncio.sleep(1)

    db.get_scan_cycle_summary = slow_scan_summary

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
    ):
        await asyncio.wait_for(commands.cmd_funnel(update, context), timeout=0.2)

    message = update.message.reply_text.await_args_list[-1].args[0]
    assert "Provider Pipeline" in message
    assert "Observed: <code>10</code>  ·  Evaluated: <code>8</code>" in message
    assert "Observed: <code>5</code>  ·  Evaluated: <code>4</code>" in message
    assert "Candidate records tracked: <b>3</b>" in message
    assert "Could not load funnel data" not in message


@pytest.mark.asyncio
async def test_current_provider_reports_do_not_present_pandascore():
    update, context = _update_and_context()
    monitor = _health_monitor()

    with (
        patch("commands._check_allowed", return_value=True),
        patch("providers.get_health_monitor", return_value=monitor),
    ):
        await commands.cmd_providers(update, context)

    providers_message = update.message.reply_text.await_args.args[0]
    assert "Underdog" in providers_message
    assert "PrizePicks" in providers_message
    assert "PandaScore" not in providers_message

    update, context = _update_and_context()
    with (
        patch("commands._check_allowed", return_value=True),
        patch("providers.get_health_monitor", return_value=monitor),
    ):
        await commands.cmd_config(update, context)

    config_message = update.message.reply_text.await_args.args[0]
    assert "CS2 current PrizePicks lines: <b>PropLine</b>" in config_message
    assert "PandaScore" not in config_message