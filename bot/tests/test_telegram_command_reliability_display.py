"""Focused regressions for Telegram command reliability and provider reporting."""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import commands
import main
import market_engine
from alerts import broadcast_alert, format_help_message
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


def test_alert_readiness_distinguishes_missing_data_from_no_pick():
    with patch.object(commands.config, "UD_ALERT_SPORTS_RAW", "MLB,NBA,CS"):
        lines = commands._alert_readiness_lines(
            {
                "MLB": {"mlb_stats_api": 30},
                "NBA": {"sleeper_stats_weekly": 40},
                "CS": {},
            },
            {"MLB": 2},
        )

    rendered = "\n".join(lines)
    assert "MLB" in rendered
    assert "2 directional picks logged" in rendered
    assert "NBA" in rendered
    assert "shadow/settlement data only" in rendered
    assert "CS" in rendered
    assert "no real player-game history available" in rendered


def test_alert_readiness_marks_real_data_without_recent_pick():
    with patch.object(commands.config, "UD_ALERT_SPORTS_RAW", "DOTA"):
        lines = commands._alert_readiness_lines(
            {"DOTA": {"opendota_series": 32}},
            {},
        )

    assert "DOTA" in "\n".join(lines)
    assert "data ready; no qualifying pick logged in the last 24h" in "\n".join(lines)


def test_current_display_sport_filter_uses_configured_active_slate():
    with patch.object(commands.config, "ACTIVE_SPORTS_RAW", "WNBA,CS"):
        assert commands._is_active_display_sport("wnba")
        assert commands._is_active_display_sport("CS")
        assert not commands._is_active_display_sport("NBA")
        assert not commands._is_active_display_sport(None)


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
    registered = {command for command, _callback in main.TELEGRAM_COMMAND_HANDLERS}
    help_text = format_help_message()

    assert registered
    assert all(f"/{command}" in help_text for command in registered)
    assert "/restarts" not in help_text


def test_every_registered_command_resolves_to_an_async_handler():
    registrations = main.TELEGRAM_COMMAND_HANDLERS

    assert len(registrations) == 36  # 35 production commands + legacy /learning alias
    assert len({command for command, _callback in registrations}) == len(registrations)
    for command, handler in registrations:
        assert command
        assert handler is not None, f"/{command} has no callback"
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
    assert any("Performance Dashboard" in message for message in messages)
    assert any("UNAVAILABLE" in message for message in messages)


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
    assert "Some database counters are temporarily unavailable" not in message
    assert "Alerts today: —" in message
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
    assert "Some database counters are temporarily unavailable" not in message
    assert "Alerts today: —" in message
    assert "/status failed" not in message


@pytest.mark.asyncio
async def test_dashboard_activity_section_keeps_provider_counts_separate():
    update, context = _update_and_context()
    db = MagicMock()
    report = DashboardReport(
        provider_pipeline={
            "Underdog": {"observed": 70, "active": 50},
            "PrizePicks": {"observed": 30, "active": 20},
        }
    )

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("engine.dashboard.DashboardEngine.gather", AsyncMock(return_value=report)),
        patch("providers.get_health_monitor", return_value=_health_monitor()),
    ):
        await commands.cmd_dashboard(update, context)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    activity = messages[1]
    assert "Underdog" in activity
    assert "Observed: <code>70</code>" in activity
    assert "PrizePicks" in activity
    assert "Observed: <code>30</code>" in activity
    assert activity.count("Provider Pipeline") == 1


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

    assert "Underdog" in message
    assert "Observed: <code>12</code>" in message
    assert "PrizePicks" in message
    assert "Observed: <code>7</code>" in message
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
async def test_funnel_sport_breakdown_uses_aligned_columns_and_active_sort():
    update, context = _update_and_context()
    db = MagicMock()
    db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
    db.get_funnel_summary = AsyncMock(return_value={
        "active_tracked": 4_321,
        "historical_observations": 9_876,
        "evaluated": 0,
        "directional_total": 0,
        "delivered": 0,
        "pending": 0,
        "resolved": 0,
        "result_counts": {},
        "active_by_sport": {
            "NBA": 123,
            "MLB": 1_234,
            "Soccer": 1_234,
            "FIFA": 0,
            "NFL": 9_999,
        },
        "observations_by_sport": {
            "NBA": 456,
            "MLB": 5_678,
            "Soccer": 5_678,
            "FIFA": 7,
            "NFL": 9_999,
        },
        "directional_by_sport": {"NBA": 12, "MLB": 90, "Soccer": 0, "FIFA": 0},
        "delivered_by_sport": {"NBA": 3, "MLB": 45, "Soccer": 0, "FIFA": 0},
    })
    db.get_telegram_reconciliation = AsyncMock(return_value={"by_provider": {}})

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
    ):
        await commands.cmd_funnel(update, context)

    message = update.message.reply_text.await_args.args[0]
    table = message.split("Sport Funnel Breakdown", 1)[1]
    row_lines = [line for line in table.splitlines() if line.startswith("<code>")]

    assert re.search(
        r"Sport\s+Active\s+Observed\s+Directional\s+Delivered", table
    )
    assert len(row_lines) == 4
    assert [line.split()[0].removeprefix("<code>") for line in row_lines] == [
        "MLB", "Soccer", "NBA", "FIFA",
    ]
    assert "<code>MLB" in row_lines[0]
    assert "1,234" not in row_lines[0]
    assert "FIFA" in row_lines[-1]
    assert "NFL" not in table
    assert re.search(r"<code>MLB\s+1234\s+5678\s+90\s+45</code>", table)


@pytest.mark.asyncio
async def test_funnel_provider_populations_and_zero_directional_sport_stay_separate():
    update, context = _update_and_context()
    db = MagicMock()
    db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
    db.get_funnel_summary = AsyncMock(return_value={
        "active_tracked": 3,
        "historical_observations": 6,
        "evaluated": 4,
        "directional_total": 2,
        "actionable_total": 2,
        "delivered": 1,
        "pending": 1,
        "resolved": 0,
        "result_counts": {},
        "active_by_sport": {"Soccer": 3},
        "observations_by_sport": {"Soccer": 6},
        "directional_by_sport": {},
        "delivered_by_sport": {},
    })
    db.get_telegram_reconciliation = AsyncMock(return_value={
        "by_provider": {
            "Underdog": {
                "observed": 5,
                "tracked": 3,
                "evaluated": 4,
                "directional": 2,
                "actionable_persisted": 2,
                "audit_delivered_alerts": 1,
                "pending": 1,
                "resolved": 0,
                "hit": 0,
                "miss": 0,
                "push": 0,
            },
            "PrizePicks": {
                "observed": 9,
                "tracked": 7,
                "evaluated": 8,
                "directional": 6,
                "actionable_persisted": 6,
                "audit_delivered_alerts": 4,
                "pending": 2,
                "resolved": 3,
                "hit": 2,
                "miss": 1,
                "push": 0,
            },
        }
    })

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
    ):
        await commands.cmd_funnel(update, context)

    message = update.message.reply_text.await_args.args[0]
    assert "Underdog" in message and "PrizePicks" in message
    assert "Observed: <code>5</code>  ·  Evaluated: <code>4</code>" in message
    assert "Observed: <code>9</code>  ·  Evaluated: <code>8</code>" in message
    assert "Soccer" in message
    assert "Soccer" in message.split("Sport Funnel Breakdown", 1)[1]
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
        patch.object(market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA"),
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
        patch.object(market_engine.config, "PRIZEPICKS_LEAGUES_RAW", "NBA"),
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
async def test_backfill_reports_busy_database_instead_of_hanging():
    update, context = _update_and_context()
    db = MagicMock()

    async def busy_pending_read(*args, **kwargs):
        await asyncio.sleep(1)

    db.get_pending_opportunities = busy_pending_read
    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
    ):
        await asyncio.wait_for(commands.cmd_backfill(update, context), timeout=0.2)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("still working" in message for message in messages)
    assert all("Traceback" not in message for message in messages)


@pytest.mark.asyncio
async def test_tracking_reports_unavailable_when_summary_is_busy():
    update, context = _update_and_context()
    db = MagicMock()

    async def busy_summary():
        await asyncio.sleep(1)

    db.get_tracking_summary = busy_summary
    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
    ):
        await asyncio.wait_for(commands.cmd_tracking(update, context), timeout=0.2)

    message = update.message.reply_text.await_args.args[0]
    assert "temporarily unavailable" in message
    assert "database is busy" in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_analyst_reports_unavailable_when_recent_picks_read_is_busy():
    update, context = _update_and_context()
    db = MagicMock()

    async def busy_recent_snapshots(*args, **kwargs):
        await asyncio.sleep(1)

    db.get_recent_underdog_snapshots = busy_recent_snapshots
    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
    ):
        await asyncio.wait_for(commands.cmd_analyst(update, context), timeout=0.2)

    messages = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("Analyst evidence is temporarily unavailable" in message for message in messages)
    assert all("Traceback" not in message for message in messages)


@pytest.mark.asyncio
async def test_picks_reports_unavailable_when_live_report_exceeds_deadline():
    update, context = _update_and_context()
    db = MagicMock()

    async def slow_picks(*args, **kwargs):
        await asyncio.sleep(1)

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
        patch("commands._cmd_picks_inner", AsyncMock(side_effect=slow_picks)),
    ):
        await asyncio.wait_for(commands.cmd_picks(update, context), timeout=0.2)

    message = update.message.reply_text.await_args.args[0]
    assert "Picks are temporarily unavailable" in message
    assert "scan continues independently" in message
    assert "failed" not in message.lower()


@pytest.mark.asyncio
async def test_grade_reports_unavailable_when_live_report_exceeds_deadline():
    update, context = _update_and_context()
    db = MagicMock()

    async def slow_grade(*args, **kwargs):
        await asyncio.sleep(1)

    with (
        patch("commands._check_allowed", return_value=True),
        patch("commands._db", db),
        patch("commands._COMMAND_TIMEOUT_SECONDS", 0.01),
        patch("commands._cmd_grade_inner", AsyncMock(side_effect=slow_grade)),
    ):
        await asyncio.wait_for(commands.cmd_grade(update, context), timeout=0.2)

    message = update.message.reply_text.await_args.args[0]
    assert "Grade data is temporarily unavailable" in message
    assert "monitoring continues" in message
    assert "failed" not in message.lower()


@pytest.mark.asyncio
async def test_slip_section_has_confidence_helper_in_its_own_scope():
    """The live /slip path must not depend on /picks' local imports."""
    from commands import _render_slip_section

    leg = SimpleNamespace(
        confidence=80,
        pp_line_value=25.5,
        prev_line=None,
        game_time=None,
        player_name="Test Player",
        sport="NBA",
        stat_type="Points",
        best_side="OVER",
        comp=None,
    )
    slip = SimpleNamespace(legs=[leg], correlation_warnings=[], excluded=[])

    lines = _render_slip_section(2, slip)
    assert any("Test Player" in line for line in lines)


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
    assert "Tracked observations: <b>12</b>" in message
    assert "99 Underdog snapshots" in message


@pytest.mark.asyncio
async def test_direct_broadcast_sanitizes_retired_labels_without_losing_pick_context():
    """Legacy/direct callers cannot leak internal classifications to Telegram."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    raw = (
        "🎯 <b>OVER</b> · Player Points 12.5\n"
        "S-Tier · Tier 1 · WATCHLIST · Near-Miss · PASS · decision_pass\n"
        "Evidence: 8 games · Quality: 88 · Line moved +0.5 · 2026-08-28 14:00 UTC"
    )

    result = await broadcast_alert(bot, [123], raw, sport="NFL")

    assert result == {"sent": 0, "failed": 0}
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_alert_does_not_use_strong_bet_label_as_a_send_gate():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    weak = "📊 <b>SHARP MONEY PLAY</b>\n📌 <b>PICK:</b> 🟢 <b>OVER 0.5</b>"
    assert await broadcast_alert(bot, [123], weak, sport="MLB") == {
        "sent": 1,
        "failed": 0,
    }
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_boundary_removes_all_quality_metadata_but_keeps_pick_context():
    """Legacy/direct messages cannot expose internal quality classifications."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    raw = (
        "📊 <b>SHARP MONEY PLAY</b>\n"
        "👤 <b>Yordan Alvarez</b> · Hits + Runs + RBIs\n"
        "📌 <b>PICK:</b> 🟢 <b>OVER 1.5</b>\n"
        "💪 STRONG BET\n"
        "Evidence: 8 completed games · Provider: Underdog\n"
        "Quality: 66/100 ★★★☆☆\n"
        "Bet Quality: 66/100\n"
        "Market Quality: HIGH · 80/100\n"
        "Proxy Match Confidence: 90/100 [█████████░]\n"
        "AI Confidence: 66/100\n"
        "Rating: ★★★☆☆\n"
        "Score: 66 · S-Tier · Tier 1 · A/B/C Tier"
    )

    result = await broadcast_alert(bot, [123], raw, sport="MLB")

    assert result == {"sent": 1, "failed": 0}
    delivered = bot.send_message.await_args.kwargs["text"]
    assert "Yordan Alvarez" in delivered
    assert "OVER 1.5" in delivered
    assert "Evidence: 8 completed games" in delivered
    assert "Provider: Underdog" in delivered
    assert "💪 STRONG BET" in delivered
    for leaked in (
        "Quality",
        "Confidence",
        "Rating",
        "Score",
        "S-Tier",
        "Tier 1",
        "A/B/C",
        "★",
        "⭐",
    ):
        assert leaked not in delivered


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

    activity = update.message.reply_text.await_args_list[1].args[0]
    assert "Provider Pipeline" in activity
    assert "Underdog" in activity
    assert "PrizePicks" in activity


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