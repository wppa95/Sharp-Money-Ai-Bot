"""
Telegram command handlers.

Each handler is a standalone async function registered with the Application.
"""

from __future__ import annotations

import asyncio
import html as _html
import json as _json
import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

from httpx import ReadError
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError
from telegram.ext import ContextTypes

from alerts import (
    AlertDelivery,
    format_help_message,
    format_start_message,
    format_steam_alert,
    format_status_message,
    format_odds,
    format_probability,
    format_ev,
    EMOJI,
    is_telegram_suppressed_sport,
)
from config import config
from database import (
    Database,
    is_gradeable_player_result_source,
    is_official_underdog_settlement_source,
)
from engine import AnalysisEngine
from engine.strongest_pick import (
    is_strong_actionable_pick,
    select_strongest_actionable_picks,
)
from engine.ud_bet_decision import evidence_source_label
from models import MarketType, Sport

logger = logging.getLogger(__name__)

# ── Bot startup time for uptime tracking ──────────────────────────────────────
_START_TIME = time.monotonic()
# Dashboard reads aggregate several large SQLite populations. Individual
# counters are deadline-bounded inside DashboardEngine, but the command still
# needs enough room to render a complete partial report during a busy scan.
_DASHBOARD_TIMEOUT_SECONDS = 15.0
_PROVIDER_REPORT_TIMEOUT_SECONDS = 2.0
_COMMAND_TIMEOUT_SECONDS = 8.0
# Grade/tracking aggregate reads span large append-only populations. Keep the
# normal command deadline for lightweight commands, but give these two
# database-backed reports enough time to cross a busy history-write window.
# Tests can still shorten the base deadline by patching _COMMAND_TIMEOUT_SECONDS.
_AGGREGATE_REPORT_TIMEOUT_MULTIPLIER = 4.0
_PICKS_TIMEOUT_SECONDS = 12.0
_STATUS_DB_READ_TIMEOUT_SECONDS = 4.0
_CURRENT_PROP_MAX_FUTURE_HOURS = 24 * 14


def _evidence_label_from_reason_codes(value: object) -> str:
    """Read persisted decision provenance without inferring from raw windows."""
    codes = value
    if isinstance(value, str):
        try:
            codes = _json.loads(value)
        except (TypeError, ValueError):
            codes = [part.strip() for part in value.split(",")]
    if isinstance(codes, list):
        if "EVIDENCE:SETTLED_RESULTS" in codes:
            return evidence_source_label("settled_results")
        if "EVIDENCE:HISTORICAL_AVERAGE" in codes:
            return evidence_source_label("historical_average")
    return evidence_source_label(None)


async def _bounded(coro, timeout: Optional[float] = None):
    """Bound a report operation without waiting for slow cancellation cleanup.

    ``asyncio.wait_for(coro)`` waits for the coroutine's cancellation to finish.
    That is normally harmless, but an aiosqlite worker can take a while to
    unwind a locked operation.  A Telegram command must be able to return its
    unavailable/partial response without inheriting that delay.
    """
    timeout_value = _COMMAND_TIMEOUT_SECONDS if timeout is None else timeout
    task = asyncio.ensure_future(coro)

    def _consume_finished(report_task: asyncio.Future) -> None:
        if report_task.cancelled():
            return
        try:
            report_task.exception()
        except Exception:
            pass

    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_value)
    except asyncio.CancelledError:
        # An outer report deadline may cancel a nested _bounded() call. Ensure
        # its separately-created DB task does not survive the cancelled command.
        task.cancel()
        task.add_done_callback(_consume_finished)
        raise
    if task in done:
        return task.result()

    task.cancel()

    # Consume a late exception without awaiting slow database cleanup.
    task.add_done_callback(_consume_finished)
    raise asyncio.TimeoutError


async def _independent_report_reads(
    reads: list,
    timeout: Optional[float] = None,
) -> list:
    """Run optional report reads together and return unavailable per slow read."""
    timeout_value = _COMMAND_TIMEOUT_SECONDS if timeout is None else timeout

    async def _resolve(read):
        if hasattr(read, "__await__"):
            return await read
        return read

    tasks = [asyncio.create_task(_resolve(read)) for read in reads]

    def _consume_finished(report_task: asyncio.Future) -> None:
        if report_task.cancelled():
            return
        try:
            report_task.exception()
        except Exception:
            pass

    done, _pending = await asyncio.wait(tasks, timeout=timeout_value)
    values: list = []
    for task in tasks:
        if task not in done:
            task.cancel()
            # A canceled aiosqlite operation may finish after the command
            # response. Consume any late exception without blocking the
            # reporting command or leaking an unhandled task warning.
            task.add_done_callback(_consume_finished)
            values.append(None)
            continue
        try:
            values.append(task.result())
        except Exception as exc:  # noqa: BLE001
            logger.warning("report counter unavailable: %s", exc)
            values.append(None)
    return values


async def _report_read(coro, *, fallback=None, timeout: Optional[float] = None):
    """Read one optional report value without leaking DB contention to Telegram."""
    try:
        return await _bounded(coro, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("report read unavailable: %s", exc)
        return fallback


async def _status_db_read(label: str, coro):
    """Return one status counter without allowing a busy SQLite DB to fail /status."""
    try:
        return await asyncio.wait_for(coro, timeout=_STATUS_DB_READ_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        # If the outer report deadline fires while aiosqlite is still unwinding
        # its cancellation, this optional counter must not cancel /status too.
        # This is an optional report counter.  The command intentionally
        # continues with a neutral unavailable value when SQLite cancellation
        # outlives the report deadline, so this is expected degradation rather
        # than a command failure.
        logger.info("cmd_status: %s counter cancelled; marking unavailable", label)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_status: %s counter unavailable: %s", label, exc)
        return None


async def _status_db_reads(reads: list) -> list:
    """Collect status counters without letting pending cancellation abort /status."""
    tasks = [asyncio.create_task(read) for read in reads]
    done, pending = await asyncio.wait(
        tasks,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    for task in pending:
        task.cancel()

    values = []
    for task in tasks:
        if task not in done:
            values.append(None)
            continue
        try:
            values.append(task.result())
        except Exception as exc:  # noqa: BLE001
            logger.warning("cmd_status: counter task failed: %s", exc)
            values.append(None)
    return values


def _format_status_count(value: object, *, missing: str = "—") -> str:
    """Render a counter without turning a transient report miss into an alarm."""
    if value is None:
        return missing
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return missing


_SHADOW_PLAYER_RESULT_SOURCES = frozenset({
    "sleeper_stats_weekly",
    "graded_prop",
})

_PLAYER_RESULT_SOURCE_LABELS = {
    "mlb_stats_api": "MLB Stats API",
    "opendota_series": "OpenDota",
    "espn_gamelog": "ESPN gamelog",
    "sleeper_stats": "Sleeper per-game",
    "sleeper_stats_weekly": "Sleeper weekly shadow",
    "graded_prop": "settlement shadow",
}


def _alert_readiness_lines(
    coverage: object,
    directional_counts: object,
) -> list[str]:
    """Render configured-sport readiness without implying a pick exists.

    A sport is only marked data-ready when stored player-game evidence exists.
    Shadow/settlement rows do not satisfy that requirement because they cannot
    support a safe current directional recommendation.
    """
    if not isinstance(coverage, dict) or not isinstance(directional_counts, dict):
        return []
    sports = sorted(
        {
            str(sport).strip().upper()
            for sport in getattr(config, "ud_alert_sports", frozenset())
            if str(sport).strip()
        }
    )
    if not sports:
        return []

    lines = ["", "🧭 <b>Alert Readiness</b>  <i>(configured active sports)</i>"]
    for sport in sports:
        source_counts = coverage.get(sport, {})
        if not isinstance(source_counts, dict):
            source_counts = {}
        total_rows = sum(
            int(count or 0)
            for count in source_counts.values()
            if isinstance(count, (int, float))
        )
        real_rows = sum(
            int(count or 0)
            for source, count in source_counts.items()
            if str(source).strip().lower() not in _SHADOW_PLAYER_RESULT_SOURCES
            and isinstance(count, (int, float))
        )
        def _source_detail(*, real_only: bool) -> str:
            selected = []
            for source, count in sorted(source_counts.items()):
                source_key = str(source).strip().lower()
                if real_only and source_key in _SHADOW_PLAYER_RESULT_SOURCES:
                    continue
                if not isinstance(count, (int, float)) or not int(count or 0):
                    continue
                label = _PLAYER_RESULT_SOURCE_LABELS.get(source_key, source_key)
                selected.append(f"{label} {int(count):,}")
            return ", ".join(selected)

        real_sources = _source_detail(real_only=True)
        all_sources = _source_detail(real_only=False)
        if real_rows <= 0:
            if total_rows:
                detail = (
                    f"shadow/settlement data only; player-game rows={total_rows:,} "
                    f"({all_sources})"
                )
            else:
                detail = "no real player-game history available; player-game rows=0"
            lines.append(f"  ⚠️ <b>{_html.escape(sport)}</b>  ·  {detail}")
            continue

        directional = directional_counts.get(sport, 0)
        try:
            directional = int(directional or 0)
        except (TypeError, ValueError):
            directional = 0
        if directional:
            detail = f"{directional:,} directional pick{'s' if directional != 1 else ''} logged"
            icon = "✅"
        else:
            detail = (
                "data ready; no qualifying pick logged in the last 24h"
                f" · real player-game rows={real_rows:,} ({real_sources})"
            )
            icon = "ℹ️"
        lines.append(f"  {icon} <b>{_html.escape(sport)}</b>  ·  {detail}")
    return lines


def _primary_player_prop_provider_lines(
    health_monitor: object | None = None,
) -> list[str]:
    """Render configured provider roles with the current health-state icon."""
    underdog_enabled = bool(config.UNDERDOG_ENABLED)
    provider_icons = {"Underdog": "🟢" if underdog_enabled else "⚪"}
    if health_monitor is not None and hasattr(health_monitor, "get_health"):
        for provider in ("Underdog",):
            try:
                health = health_monitor.get_health(provider)
                # Registration alone is not a successful fetch.  Do not show a
                # green runtime state until this process has observed one.
                provider_icons[provider] = (
                    "⚪"
                    if (
                        not getattr(health, "last_success", None)
                        and getattr(getattr(health, "status", None), "value", "")
                        == "ok"
                    )
                    else health.status_emoji
                )
                if health.status.value == "disabled":
                    if provider == "Underdog":
                        underdog_enabled = False
            except Exception:
                pass

    _health_icons = set(provider_icons.values())
    overall_icon = (
        "🚨" if "🚨" in _health_icons
        else "🟡" if "🟡" in _health_icons
        else "🟢" if "🟢" in _health_icons
        else "⚪"
    )
    source_states = f"Underdog: {'available' if underdog_enabled else 'unavailable'}"
    return [
        f"  {overall_icon} <b>Player Props</b> — {source_states}",
    ]


def _primary_provider_runtime_lines(health_monitor: object | None) -> list[str]:
    """Render one compact live-fetch line for the unified product."""
    if health_monitor is None or not hasattr(health_monitor, "get_health"):
        return ["  Fetch: unavailable"]
    provider_runtime: list[str] = []
    for provider in ("Underdog",):
        try:
            health = health_monitor.get_health(provider)
            awaiting_first_fetch = not getattr(health, "last_success", None)
            last_ok = (
                health.format_last_success()
                if not awaiting_first_fetch
                else "awaiting first successful fetch"
            )
            state = (
                "awaiting first successful fetch"
                if awaiting_first_fetch and getattr(health.status, "value", "") == "ok"
                else health.status.value
            )
            icon = "⚪" if state == "awaiting first successful fetch" else health.status_emoji
            failure_note = (
                f" · {health.consecutive_failures} "
                f"failure{'s' if health.consecutive_failures != 1 else ''}"
                if health.consecutive_failures
                else ""
            )
            provider_runtime.append(
                f"{icon} {provider}: "
                f"{state} · last ✓: {last_ok}{failure_note}"
            )
        except Exception:
            provider_runtime.append(f"⚪ {provider}: unavailable")
    return ["  Fetch: " + " · ".join(provider_runtime)]


def _prizepicks_feed_count_lines(health_tracker: object | None) -> list[str]:
    """PropLine is internal and never appears in Telegram command output."""
    return []


# ── Module-level references (set by main.py on startup) ───────────────────────
_db: Optional[Database] = None
_engine: Optional[AnalysisEngine] = None
_alert_chat_ids: list[int] = []
_total_alerts_sent: int = 0


def init_handlers(
    db: Database,
    analysis_engine: AnalysisEngine,
    alert_chat_ids: list[int],
) -> None:
    """Call this once from main.py after the database and engine are ready."""
    global _db, _engine, _alert_chat_ids
    _db             = db
    _engine         = analysis_engine
    _alert_chat_ids = alert_chat_ids


def _uptime_str() -> str:
    seconds = int(time.monotonic() - _START_TIME)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _fmt_user_ts(dt: Optional[datetime]) -> str:
    """Format a stored UTC datetime in the canonical reporting timezone.

    Output format: ``Aug 08 · 5:05 PM``  (12-hour clock, no leading zero on hour)

    Stored timestamps and internal logic remain in UTC.  Returns "—" when
    dt is None.
    """
    if dt is None:
        return "—"
    from reporting import to_reporting_timezone
    dt = to_reporting_timezone(dt)
    # lstrip("0") removes leading zero; "or '12'" handles midnight (12 AM)
    hour_str = dt.strftime("%I").lstrip("0") or "12"
    return dt.strftime(f"%b %d · {hour_str}:%M %p")


def _check_allowed(update: Update) -> bool:
    """Return True if the user is in the allowed list (or if the list is empty)."""
    if getattr(update, "source", None) == "discord":
        allowed_discord = config.discord_allowed_user_ids
        if not allowed_discord:
            return True
        user_id = update.effective_user.id if update.effective_user else None
        return user_id in allowed_discord
    allowed = config.allowed_user_ids
    if not allowed:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in allowed


async def _send_in_chunks(
    update: Update,
    text: str,
    parse_mode: str = ParseMode.HTML,
    sep: str = "\n\n",
    max_len: int = 3800,
) -> None:
    """Split *text* on *sep* boundaries and send in ≤max_len character bursts.

    Telegram rejects messages longer than 4096 chars.  By splitting on the
    double-newline that separates each prop block we avoid cutting inside a
    block's HTML tags (which would produce malformed HTML).
    """
    parts = text.split(sep)
    chunk: list[str] = []
    chunk_len = 0
    for part in parts:
        part_len = len(part) + len(sep)
        if chunk and chunk_len + part_len > max_len:
            await update.message.reply_text(sep.join(chunk), parse_mode=parse_mode)
            chunk = []
            chunk_len = 0
        chunk.append(part)
        chunk_len += part_len
    if chunk:
        await update.message.reply_text(sep.join(chunk), parse_mode=parse_mode)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — welcome message."""
    chat_id = update.effective_chat.id
    user_id = getattr(update.effective_user, 'id', None)
    logger.info("cmd_start: chat_id=%s user_id=%s", chat_id, user_id)
    try:
        if not _check_allowed(update):
            await update.message.reply_text(
                f"⛔ Unauthorized.\n\n"
                f"<i>Your chat ID is <code>{chat_id}</code>. "
                f"Add it to ALLOWED_USER_IDS to enable alerts.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        await update.message.reply_text(
            format_start_message(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        # Remind authorised users of their chat ID for easy reference.
        await update.message.reply_text(
            f"ℹ️ Your chat ID: <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("cmd_start: error: %s", exc)
        try:
            await update.message.reply_text("⚠️ /start failed. Check bot logs.")
        except Exception:
            pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show available commands."""
    uid = getattr(update.effective_user, "id", "?")
    logger.info("cmd_help: user_id=%s", uid)
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        db = getattr(context, "bot_data", {}).get("db")
        catalog = None
        if db is not None and hasattr(db, "get_telegram_commands"):
            catalog = await _report_read(
                db.get_telegram_commands(enabled_only=True),
                fallback=None,
                timeout=3.0,
            )
        await update.message.reply_text(
            format_help_message(catalog),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("cmd_help: reply_text failed: %s", exc)
        await update.message.reply_text("⚠️ Help message failed to send. Check bot logs.")


def _fmt_provider_status_line(provider_name: str, display_name: str) -> str:
    """Single-line provider status for the dashboard health section."""
    try:
        from providers import get_health_monitor
        mon = get_health_monitor()
        if mon:
            h = mon.get_health(provider_name)
            detail = ""
            if h.quota_remaining is not None:
                detail = f" ({h.quota_remaining:,} req remaining)"
            elif h.error_msg:
                detail = f" — {h.error_msg[:50]}"
            return f"  {display_name}:  {h.status_emoji} {h.status.value}{detail}"
    except ImportError:
        pass
    return f"  {display_name}:  ⚪ not tracked"


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/health — show background job health: last run, last error per job."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    logger.info("cmd_health: command received")
    try:
        from engine.health import get_health_tracker
        ht = get_health_tracker()
        if ht is None:
            await update.message.reply_text("⚠️ Health tracker not initialised.")
            return

        logger.info("cmd_health: health tracker loaded")

        lines: list[str] = [
            "❤️ <b>Bot Health</b>",
            "",
            f"Uptime:           {_uptime_str()}",
            f"Heartbeat:        {ht.heartbeat_age_str()}",
            f"Last startup:     {_html.escape(ht.last_startup() or 'unknown')}",
            "",
            "<b>📋 Background Jobs</b>",
        ]

        _JOB_LABELS = {
            "underdog_monitor":      "Underdog monitor",
            "opportunity_grader":    "Opportunity grader",
            "player_history_collector": "Player history",
            "prop_history_pruner":   "History pruner",
        }

        logger.info("cmd_health: reading job info")
        jobs = ht.get_all_jobs()
        for jid, label in _JOB_LABELS.items():
            info = jobs.get(jid, {})
            last_run    = ht.job_last_run_str(jid)
            fail_streak = info.get("fail_streak", 0)
            icon = "✅" if fail_streak == 0 else "⚠️" if fail_streak < 3 else "🚨"
            line = f"  {icon} <b>{_html.escape(label)}</b>  ·  last run: {_html.escape(last_run)}"
            if fail_streak:
                line += f"  ·  fails: {fail_streak}"
            lines.append(line)
            last_err = info.get("last_error")
            # Only show the error text when the job is currently in a fail streak.
            # Suppressing stale errors after recovery prevents old messages from
            # appearing indefinitely alongside a green (✅) status icon.
            if last_err and fail_streak > 0:
                # HTML-escape the error text — Python exception strings may
                # contain angle-brackets (e.g. "<class 'X'>") which break HTML.
                lines.append(f"      ↳ {_html.escape(str(last_err)[:100])}")

        # ── Market provider health (live prop sources) ────────────────────────
        lines.append("")
        lines.append("<b>📡 Market Providers</b>")
        _market_provider_labels = {"Underdog": "Underdog source"}
        _market_rows: list[str] = []
        for provider in ("Underdog",):
            info = ht.get_provider_info(provider)
            if info:
                last_fetch = ht.provider_last_fetch_str(provider)
                streak = info.get("error_streak", 0)
                icon = "✅" if streak == 0 else "⚠️" if streak < 3 else "🚨"
                _market_rows.append(
                    f"  {icon} <b>{_html.escape(provider)}</b> — "
                    f"{_market_provider_labels[provider]}  ·  "
                    f"last fetch: {_html.escape(last_fetch)}"
                    + (f"  ·  err streak: {streak}" if streak else "")
                )
                last_err_msg = info.get("last_error_msg")
                if last_err_msg and streak:
                    _market_rows.append(f"      ↳ {_html.escape(str(last_err_msg)[:100])}")
            else:
                _market_rows.append(
                    f"  ⚪ <b>{_html.escape(provider)}</b> — "
                    f"{_market_provider_labels[provider]}  ·  not yet fetched"
                )
        lines.append("  <b>Player Props</b> — unified feed health")
        lines.extend(_market_rows)

        # ── Stat provider health (historical enrichment) ──────────────────────
        lines.append("")
        lines.append("<b>📊 Stat Providers</b>")
        try:
            from config import config as _cfg_h
            _sleeper_enabled = getattr(_cfg_h, "UD_SLEEPER_ENABLED", True)
        except Exception:
            _sleeper_enabled = True
        _sleeper_info = ht.get_provider_info("Sleeper")
        if _sleeper_info:
            _slp_fetch = ht.provider_last_fetch_str("Sleeper")
            _slp_streak = _sleeper_info.get("error_streak", 0)
            _slp_icon = "✅" if _slp_streak == 0 else "⚠️"
            lines.append(
                f"  {_slp_icon} <b>Sleeper</b>  ·  NFL enrichment  ·  last sync: {_html.escape(_slp_fetch)}"
                + (f"  ·  err streak: {_slp_streak}" if _slp_streak else "")
            )
        elif _sleeper_enabled:
            lines.append("  ⚪ <b>Sleeper</b>  ·  NFL enrichment  ·  pending first sync")
        else:
            lines.append("  ⏸️ <b>Sleeper</b>  ·  <i>disabled</i>")
        # Other stat providers (ESPN, NHL, PandaScore) are fetched on-demand
        lines.append("  ⚪ <b>ESPN</b>  ·  on-demand gamelog (NBA/NFL/WNBA/MLB)")
        lines.append("  ⚪ <b>NHL API</b>  ·  on-demand skater/goalie logs")

        last_err_global = ht.last_error()
        # Only show the global last error if it's recent (within 2 hours).
        # Old errors from prior days are stale and misleading once the bot recovers.
        _show_global_err = False
        if last_err_global:
            _err_ts = ht.last_error_ts()
            if _err_ts:
                try:
                    from datetime import timezone as _tz
                    # _now_iso() stores "YYYY-MM-DD HH:MM:SS UTC" — fromisoformat can't
                    # handle the " UTC" suffix directly; strip it and treat as UTC.
                    _ts_clean = str(_err_ts).replace(" UTC", "").replace("Z", "").strip()
                    _err_dt = datetime.fromisoformat(_ts_clean).replace(tzinfo=_tz.utc)
                    _err_age_h = (datetime.now(_tz.utc) - _err_dt).total_seconds() / 3600
                    _show_global_err = _err_age_h < 2.0
                except Exception:
                    _show_global_err = True  # can't parse — show it to be safe
            else:
                _show_global_err = True
        if _show_global_err:
            lines.append("")
            lines.append(f"⚠️ <b>Last error:</b> {_html.escape(str(last_err_global)[:120])}")
            ts = ht.last_error_ts()
            if ts:
                lines.append(f"   at {_html.escape(str(ts))}")

        # ── Recovery events ───────────────────────────────────────────────────
        # Stale-recovery threshold: recoveries older than this are historical
        # (the bot is clearly running fine now) and must not be presented as
        # the current active recovery.  Matches the same philosophy as the
        # global-error and pipeline-failure staleness gates above.
        _RECOVERY_STALE_HOURS = 6.0

        recovery = ht.last_recovery_event()
        lines.append("")
        if recovery:
            _rec_age_h = ht.last_recovery_age_hours()
            _rec_age_str = ht.last_recovery_age_str()
            _rec_job = _html.escape(recovery.get("job", "?"))
            if _rec_age_h is not None and _rec_age_h >= _RECOVERY_STALE_HOURS:
                # Historical recovery — label it clearly so it isn't read as
                # an active or ongoing issue.
                lines.append(
                    f"ℹ️ <b>Last recovery:</b>  {_html.escape(_rec_age_str)}"
                    f"  ·  job: {_rec_job}"
                    f"  <i>(historical — no recent failures)</i>"
                )
            else:
                # Recent recovery — show prominently.
                lines.append(
                    f"✅ <b>Last recovery:</b>  {_html.escape(_rec_age_str)}"
                    f"  ·  job: {_rec_job}"
                )
                reason_txt = recovery.get("reason", "")
                if reason_txt:
                    lines.append(f"   ↳ <i>{_html.escape(str(reason_txt)[:100])}</i>")
        else:
            lines.append("✅ <b>Last recovery:</b>  <i>No recovery events recorded</i>")

        # ── Phase 2: extended runtime telemetry ───────────────────────────────
        lines.append("")
        lines.append("<b>📡 Player Props Pipeline</b>")
        ud_scan = ht.last_underdog_scan()
        ud_scan_age = ht.last_underdog_scan_age_str()
        ud_props   = ht.last_underdog_props()
        ud_alerts  = ht.last_underdog_alerts()
        if ud_scan:
            lines.append(
                f"  Last scan:    {_html.escape(ud_scan_age)}"
                + (f"  props={ud_props}" if ud_props is not None else "")
                + (f"  alerts={ud_alerts}" if ud_alerts is not None else "")
            )
        else:
            lines.append("  Last scan:    not yet run this session")

        db_write = ht.last_db_write()
        db_age   = ht.last_db_write_age_str()
        lines.append(f"  Last DB write: {_html.escape(db_age) if db_write else 'not recorded'}")

        pf = ht.last_pipeline_fail_info()
        if pf:
            # Only surface pipeline failures that are recent (< 2 h).
            # Older resolved errors clutter health for a healthy bot.
            _pf_recent = False
            _pf_ts_raw = pf.get("ts", "")
            if _pf_ts_raw:
                try:
                    from datetime import timezone as _pf_tz
                    # Same UTC-suffix handling as global last_error: strip " UTC" before parse.
                    _pf_ts_clean = str(_pf_ts_raw).replace(" UTC", "").replace("Z", "").strip()
                    _pf_dt = datetime.fromisoformat(_pf_ts_clean).replace(tzinfo=_pf_tz.utc)
                    _pf_age_h = (datetime.now(_pf_tz.utc) - _pf_dt).total_seconds() / 3600
                    _pf_recent = _pf_age_h < 2.0
                except Exception:
                    _pf_recent = True  # can't parse → show to be safe
            else:
                _pf_recent = True
            if _pf_recent:
                lines.append("")
                lines.append(
                    f"🚨 <b>Last pipeline failure:</b>"
                    f"  stage={_html.escape(pf.get('stage','?'))}"
                    f"  module={_html.escape(pf.get('module','?'))}"
                )
                lines.append(f"   at {_html.escape(_pf_ts_raw)}")
                lines.append(f"   ↳ {_html.escape(str(pf.get('error',''))[:100])}")

        logger.info("cmd_health: sending response (%d lines)", len(lines))
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        logger.info("cmd_health: response sent")
    except Exception as exc:
        logger.exception("cmd_health: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /health failed. Check bot logs.")


def _tracked_prop_grade_lines(summary: object) -> list[str]:
    """Format universal tracked-line counts without presenting them as picks."""
    heading = "🧾 <b>ALL TRACKED PROP LINES</b>"
    if not isinstance(summary, dict):
        return [
            heading,
            "  <i>Tracked-line grades are UNAVAILABLE; this is not zero.</i>",
        ]

    total = int(summary.get("total", summary.get("tracked", 0)) or 0)
    pending = int(summary.get("pending", 0) or 0)
    resolved = int(summary.get("resolved", 0) or 0)
    verified = int(summary.get("verified", 0) or 0)
    unverified = int(summary.get("unverified", 0) or 0)
    hit = int(summary.get("hit", 0) or 0)
    miss = int(summary.get("miss", 0) or 0)
    push = int(summary.get("push", 0) or 0)
    void = int(summary.get("void", 0) or 0)
    denominator = int(summary.get("denominator", hit + miss + push) or 0)
    above_line_rate = summary.get("above_line_rate")
    rate_text = (
        f"{float(above_line_rate):.1f}%"
        if denominator and above_line_rate is not None
        else "—"
    )
    lines = [
        heading,
        f"  Distinct tracked lines: <b>{total:,}</b>",
        f"  Pending: <b>{pending:,}</b> · Resolved: <b>{resolved:,}</b> · "
        f"Source-verified: <b>{verified:,}</b> · Unverified: <b>{unverified:,}</b>",
        f"  Above line: {hit:,} · Below line: {miss:,} · Push: {push:,} · "
        f"Void/other: {void:,}",
        f"  Above-line rate: <b>{rate_text}</b> "
        f"(HIT / (HIT + MISS + PUSH), n={denominator:,})",
        "  <i>For neutral rows, HIT/MISS means above/below the line, not a recommended pick.</i>",
    ]

    by_provider = summary.get("by_provider", {})
    if isinstance(by_provider, dict) and by_provider:
        lines.append("  <b>By provider</b>")
        for provider, row in sorted(
            by_provider.items(),
            key=lambda item: -int(item[1].get("total", 0) or 0),
        ):
            lines.append(
                f"    {_html.escape(str(provider))}: "
                f"{int(row.get('total', 0) or 0):,} lines · "
                f"{int(row.get('hit', 0) or 0):,} above / "
                f"{int(row.get('miss', 0) or 0):,} below / "
                f"{int(row.get('push', 0) or 0):,} push · "
                f"{int(row.get('pending', 0) or 0):,} pending"
            )

    by_sport = summary.get("by_sport", {})
    if isinstance(by_sport, dict) and by_sport:
        lines.append("  <b>By sport</b>")
        sorted_sports = sorted(
            by_sport.items(),
            key=lambda item: -int(item[1].get("total", 0) or 0),
        )
        for sport, row in sorted_sports[:8]:
            lines.append(
                f"    {_html.escape(str(sport))}: "
                f"{int(row.get('total', 0) or 0):,} lines · "
                f"{int(row.get('pending', 0) or 0):,} pending · "
                f"{int(row.get('verified', 0) or 0):,} verified · "
                f"{int(row.get('unverified', 0) or 0):,} unverified"
            )
        if len(sorted_sports) > 8:
            other_rows = [row for _sport, row in sorted_sports[8:]]
            other_total = sum(int(row.get("total", 0) or 0) for row in other_rows)
            lines.append(
                f"    Other {len(other_rows)} sports: {other_total:,} lines"
            )
    return lines


async def cmd_rollups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rollups — show learning performance rollups by sport and stat type."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        db: "Database" = context.bot_data.get("db")
        if db is None:
            await update.message.reply_text("⚠️ Database not available.")
            return

        rollups_raw, tg_perf_raw, tracked_grades_raw = await _independent_report_reads([
            db.get_learning_rollups(enabled_sports=config.active_sports),
            db.get_telegram_pick_performance(),
            db.get_tracked_prop_grade_summary(),
        ])
        rollups_available = isinstance(rollups_raw, dict)
        tg_perf_available = isinstance(tg_perf_raw, dict)
        rollups = rollups_raw if rollups_available else {}
        tg_perf = tg_perf_raw if tg_perf_available else {}
        if not rollups_available:
            await update.message.reply_text(
                "📊 <b>Learning Rollups</b>\n\n"
                + "\n".join(_tracked_prop_grade_lines(tracked_grades_raw))
                + "\n\n"
                "<i>Historical learning data is UNAVAILABLE; the database "
                "query failed or timed out. This is not zero.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        total   = rollups.get("total_graded", 0)
        intel   = rollups.get("intelligence", {})
        adaptive = rollups.get("adaptive_learning", {})

        # ── 🎯 Telegram actionable picks block (always shown first) ───────────
        tg_lines = ["📊 <b>Learning Rollups</b>", "", "🎯 <b>TELEGRAM ACTIONABLE PICKS</b>"]
        tg_total = tg_perf.get("total", 0)
        if not tg_perf_available:
            tg_lines.append("  <i>Telegram PLAY performance: UNAVAILABLE.</i>")
        elif tg_total == 0:
            tg_lines.append("  <i>No picks sent yet.</i>")
        else:
            tg_h    = tg_perf["hit"]
            tg_m    = tg_perf["miss"]
            tg_p    = tg_perf["push"]
            tg_v    = tg_perf.get("void", 0)
            tg_pend = tg_perf["pending"]
            tg_hr   = tg_perf["hit_rate"]
            tg_lines.append(f"  Total: <b>{tg_total}</b>")
            tg_lines.append(f"  ✅ Hits: {tg_h}   ❌ Misses: {tg_m}   🟡 Pushes: {tg_p}   🚫 Void/other: {tg_v}   ⏳ Pending: {tg_pend}")
            tg_unverified = int(tg_perf.get("unverified", 0) or 0)
            if tg_unverified:
                tg_lines.append(
                    f"  Unverified results: {tg_unverified} "
                    "(excluded from hit rate)"
                )
            tg_denominator = int(tg_perf.get("denominator", 0) or 0)
            if tg_denominator > 0:
                tg_lines.append(
                    f"  Hit Rate: <b>{tg_hr}%</b> "
                    f"(HIT / (HIT + MISS), n={tg_denominator})"
                )
            elif tg_perf["graded"] > 0:
                tg_lines.append("  Hit Rate: — (no HIT/MISS; pushes are excluded)")
            for source, source_perf in sorted(tg_perf.get("by_provider", {}).items()):
                if str(source).strip().lower() != "underdog":
                    continue
                tg_lines.append(
                    f"  <b>{_html.escape(source)}:</b> "
                    f"{source_perf['hit']}H-{source_perf['miss']}M-"
                    f"{source_perf['push']}P · {source_perf.get('void', 0)} void · "
                    f"{source_perf['pending']} pending · "
                    f"{source_perf.get('unverified', 0)} unverified"
                )
        tg_lines.append("")
        tg_lines.extend(_tracked_prop_grade_lines(tracked_grades_raw))
        tg_lines.append("")
        if not rollups_available:
            tg_lines += [
                "📚 <b>All Directional Grading</b>",
                "  <i>Persistent learning rollups: UNAVAILABLE.</i>",
                "  <i>The database query failed or timed out; this is not zero.</i>",
            ]
            await update.message.reply_text("\n".join(tg_lines), parse_mode=ParseMode.HTML)
            return
        if intel.get("sample_size", 0):
            _cal = "calibrated sample" if intel.get("calibrated") else "descriptive only (<5)"
            tg_lines.extend([
                "🧠 <b>Sharp Confidence</b>",
                f"  Avg: <b>{intel.get('sharp_confidence_avg') if intel.get('sharp_confidence_avg') is not None else '—'}/100</b>"
                f"  · Evidence: {intel.get('evidence_completeness_avg') if intel.get('evidence_completeness_avg') is not None else '—'}%",
                f"  Sample: {intel.get('sample_size', 0)}  · <i>{_cal}</i>",
                "",
            ])

        # Adaptive weights are ranking-only and auditable.  Keep their status
        # visible without implying that descriptive or insufficient groups
        # affect qualification or Telegram delivery.
        if adaptive:
            adaptive_version = adaptive.get("version") or "none"
            adaptive_state = (
                "ACTIVE" if adaptive.get("active") else adaptive.get("status", "NOT_INITIALIZED")
            )
            tg_lines.extend([
                "🧮 <b>Adaptive ranking weights</b>",
                f"  Version: <b>{_html.escape(str(adaptive_version))}</b> · "
                f"Status: <b>{_html.escape(str(adaptive_state))}</b>",
                f"  Eligible groups: {adaptive.get('eligible_groups', 0)}/"
                f"{adaptive.get('total_groups', 0)} · "
                f"Minimum sample: {adaptive.get('min_sample_size', '—')} · "
                f"Model misses: {adaptive.get('min_model_errors', '—')}",
                f"  Max multiplier delta: ±{float(adaptive.get('max_delta', 0.0)) * 100:.0f}% · "
                "<i>ranking only; delivery gates unchanged</i>",
                "",
            ])

        if total == 0 and int(rollups.get("unverified_graded", 0) or 0) == 0:
            tg_lines.append(
                "<i>No source-verified directional plays yet — results are "
                "graded after game_time passes.</i>"
            )
            await update.message.reply_text("\n".join(tg_lines), parse_mode=ParseMode.HTML)
            return

        def _result_row(k: str, v: dict) -> str:
            w, l, p = v.get("W", 0), v.get("L", 0), v.get("P", 0)
            denominator = int(v.get("denominator", w + l) or 0)
            pct = f"{v.get('win_pct', 0.0):.1f}%" if denominator else "—"
            return (
                f"  <b>{k}:</b>  {w}W-{l}L-{p}P  "
                f"({pct}, n={denominator})"
            )

        # Extend the Telegram-picks header block with the overall grading section.
        lines = tg_lines + [
            "<b>All Directional Grading</b>",
            "",
            f"Total graded directional evaluations: <b>{total}</b>",
            f"Unverified terminal outcomes excluded: "
            f"<b>{int(rollups.get('unverified_graded', 0) or 0)}</b>",
            "<i>Hit rates and learning use only source-verified completed results.</i>",
            f"Current delivered PLAYs graded: <b>{rollups.get('telegram_graded', 0)}</b>",
            f"Historical/legacy graded evaluations: <b>{rollups.get('historical_legacy_graded', 0)}</b>",
            "",
        ]

        # ── By sport ──────────────────────────────────────────────────────────
        by_sport = rollups.get("by_sport", {})
        if by_sport:
            lines.append("<b>By Sport</b>")
            for sport, v in sorted(by_sport.items(), key=lambda kv: -kv[1].get("total", 0))[:6]:
                lines.append(_result_row(sport, v))
            lines.append("")

        # ── Top stat types ────────────────────────────────────────────────────
        by_stat = rollups.get("by_stat_type", {})
        if by_stat:
            lines.append("<b>Top Prop Types</b>")
            for stat, v in list(by_stat.items())[:8]:
                lines.append(_result_row(stat[:28], v))
            lines.append("")

        # ── Error type breakdown ──────────────────────────────────────────────
        by_err = rollups.get("by_error_type", {})
        if by_err:
            lines.append("<b>Miss Classification</b>")
            for etype, n in sorted(by_err.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {_html.escape(str(etype))}: {n}")
            lines.append("")

        # ── Player trend (top performers) ─────────────────────────────────────
        player_trend = rollups.get("player_trend", [])
        if player_trend:
            lines.append("<b>Player Trend (top by volume)</b>")
            for pt in player_trend[:6]:
                w, l, pct = pt["W"], pt["L"], pt.get("win_pct", 0.0)
                lines.append(
                    f"  {_html.escape(pt['player'][:22])} ({pt['sport']})"
                    f"  {w}W-{l}L  ({pct:.0f}%)"
                    f"  <i>{_html.escape(pt['stat_type'][:18])}</i>"
                )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_rollups: failed: %s", exc)
        await update.message.reply_text("⚠️ /rollups failed. Check bot logs.")


async def cmd_learning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/learning — report active weights, or explicitly rebuild them."""
    args = [
        str(arg).strip().lower()
        for arg in (getattr(context, "args", None) or [])
    ]
    if args and args[0] == "rebuild":
        if not _check_allowed(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        try:
            db: "Database" = context.bot_data.get("db")
            if db is None:
                await update.message.reply_text("⚠️ Database not available.")
                return
            result = await db.rebuild_adaptive_learning_weights()
            state = "ACTIVE" if result["active"] else "DESCRIPTIVE ONLY"
            await update.message.reply_text(
                "🧮 <b>Adaptive weights rebuilt</b>\n\n"
                f"Version: <b>{_html.escape(str(result['version']))}</b>\n"
                f"Status: <b>{state}</b>\n"
                f"Eligible groups: {result['eligible_groups']}/"
                f"{result['total_groups']}\n"
                f"Minimum sample: {result['min_sample_size']}\n"
                f"Model misses required: {result['min_model_errors']}\n"
                f"Maximum multiplier delta: ±{result['max_delta'] * 100:.0f}%\n\n"
                "<i>Weights affect ranking only. Qualification, delivery, "
                "alerts, and grading are unchanged.</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("cmd_learning rebuild: failed: %s", exc)
            await update.message.reply_text("⚠️ Adaptive weight rebuild failed. Check bot logs.")
        return
    await cmd_rollups(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show bot and market status."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        # Bound all DB reads as one unit so a scan cannot hold this command.
        if _db:
            (
                _alerts_today,
                total_prop_history,
                ud_history,
            ) = await _status_db_reads(
                [
                    _status_db_read(
                        "alerts today", _db.count_today_actionable_alerts()
                    ),
                    _status_db_read(
                        "shared prop history", _db.count_prop_line_history()
                    ),
                    _status_db_read(
                        "Underdog prop history",
                        _db.count_prop_line_history(provider="Underdog"),
                    ),
                ]
            )
        else:
            _alerts_today = total_prop_history = ud_history = 0
        # Load provider health monitor singleton
        try:
            from providers import get_health_monitor
            _mon = get_health_monitor()
        except ImportError:
            _mon = None
        try:
            from engine.health import get_health_tracker as _get_ht_pipeline
            _ht_pipeline = _get_ht_pipeline()
        except Exception:
            _ht_pipeline = None

        # /status is intentionally a concise bettor-facing health view.  The
        # detailed provider pipeline, scheduler internals, and raw DB counts
        # belong in /health, /funnel, and /reconcile respectively.
        lines: list[str] = [
            f"🤖 <b>Sharp Money Bot</b>  ·  Uptime: {_uptime_str()}",
            f"📬 Alerts today: {_format_status_count(_alerts_today)}",
            "",
            "📡 <b>Market Providers</b>",
            *_primary_player_prop_provider_lines(_mon),
            (
                f"  History: Underdog: {_format_status_count(ud_history)}"
            ),
        ]
        if _ht_pipeline is not None:
            lines.append(
                f"  Heartbeat: {_html.escape(_ht_pipeline.heartbeat_age_str())}"
            )
            try:
                last_cycle = _ht_pipeline.last_monitoring_cycle_age_str()
                lines.append(
                    f"📊 Latest monitoring cycle: {_html.escape(str(last_cycle))}"
                )
            except Exception:
                pass
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_status: unexpected error: %s", exc)
        await update.message.reply_text(
            "ℹ️ Status is updating; live monitoring continues."
        )


async def cmd_system(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/system — concise runtime status for the bot process."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        from engine.health import get_health_tracker
        from providers import get_health_monitor

        ht = get_health_tracker()
        monitor = get_health_monitor()
        lines = [
            "⚙️ <b>System Status</b>",
            f"  Uptime: {_uptime_str()}",
            (
                f"  Heartbeat: {_html.escape(ht.heartbeat_age_str())}"
                if ht is not None
                else "  Heartbeat: unavailable"
            ),
            "",
            "📡 <b>Runtime providers</b>",
            *_primary_player_prop_provider_lines(monitor),
        ]
        if ht is not None:
            ud_info = ht.get_provider_info("Underdog") or {}
            for name, info in (("Underdog", ud_info),):
                if info:
                    age = ht.provider_last_fetch_str(name)
                    streak = int(info.get("error_streak", 0) or 0)
                    lines.append(
                        f"  {name} last fetch: {_html.escape(str(age))}"
                        + (f" · {streak} consecutive failures" if streak else "")
                    )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_system: unexpected error: %s", exc)
        await update.message.reply_text(
            "⚠️ System status is temporarily unavailable. Monitoring continues."
        )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /analyze — manually analyze a betting line.

    Usage: /analyze [sport] [selection] [your_odds] [opp_odds]
    Example: /analyze NFL Chiefs-3 -110 -110
    Example: /analyze NBA LeBron_Over_25.5_Points -115 -105
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    args = context.args or []
    if len(args) < 4:
        await update.message.reply_text(
            f"{EMOJI['warn']} <b>Usage:</b> <code>/analyze [sport] [selection] [your_odds] [opp_odds]</code>\n\n"
            f"<b>Examples:</b>\n"
            f"  <code>/analyze NFL Chiefs-3 -110 -110</code>\n"
            f"  <code>/analyze NBA LeBron_Over_25.5 -115 -105</code>\n\n"
            f"<i>Odds must be American format (e.g. -110, +150)</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    sport_raw, selection, odds_a_raw, odds_b_raw = args[0], args[1], args[2], args[3]

    # Parse odds
    try:
        odds_a = int(odds_a_raw)
        odds_b = int(odds_b_raw)
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['warn']} Invalid odds format. Use American odds (e.g. <code>-110</code> or <code>+150</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    if odds_a == 0 or odds_b == 0:
        await update.message.reply_text(f"{EMOJI['warn']} Odds cannot be zero.")
        return

    # Map sport string to enum (case-insensitive, fallback to OTHER)
    sport = Sport.OTHER
    for s in Sport:
        if s.value.upper() == sport_raw.upper():
            sport = s
            break

    selection_display = selection.replace("_", " ")

    if _engine is None:
        await update.message.reply_text(f"{EMOJI['warn']} Engine not initialised. Try again shortly.")
        return

    try:
        opp = _engine.analyze_line(
            sport=sport,
            market_type=MarketType.MONEYLINE,
            event=f"Manual analysis — {sport_raw.upper()}",
            selection=selection_display,
            player=None,
            line=None,
            side_a_odds=odds_a,
            side_b_odds=odds_b,
            is_side_a=True,
            best_book="Manual",
        )

        # Build concise analysis response
        ev_sign = "✅ POSITIVE EV" if opp.expected_value > 0 else "❌ NEGATIVE EV"
        response = "\n".join([
            f"{EMOJI['chart']} <b>Line Analysis</b>",
            "",
            f"<b>Sport:</b>     {sport.value}",
            f"<b>Selection:</b> {selection_display}",
            f"<b>Your Odds:</b> <code>{format_odds(odds_a)}</code>",
            f"<b>Opp Odds:</b>  <code>{format_odds(odds_b)}</code>",
            "",
            f"{EMOJI['target']} <b>Fair Probability:</b>  <code>{format_probability(opp.fair_probability)}</code>",
            f"   <i>(Vig removed: {opp.ev_result.fair_odds.vig_percentage:.2f}%)</i>",
            "",
            f"{EMOJI['money']} <b>Expected Value:</b>    <code>{format_ev(opp.expected_value)}</code>  {ev_sign}",
            f"   <i>Edge: {opp.ev_result.edge:+.4f}</i>",
            "",
            f"📐 <b>Kelly Criterion:</b>",
            f"   Full Kelly:  <code>{opp.ev_result.kelly_fraction:.2%}</code>",
            f"   Half Kelly:  <code>{opp.ev_result.half_kelly:.2%}</code>",
            "",
            f"<b>Recommendation:</b>  {opp.star_display}  {opp.recommendation.value}",
            "",
            "<b>Reason Codes:</b>",
            *[f"  • {r}" for r in opp.reason_codes],
        ])

        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_analyze: analysis failed: %s", exc)
        await update.message.reply_text("⚠️ /analyze failed. Check bot logs.")


async def cmd_steam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/steam — show latest detected steam alerts from the database."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        records = await _report_read(_db.get_recent_steam(limit=5), fallback=None)

        if records is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} Steam history is temporarily unavailable. "
                "Live monitoring is unaffected."
            )
            return

        if not records:
            await update.message.reply_text(
                f"{EMOJI['fire']} <b>Steam Alerts</b>\n\n"
                f"No steam moves detected yet.\n\n"
                f"<i>Steam detection activates when live odds polling is configured.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        await update.message.reply_text(
            f"{EMOJI['fire']} <b>Recent Steam Moves ({len(records)})</b>\n\n"
            f"<i>Showing latest {len(records)} detected steam alerts:</i>",
            parse_mode=ParseMode.HTML,
        )

        from models import AlertType, SteamAlert as SteamAlertModel
        for r in records:
            # Reconstruct a lightweight display object
            sa = SteamAlertModel(
                alert_type=AlertType.STEAM,
                sport=r.sport,
                market_type=r.market_type,
                event=r.event,
                selection=r.selection,
                opening_odds=r.opening_odds,
                current_odds=r.current_odds,
                steam_score=r.steam_score,
                steam_direction=r.steam_direction,
                books_moved=r.books_moved.split(",") if r.books_moved else [],
                timestamp=r.detected_at,
                notes=r.notes,
            )
            await update.message.reply_text(
                format_steam_alert(sa),
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.exception("cmd_steam: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /steam failed. Check bot logs.")


async def cmd_ev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ev — show latest +EV opportunities from the database."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        records = await _report_read(_db.get_recent_ev(limit=5), fallback=None)

        if records is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} +EV history is temporarily unavailable. "
                "Live monitoring is unaffected."
            )
            return

        if not records:
            await update.message.reply_text(
                f"{EMOJI['ev']} <b>+EV Opportunities</b>\n\n"
                f"No +EV opportunities stored yet.\n\n"
                f"<i>Automatic +EV detection starts when live odds polling is enabled.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        await update.message.reply_text(
            f"{EMOJI['ev']} <b>Recent +EV Opportunities ({len(records)})</b>",
            parse_mode=ParseMode.HTML,
        )

        for r in records:
            stars = EMOJI["star"] * r.stars + EMOJI["star_e"] * (5 - r.stars)
            line_str = f" ({r.line:+g})" if r.line else ""
            player_line = f"\n<b>Player:</b>  {r.player}" if r.player else ""
            reasons = "\n".join(f"  • {rc}" for rc in r.reason_codes.split(",") if rc)

            msg = "\n".join([
                f"{EMOJI['ev']} <b>+EV Opportunity</b>",
                "",
                f"<b>Sport:</b>   {r.sport}",
                f"<b>Market:</b>  {r.market_type}",
                f"<b>Event:</b>   {r.event}",
                f"{player_line}<b>Line:</b>    {r.selection}{line_str}",
                f"<b>Odds:</b>    <code>{format_odds(r.best_odds)}</code> @ {r.best_book}",
                "",
                f"{EMOJI['target']} <b>Fair Prob:</b>  <code>{r.fair_probability * 100:.1f}%</code>",
                f"{EMOJI['money']} <b>EV:</b>         <code>{format_ev(r.expected_value)}</code>",
                f"{EMOJI['fire']} <b>Steam:</b>      <code>{r.steam_score}/100</code>",
                f"{EMOJI['robot']} <b>Confidence:</b> <code>{r.ai_confidence}/100</code>",
                f"<b>Rating:</b>  {stars}  {r.recommendation}",
                "",
                f"<b>Reasons:</b>",
                reasons,
                "",
                f"{EMOJI['clock']} <i>{_fmt_user_ts(r.detected_at)}</i>",
            ])
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_ev: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /ev failed. Check bot logs.")


async def cmd_clv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clv — Show post-close Closing Line Value performance history.

    CLV records are written after events close (when real closing odds are
    available). While no events have closed yet, the response explains this
    and shows how many CLV opportunity alerts have been sent so far.
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        from alerts_multiplatform import format_clv_history
        records = await _report_read(_db.get_recent_clv_records(limit=20), fallback=None)
        if records is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} CLV history is temporarily unavailable. "
                "It can be retried without interrupting monitoring."
            )
            return
        msg = format_clv_history(records)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as exc:
        logger.exception("cmd_clv error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Could not load CLV data: {exc}",
            parse_mode="HTML",
        )


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/market — Show line movement and validation summary."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    try:
        from market_engine import _snapshot_cache
        from engine.consensus import compute_consensus
        from alerts_multiplatform import format_consensus_summary
        from config import config

        # Reports should survive a process restart when persisted observations
        # exist. The in-memory cache remains a fallback for a just-started
        # process whose latest cycle has not been written yet.
        all_snaps = []
        if _db is not None and hasattr(_db, "get_recent_market_snapshots"):
            all_snaps = await _report_read(
                _db.get_recent_market_snapshots(since_hours=24, limit=5000),
                fallback=[],
                timeout=_PROVIDER_REPORT_TIMEOUT_SECONDS,
            )
        all_snaps = all_snaps or [
            s for snaps in _snapshot_cache.values() for s in snaps
        ]

        if not all_snaps:
            await update.message.reply_text(
                f"📊 <b>Market Intelligence</b>\n\n"
                f"No market snapshot data available yet.\n"
                f"<i>Data accumulates as Underdog props are polled and scored.</i>",
                parse_mode="HTML",
            )
            return

        results = await _bounded(asyncio.to_thread(
            compute_consensus,
            all_snaps,
            min_books=config.CONSENSUS_MIN_BOOKS,
            outlier_threshold=config.INEFFICIENCY_THRESHOLD,
        ), timeout=_PROVIDER_REPORT_TIMEOUT_SECONDS)
        msg = format_consensus_summary(results)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as exc:
        logger.exception("cmd_market error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Market data is temporarily unavailable. "
            "Live monitoring is unaffected.",
            parse_mode="HTML",
        )


async def _send_performance_by_sport(
    update: Update,
    db: Database,
) -> None:
    """Render delivery-independent directional performance by sport."""
    await update.message.reply_text("⏳ Loading performance…")
    summary = await _report_read(
        db.get_directional_performance_summary(
            enabled_sports=config.active_sports
        ),
        fallback=None,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    if not isinstance(summary, dict):
        await update.message.reply_text(
            "📈 <b>Pick Performance by Sport</b>\n\n"
            "<i>Performance data is UNAVAILABLE; the database query failed "
            "or timed out. This is not zero.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    by_sport = summary.get("by_sport", {}) or {}
    lines = [
        "📈 <b>Pick Performance by Sport</b>",
        "<i>Directional opportunities; Telegram delivery is reported separately.</i>",
        "",
    ]
    if not by_sport:
        lines.append("<i>No directional opportunities have been recorded yet.</i>")
    else:
        for sport, row in sorted(by_sport.items()):
            hit = int(row.get("hit", 0) or 0)
            miss = int(row.get("miss", 0) or 0)
            push = int(row.get("push", 0) or 0)
            pending = int(row.get("pending", 0) or 0)
            directional = int(row.get("directional", 0) or 0)
            valid = hit + miss + push
            rate = f"{hit / valid * 100:.0f}%" if valid else "—"
            void = int(row.get("void", 0) or 0)
            unverified = int(row.get("unverified", 0) or 0)
            extra_parts = []
            if void:
                extra_parts.append(f"{void} void/other")
            if unverified:
                extra_parts.append(f"{unverified} unverified")
            extra = f" · {' · '.join(extra_parts)}" if extra_parts else ""
            clv_count = int(row.get("clv_count", 0) or 0)
            clv_coverage = row.get("clv_coverage_pct")
            clv_coverage_text = (
                f"{clv_coverage:.0f}% graded"
                if clv_coverage is not None
                else "unavailable"
            )

            def _format_clv(key: str) -> str:
                value = row.get(key)
                return f"{value:+.2f}%" if value is not None else "—"

            lines.append(
                f"<b>{_html.escape(str(sport))}</b> · {directional} picks\n"
                f"  ✅ {hit}H  ❌ {miss}M  ➖ {push}P · "
                f"{pending} pending · hit rate {rate} "
                f"(HIT/(HIT+MISS+PUSH), n={valid}){extra}\n"
                f"  CLV H/M/P: {_format_clv('clv_hit_avg')} / "
                f"{_format_clv('clv_miss_avg')} / {_format_clv('clv_push_avg')} "
                f"· {clv_count} samples · {clv_coverage_text}"
            )
            direction_rows = (
                (summary.get("by_sport_direction", {}) or {}).get(sport, {}) or {}
            )
            for direction in ("OVER", "UNDER"):
                direction_row = direction_rows.get(direction)
                if not direction_row:
                    continue
                direction_hit = int(direction_row.get("hit", 0) or 0)
                direction_miss = int(direction_row.get("miss", 0) or 0)
                direction_push = int(direction_row.get("push", 0) or 0)
                direction_pending = int(direction_row.get("pending", 0) or 0)
                direction_unverified = int(
                    direction_row.get("unverified", 0) or 0
                )
                direction_valid = direction_hit + direction_miss + direction_push
                direction_rate = (
                    f"{direction_hit / direction_valid * 100:.0f}%"
                    if direction_valid else "—"
                )
                lines.append(
                    f"  {direction}: {direction_hit}W-{direction_miss}L-"
                    f"{direction_push}P · {direction_pending} pending · "
                    f"{direction_unverified} unverified · "
                    f"{direction_rate}"
                )
    lines += [
        "",
        "<i>Only source-verified outcomes count. Hit rate is "
        "HIT/(HIT+MISS+PUSH); pending and unverified results are excluded.</i>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/performance — Historical win rate, CLV and ROI broken down by sport, market and tier."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return
    # Keep /performance distinct from /grade and /backtest.  This compact
    # grouped query is the command's authoritative report population.
    try:
        return await _send_performance_by_sport(update, _db)
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "📈 <b>Pick Performance by Sport</b>\n\n"
            "<i>Performance data is UNAVAILABLE because the database is busy. "
            "This is not zero.</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as exc:
        logger.exception("cmd_performance summary error: %s", exc)
        await update.message.reply_text(
            "⚠️ Performance data is temporarily unavailable. Please retry shortly.",
            parse_mode=ParseMode.HTML,
        )
        return

async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backtest [limit] — Run a full backtest on the last N resolved alerts."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    # Optional limit argument
    limit = 200
    args = context.args or []
    if args:
        try:
            limit = max(10, min(int(args[0]), 1000))
        except ValueError:
            pass

    try:
        from engine.backtesting import BacktestEngine, run_backtest

        await update.message.reply_text(
            f"⏳ Running backtest on last <b>{limit}</b> resolved records…",
            parse_mode="HTML",
        )

        tracked_records = await _report_read(
            _db.get_resolved_tracked_opportunities(limit=limit),
            fallback=None,
        )
        if tracked_records:
            # PropOpportunityLog is the provider-separated persistent tracking
            # stream. It is preferred over legacy sportsbook EVRecord rows.
            engine = BacktestEngine()
            report = await _bounded(asyncio.to_thread(
                engine.run_prop_opportunities,
                tracked_records,
            ))
            msg = report.to_telegram()
            msg += (
                "\n\n<i>Source: resolved directional tracked opportunities "
                "(delivery-independent; provider identity preserved).</i>"
            )
            await update.message.reply_text(msg, parse_mode="HTML")
            return

        # Legacy fallback for installations with no resolved tracked props yet.
        records = await _bounded(
            _db.get_ev_records_with_results(limit=limit, include_pending=False)
        )

        if not records:
            await update.message.reply_text(
                "📊 <b>Backtest</b>\n\n"
                "<i>No resolved records found.  Results are tracked automatically"
                " once events finish.  Use /performance once results accumulate.</i>",
                parse_mode="HTML",
            )
            return

        # Backtesting is CPU-bound synchronous code.  Running it in the event
        # loop prevents Telegram from processing cancellation/timeouts.
        report = await _bounded(asyncio.to_thread(run_backtest, records))
        msg = report.to_telegram()
        provider_perf = await _report_read(
            _db.get_telegram_pick_performance(),
            fallback=None,
        )
        if isinstance(provider_perf, dict) and provider_perf.get("by_provider"):
            msg += "\n\n📡 <b>Provider context</b>"
            msg += "\n<i>Backtest metrics above use legacy EVRecord rows.</i>"
            for provider, stats in sorted(provider_perf["by_provider"].items()):
                if str(provider).strip().lower() != "underdog":
                    continue
                msg += (
                    f"\n  {provider}: {stats.get('hit', 0)}H-"
                    f"{stats.get('miss', 0)}M-{stats.get('push', 0)}P · "
                    f"{stats.get('pending', 0)} pending"
                )
        else:
            msg += (
                "\n\n<i>Provider identity is unavailable in the legacy EVRecord "
                "backtest rows.</i>"
            )
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as exc:
        logger.exception("cmd_backtest error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Backtest is temporarily unavailable. "
            "The report can be retried while monitoring continues.",
            parse_mode="HTML",
        )


# ── Season-futures display filter ─────────────────────────────────────────────
# Applied ONLY in /picks and /slip output — never affects scanning, storage,
# database records, alerts, or the opportunity tracker.
_SEASON_FUTURE_PREFIXES: tuple[str, ...] = (
    "season ",           # "Season Receiving Yards", "Season TDs", …
    "regular season ",   # "Regular Season Games Started", …
    "playoff season ",
    "career ",
)
_SEASON_FUTURE_EXACT_WORDS: tuple[str, ...] = (
    " season",           # " season" appearing anywhere in the stat name
)

def _is_season_future(stat_type: str) -> bool:
    """
    Return True when stat_type represents a long-term future rather than a
    single-game player prop.  Examples that return True:
      "Season Receiving Yards", "Regular Season Games Started",
      "Season Receiving TDs", "Career Home Runs"

    Case-insensitive.  Only called for display filtering — scanning, storage,
    alert delivery, and opportunity tracking are completely unaffected.
    """
    lower = stat_type.strip().lower()
    # starts-with checks (fast path)
    if any(lower.startswith(p) for p in _SEASON_FUTURE_PREFIXES):
        return True
    # whole-word " season" appearing anywhere (e.g. "Regular Season ...")
    # but NOT "this season's stats" type wording — simple substring is fine
    # because all real single-game stats don't include the word "season".
    if " season" in lower:
        return True
    return False


def _is_upcoming_game(
    game_time: Optional[datetime],
    game_status: Optional[str] = None,
) -> bool:
    """Return True only when the event is safely known to be pregame."""
    from engine.timing import is_event_pregame

    safe, _reason = is_event_pregame(game_time, game_status=game_status)
    return safe
    if game_time is None:
        return True
    now = datetime.now(timezone.utc)
    if game_time.tzinfo is None:
        return game_time > now.replace(tzinfo=None)
    return game_time > now


def _is_today_reporting_game(game_time: Any) -> bool:
    """Fail closed unless the event is dated for today in the reporting zone."""
    if not isinstance(game_time, datetime):
        return False
    from reporting import is_current_reporting_day

    return is_current_reporting_day(game_time)


def _is_active_display_sport(sport: Optional[str]) -> bool:
    """Keep current pick/slip views aligned with the configured live slate."""
    sport_key = str(sport or "").strip().upper()
    return bool(sport_key) and sport_key in {
        str(value).strip().upper() for value in config.active_sports
    }


def _is_current_provider_sport(
    sport: Optional[str],
    provider: Optional[str],
) -> bool:
    """Apply the provider's own current-slate scope."""
    return _is_active_display_sport(sport)


def _has_current_prop_data(
    prop: Any,
    now: Optional[datetime] = None,
    *,
    require_direction: bool = True,
    require_scoring: bool = False,
) -> bool:
    """Keep user-facing pick/slip views on current, actionable rows only.

    This is a presentation gate. It does not affect scanning, alert
    qualification, cadence, or persisted tracking.

    Reports must have provider-owned identity and a fresh upcoming event.
    Direction is checked separately by default. Scoring checks are opt-in and
    must not become a delivery or pick-eligibility blocker.
    """
    # Stored provider times are UTC-naive, but adapters may supply aware
    # offsets. Normalize both sides by instant before applying this display gate.
    now = now or datetime.utcnow()
    if now.tzinfo is not None and now.utcoffset() is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    provider = str(getattr(prop, "provider", "") or "").strip().lower()
    if provider != "underdog":
        return False
    player = str(getattr(prop, "player_name", "") or "").strip()
    sport = str(getattr(prop, "sport", "") or "").strip()
    stat = str(getattr(prop, "stat_type", "") or "").strip()
    source_id = str(
        getattr(prop, "external_id", None)
        or getattr(prop, "game_id", None)
        or ""
    ).strip()
    if not player or not sport or not stat or not source_id:
        return False
    try:
        line = float(getattr(prop, "line_value", None))
        if line < 0 or line != line or line in (float("inf"), float("-inf")):
            return False
    except (TypeError, ValueError):
        return False
    fetched_at = getattr(prop, "fetched_at", None)
    game_time = getattr(prop, "game_time", None)
    if not isinstance(fetched_at, datetime) or not isinstance(game_time, datetime):
        return False
    fetched = (
        fetched_at.astimezone(timezone.utc).replace(tzinfo=None)
        if fetched_at.tzinfo is not None and fetched_at.utcoffset() is not None
        else fetched_at.replace(tzinfo=None)
    )
    game = (
        game_time.astimezone(timezone.utc).replace(tzinfo=None)
        if game_time.tzinfo is not None and game_time.utcoffset() is not None
        else game_time.replace(tzinfo=None)
    )
    from datetime import timedelta
    if fetched > now + timedelta(minutes=5):
        return False
    if game <= now or game > now + timedelta(hours=_CURRENT_PROP_MAX_FUTURE_HOURS):
        return False
    if getattr(prop, "removed", False) is True:
        return False
    recommendation = str(
        getattr(prop, "bet_recommendation", "") or ""
    ).upper()
    if require_direction:
        if recommendation not in {"OVER", "UNDER"}:
            return False
    if require_scoring:
        if recommendation not in {"OVER", "UNDER"}:
            return False
        try:
            confidence = float(getattr(prop, "bet_confidence", 0) or 0)
        except (TypeError, ValueError):
            return False
        if confidence <= 0:
            return False
        market_quality = getattr(prop, "market_quality_score", None)
        if market_quality is not None:
            try:
                if float(market_quality) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def _display_prop_from_opportunity(opportunity: Any) -> Any:
    """Adapt the canonical delivered opportunity to the pick/slip row shape."""
    return SimpleNamespace(
        provider="Underdog",
        product="player_props",
        player_name=getattr(opportunity, "player_name", ""),
        team=getattr(opportunity, "team", "") or "",
        sport=getattr(opportunity, "sport", ""),
        stat_type=getattr(opportunity, "stat_type", ""),
        line_value=getattr(opportunity, "line_value", 0.0),
        game_time=getattr(opportunity, "game_time", None),
        provider_status=None,
        external_id=getattr(opportunity, "external_id", "") or "",
        game_id=getattr(opportunity, "game_id", "") or "",
        fetched_at=(
            getattr(opportunity, "alert_sent_at", None)
            or getattr(opportunity, "detected_at", None)
            or datetime.utcnow()
        ),
        first_seen=getattr(opportunity, "detected_at", None),
        prev_line=None,
        opening_line=None,
        removed=False,
        score_tier=None,
        bet_recommendation=getattr(opportunity, "recommendation", None),
        bet_confidence=getattr(opportunity, "confidence", None),
        score_total=None,
        market_quality_score=None,
        alert_sent=True,
        canonical_alert=True,
        result=getattr(opportunity, "result", "PENDING"),
    )


def _is_canonical_alert_prop(prop: Any) -> bool:
    return bool(getattr(prop, "canonical_alert", False))


def _pick_source_identity(provider: str, prop: Any) -> tuple[str, ...]:
    """Provider-owned identity for current /picks rows."""
    source_id = str(
        getattr(prop, "external_id", None)
        or getattr(prop, "game_id", None)
        or ""
    ).strip().lower()
    return (
        str(provider or "").strip().lower(),
        str(getattr(prop, "player_name", "")).strip().lower(),
        str(getattr(prop, "sport", "")).strip().upper(),
        str(getattr(prop, "stat_type", "")).strip().lower(),
        source_id,
    )


def _ud_recommendation_identity(prop: Any) -> tuple[str, str, str, str]:
    """Source-aware identity used for bounded Underdog recommendation reads."""
    source_id = str(
        getattr(prop, "external_id", None)
        or getattr(prop, "game_id", None)
        or ""
    ).strip()
    return (
        str(getattr(prop, "player_name", "")),
        str(getattr(prop, "sport", "")),
        str(getattr(prop, "stat_type", "")),
        source_id,
    )


def _fair_provider_limit(
    rows: list[tuple[str, Any]],
    limit: int,
) -> list[tuple[str, Any]]:
    """Round-robin current directional rows so one provider cannot crowd out another."""
    if limit <= 0 or not rows:
        return []
    buckets: dict[str, list[tuple[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(row[0], []).append(row)
    selected: list[tuple[str, Any]] = []
    while len(selected) < limit:
        added = False
        for provider_rows in buckets.values():
            if provider_rows:
                selected.append(provider_rows.pop(0))
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
    return selected


def _select_directional_provider_rows(
    ud_props: list[Any],
    pp_props: list[Any],
    rec_map: dict[tuple[str, str, str, str], tuple[Optional[str], Optional[int]]],
    limit: int,
) -> list[tuple[str, Any]]:
    """Resolve Underdog direction, then apply a fair cap.

    ``pp_props`` remains in the signature for callers written against the
    previous multi-provider command surface, but is intentionally ignored:
    PropLine is internal evidence and never a Telegram candidate.
    """
    directional_by_identity: dict[tuple[str, ...], tuple[str, Any]] = {}
    for provider, prop in (("Underdog", row) for row in ud_props):
        effective_rec = getattr(prop, "bet_recommendation", None)
        if provider == "Underdog":
            if not _is_canonical_alert_prop(prop):
                live_rec, _ = rec_map.get(
                    _ud_recommendation_identity(prop),
                    (None, None),
                )
                if str(live_rec or "").strip().upper() in ("OVER", "UNDER"):
                    effective_rec = str(live_rec).strip().upper()
        if effective_rec not in ("OVER", "UNDER"):
            continue
        directional_by_identity[_pick_source_identity(provider, prop)] = (
            provider,
            prop,
        )
    return _fair_provider_limit(
        list(directional_by_identity.values()),
        limit,
    )


class PropPickAdapter:
    """
    Wraps (PropLineHistory + PlayerPropMarketComparison) into the interface
    expected by the slip optimizer (check_correlation) and _render_slip_section.

    Replaces the old PPEdgeRecord in the /picks and /slip flows so both commands
    can operate without any PrizePicks edge data.
    """
    __slots__ = (
        "player_name", "stat_type", "sport", "team", "game_time",
        "game_description", "confidence", "tier", "best_edge",
        "best_side", "pp_line_value", "prev_line", "opening_line",
        "sportsbook", "external_id", "game_id", "result", "detected_at", "comp",
        "canonical_alert",
    )

    def __init__(
        self,
        plh: Any,                   # PropLineHistory — typed as Any to avoid circular import
        comp: Optional[Any] = None, # PlayerPropMarketComparison | None
    ) -> None:
        self.player_name = plh.player_name
        self.stat_type   = plh.stat_type
        self.sport       = plh.sport
        self.team        = plh.team or ""
        self.game_time   = plh.game_time
        # PropLineHistory only stores the player's team.  It does not contain
        # an opponent, so never turn that team into a fabricated "vs" value;
        # doing so makes unrelated legs look correlated.
        self.game_description = str(
            getattr(plh, "game_description", "") or ""
        ).strip()
        self.external_id = str(getattr(plh, "external_id", "") or "").strip()
        self.game_id = str(getattr(plh, "game_id", "") or "").strip()
        self.detected_at = plh.fetched_at
        self.result      = None
        self.sportsbook  = getattr(plh, "provider", None) or "Underdog"
        self.comp        = comp
        self.canonical_alert = _is_canonical_alert_prop(plh)

        # Tiers and score-derived confidence are removed from current
        # pick/slip candidates. Keep neutral compatibility attributes for
        # older optimizer/report helpers without using them for eligibility.
        self.confidence = None
        self.tier = None

        # Best line from comparison or fallback to UD line
        best_line = (comp.best_line if comp else None) or plh.line_value
        self.pp_line_value = float(best_line)
        self.prev_line     = plh.prev_line
        self.opening_line  = None  # not tracked in PropLineHistory; no move note in slip

        # Edge proxy: use absolute line movement scaled to a % signal
        movement = comp.movement if comp is not None else None
        self.best_edge = abs(movement) * 2.0 if movement is not None else None

        # Direction: market-information label, not a betting call
        # Direction placeholder — updated from DB recommendation in cmd_picks/cmd_slip
        # rendering loop so it reflects an available OVER/UNDER direction.
        self.best_side = "—"


def _build_dk_fd_index(records: list) -> "dict[tuple[str,str], float]":
    """Build {(player_lower, sportsbook): line} from a list of OddsRecord objects.

    OddsRecord.selection is "PlayerName Over" / "PlayerName Under"; the
    line value is the same for both sides so we keep the first occurrence.
    """
    index: dict = {}
    for rec in records:
        sel      = (getattr(rec, "selection", None) or "").strip()
        line_val = getattr(rec, "line", None)
        if line_val is None:
            continue
        for suffix in (" Over", " Under"):
            if sel.endswith(suffix):
                pkey = sel[: -len(suffix)].strip().lower()
                key  = (pkey, rec.sportsbook)
                if key not in index:
                    index[key] = float(line_val)
                break
    return index


async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/picks [sport|N] — Live player prop picks from the multi-provider market engine.

    Usage:
      /picks           — top 10 props from the last 6 hours
      /picks 5         — top 5
      /picks NBA       — filter to NBA only
      /picks MLB 5     — MLB, top 5
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    logger.info("cmd_picks: command received from user %s",
                update.effective_user.id if update.effective_user else "?")
    try:
      picks_timeout = (
          _PICKS_TIMEOUT_SECONDS
          if _COMMAND_TIMEOUT_SECONDS == 3.0
          else _COMMAND_TIMEOUT_SECONDS
      )
      return await _bounded(
          _cmd_picks_inner(update, context),
          timeout=picks_timeout,
      )
    except asyncio.TimeoutError:
        logger.warning(
            "cmd_picks: report exceeded %.1fs; returning unavailable state",
            _PICKS_TIMEOUT_SECONDS,
        )
        await update.message.reply_text(
            "⚠️ Picks are temporarily unavailable because monitoring data is busy. "
            "Please retry shortly; the scan continues independently.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("cmd_picks: unhandled error: %s", exc)
        await update.message.reply_text(
            "⚠️ /picks failed — check bot logs for details."
        )


async def _cmd_picks_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inner implementation of /picks — wrapped by cmd_picks try/except.

    Compatibility note: persisted historical rows may still contain
    score_tier, proxy_match_confidence, and _db_conf fields. The old
    proxy-confidence fallback was triggered when proxy_match_confidence < 30,
    with legacy display bands "S"=85, "A"=70, "B"=55 and tier icons for
    "S"/"A"/"B". Current /picks does not
    use those deprecated fields to qualify or rank a pick.
    """
    _perf_started = time.monotonic()
    args = context.args or []
    limit = 10
    sport_filter: Optional[str] = None
    limit_seen = False
    for arg in args:
        if arg.isdigit():
            parsed_limit = int(arg)
            if parsed_limit < 1 or parsed_limit > 20:
                await update.message.reply_text(
                    "Usage: /picks [1–20] [sport]\n"
                    "Examples: /picks 5 · /picks MLB · /picks TENNIS 5"
                )
                return
            if limit_seen:
                await update.message.reply_text("Usage: /picks [1–20] [sport]")
                return
            limit_seen = True
            limit = parsed_limit
        else:
            candidate_sport = arg.upper()
            if sport_filter is not None:
                await update.message.reply_text("Usage: /picks [1–20] [sport]")
                return
            sport_filter = candidate_sport

    if len(args) > 2 or (
        sport_filter is not None
        and sport_filter not in config.ud_alert_sports
        and sport_filter not in config.active_sports
    ):
        await update.message.reply_text(
            "Usage: /picks [1–20] [sport]\n"
            "Supported sports: "
            + ", ".join(sorted(set(config.active_sports) | set(config.ud_alert_sports)))
        )
        return

    from engine.timing import format_game_time_label as _fmt_gt
    from engine.player_prop_market import (
        build_player_prop_market_comparison,
        PROVIDER_EMOJI as _PROV_EMOJI,
    )

    now   = datetime.utcnow()
    today = now.strftime("%b %d, %Y")

    # Window: hours since UTC midnight today (minimum 4h so early-AM runs still show data).
    # This prevents yesterday's completed-game slate from appearing as "today's picks".
    _midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _since_hours  = max((now - _midnight_utc).total_seconds() / 3600, 4.0)

    # ── 1. Provider-owned props ──────────────────────────────────────────────
    # PrizePicks is actionable on its own; it is not merely Underdog
    # enrichment. Keep the provider in the candidate identity so matching
    # player/stat rows remain separate in this analyst view.
    _provider_overfetch = max(limit * 4, 40)
    logger.info("cmd_picks: fetching current provider props (sport=%s)", sport_filter)
    _stage_started = time.monotonic()
    ud_props = await _bounded(_db.get_top_ud_props_for_picks(
        limit=_provider_overfetch,
        since_hours=_since_hours,
        sports=[sport_filter] if sport_filter else config.active_sports,
    ))
    _ud_query_seconds = time.monotonic() - _stage_started
    ud_props = [
        p for p in ud_props
        if not _is_season_future(p.stat_type)
        # Underdog's same-cycle recommendation can be fresher than the
        # synced PropLineHistory column; direction is resolved immediately
        # after this provider fetch.
        and _has_current_prop_data(
            p, now, require_direction=False, require_scoring=False
        )
        and _is_active_display_sport(p.sport)
        and not is_telegram_suppressed_sport(p.sport)
        and _is_upcoming_game(
            getattr(p, "game_time", None),
            getattr(p, "provider_status", None),
        )
    ]

    if sport_filter:
        ud_props = [p for p in ud_props if p.sport.upper() == sport_filter]

        # Delivered opportunities are the canonical actionable population. Merge
        # them ahead of the live observation pool so a later removed snapshot
    # cannot make a sent alert disappear from /picks.
    try:
        _alerted_opps = await _bounded(
            _db.get_current_actionable_opportunities(limit=500)
        )
    except Exception as _alerted_exc:
        logger.warning("cmd_picks: delivered opportunity lookup failed: %s", _alerted_exc)
        _alerted_opps = []
    _canonical_props = [
        _display_prop_from_opportunity(row)
        for row in (_alerted_opps or [])
        if not _is_season_future(getattr(row, "stat_type", ""))
        and _is_active_display_sport(getattr(row, "sport", None))
        and not is_telegram_suppressed_sport(getattr(row, "sport", None))
        and _is_upcoming_game(
            getattr(row, "game_time", None),
            getattr(row, "provider_status", None),
        )
        and (
            not sport_filter
            or str(getattr(row, "sport", "")).upper() == sport_filter
        )
    ]
    _seen_pick_ids = {
        _pick_source_identity("Underdog", row)
        for row in _canonical_props
    }
    ud_props = _canonical_props + [
        row for row in ud_props
        if _pick_source_identity("Underdog", row) not in _seen_pick_ids
    ]

    # PropLine is an internal evidence source, not a separate Telegram
    # provider. Keep the bounded legacy read for comparison compatibility, but
    # never expose those rows as a command provider or candidate.
    try:
        await _bounded(_db.get_latest_props_for_provider(
            "PrizePicks",
            since_hours=_since_hours,
            limit=_provider_overfetch,
            directional_first=True,
        ))
    except Exception:
        pass
    pp_props = []
    _pp_query_seconds = 0.0
    # Resolve direction before deduplication or limiting. /picks is a current
    # directional research view, not a second Telegram delivery gate.
    _rec_map: dict = {}
    try:
        _stage_started = time.monotonic()
        _rec_map = await _bounded(_db.get_ud_recommendations_bulk(
            [_ud_recommendation_identity(p) for p in ud_props],
            since_hours=24,
        ))
        _rec_query_seconds = time.monotonic() - _stage_started
    except Exception as _rec_exc:
        logger.warning("cmd_picks: recommendation lookup failed: %s", _rec_exc)
        _rec_query_seconds = time.monotonic() - _stage_started

    # The provider-owned snapshot is the freshest source for direction.
    # Refresh the current-state row before applying the presentation gate;
    # legacy confidence metadata is intentionally ignored.
    for _prop in ud_props:
        if _is_canonical_alert_prop(_prop):
            continue
        _live_rec, _ = _rec_map.get(
            _ud_recommendation_identity(_prop), (None, None)
        )
        if str(_live_rec or "").upper() in {"OVER", "UNDER"}:
            setattr(_prop, "bet_recommendation", str(_live_rec).upper())
    ud_props = [
        p for p in ud_props
        if _is_canonical_alert_prop(p)
        or _has_current_prop_data(
            p,
            now,
            require_direction=False,
            require_scoring=False,
        )
    ]

    _provenance_map: dict = {}
    _provenance_reader = getattr(_db, "get_ud_evidence_provenance_bulk", None)
    if callable(_provenance_reader):
        try:
            _provenance_map = await _bounded(_provenance_reader(
                [_ud_recommendation_identity(p) for p in ud_props],
                since_hours=24,
            ))
        except Exception as _prov_exc:
            logger.warning("cmd_picks: evidence provenance lookup failed: %s", _prov_exc)

    _direction_started = time.monotonic()
    provider_props = _select_directional_provider_rows(
        ud_props,
        [],
        _rec_map,
        _provider_overfetch,
    )
    _direction_seconds = time.monotonic() - _direction_started

    if not provider_props:
        hint = f" for {sport_filter}" if sport_filter else ""
        await update.message.reply_text(
            f"🎯 <b>Actionable Player Prop Picks</b>\n\n"
            f"No qualifying player props available right now{hint}.\n\n"
            f"<i>Season-long futures are excluded from this view.\n"
            f"All Player Props lines are combined in this view.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    logger.info(
        "cmd_picks: provider fetch done — current UD=%d PP=%d directional=%d "
        "(sport=%s; ud=%.3fs pp=%.3fs rec=%.3fs direction=%.3fs)",
        len(ud_props), len(pp_props), len(provider_props), sport_filter,
        _ud_query_seconds, _pp_query_seconds, _rec_query_seconds,
        _direction_seconds,
    )

    # ── 2. Build comparisons without inventing a cross-provider match ────────
    picks: list[tuple] = []   # [(PropLineHistory, PlayerPropMarketComparison|None)]
    for provider, plh in provider_props:
        # The comparison helper is Underdog-anchored. For a standalone
        # PrizePicks row, comp=None prevents a PP line being mislabeled as UD.
        comp = None
        if provider == "Underdog":
            comp = build_player_prop_market_comparison(
                player_name    = plh.player_name,
                sport          = plh.sport,
                stat_type      = plh.stat_type,
                ud_line        = plh.line_value,
                previous_line  = plh.prev_line,
                fetched_at     = plh.fetched_at,
                pp_rows        = [],
                now            = now,
                min_confidence = 0,
            )
        picks.append((plh, comp))

    # Sort within each sport independently without confidence, tier, quality,
    # or market-movement ranking.
    import datetime as _dt_mod

    def _pick_sort(item: tuple) -> tuple:
        plh, comp = item
        gt = plh.game_time
        if gt is None:
            gt_sort = _dt_mod.datetime.max
        else:
            gt_sort = gt.replace(tzinfo=None) if gt.tzinfo else gt
        return (gt_sort, str(plh.player_name or "").lower(), str(plh.stat_type or "").lower())

    # Per-sport sort: group → sort each sport independently → flatten in sport order.
    # This ensures MLB's large volume cannot push smaller sports off the leaderboard.
    _by_sport_sort: dict = {}
    for _item in picks:
        _sk = (_item[0].sport or "UNKNOWN").upper()
        _by_sport_sort.setdefault(_sk, []).append(_item)
    picks = []
    for _sk in sorted(_by_sport_sort.keys()):
        _by_sport_sort[_sk].sort(key=_pick_sort)
        picks.extend(_by_sport_sort[_sk])
    logger.info("cmd_picks: built %d comparisons, sorted per-sport", len(picks))

    # ── 4. Fetch historical hit-rates concurrently (non-blocking) ────────────
    from engine.player_results import compute_hit_rates as _compute_hr
    import asyncio as _asyncio

    async def _get_hr(plh: "PropLineHistory") -> "tuple":
        """Return (PlayerHitRates | None, opponent_str | None)."""
        try:
            results = await _db.get_player_results(
                plh.player_name, plh.sport, plh.stat_type, limit=30
            )
            if not results:
                return None, None
            # Never infer the current opponent from the newest historical
            # result.  That result belongs to a completed event and can be a
            # different matchup from the provider-owned current event.
            _opp: Optional[str] = getattr(plh, "opponent", None)
            hr = _compute_hr(results, plh.line_value, opponent=_opp)
            return hr, _opp
        except Exception:
            return None, None

    logger.info("cmd_picks: starting hit-rate gather for %d props", len(picks))
    _hr_raw    = await _bounded(
        _asyncio.gather(*[_get_hr(plh) for plh, _ in picks], return_exceptions=True)
    )
    _hit_rates = [(None, None) if isinstance(r, Exception) else r for r in _hr_raw]
    logger.info("cmd_picks: hit-rate gather complete")

    # Apply the same strongest-pick eligibility and one-per-player selection
    # used by alert delivery.  Direction alone is not enough for this surface:
    # complete history must support the same side across every available
    # decision window, which removes mediocre 40–60% rows from /picks.
    _strong_candidates: list[dict] = []
    for _idx, ((plh, comp), (_hr, _opp)) in enumerate(zip(picks, _hit_rates)):
        _effective_rec = str(
            getattr(plh, "bet_recommendation", None) or ""
        ).upper()
        _live_rec, _live_conf = _rec_map.get(
            _ud_recommendation_identity(plh), (None, None)
        )
        if (
            (getattr(plh, "provider", None) or "Underdog") == "Underdog"
            and not _is_canonical_alert_prop(plh)
            and _live_rec is not None
        ):
            _effective_rec = str(_live_rec).upper()
        _strong_candidates.append({
            "provider": getattr(plh, "provider", None) or "Underdog",
            "player": getattr(plh, "player_name", ""),
            "sport": getattr(plh, "sport", ""),
            "stat_type": getattr(plh, "stat_type", ""),
            "direction": _effective_rec,
            "confidence": (
                _live_conf
                if _live_conf is not None
                else getattr(plh, "bet_confidence", None)
            ),
            "hit_rates": _hr,
            "evidence_available": bool(
                _hr is not None and getattr(_hr, "has_real_data", False)
            ),
            "_pick_index": _idx,
        })

    _strong_candidates = [
        _candidate
        for _candidate in _strong_candidates
        if is_strong_actionable_pick(_candidate)
    ]
    _strong_eligible_count = len(_strong_candidates)
    _strong_candidates = select_strongest_actionable_picks(
        _strong_candidates,
        require_strong=True,
    )[:limit]
    _selected_indices = {
        int(_candidate["_pick_index"]) for _candidate in _strong_candidates
    }
    picks = [
        _item for _idx, _item in enumerate(picks) if _idx in _selected_indices
    ]
    _hit_rates = [
        _rate for _idx, _rate in enumerate(_hit_rates)
        if _idx in _selected_indices
    ]
    logger.info(
        "cmd_picks: strongest filter kept %d/%d rows",
        len(picks),
        _strong_eligible_count,
    )

    if not picks:
        hint = f" for {sport_filter}" if sport_filter else ""
        await update.message.reply_text(
            f"🎯 <b>Actionable Player Prop Picks</b>\n\n"
            f"No strong player props available right now{hint}.\n\n"
            f"<i>Rows are shown only when the complete directional history "
            f"supports the same OVER or UNDER side.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── 4b. Fetch alternate lines concurrently ────────────────────────────────
    from engine.player_prop_market import _line_label as _ll

    logger.info("cmd_picks: fetching alternate lines")
    async def _get_alt_lines(plh):
        if (getattr(plh, "provider", None) or "Underdog") != "Underdog":
            return []
        return await _db.get_all_ud_lines_for_prop(
            plh.player_name, plh.sport, plh.stat_type, since_hours=24
        )

    _alt_raw = await _bounded(_asyncio.gather(
        *[_get_alt_lines(plh) for plh, _ in picks],
        return_exceptions=True,
    ))
    _alt_lines: list[list[float]] = [
        [] if isinstance(r, Exception) else (r or [])
        for r in _alt_raw
    ]
    logger.info("cmd_picks: recs=%d, alt-line queries done", len(_rec_map))

    try:
        _intel_map = await _bounded(_db.get_latest_downstream_intelligence(
            [
                (
                    getattr(plh, "provider", None) or "Underdog",
                    plh.player_name,
                    plh.sport,
                    plh.stat_type,
                    getattr(plh, "external_id", None)
                    or getattr(plh, "game_id", None)
                    or "",
                )
                for plh, _ in picks
            ],
            since_hours=72,
        ))
    except Exception as _intel_exc:
        logger.warning("cmd_picks: downstream intelligence lookup failed: %s", _intel_exc)
        _intel_map = {}

    # ── 5. Format ─────────────────────────────────────────────────────────────
    _PICK_LABEL: dict[str, str] = {
        "OVER":  "OVER (More) ⬆",
        "UNDER": "UNDER (Less) ⬇",
    }

    _SPORT_ICON: dict[str, str] = {
        "MLB":    "⚾", "WNBA": "🏀", "NBA":    "🏀",
        "NFL":    "🏈", "DOTA": "🎮", "CS":     "🖥️", "TENNIS": "🎾",
    }

    header = f"🎯 <b>Actionable Player Prop Picks — {today}</b>"
    if sport_filter:
        header += f"  <i>({sport_filter})</i>"
    out: list[str] = [header, ""]

    # ── Per-sport grouping — actionable picks only ────────────────────────────
    # Preserve the within-sport confidence order. Never rank sports against each
    # other. Skip props with no actionable direction.
    from collections import OrderedDict as _OD
    _sport_groups: _OD = _OD()
    for _flat_idx, (plh, comp) in enumerate(picks):
        _key = plh.sport.upper()
        # Resolve effective recommendation: live snapshot overrides synced column
        _eff_rec = getattr(plh, "bet_recommendation", None)
        _live_r, _ = _rec_map.get(_ud_recommendation_identity(plh), (None, None))
        if (
            (getattr(plh, "provider", None) or "Underdog") == "Underdog"
            and not _is_canonical_alert_prop(plh)
            and _live_r is not None
        ):
            _eff_rec = _live_r
        # Skip props without a direction — user only wants actionable picks.
        if _eff_rec not in ("OVER", "UNDER"):
            logger.debug(
                "cmd_picks: skipping %s/%s — no actionable direction (rec=%s)",
                plh.player_name, plh.stat_type, _eff_rec,
            )
            continue
        # Keep this view aligned with the same provider/event integrity
        # contract as /slip.  Delivery remains independently gated by
        # AlertDelivery; this only prevents malformed current rows from being
        # presented as picks.
        if _key not in _sport_groups:
            _sport_groups[_key] = []
        _sport_groups[_key].append((_flat_idx, plh, comp))

    def _render_pick_entry(rank: int, flat_idx: int, plh, comp) -> str:

        # Keep the source line canonical for this row. A PropLine-only row
        # must never be rendered as though its line came from native Underdog.
        provider = getattr(plh, "provider", None) or "Underdog"
        provider_emoji = _PROV_EMOJI.get(provider, "📡")
        source_id = str(
            getattr(plh, "external_id", None)
            or getattr(plh, "game_id", None)
            or ""
        ).strip()
        provider_line = (
            comp.lines.get(provider) if comp and provider in comp.lines else None
        )
        source_v = (
            provider_line.line_value
            if provider_line is not None and provider_line.available
            else plh.line_value
        )

        # Bet direction — primary: synced column on PropLineHistory (always fresh)
        # Secondary: live UnderdogSnapshotRecord via _rec_map (handles same-cycle updates)
        bet_rec  = getattr(plh, "bet_recommendation", None)
        rec_key  = _ud_recommendation_identity(plh)
        _live_rec, _live_conf = _rec_map.get(rec_key, (None, None))
        if _live_rec is None and _rec_map:
            # Case-insensitive fallback (normalisation drift between tables)
            _rec_key_lower = tuple(str(part).lower() for part in rec_key)
            for _stored_key, _rv in _rec_map.items():
                if tuple(str(part).lower() for part in _stored_key) == _rec_key_lower:
                    _live_rec, _live_conf = _rv
                    break
        # Prefer live snapshot if available; fall back to synced column
        if (
            (getattr(plh, "provider", None) or "Underdog") == "Underdog"
            and not _is_canonical_alert_prop(plh)
            and _live_rec is not None
        ):
            bet_rec = _live_rec
        if bet_rec is None:
            logger.debug(
                "cmd_picks: no direction available for %s/%s",
                plh.player_name, plh.stat_type,
            )
        pick_label = _PICK_LABEL.get(bet_rec or "", "—")
        _evidence_source = _provenance_map.get(rec_key)
        if _evidence_source is None and _provenance_map:
            _rec_key_lower = tuple(str(part).lower() for part in rec_key)
            for _stored_key, _stored_source in _provenance_map.items():
                if tuple(str(part).lower() for part in _stored_key) == _rec_key_lower:
                    _evidence_source = _stored_source
                    break
        _evidence_label = evidence_source_label(_evidence_source)

        # Line movement annotation
        move_str = ""
        if comp and comp.movement is not None and comp.movement != 0:
            sign  = "+" if comp.movement > 0 else ""
            arrow = "↑" if comp.movement > 0 else "↓"
            move_str = f"  <code>{sign}{comp.movement:.1f}{arrow}</code>"

        # Game time
        gt = plh.game_time
        gt_str = ""
        if gt:
            lbl = _fmt_gt(gt)
            if lbl:
                gt_str = f"⏰ {lbl}  ·  "

        # ── Build entry ───────────────────────────────────────────────────────
        entry_lines: list[str] = [
            "━━━━━━━━━━━━━━━━━━",
            f"<b>#{rank} {plh.player_name}</b>",
            "🎯 <b>PLAYER PROP PICK</b>",
            f"Market: {plh.stat_type}",
            f"🎯 Line: <code>{source_v:.1f}</code>  {_ll(source_v)}",
            f"{gt_str}{plh.sport}",
            f"Event: <code>{_html.escape(source_id) if source_id else 'unavailable'}</code>",
            "",
            f"🎯 Pick: <b>{pick_label}</b>{move_str}",
            f"🧾 Evidence basis: <b>{_html.escape(_evidence_label)}</b>",
        ]

        # Persisted Task #135 downstream intelligence.  Do not derive a model
        # projection, movement quality, or historical category when it is absent.
        _intel_key = (
            str(getattr(plh, "provider", None) or "Underdog").lower(),
            str(plh.player_name).lower(),
            str(plh.sport).upper(),
            str(plh.stat_type).lower(),
            str(
                getattr(plh, "external_id", None)
                or getattr(plh, "game_id", None)
                or ""
            ).lower(),
        )
        _intel = _intel_map.get(_intel_key, {})
        _value = _intel.get("value") if isinstance(_intel, dict) else {}
        _movement = _intel.get("movement") if isinstance(_intel, dict) else {}
        _evidence = _intel.get("evidence") if isinstance(_intel, dict) else {}
        _complete = _intel.get("evidence_completeness") if isinstance(_intel, dict) else {}
        _sharp = _intel.get("sharp_confidence") if isinstance(_intel, dict) else {}

        def _shown(value: object, suffix: str = "") -> str:
            return "unavailable" if value is None else f"{value}{suffix}"

        _projection = _value.get("projection") if isinstance(_value, dict) else None
        _edge = _value.get("edge") if isinstance(_value, dict) else None
        _movement_quality = "unavailable"
        if isinstance(_movement, dict) and _movement.get("available"):
            _mag = _movement.get("magnitude")
            _persist = _movement.get("persistence")
            if _mag is not None and _persist is not None:
                _movement_quality = f"{_movement.get('direction', 'UNKNOWN')} · {_mag} · {_persist}% persistent"
            elif _mag is not None:
                _movement_quality = f"{_movement.get('direction', 'UNKNOWN')} · {_mag}"
        _historical = _evidence.get("historical") if isinstance(_evidence, dict) else None
        entry_lines.extend([
            "",
            "🧠 <b>Downstream Intelligence</b>",
            f"  Sharp Confidence: {_shown(_sharp.get('score') if isinstance(_sharp, dict) else None, '/100')}",
            f"  Model Projection: {_shown(_projection)}",
            f"  Current Line: {_shown(source_v)}",
            f"  Model Edge / Current-Line Value: {_shown(_edge)}",
            f"  Line Movement: {_shown(_movement.get('direction') if isinstance(_movement, dict) and _movement.get('available') else None)}",
            f"  Movement Quality: {_movement_quality}",
            f"  Evidence Completeness: {_shown(_complete.get('score') if isinstance(_complete, dict) else None, '/100')}",
            f"  Historical Evidence: {_shown(_json.dumps(_historical, separators=(',', ':')) if _historical else None)}",
        ])

        # ── Evidence block ────────────────────────────────────────────────────
        hr, _opp = _hit_rates[flat_idx]
        has_evidence = hr is not None and getattr(hr, "has_real_data", False)

        if has_evidence:
            evid: list[str] = []
            for _wlbl, _ws in [
                ("L5",     hr.l5),
                ("L10",    hr.l10),
                ("L20",    hr.l20),
                ("L30",    hr.l30),
                ("Season", hr.season),
            ]:
                if _ws and _ws.games >= 3:
                    evid.append(
                        f"  {_wlbl:<7} {_ws.over_count}/{_ws.games}"
                        f" ({_ws.hit_rate:.0%})  avg {_ws.average:.1f}"
                    )

            if evid:
                entry_lines.append("")
                entry_lines.append(
                    f"📊 <b>Evidence</b>  <i>(vs {source_v:.1f} line)</i>"
                )
                entry_lines.extend(evid)

            # H2H — only shown when ≥3 games vs current opponent exist
            if hr.h2h is not None and hr.h2h.games >= 3 and _opp:
                _h = hr.h2h
                _opp_short = _opp[:18] + "…" if len(_opp) > 18 else _opp
                entry_lines.append(
                    f"  H2H vs {_opp_short} ({_h.games}g): "
                    f"{_h.over_count}/{_h.games} ({_h.hit_rate:.0%})"
                    f"  avg {_h.average:.1f}"
                )

        # ── Alternate lines (if Underdog offers multiple) ─────────────────────
        alt = _alt_lines[flat_idx]
        # Only show when there are ≥2 distinct lines
        if len(alt) >= 2 and provider == "Underdog":
            entry_lines.append("")
            entry_lines.append("📊 <b>Available Lines:</b>")
            alt_labeled = "  ".join(
                f"{_ll(v).split(' ')[0]} <code>{v:.1f}</code>" for v in alt
            )
            entry_lines.append(f"  {alt_labeled}")
            entry_lines.append(
                f"  Current Selected: {provider_emoji} <code>{source_v:.1f}</code>"
            )

        return "\n".join(entry_lines)

    # Guard: if all fetched props lack a valid direction, say so.
    if not _sport_groups:
        await update.message.reply_text(
            "No actionable betting picks right now.\n\n"
            "<i>Props are scanned continuously — picks only appear once a prop "
            "has a valid OVER/UNDER direction.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Emit each sport group with its own header and per-sport rank numbers
    for _sport_key, _group in _sport_groups.items():
        _sport_icon = _SPORT_ICON.get(_sport_key, "🔸")
        out.append(f"{_sport_icon} <b>{_sport_key}</b>")
        for _sport_rank, (_flat_idx, plh, comp) in enumerate(_group, 1):
            out.append(_render_pick_entry(_sport_rank, _flat_idx, plh, comp))
        out.append("")  # blank line between sport sections

    if len(picks) >= limit and not sport_filter:
        out.append(f"\n<i>Showing top {limit}. Use /picks [sport] to filter.</i>")

    # Telegram HTML message; chunked to stay under the 4096-char API limit.
    full_text = "\n\n".join(out)
    logger.info(
        "cmd_picks: formatted output %d chars, sending (%d props; enrichment=%.3fs)",
        len(full_text), len(picks),
        time.monotonic() - _direction_started - _direction_seconds,
    )
    await _send_in_chunks(update, full_text, parse_mode=ParseMode.HTML)
    logger.info("cmd_picks: send complete total=%.3fs", time.monotonic() - _perf_started)


async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testalert — Send a mock Actionable Pick Alert to verify Telegram delivery."""
    uid = getattr(update.effective_user, "id", "?")
    logger.info("cmd_testalert: user_id=%s", uid)
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await update.message.reply_text("⏳ Generating mock Actionable Pick Alert…")

    try:
        from alerts_multiplatform import format_underdog_change_alert
        from alerts import _sanitize_user_alert_message

        # MLB strikeouts — Freddy Peralta, line moved 5.0 → 5.5.
        # Uses the same format as live 🎯 ACTIONABLE BET PICK alerts.
        msg = format_underdog_change_alert(
            player_name = "Freddy Peralta",
            team        = "MIL",
            sport       = "MLB",
            stat_type   = "Strikeouts",
            old_line    = 5.0,
            new_line    = 5.5,
            score       = None,
            validation  = None,
            decision    = None,
        )
        await update.message.reply_text(
            _sanitize_user_alert_message(msg),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await update.message.reply_text("✅ Actionable Pick Alert test sent.")

    except Exception as exc:
        logger.exception("cmd_testalert error: %s", exc)
        await update.message.reply_text(f"{EMOJI['warn']} Test alert failed: {exc}")


def _render_slip_section(
    size: int,
    slip_result: "OptimizedSlip",
    label: str = "",
) -> list[str]:
    """Render a single slip size into HTML lines (shared by cmd_slip).

    Works with both PPEdgeRecord legs (old pipeline) and PropPickAdapter legs
    (new player prop market framework).
    """
    from engine.timing import format_game_time_label as _fmt_gt
    records    = slip_result.legs
    actual     = len(records)
    total_move = 0.0   # replaces avg_edge for PropPickAdapter legs
    move_count = 0
    has_adapters = any(hasattr(r, "comp") for r in records)

    heading = f"🎯 <b>{size}-Man Slip</b>"
    if label:
        heading += f"  <i>{label}</i>"
    section: list[str] = [heading, ""]

    for i, r in enumerate(records, 1):
        best_line = getattr(r, "pp_line_value", None)
        line_str  = f"<code>{best_line:g}</code>" if best_line is not None else "—"

        # Movement note — use the comparison when available, otherwise use
        # the provider row's own persisted previous line.
        move_note = ""
        prev = getattr(r, "prev_line", None)
        if has_adapters:
            comp = getattr(r, "comp", None)
            mv = (
                getattr(comp, "movement", None)
                if comp is not None
                else (
                    float(best_line) - float(prev)
                    if best_line is not None and prev is not None
                    else None
                )
            )
            if mv is not None and mv != 0:
                sign  = "+" if mv > 0 else ""
                arrow = "↑" if mv > 0 else "↓"
                move_note = f"  <code>{sign}{mv:.1f}{arrow}</code>"
                total_move += abs(mv)
                move_count += 1
        else:
            opening = getattr(r, "opening_line", None)
            if (opening is not None and best_line is not None
                    and opening != best_line):
                delta = best_line - opening
                move_note = f"  {'▲' if delta > 0 else '▼'}{abs(delta):.1f}"

        gt = getattr(r, "game_time", None)
        gt_label = f"  ⏰ {_fmt_gt(gt)}" if gt is not None and _fmt_gt(gt) else ""

        # Decision engine — only meaningful for PPEdgeRecord legs
        _dec_leg = ""
        if not has_adapters:
            try:
                from engine.decision_engine import make_pp_decision
                _d = make_pp_decision(r)
                if _d.suggested_units > 0:
                    _dec_leg = (
                        f"\n    {_d.action_label}  ·  "
                        f"Kelly {_d.kelly_full * 100:.1f}%  ·  "
                        f"<b>{_d.suggested_units:.2f}u</b>"
                    )
                else:
                    _dec_leg = f"\n    {_d.action_label}"
            except Exception:
                pass

        # Provider lines summary for PropPickAdapter legs — available only
        provider_row = ""
        if has_adapters:
            comp = getattr(r, "comp", None)
            if comp and comp.lines:
                def _lv2(v: Optional[float]) -> str:
                    return f"<code>{v:.1f}</code>" if v is not None else "—"
                row_parts = [f"🎯 Player Prop {_lv2(best_line)}"]
                provider_row = "\n    " + "  ".join(row_parts)

        section.append(
            f"  <b>Leg {i}</b>\n"
            f"    <b>{r.player_name}</b> · {r.stat_type}\n"
            f"    {r.best_side} {line_str}"
            f"{move_note}{gt_label}"
            f"{provider_row}"
            f"{_dec_leg}"
        )

    if has_adapters:
        avg_mv = total_move / move_count if move_count else None
        summary = (
            f"\n  Avg move <code>{avg_mv:.1f}</code>" if avg_mv is not None
            else "\n  Avg move <code>unavailable</code>"
        ) + (
            f"  ·  {actual} legs"
        )
    else:
        edges = [
            r.best_edge for r in records
            if getattr(r, "best_edge", None) is not None
        ]
        avg_edge = sum(edges) / len(edges) if edges else None
        summary = (
            f"\n  Avg edge <code>+{avg_edge:.1f}%</code>"
            if avg_edge is not None
            else "\n  Avg edge <code>unavailable</code>"
        ) + (
            f"{actual} legs"
        )

    section.append(summary)

    if slip_result.correlation_warnings:
        for _w in slip_result.correlation_warnings:
            section.append(f"  <i>⚠️ {_w}</i>")

    if slip_result.excluded:
        _excl = ", ".join(
            f"{_r.player_name} · {_r.stat_type}"
            for _r, _ in slip_result.excluded[:2]
        )
        section.append(
            f"  <i>{len(slip_result.excluded)} filtered by correlation: {_excl}…</i>"
        )

    return section


async def _cmd_slip_journal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    args: list[str],
) -> None:
    """Handle /slip journal subcommands: create, add, grade, journal/history."""
    subcmd = args[0].lower() if args else "journal"

    # ── /slip create [stake] ────────────────────────────────────────────────
    if subcmd == "create":
        stake: Optional[float] = None
        if len(args) >= 2:
            try:
                stake = float(args[1])
            except ValueError:
                pass
        code = await _db.create_slip_journal(stake=stake)
        stake_str = f"  ·  ${stake:.2f} staked" if stake else ""
        await update.message.reply_text(
            f"📓 <b>Slip Journal Created</b>\n\n"
            f"Code: <code>{code}</code>{stake_str}\n"
            f"Status: OPEN\n\n"
            f"Add picks with:\n"
            f"<code>/slip add &lt;player name or pick ID&gt;</code>\n\n"
            f"Grade when ready:\n"
            f"<code>/slip grade</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── /slip add &lt;query&gt; ────────────────────────────────────────────────────
    if subcmd == "add":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: <code>/slip add &lt;player name or pick ID&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        query = " ".join(args[1:])
        # Ensure an open slip exists
        open_slip = await _db.get_open_slip_journal()
        if open_slip is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} No open slip. Create one first: <code>/slip create</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        opp = await _db.find_opportunity_for_slip(query)
        if opp is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} No matching pick found for <b>{query}</b>.\n"
                f"Try the player name, or use a numeric ID from <code>/picks</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        leg = await _db.add_slip_journal_leg(
            slip_code   = open_slip.slip_code,
            player_name = opp.player_name,
            stat_type   = opp.stat_type,
            opp_id      = opp.id,
            team        = opp.team or "",
            sport       = opp.sport or "",
            line_value  = opp.line_value,
            direction   = opp.recommendation,
            tier        = None,
            confidence  = None,
            game_time   = opp.game_time,
            provider    = getattr(opp, "provider", None) or "Underdog",
        )
        legs = await _db.get_slip_journal_legs(open_slip.slip_code)
        await update.message.reply_text(
            f"✅ <b>Leg added to {open_slip.slip_code}</b>\n\n"
            f"🎯 <b>PLAYER PROP PICK</b>\n"
            f"<b>{opp.player_name}</b> · {opp.stat_type}\n"
            f"{opp.recommendation} <code>{opp.line_value:g}</code>  ·  "
            f"{opp.sport}\n\n"
            f"<i>{open_slip.slip_code} now has {len(legs)} leg{'s' if len(legs) != 1 else ''}.</i>\n"
            f"Grade: <code>/slip grade</code>  ·  View: <code>/slip journal</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── /slip grade [payout] ─────────────────────────────────────────────────
    if subcmd == "grade":
        open_slip = await _db.get_open_slip_journal()
        if open_slip is None:
            # Try to grade the most recent graded slip (idempotent re-grade)
            history = await _db.get_slip_journal_history(limit=1)
            if history:
                open_slip = history[0]
            else:
                await update.message.reply_text(
                    f"{EMOJI['warn']} No slip to grade. Create one: <code>/slip create</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
        payout: Optional[float] = None
        if len(args) >= 2:
            try:
                payout = float(args[1])
            except ValueError:
                pass
        summary = await _db.grade_slip_journal(open_slip.slip_code, payout=payout)
        legs    = await _db.get_slip_journal_legs(open_slip.slip_code)

        hit_icons  = {l.player_name + l.stat_type: l.result for l in legs}
        result_sym = {"HIT": "✅", "MISS": "❌", "PUSH": "🔁", "PENDING": "⏳"}

        lines = [
            f"📋 <b>Slip Grade — {open_slip.slip_code}</b>",
            "",
        ]
        for leg in legs:
            sym = result_sym.get(leg.result, "⏳")
            av  = f"  <i>(actual: {leg.actual_value:g})</i>" if leg.actual_value is not None else ""
            _leg_provider = getattr(leg, "provider", None) or "Underdog"
            lines.append(
                f"{sym} 🎯 "
                f"<b>PLAYER PROP PICK</b> · <b>{leg.player_name}</b> {leg.direction or ''} "
                f"<code>{leg.line_value:g}</code> · {leg.stat_type}{av}"
            )

        lines += [""]
        if not legs:
            lines.append("🟡 <b>OPEN / EMPTY</b> — no legs have been added yet.")
            lines.append("<i>This slip has not been graded.</i>")
        elif summary["pending"] > 0:
            lines.append(
                f"🟡 <b>OPEN / PENDING</b> — "
                f"{summary['hit']}H / {summary['miss']}M / {summary['push']}P"
                f" / {summary['pending']} pending"
            )
        else:
            lines.append(
                f"{'✅ CASH' if summary['all_hit'] else '❌ NO CASH'} — "
                f"{summary['hit']}H / {summary['miss']}M / {summary['push']}P"
            )
        if payout is not None and open_slip.stake:
            roi_str = f"+{summary['roi_pct']:.1f}%" if (summary.get("roi_pct") or 0) >= 0 else f"{summary['roi_pct']:.1f}%"
            lines.append(f"${open_slip.stake:.2f} → ${payout:.2f}  ·  ROI {roi_str}")

        if summary["pending"] > 0:
            lines.append(f"\n<i>{summary['pending']} leg(s) still pending — run /slip grade again after results are in.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # ── /slip journal | /slip j | /slip history ──────────────────────────────
    history = await _db.get_slip_journal_history(limit=8)
    try:
        _grade_auto_slips = getattr(
            _db, "grade_auto_slip_notification_legs", None
        )
        if callable(_grade_auto_slips):
            await _grade_auto_slips()
    except Exception as exc:
        logger.warning("slip journal: auto-slip settlement refresh failed: %s", exc)
    auto_history = []
    auto_stats = {}
    _auto_history_reader = getattr(
        _db, "get_auto_slip_notification_history", None
    )
    if callable(_auto_history_reader):
        try:
            auto_history = await _auto_history_reader(limit=8) or []
        except Exception as exc:
            logger.warning("slip journal: auto-slip history unavailable: %s", exc)
    _auto_stats_reader = getattr(_db, "get_auto_slip_notification_stats", None)
    if callable(_auto_stats_reader):
        try:
            auto_stats = await _auto_stats_reader() or {}
        except Exception as exc:
            logger.warning("slip journal: auto-slip totals unavailable: %s", exc)

    if not history and not auto_history:
        lines = [
            "📓 <b>Slip Journal</b>",
            "",
            "No manual slips or gradeable auto-sent slips are available.",
            "",
            "Create one: <code>/slip create [stake]</code>",
            "Add picks:  <code>/slip add &lt;player&gt;</code>",
            "Grade:      <code>/slip grade [payout]</code>",
        ]
        if auto_stats.get("untracked_deliveries"):
            lines += [
                "",
                f"<i>{auto_stats['untracked_deliveries']} earlier auto-slip "
                "delivery record(s) have no saved legs and cannot be graded.</i>",
            ]
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
        return

    status_sym = {"OPEN": "🟡", "GRADED": "✅", "VOID": "❌"}
    lines = ["📓 <b>Slip Journal</b>"]
    if history:
        lines += ["", f"<b>Manual slips</b>  ({len(history)} recent)", ""]
    for slip in history:
        sym   = status_sym.get(slip.status, "❓")
        st    = f"${slip.stake:.2f}" if slip.stake else "—"
        pay   = f"${slip.payout:.2f}" if slip.payout else "—"
        roi   = f"  {slip.roi_pct:+.1f}%" if slip.roi_pct is not None else ""
        ttype = slip.slip_type or "?"
        date  = slip.created_at.strftime("%b %d") if slip.created_at else "?"
        lines.append(f"{sym} <b>{slip.slip_code}</b>  {ttype}  ·  {date}  ·  {st} → {pay}{roi}")

    open_slip = next((s for s in history if s.status == "OPEN"), None)
    if open_slip:
        legs = await _db.get_slip_journal_legs(open_slip.slip_code)
        lines += ["", f"<b>Open: {open_slip.slip_code}</b>  ({len(legs)} leg{'s' if len(legs) != 1 else ''})"]
        for leg in legs:
            sym = {"HIT": "✅", "MISS": "❌", "PUSH": "🔁", "PENDING": "⏳"}.get(leg.result, "⏳")
            _leg_provider = getattr(leg, "provider", None) or "Underdog"
            lines.append(
                f"  {sym} 🎯 PLAYER PROP · {leg.player_name} · "
                f"{leg.direction or ''} {leg.line_value:g} · {leg.stat_type}"
            )

    lines += ["", "<b>Auto-sent combinations</b>"]
    auto_result_sym = {
        "HIT": "✅",
        "MISS": "❌",
        "PUSH": "🔁",
        "PENDING": "⏳",
        "VOID": "⚪",
    }
    if auto_history:
        lines.append("")
        for auto_slip in auto_history:
            outcome = str(auto_slip.get("result") or "PENDING").upper()
            sym = auto_result_sym.get(outcome, "⏳")
            sent_at = auto_slip.get("sent_at")
            date = sent_at.strftime("%b %d") if sent_at else "?"
            size = int(auto_slip.get("slip_size") or 0)
            lines.append(
                f"{sym} <b>{size}-leg</b> · {date} · "
                f"<b>{_html.escape(outcome)}</b>"
            )
            for leg in auto_slip.get("legs", []):
                leg_result = str(leg.get("result") or "PENDING").upper()
                leg_sym = auto_result_sym.get(leg_result, "⏳")
                actual = leg.get("actual_value")
                actual_text = (
                    f" · actual {float(actual):g}" if actual is not None else ""
                )
                player = _html.escape(str(leg.get("player_name") or "Unknown"))
                direction = _html.escape(str(leg.get("direction") or ""))
                stat = _html.escape(str(leg.get("stat_type") or ""))
                line = leg.get("line_value")
                line_text = f"{float(line):g}" if line is not None else "?"
                lines.append(
                    f"  {leg_sym} {player} · {direction} {line_text} "
                    f"{stat}{actual_text}"
                )
        lines += [
            "",
            "<i>HIT = all legs hit · MISS = any leg missed · "
            "PUSH = no misses and at least one push. This is not a payout record.</i>",
        ]
    else:
        lines.append("  <i>No tracked auto-sent combinations yet.</i>")
    if auto_stats.get("untracked_deliveries"):
        lines.append(
            f"<i>{auto_stats['untracked_deliveries']} earlier auto-slip "
            "delivery record(s) have no saved legs and cannot be graded.</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _order_slip_candidates_by_sport(
    entries: list[tuple[PropPickAdapter, Any, Any]],
) -> list[PropPickAdapter]:
    """Rank qualified legs by evidence within each sport, then interleave.

    Hit-rate strength is never compared across sports. Each sport gets its
    own evidence ordering; ties use sample size, game time, and stable identity
    fields so the pair selection is reproducible rather than input-order noise.
    """
    from engine.strongest_pick import (
        directional_hit_rate_strength,
        season_directional_hit_rate_strength,
    )
    from reporting import to_reporting_timezone

    grouped: dict[str, list[tuple[tuple, PropPickAdapter]]] = {}
    for candidate, prop, history in entries:
        sport = str(getattr(prop, "sport", "") or "").strip().upper()
        direction = str(getattr(candidate, "best_side", "") or "").upper()
        ranking_input = {
            "sport": sport,
            "direction": direction,
            "hit_rates": history,
        }
        strength = season_directional_hit_rate_strength(ranking_input)
        if strength is None:
            strength = directional_hit_rate_strength(ranking_input)
        try:
            sample_count = max(0, int(getattr(history, "total_games", 0) or 0))
        except (TypeError, ValueError):
            sample_count = 0
        local_game_time = to_reporting_timezone(
            getattr(candidate, "game_time", None)
        )
        game_order = (
            local_game_time.timestamp()
            if local_game_time is not None
            else float("inf")
        )
        rank_key = (
            0 if strength is not None else 1,
            -(strength if strength is not None else 0.0),
            -sample_count,
            game_order,
            str(getattr(candidate, "player_name", "") or "").casefold(),
            str(getattr(candidate, "stat_type", "") or "").casefold(),
            str(getattr(candidate, "external_id", "") or "").casefold(),
        )
        grouped.setdefault(sport, []).append((rank_key, candidate))

    for sport_entries in grouped.values():
        sport_entries.sort(key=lambda item: item[0])

    # Round-robin preserves sport-level quality ordering without comparing
    # the hit-rate values of different sports.
    ordered: list[PropPickAdapter] = []
    sports = sorted(grouped)
    while True:
        added = False
        for sport in sports:
            if grouped[sport]:
                ordered.append(grouped[sport].pop(0)[1])
                added = True
        if not added:
            return ordered


async def cmd_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/slip — Build correlation-aware prop slips (2–6 legs) from live market props.

    Usage:
      /slip             — show best 2-man through 6-man slips simultaneously
      /slip 3           — show only the 3-man slip
      /slip create [N]  — create a new betting journal slip ($N stake)
      /slip add NAME    — add a pick to the open journal slip
      /slip grade [N]   — grade open slip ($N payout)
      /slip journal     — show slip journal history
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    args = context.args or []

    # ── Journal subcommand dispatcher ────────────────────────────────────────
    _JOURNAL_SUBCMDS = {"create", "add", "grade", "journal", "j", "history"}
    if args and args[0].lower() in _JOURNAL_SUBCMDS:
        try:
            await _bounded(_cmd_slip_journal(update, context, args))
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"{EMOJI['warn']} Slip journal is busy right now. "
                "No report data was changed; please retry."
            )
        return

    single_size: Optional[int] = None
    if args:
        try:
            single_size = int(args[0])
        except (TypeError, ValueError):
            await update.message.reply_text("Usage: /slip [2–6]")
            return
        if single_size < 2 or single_size > 6:
            await update.message.reply_text("Usage: /slip [2–6]")
            return

    from engine.slip_builder import build_all_slips
    from engine.player_prop_market import build_player_prop_market_comparison

    from reporting import reporting_now

    now   = datetime.utcnow()
    today = reporting_now().strftime("%b %d, %Y")

    # ── 1. Fetch provider-owned props (same pool as /picks) ──────────────────
    ud_props = await _report_read(
        _db.get_top_ud_props_for_picks(
            limit=30,
            since_hours=6,
            sports=config.active_sports,
        ),
        fallback=[],
    )
    try:
        _alerted_opps = await _report_read(
            _db.get_current_actionable_opportunities(limit=500),
            fallback=[],
        )
    except Exception:
        _alerted_opps = []
    _canonical_props = [
        _display_prop_from_opportunity(row)
        for row in (_alerted_opps or [])
        if not _is_season_future(getattr(row, "stat_type", ""))
        and _is_active_display_sport(getattr(row, "sport", None))
        and not is_telegram_suppressed_sport(getattr(row, "sport", None))
        and _is_today_reporting_game(getattr(row, "game_time", None))
        and _is_upcoming_game(
            getattr(row, "game_time", None),
            getattr(row, "provider_status", None),
        )
    ]
    # Display filter: hide season-long futures and already-started events
    # (stored & tracked as normal).
    ud_props = [
        p for p in ud_props
        if not _is_season_future(p.stat_type)
        # Direction may be supplied by the fresh snapshot lookup below.
        and _has_current_prop_data(
            p, now, require_direction=False, require_scoring=False
        )
        and _is_active_display_sport(p.sport)
        and not is_telegram_suppressed_sport(p.sport)
        and _is_today_reporting_game(getattr(p, "game_time", None))
        and _is_upcoming_game(
            getattr(p, "game_time", None),
            getattr(p, "provider_status", None),
        )
    ]
    _seen_slip_ids = {
        _pick_source_identity("Underdog", row)
        for row in _canonical_props
    }
    ud_props = _canonical_props + [
        row for row in ud_props
        if _pick_source_identity("Underdog", row) not in _seen_slip_ids
    ]
    # PropLine is internal evidence for Underdog, not a separate Telegram
    # provider. Only Underdog-owned rows enter the slip display.
    provider_props = [("Underdog", p) for p in ud_props]
    if not provider_props:
        await update.message.reply_text(
            "🎯 <b>Sharp Money Prop Slip</b>\n\n"
            "No eligible player props for today's games right now.\n\n"
            "<i>Season-long futures are excluded from this view.\n"
        "Run /picks to check current opportunities.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── 2. Build PropPickAdapter candidates from Underdog rows ───────────────
    _candidates: list[PropPickAdapter] = []
    for provider, plh in provider_props:
        comp = None
        comp = build_player_prop_market_comparison(
            player_name    = plh.player_name,
            sport          = plh.sport,
            stat_type      = plh.stat_type,
            ud_line        = plh.line_value,
            previous_line  = plh.prev_line,
            fetched_at     = plh.fetched_at,
            pp_rows        = [],
            now            = now,
            min_confidence = 0,
        )
        adapter = PropPickAdapter(plh, comp)
        # Populate bet direction from synced PropLineHistory column
        _plh_rec = getattr(plh, "bet_recommendation", None)
        if _plh_rec in ("OVER", "UNDER"):
            adapter.best_side = _plh_rec
        _candidates.append(adapter)

    # Override with fresher provider-owned directions where available.  The
    # generic PropLineHistory row is authoritative for PP; the snapshot table
    # is only an Underdog freshness source.
    try:
        _slip_rec_map = await _report_read(
            _db.get_ud_recommendations_bulk(
                [
                    _ud_recommendation_identity(plh)
                    for provider, plh in provider_props
                    if provider == "Underdog"
                ],
                since_hours=24,
            ),
            fallback={},
        )
        for _cand, (_provider, _plh) in zip(_candidates, provider_props):
            _k = _ud_recommendation_identity(_plh)
            _lr, _lc = _slip_rec_map.get(_k, (None, None))
            if (
                _cand.sportsbook == "Underdog"
                and not _cand.canonical_alert
                and _lr in ("OVER", "UNDER")
            ):
                _cand.best_side = _lr
                setattr(_plh, "bet_recommendation", _lr)
            if _lc is not None:
                setattr(_plh, "bet_confidence", _lc)
    except Exception as _slip_rec_exc:
        logger.debug("cmd_slip: rec_map lookup failed: %s", _slip_rec_exc)

    # Keep current-row validation separate from direction/history eligibility.
    _current_provider_props = []
    _current_candidates = []
    for _cand, (_provider, _plh) in zip(_candidates, provider_props):
        if _is_canonical_alert_prop(_plh) or _has_current_prop_data(
            _plh,
            now,
            require_direction=False,
            require_scoring=False,
        ):
            _current_provider_props.append((_provider, _plh))
            _current_candidates.append(_cand)
    provider_props = _current_provider_props
    _candidates = _current_candidates
    if not provider_props:
        await update.message.reply_text(
            "🎯 <b>Sharp Money Prop Slip</b>\n\n"
            "No eligible player props for today's games right now.\n\n"
            "<i>Season-long futures are excluded from this view.\n"
            "Run /picks to check current opportunities.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Main league sports use complete history; other sports need direction only.
    from engine.delivery_evidence import has_qualifying_directional_pick
    from engine.player_results import compute_hit_rates as _compute_slip_hit_rates

    _slip_result_keys = [
        (
            str(_plh.player_name),
            str(_plh.sport),
            str(_plh.stat_type).lower().strip(),
        )
        for _, _plh in provider_props
    ]
    if hasattr(_db, "get_player_results_bulk"):
        _slip_results_by_key = await _report_read(
            _db.get_player_results_bulk(_slip_result_keys, limit=30),
            fallback={},
            timeout=_PICKS_TIMEOUT_SECONDS,
        )
    else:
        _slip_results_by_key = {}

    _slip_ineligible = 0
    _eligible_slip_entries: list[
        tuple[PropPickAdapter, Any, Any]
    ] = []
    for _cand, (_, _plh), _key in zip(
        _candidates, provider_props, _slip_result_keys
    ):
        _direction = str(
            _cand.best_side
            or getattr(_plh, "bet_recommendation", None)
            or ""
        ).upper()
        _results = _slip_results_by_key.get(_key, [])
        try:
            _history = (
                _compute_slip_hit_rates(_results, float(_plh.line_value))
                if _results
                else None
            )
        except Exception as _history_exc:
            logger.debug(
                "cmd_slip: history computation failed for %s/%s: %s",
                _cand.player_name,
                _cand.stat_type,
                _history_exc,
            )
            _history = None
        if (
            _direction not in ("OVER", "UNDER")
            or not has_qualifying_directional_pick(
                _history,
                _direction,
                sport=_plh.sport,
            )
        ):
            _slip_ineligible += 1
            logger.debug(
                "cmd_slip: excluding %s/%s — no qualifying directional history "
                "(direction=%r)",
                _cand.player_name, _cand.stat_type, _direction,
            )
            continue
        _cand.best_side = _direction
        _eligible_slip_entries.append((_cand, _plh, _history))
    if _slip_ineligible:
        logger.info(
            "cmd_slip: filtered %d ineligible candidates (direction/history threshold); "
            "%d eligible remain",
            _slip_ineligible, len(_eligible_slip_entries),
        )
    _candidates = _order_slip_candidates_by_sport(_eligible_slip_entries)

    _slip_build_capture = getattr(context, "_slip_build_capture", None)
    slips = build_all_slips(
        _candidates,
        max_size=2 if isinstance(_slip_build_capture, dict) else 6,
    )

    if isinstance(_slip_build_capture, dict):
        _slip_build_capture["slips"] = slips
        _slip_build_capture["candidate_count"] = len(_candidates)
        return

    if not slips:
        await update.message.reply_text(
            "🎯 <b>Sharp Money Prop Slip</b>\n\n"
            f"Not enough independent picks to build a slip "
            f"({len(_candidates)} directional prop{'s' if len(_candidates) != 1 else ''} "
            f"after correlation filtering).\n\n"
            f"<i>Check back after the next poll cycle.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if single_size is not None:
        slip = slips.get(single_size)
        if slip is None:
            await update.message.reply_text(
                f"🎯 <b>Sharp Money Prop Slip</b>\n\n"
                f"Could not build a {single_size}-man slip from today's picks.\n"
                f"<i>Available sizes: {', '.join(str(s) for s in sorted(slips))}.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        # A two-leg slip is not automatically safer than the underlying
        # candidates. Do not attach a recommendation label without a separate
        # risk model.
        label = ""
        lines: list[str] = [
            f"🎯 <b>Sharp Money Prop Slips — {today}</b>",
            "",
        ] + _render_slip_section(single_size, slip, label)
    else:
        lines = [
            f"🎯 <b>Sharp Money Prop Slips — {today}</b>",
            f"<i>{len(slips)} slip size{'s' if len(slips) != 1 else ''} built "
            f"from {len(_candidates)} Underdog player props</i>",
            "",
        ]
        for size in sorted(slips):
            label = ""
            lines.extend(_render_slip_section(size, slips[size], label))
            lines.append("")

    lines.append("<i>⚠️ Research tool — not betting advice.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _auto_slip_signature(size: int, slip: object) -> str:
    """Stable identity for a slip; score changes alone do not create a new slip."""
    import hashlib
    import json

    leg_identities = []
    for leg in getattr(slip, "legs", []):
        line = getattr(leg, "pp_line_value", None)
        try:
            line_key = format(float(line), ".8g") if line is not None else None
        except (TypeError, ValueError):
            line_key = str(line)
        game_time = getattr(leg, "game_time", None)
        if hasattr(game_time, "isoformat"):
            game_time = game_time.isoformat()
        leg_identities.append(
            {
                "provider": str(getattr(leg, "sportsbook", "Underdog") or "Underdog")
                .strip()
                .lower(),
                "external_id": str(getattr(leg, "external_id", "") or "").strip(),
                "game_id": str(getattr(leg, "game_id", "") or "").strip(),
                "player": str(getattr(leg, "player_name", "") or "")
                .strip()
                .casefold(),
                "sport": str(getattr(leg, "sport", "") or "").strip().upper(),
                "stat": str(getattr(leg, "stat_type", "") or "")
                .strip()
                .casefold(),
                "direction": str(getattr(leg, "best_side", "") or "")
                .strip()
                .upper(),
                "line": line_key,
                "game_time": str(game_time or ""),
            }
        )
    leg_identities.sort(
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    )
    payload = json.dumps(
        {"size": int(size), "legs": leg_identities},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def auto_slip_notifications_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a newly qualifying two-leg slip after an Underdog scan."""
    if _db is None:
        return
    recipients = sorted(config.allowed_user_ids)
    if not recipients:
        return

    import hashlib
    from types import SimpleNamespace

    async def _discard_command_reply(*_args, **_kwargs) -> None:
        return None

    try:
        # Reuse the same eligibility, freshness, history, and correlation logic
        # as /slip instead of maintaining a second selection pipeline.
        capture: dict = {}
        fake_update = SimpleNamespace(
            source=None,
            effective_user=SimpleNamespace(id=recipients[0]),
            message=SimpleNamespace(reply_text=_discard_command_reply),
        )
        fake_context = SimpleNamespace(args=[], _slip_build_capture=capture)
        await cmd_slip(fake_update, fake_context)
        slips = capture.get("slips") or {}
        if not slips:
            return
    except Exception:
        logger.exception("auto_slip_notifications: slip build failed")
        return

    bot = getattr(getattr(context, "application", None), "bot", None)
    if bot is None:
        logger.warning("auto_slip_notifications: bot client unavailable")
        return

    for chat_id in recipients:
        claimed: list[tuple[str, int, object]] = []
        try:
            for size in (2,):
                slip = slips.get(size)
                if slip is None:
                    continue
                combination_signature = _auto_slip_signature(size, slip)
                leg_payload = [
                    {
                        "provider": getattr(leg, "sportsbook", "Underdog")
                        or "Underdog",
                        "external_id": getattr(leg, "external_id", "") or "",
                        "game_id": getattr(leg, "game_id", "") or "",
                        "player_name": getattr(leg, "player_name", "") or "",
                        "sport": getattr(leg, "sport", "") or "",
                        "stat_type": getattr(leg, "stat_type", "") or "",
                        "line_value": getattr(leg, "pp_line_value", None),
                        "direction": getattr(leg, "best_side", "") or "",
                        "game_time": getattr(leg, "game_time", None),
                    }
                    for leg in getattr(slip, "legs", [])
                ]
                claim_signature = hashlib.sha256(
                    f"{chat_id}:{combination_signature}".encode("utf-8")
                ).hexdigest()
                claimed_now = await _db.claim_auto_slip_notification(
                    claim_signature,
                    size,
                    combination_signature=combination_signature,
                    legs=leg_payload,
                )
                if claimed_now:
                    claimed.append((claim_signature, size, slip))
                else:
                    backfill = getattr(
                        _db, "backfill_auto_slip_notification_legs", None
                    )
                    if callable(backfill):
                        try:
                            await backfill(
                                claim_signature,
                                combination_signature,
                                leg_payload,
                            )
                        except Exception as exc:
                            logger.warning(
                                "auto_slip_notifications: legacy leg backfill "
                                "failed: %s",
                                exc,
                            )
        except Exception:
            for signature, _, _ in claimed:
                try:
                    await _db.release_auto_slip_notification(signature)
                except Exception:
                    logger.exception(
                        "auto_slip_notifications: could not release failed claim"
                    )
            logger.exception("auto_slip_notifications: dedup claim failed")
            continue

        if not claimed:
            continue

        lines = [
            "🔔 <b>Today's qualifying two-pick player-prop slip</b>",
            "",
            "<i>Each leg passed the current direction/history checks.</i>",
            "",
        ]
        for _, size, slip in claimed:
            lines.extend(_render_slip_section(size, slip))
            lines.append("")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            for signature, _, _ in claimed:
                try:
                    await _db.release_auto_slip_notification(signature)
                except Exception:
                    logger.exception(
                        "auto_slip_notifications: could not release failed claim"
                    )
            logger.exception(
                "auto_slip_notifications: Telegram delivery failed for recipient"
            )
            continue

        for signature, size, _ in claimed:
            try:
                marked = await _db.mark_auto_slip_notification_sent(signature)
                if not marked:
                    logger.warning(
                        "auto_slip_notifications: sent %d-leg slip but could not "
                        "mark its dedup record",
                        size,
                    )
            except Exception:
                logger.exception(
                    "auto_slip_notifications: sent %d-leg slip but failed to "
                    "persist its delivery marker",
                    size,
                )


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dashboard — Full performance dashboard: alerts, EV, CLV, sport/market breakdown."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    await update.message.reply_text("⏳ Gathering stats…")

    try:
        from engine.dashboard import DashboardEngine
        report = await _bounded(
            DashboardEngine.gather(_db),
            timeout=_DASHBOARD_TIMEOUT_SECONDS,
        )
        msg = report.to_telegram()
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except asyncio.TimeoutError:
        logger.info(
            "cmd_dashboard: aggregation exceeded %.1fs; returning partial report",
            _DASHBOARD_TIMEOUT_SECONDS,
        )
        # Keep the promised dashboard contract.  A timeout is not permission
        # to substitute a provider-only status message for dashboard data.
        from engine.dashboard import DashboardReport
        report = DashboardReport(
            unavailable={
                "totals", "tracking", "ev", "clv", "sport", "market",
                "daily", "system", "intelligence",
            }
        )
        await update.message.reply_text(
            report.to_telegram(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("cmd_dashboard: DashboardEngine failed: %s", exc)
        from engine.dashboard import DashboardReport
        report = DashboardReport(
            unavailable={
                "totals", "tracking", "ev", "clv", "sport", "market",
                "daily", "system", "intelligence",
            }
        )
        await update.message.reply_text(
            report.to_telegram(),
            parse_mode=ParseMode.HTML,
        )

async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts — Recent player prop alert history."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    lines: list[str] = [
        "🔔 <b>Alert History</b>",
        "",
    ]

    # Readiness belongs to /alerts, not /status. These are independent optional
    # SQLite counters: a transient lock omits this block without delaying or
    # failing the alert-history command.
    _coverage, _directional = await _independent_report_reads(
        [
            _db.get_player_result_coverage_by_sport(
                getattr(config, "ud_alert_sports", frozenset())
            ),
            _db.get_directional_opportunity_counts_by_sport(
                sports=getattr(config, "ud_alert_sports", frozenset())
            ),
        ],
        timeout=_STATUS_DB_READ_TIMEOUT_SECONDS,
    )
    lines.extend(_alert_readiness_lines(_coverage, _directional))
    if len(lines) > 2:
        lines.append("")

    # ── Actionable pick alerts (PropOpportunityLog — canonical delivery source) ─
    # PropOpportunityLog.alert_sent=True is set only when broadcast_alert()
    # successfully delivers a 🎯 ACTIONABLE BET PICK to Telegram.  Using
    # PropLineHistory.lifecycle_state was unreliable because new PropLineHistory
    # rows written by subsequent scans reset that field to None/DISCOVERED.
    lines.append("🎯 <b>Actionable Pick Alerts</b>")
    try:
        # Calendar-day window in the canonical reporting timezone; historical
        # records remain stored in PropOpportunityLog.
        from reporting import current_reporting_day_bounds
        _day_start, _day_end = current_reporting_day_bounds()
        alerted_opps = await _bounded(
            _db.get_alerted_opportunity_log(
                start_at=_day_start,
                end_at=_day_end,
                limit=8,
            )
        )
        if alerted_opps:
            # "sent" = Telegram API accepted the send; no message ID stored.
            lines.append(f"  <i>{len(alerted_opps)} shown (today)</i>")
            lines.append("")
            for r in alerted_opps:
                if is_telegram_suppressed_sport(getattr(r, "sport", None)):
                    continue
                _lv   = float(getattr(r, "line_value", 0) or 0)
                _ts   = getattr(r, "alert_sent_at", None)
                _ts_str = _fmt_user_ts(_ts)
                _rec  = getattr(r, "recommendation", "")
                _rec_icon = "⬆️" if _rec == "OVER" else ("⬇️" if _rec == "UNDER" else "")
                _evidence_label = _evidence_label_from_reason_codes(
                    getattr(r, "reason_codes", None)
                )
                lines.append(
                    f"  🎯 <b>PLAYER PROP PICK</b> · "
                    f"{_rec_icon} <b>{r.player_name}</b>  {r.stat_type}  "
                    f"<code>{_lv:.1f}</code>  ·  <i>{r.sport}</i>"
                    f"  ·  {_ts_str}"
                )
                lines.append(
                    f"      🧾 Evidence basis: <b>{_html.escape(_evidence_label)}</b>"
                )
        else:
            lines.append("  <i>No player prop alerts sent today.</i>")
            lines.append("  <i>New alerts appear here after delivery.</i>")
    except Exception as exc:
        logger.warning("cmd_alerts: prop history lookup failed: %s", exc)
        lines.append(
            "  <i>Alert-history rows are temporarily busy; readiness and the "
            "rest of this screen are still current.</i>"
        )

    _alerts_body = "\n".join(lines)
    try:
        await update.message.reply_text(_alerts_body, parse_mode=ParseMode.HTML)
    except Exception as _send_exc:
        logger.warning("cmd_alerts: HTML send failed (%s), retrying as plain text", _send_exc)
        import re as _re
        await update.message.reply_text(_re.sub(r"<[^>]+>", "", _alerts_body))


async def cmd_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reconcile [hours] — reconcile source volume with Telegram delivery."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        hours = int(context.args[0]) if context.args else 72
        hours = max(1, min(hours, 720))
        report = await _report_read(
            _db.get_telegram_reconciliation(
                since_hours=hours,
                bucket_minutes=60,
            ),
            fallback=None,
            timeout=max(8.0, min(20.0, 4.0 + hours / 8.0)),
        )
        if report is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} Reconciliation is temporarily unavailable. "
                "Live monitoring is unaffected."
            )
            return
        lines = [
            "🔎 <b>Telegram Alert Reconciliation</b>",
            f"<i>UTC window: last {hours} hours · hourly buckets</i>",
            "",
            "<b>Provider populations (current tracking + report window)</b>",
            "<code>Provider       Active   Observed  Evaluated  Directional  "
            "Actionable  Delivered  Pending  Resolved  H/M/P</code>",
        ]
        for provider, row in sorted(report.get("by_provider", {}).items()):
            if str(provider).strip().lower() != "underdog":
                continue
            # Only identity-complete confirmed PLAY audits count as delivered.
            # Legacy/source flags are shown separately as UNKNOWN below.
            delivered = row.get("audit_delivered_alerts", 0)
            lines.append(
                f"<code>{_html.escape(provider[:13]):<13} "
                f"{row.get('tracked', 0):>7,} "
                f"{row.get('observed', 0):>8,} "
                f"{row.get('evaluated', 0):>10,} "
                f"{row.get('directional', row.get('actionable_persisted', 0)):>11,} "
                f"{row.get('actionable_persisted', 0):>10,} "
                f"{delivered:>9,} "
                f"{row.get('pending', 0):>7,} "
                f"{row.get('resolved', 0):>8,} "
                f"{row.get('hit', 0)}/{row.get('miss', 0)}/{row.get('push', 0)}</code>"
            )

        lines += [
            "",
            "<b>Accounting interpretation</b>",
            f"  Durable audit rows: <code>{report.get('audit_rows', 0):,}</code>",
            f"  UNKNOWN/UNVERIFIED deliveries: "
            f"<code>{report.get('unverified_historical', 0):,}</code>",
            f"  Duplicate audited deliveries: "
            f"<code>{report.get('duplicate_delivery_events', 0):,}</code>",
        ]
        if report.get("exact_recipient_count_available"):
            lines.append("  ✅ Recipient counts are available from the delivery ledger.")
        else:
            lines += [
                "  ⚠️ Legacy rows prove a successful send attempt only.",
                "  Telegram message IDs, recipients, and retries were not retained",
                "  before the delivery ledger was added.",
            ]

        buckets = report.get("time_buckets", [])
        if buckets:
            lines += [
                "",
                "<b>Hourly delivered alerts</b>",
                "<code>UTC bucket          Provider       Alerts  Recipients</code>",
            ]
            for bucket in buckets[-24:]:
                bucket_alerts = bucket.get("audit_delivered_alerts", 0)
                bucket_recipients = bucket.get("audit_recipients", 0)
                lines.append(
                    f"<code>{bucket['bucket'][:16]:<18} "
                    f"{_html.escape(bucket['provider'][:13]):<13} "
                    f"{bucket_alerts:>6,} "
                    f"{bucket_recipients:>10,}</code>"
                )

        lines += [
            "",
            "<i>Raw provider observations and funnel evaluations are not Telegram "
            "messages. Current player-prop data remains separate from delivery.</i>",
        ]
        body = "\n".join(lines)
        await update.message.reply_text(body, parse_mode=ParseMode.HTML)
    except (TypeError, ValueError):
        await update.message.reply_text("Usage: /reconcile [hours]")
    except Exception as exc:
        logger.exception("cmd_reconcile: failed: %s", exc)
        await update.message.reply_text("⚠️ /reconcile failed. Check bot logs.")


async def cmd_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grade — Grade resolved actionable picks (WIN/LOSS/PUSH breakdown)."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        return await _bounded(
            _cmd_grade_inner(update, context),
            timeout=_COMMAND_TIMEOUT_SECONDS * _AGGREGATE_REPORT_TIMEOUT_MULTIPLIER,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "cmd_grade: report exceeded %.1fs; returning unavailable state",
            _COMMAND_TIMEOUT_SECONDS * _AGGREGATE_REPORT_TIMEOUT_MULTIPLIER,
        )
        await update.message.reply_text(
            "⚠️ Grade data is temporarily unavailable because the database is busy. "
            "Please retry shortly; monitoring continues.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("cmd_grade: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /grade failed. Check bot logs.")


async def _cmd_grade_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Inner implementation of /grade.

    Source of truth: directional PropOpportunityLog rows, whether or not they
    were delivered to Telegram.  PPEdgeRecord (legacy PP system) is NOT used.

    PropOpportunityLog result values: HIT | MISS | PUSH | PENDING
    Mapped to display:  HIT→W  MISS→L  PUSH/REFUND→P
    """
    summary, tracked_grades = await _independent_report_reads(
        [
            _db.get_directional_performance_summary(
                enabled_sports=config.active_sports
            ),
            _db.get_tracked_prop_grade_summary(),
        ],
        timeout=_COMMAND_TIMEOUT_SECONDS * _AGGREGATE_REPORT_TIMEOUT_MULTIPLIER,
    )
    directional_available = isinstance(summary, dict)
    tracked_available = isinstance(tracked_grades, dict)
    if not directional_available and not tracked_available:
        await update.message.reply_text(
            "⚠️ <b>Sharp Money Pick Grades</b>\n\n"
            "<i>Grade data is UNAVAILABLE; the database queries failed or "
            "timed out. This is not zero.</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    summary = summary if directional_available else {}
    by_sport = summary.get("by_sport", {}) or {}
    by_provider = summary.get("by_provider", {}) or {}
    total_all = sum(int(row.get("directional", 0) or 0) for row in by_sport.values())
    pending_count = sum(int(row.get("pending", 0) or 0) for row in by_sport.values())
    resolved_count = sum(int(row.get("resolved", 0) or 0) for row in by_sport.values())
    overall_w = sum(int(row.get("hit", 0) or 0) for row in by_sport.values())
    overall_l = sum(int(row.get("miss", 0) or 0) for row in by_sport.values())
    overall_p = sum(int(row.get("push", 0) or 0) for row in by_sport.values())
    unverified_count = sum(
        int(row.get("unverified", 0) or 0) for row in by_sport.values()
    )
    denominator = overall_w + overall_l + overall_p
    overall_rate = f"{overall_w / denominator * 100:.0f}%" if denominator else "—"

    lines: list[str] = [
        "📊 <b>Player Prop Grades</b>",
        "",
        *_tracked_prop_grade_lines(tracked_grades),
        "",
    ]

    if not directional_available:
        lines.extend([
            "─ <b>Directional Picks</b> " + "─" * 15,
            "  <i>Directional grade data is UNAVAILABLE; this is not zero.</i>",
        ])
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    lines += [
        "─ <b>Directional Picks (OVER/UNDER)</b> " + "─" * 3,
        f"  Stored directional picks: <b>{total_all}</b>",
        f"  <b>{resolved_count}</b> verified outcomes  "
        f"W:{overall_w}  L:{overall_l}  P:{overall_p}  "
        f"→ hit rate <code>{overall_rate}</code> "
        f"(HIT/(HIT+MISS+PUSH), n={denominator})",
        f"  Pending: <b>{pending_count}</b> · "
        f"Unverified: <b>{unverified_count}</b> (excluded from hit rate)",
        "<i>Only source-verified completed-game outcomes count. "
        "Pushes are included in the hit-rate denominator.</i>",
    ]
    if by_provider:
        lines += ["", "─ <b>By Provider</b> " + "─" * 15]
        for provider, row in sorted(by_provider.items()):
            provider_hit = int(row.get("hit", 0) or 0)
            provider_miss = int(row.get("miss", 0) or 0)
            provider_denom = (
                provider_hit + provider_miss + int(row.get("push", 0) or 0)
            )
            provider_rate = (
                f"{provider_hit / provider_denom * 100:.0f}%"
                if provider_denom else "—"
            )
            lines.append(
                f"  {_html.escape(str(provider))}: "
                f"{provider_hit}H / "
                f"{provider_miss}M / "
                f"{int(row.get('push', 0) or 0)}P · "
                f"{int(row.get('pending', 0) or 0)} pending · "
                f"{int(row.get('unverified', 0) or 0)} unverified · "
                f"{provider_rate} (n={provider_denom})"
            )
    if by_sport:
        lines += ["", "─ <b>By Sport</b> " + "─" * 18]
        for sport, row in sorted(by_sport.items()):
            graded = (
                int(row.get("hit", 0) or 0)
                + int(row.get("miss", 0) or 0)
                + int(row.get("push", 0) or 0)
            )
            rate = f"{int(row.get('hit', 0) or 0) / graded * 100:.0f}%" if graded else "—"
            lines.append(
                f"  {_html.escape(str(sport))}: "
                f"{int(row.get('directional', 0) or 0)} picks · "
                f"{int(row.get('hit', 0) or 0)}H/"
                f"{int(row.get('miss', 0) or 0)}M/"
                f"{int(row.get('push', 0) or 0)}P · "
                f"{int(row.get('pending', 0) or 0)} pending · "
                f"{int(row.get('unverified', 0) or 0)} unverified · "
                f"{rate} (n={graded})"
            )


    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backfill — Run grading without blocking Telegram on a busy database."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return
    try:
        await _bounded(_cmd_backfill_inner(update, context))
    except asyncio.TimeoutError:
        logger.warning(
            "cmd_backfill: grading exceeded %.1fs; background cleanup may continue",
            _COMMAND_TIMEOUT_SECONDS,
        )
        await update.message.reply_text(
            "⚠️ Backfill is still working or temporarily blocked by the database. "
            "The command stopped waiting; retry /backfill shortly and use /rollups "
            "to inspect completed grading.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("cmd_backfill: unexpected error: %s", exc)
        await update.message.reply_text(
            "⚠️ /backfill is temporarily unavailable. Monitoring continues.",
            parse_mode=ParseMode.HTML,
        )


async def _cmd_backfill_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backfill — Fetch latest stats for all pending opportunities and grade them."""
    await update.message.reply_text("⏳ Running backfill grading pass…")

    try:
        from providers.player_stats import PlayerStatsProvider
        from engine.calibration import classify_miss

        provider = PlayerStatsProvider()
        try:
            from providers.player_history import get_player_history_provider
            propline_history = get_player_history_provider()
        except Exception:
            propline_history = None
        propline_results_cache: dict[tuple[str, str, str], list] = {}

        def _is_underdog_opportunity(opp) -> bool:
            provider_name = str(
                getattr(opp, "provider", None) or ""
            ).strip().casefold()
            return not provider_name or provider_name == "underdog"

        def _is_valid_underdog_settlement(opp, result_row, game_date) -> bool:
            if not is_official_underdog_settlement_source(
                getattr(result_row, "source", "")
            ):
                return False
            event_id = str(getattr(opp, "game_id", "") or "").strip()
            if event_id:
                return str(getattr(result_row, "game_id", "") or "").strip() == event_id
            return str(getattr(result_row, "game_date", "") or "") == game_date

        async def _fetch_propline_results_for_grading(opp) -> list:
            """Fetch resolved event history only when primary grading misses."""
            if propline_history is None:
                return []
            key = (
                str(opp.player_name),
                str(opp.sport).upper(),
                str(opp.stat_type or "").lower().strip(),
            )
            if key not in propline_results_cache:
                try:
                    propline_results_cache[key] = list(
                        await propline_history.fetch_resolved_results(
                            opp.player_name,
                            opp.sport,
                            opp.stat_type,
                            source_provider=(
                                getattr(opp, "provider", None)
                                or getattr(opp, "source_provider", None)
                                or "Underdog"
                            ),
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        "backfill: PropLine settlement lookup failed for %s/%s: %s",
                        opp.player_name,
                        opp.stat_type,
                        exc,
                    )
                    propline_results_cache[key] = []
            return propline_results_cache[key]

        # All PENDING opportunities whose game finished at least 30 minutes ago.
        pending = await _db.get_pending_opportunities(cutoff_hours=0.5)

        if not pending:
            await update.message.reply_text(
                "✅ No pending opportunities eligible for grading yet.\n"
                "<i>Opportunities become eligible 30 minutes after game_time.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        # ── Step 1: Fetch stats for all unique (player, sport, stat_type) ────────
        fetched_keys:  set   = set()
        players_found: int   = 0
        for opp in pending:
            if _is_underdog_opportunity(opp):
                continue
            key = (opp.player_name, opp.sport, opp.stat_type.lower().strip())
            if key in fetched_keys:
                continue
            fetched_keys.add(key)
            try:
                raw = await provider.fetch_results(opp.player_name, opp.sport, opp.stat_type)
                for r in raw:
                    await _db.upsert_player_result(r)
                if raw:
                    players_found += 1
                    logger.info(
                        "backfill: fetched %d results for %s/%s",
                        len(raw), opp.player_name, opp.stat_type,
                    )
            except Exception as exc:
                logger.warning(
                    "backfill: fetch failed for %s/%s: %s",
                    opp.player_name, opp.stat_type, exc,
                )

        # ── Step 2: Grade every pending opportunity that now has result data ──────
        graded:  int = 0
        no_data: int = 0
        hits:    int = 0
        misses:  int = 0
        pushes:  int = 0
        for opp in pending:
            if not opp.game_time:
                continue
            game_date  = opp.game_time.strftime("%Y-%m-%d")
            _result_lookup_kwargs = {}
            if getattr(opp, "game_id", ""):
                _result_lookup_kwargs["game_id"] = opp.game_id
            _result_lookup_kwargs["provider"] = (
                getattr(opp, "provider", None) or "Underdog"
            )
            result_row = await _db.get_game_result_for_grading(
                opp.player_name, opp.sport, opp.stat_type, game_date,
                **_result_lookup_kwargs,
            )
            if (
                result_row is not None
                and (
                    not is_gradeable_player_result_source(
                        getattr(opp, "provider", None) or "Underdog",
                        getattr(result_row, "source", ""),
                    )
                    or (
                        _is_underdog_opportunity(opp)
                        and not _is_valid_underdog_settlement(
                            opp, result_row, game_date
                        )
                    )
                )
            ):
                result_row = None
            if result_row is None:
                for _propline_result in await _fetch_propline_results_for_grading(opp):
                    await _db.upsert_player_result(_propline_result)
                result_row = await _db.get_game_result_for_grading(
                    opp.player_name,
                    opp.sport,
                    opp.stat_type,
                    game_date,
                    **_result_lookup_kwargs,
                )
                if (
                    result_row is not None
                    and (
                        not is_gradeable_player_result_source(
                            getattr(opp, "provider", None) or "Underdog",
                            getattr(result_row, "source", ""),
                        )
                        or (
                            _is_underdog_opportunity(opp)
                            and not _is_valid_underdog_settlement(
                                opp, result_row, game_date
                            )
                        )
                    )
                ):
                    result_row = None
            if result_row is None:
                no_data += 1
                continue
            actual    = result_row.actual_value
            line      = opp.line_value
            _push_tol = 0.01
            if abs(actual - line) < _push_tol:
                outcome = "PUSH"
                pushes += 1
            elif (opp.recommendation or "OVER").upper() == "UNDER":
                outcome = "HIT" if actual < line else "MISS"
                hits += 1 if outcome == "HIT" else 0
                misses += 1 if outcome == "MISS" else 0
            else:
                outcome = "HIT" if actual > line else "MISS"
                hits += 1 if outcome == "HIT" else 0
                misses += 1 if outcome == "MISS" else 0
            error_type: Optional[str] = None
            if outcome == "MISS":
                error_type = classify_miss(
                    recommendation=opp.recommendation,
                    decision_tier=opp.decision_tier,
                    confidence=opp.confidence,
                    actual_value=actual,
                    line_value=opp.line_value,
                )
            await _db.grade_opportunity(
                opp.id,
                outcome,
                actual,
                error_type=error_type,
                grade_result_source=getattr(result_row, "source", None),
            )
            graded += 1

        _hit_rate_denominator = hits + misses + pushes
        hit_rate = (
            f"{hits / _hit_rate_denominator * 100:.0f}% "
            f"(n={_hit_rate_denominator})"
            if _hit_rate_denominator
            else "N/A"
        )
        lines_out = [
            "📊 <b>Backfill Complete</b>",
            "",
            f"  Pending props scanned:  <b>{len(pending)}</b>",
            f"  Unique players fetched: <b>{len(fetched_keys)}</b>",
            f"  Players with new data:  <b>{players_found}</b>",
            "",
            f"  Graded this pass:       <b>{graded}</b>",
            f"  ✅ HIT:    <b>{hits}</b>",
            f"  ❌ MISS:   <b>{misses}</b>",
            f"  ➖ PUSH:   <b>{pushes}</b>",
            f"  Hit rate:               <b>{hit_rate}</b>",
            f"  Still no data:          <b>{no_data}</b>",
            "",
            "<i>Use /rollups to see updated performance metrics.</i>",
        ]
        await update.message.reply_text("\n".join(lines_out), parse_mode=ParseMode.HTML)

    except asyncio.CancelledError:
        # The outer bounded wrapper intentionally stops waiting without
        # waiting for slow SQLite cancellation cleanup.
        raise
    except Exception as exc:
        logger.exception("cmd_backfill inner error: %s", exc)
        raise


async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/providers — Show status of every sport data provider."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    try:
        import os
        try:
            from providers import get_health_monitor
            _mon = get_health_monitor()
        except Exception:
            _mon = None
        try:
            from engine.health import get_health_tracker
            _ht_pipeline = get_health_tracker()
        except Exception:
            _ht_pipeline = None

        lines: list[str] = [
            "🔌 <b>Provider Status</b>",
            "",
            "<b>Player Prop Market Providers</b>",
            *_primary_player_prop_provider_lines(_mon),
            *_primary_provider_runtime_lines(_mon),
            *_prizepicks_feed_count_lines(_ht_pipeline),
            "",
            "<b>Historical Enrichment by Sport</b>",
        ]

        # Per-sport provider details
        providers_info = [
            ("MLB",    "⚾", "MLB Stats API",    "statsapi.mlb.com",       True,            "free — no key"),
            ("WNBA",   "🏀", "ESPN gamelog",      "espn.com/api",           True,            "free — no key"),
            ("DOTA",   "🎮", "OpenDota API",      "api.opendota.com",       True,            "free — no key"),
            ("TENNIS", "🎾", "JeffSackmann CSV", "github.com/JeffSackmann", True,            "free — no key"),
        ]

        ud_sports = config.ud_alert_sports

        for sport, icon, provider_name, host, active, note in providers_info:
            in_scope  = sport in ud_sports or (sport == "CS2" and "CS" in ud_sports)
            scope_tag = "✅" if (in_scope and active) else ("⚠️" if in_scope else "⏸️")
            lines.append(
                f"  {scope_tag} {icon} <b>{sport:<6}</b>  {provider_name}\n"
                f"         <i>{note}  ·  {host}</i>"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_providers: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /providers failed. Check bot logs.")


async def cmd_propdata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/propdata event|player — read-only PropLine analytics data.

    Examples:
      /propdata event MLB EVENT_ID player_hits
      /propdata player MLB Shohei Ohtani player_hits
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    usage = (
        "Usage:\n"
        "/propdata event SPORT EVENT_ID [MARKET]\n"
        "/propdata player SPORT PLAYER_NAME MARKET\n\n"
        "Returns resolved props, actual player stats, and historical line movement "
        "from the authorized PropLine API."
    )
    if not args or args[0].lower() not in {"event", "player"}:
        await update.message.reply_text(usage)
        return

    mode = args[0].lower()
    if mode == "event":
        if len(args) < 3:
            await update.message.reply_text(usage)
            return
        sport, event_id = args[1:3]
        market = args[3] if len(args) >= 4 else ""
        request = ("event", sport, event_id, market)
    else:
        if len(args) < 4:
            await update.message.reply_text(usage)
            return
        sport = args[1]
        market = args[-1]
        player_name = " ".join(args[2:-1])
        if not player_name:
            await update.message.reply_text(usage)
            return
        request = ("player", sport, player_name, market)

    try:
        from propline_analytics import PropLineAnalytics

        analytics = PropLineAnalytics.from_environment()
        if mode == "event":
            _, sport, event_id, market = request
            payload = await _bounded(
                analytics.event_bundle(sport, event_id, markets=market),
                timeout=12.0,
            )
        else:
            _, sport, player_name, market = request
            payload = await _bounded(
                analytics.player_bundle(sport, player_name, market),
                timeout=12.0,
            )
        rendered = _json.dumps(payload, indent=2, default=str)
        if len(rendered) > 3800:
            rendered = (
                rendered[:3800]
                + "\n... truncated; use the Python facade for the full payload"
            )
        await update.message.reply_text(
            f"<pre>{_html.escape(rendered)}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⚠️ PropLine analytics request timed out. The provider remains read-only; "
            "retry after its request window."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_propdata failed: %s", exc, exc_info=True)
        await update.message.reply_text(
            "⚠️ PropLine analytics unavailable. Check provider configuration and logs."
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — Alert generation stats, outcomes tracked, performance summary."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        # Each metric is optional.  A locked history query must not hide the
        # provider counts or make the entire Telegram command fail.
        ud_today, total_ud, tracking = await _independent_report_reads([
            _db.count_today_actionable_alerts(),
            _db.count_underdog_records(),
            _db.get_tracking_summary(),
        ])
        tracked_observations = (
            int(
                tracking.get(
                    "historical_observations",
                    tracking.get("tracked_observations", tracking.get("total", 0)),
                )
                or 0
            )
            if isinstance(tracking, dict)
            else None
        )

        lines: list[str] = [
            f"📈 <b>Pick Stats</b>  ·  Uptime: {_uptime_str()}",
            "",
            "<b>Today's Actionable Picks</b>",
            f"  Underdog:    <b>{ud_today if ud_today is not None else 'unavailable'}</b>",
            "",
            "<b>Reporting populations</b>",
        ]

        lines.append(
            "  Historical observations: <b>"
            + (
                f"{tracked_observations:,}"
                if tracked_observations is not None
                else "unavailable"
            )
            + "</b>"
        )
        # Preserve the familiar label for older clients/tests while the
        # explicit Historical observations label remains the canonical one.
        lines.append(
            "  Tracked observations: <b>"
            + (
                f"{tracked_observations:,}"
                if tracked_observations is not None
                else "unavailable"
            )
            + "</b>"
        )
        if isinstance(tracking, dict):
            lines.append(
                "  Active tracked props: <b>"
                f"{int(tracking.get('active_tracked', tracking.get('tracked', 0)) or 0):,}</b>"
            )
        if total_ud is not None:
            # Legacy metric retained as a clearly labelled provider snapshot
            # count; it is not used as the normalized tracking denominator.
            lines.append(f"  {int(total_ud):,} Underdog snapshots (legacy)")

        tracking_counts = tracking.get("counts", {}) if isinstance(tracking, dict) else {}
        directional_total = (
            int(tracking.get("directional_total", tracking.get("total", 0)) or 0)
            if isinstance(tracking, dict)
            else None
        )
        delivered = (
            int(tracking.get("delivered", 0) or 0)
            if isinstance(tracking, dict)
            else None
        )
        pending = (
            int(tracking.get("pending", 0) or 0)
            if isinstance(tracking, dict)
            else None
        )
        resolved_total = (
            int(tracking.get("resolved", 0) or 0)
            if isinstance(tracking, dict) and "resolved" in tracking
            else None
        )

        lines += [
            "",
            "<b>Directional populations (all-time)</b>",
            f"  Directional opportunities: <b>{directional_total if directional_total is not None else 'unavailable'}</b>",
            f"  Delivered alerts:         <b>{delivered if delivered is not None else 'unavailable'}</b>",
            f"  Pending grading:           <b>{pending if pending is not None else 'unavailable'}</b>",
            f"  Resolved outcomes:         <b>{resolved_total if resolved_total is not None else 'unavailable'}</b>",
        ]

        if tracking_counts:
            wins = (
                tracking_counts.get("OVER", {}).get("HIT", 0)
                + tracking_counts.get("UNDER", {}).get("HIT", 0)
            )
            losses = (
                tracking_counts.get("OVER", {}).get("MISS", 0)
                + tracking_counts.get("UNDER", {}).get("MISS", 0)
            )
            pushes = (
                tracking_counts.get("OVER", {}).get("PUSH", 0)
                + tracking_counts.get("UNDER", {}).get("PUSH", 0)
            )
            total_res = wins + losses + pushes
            hit_rate = wins / total_res * 100 if total_res > 0 else 0.0
            lines += [
                f"  Resolved:    <b>{total_res}</b>  W:{wins}  L:{losses}  P:{pushes}",
                f"  Hit rate:    <code>{hit_rate:.0f}%</code>",
            ]
        elif isinstance(tracking, dict):
            lines.append("  <i>Result breakdown unavailable right now.</i>")

        if isinstance(tracking, dict):
            lines.append("")
            lines.append("<b>Provider populations</b>")
            _active = tracking.get("active_by_provider", {}) or {}
            _observed = tracking.get("historical_observations_by_provider", tracking.get(
                "tracked_observations_by_provider", {}
            )) or {}
            _directional = tracking.get("directional_by_provider", {}) or {}
            _delivered = tracking.get("delivered_by_provider", tracking.get(
                "provider_delivered", {}
            )) or {}
            _pending = tracking.get("pending_by_provider", {}) or {}
            _resolved = tracking.get("resolved_by_provider", {}) or {}
            _providers = set(_active) | set(_observed) | set(_directional) | set(_delivered)
            for provider in sorted(_providers):
                if str(provider).strip().lower() != "underdog":
                    continue
                result_counts = (tracking.get("result_counts_by_provider", {}) or {}).get(provider, {})
                lines.append(
                    f"  <b>{_html.escape(provider)}:</b> "
                    f"active {int(_active.get(provider, 0) or 0):,} · "
                    f"observed {int(_observed.get(provider, 0) or 0):,} · "
                    f"directional {int(_directional.get(provider, 0) or 0):,} · "
                    f"delivered {int(_delivered.get(provider, 0) or 0):,} · "
                    f"pending {int(_pending.get(provider, 0) or 0):,} · "
                    f"resolved {int(_resolved.get(provider, 0) or 0):,} · "
                    f"H/M/P {int(result_counts.get('HIT', 0) or 0)}/"
                    f"{int(result_counts.get('MISS', 0) or 0)}/"
                    f"{int(result_counts.get('PUSH', 0) or 0)}"
                )

        lines += [
            "",
            "<i>Active tracked props are the current non-removed provider universe. "
            "Historical observations, directional opportunities, deliveries, and grading "
            "are reported as separate populations.</i>",
        ]

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_stats: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /stats failed. Check bot logs.")


async def cmd_calibration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/calibration — Model calibration: tier accuracy, CLV, detection vs recommendation."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    await update.message.reply_text(
        "⏳ Computing calibration metrics…", parse_mode=ParseMode.HTML
    )

    try:
        from engine.calibration import CalibrationEngine
        engine = CalibrationEngine()
        report = await _bounded(engine.compute(_db))
        await update.message.reply_text(report.to_telegram(), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_calibration error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Calibration failed: {exc}", parse_mode=ParseMode.HTML
        )


async def cmd_pp_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pp_import — Manually import PrizePicks prop data and feed PropLineHistory.

    Format (one prop per line after the command):
        PLAYER | STAT | LINE | SPORT
        PLAYER | STAT | LINE | SPORT | removed

    Example:
        /pp_import
        LeBron James | Points | 25.5 | NBA
        Mike Trout | Hits | 1.5 | MLB
        Patrick Mahomes | Pass Yards | 275.5 | NFL | removed
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
      return await _cmd_pp_import_inner(update, context)
    except Exception as exc:
        logger.exception("cmd_pp_import: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /pp_import failed. Check bot logs.")


async def _cmd_pp_import_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inner implementation of /pp_import — wrapped by cmd_pp_import try/except."""
    # Extract text after the command line
    raw_text = update.message.text or ""
    lines_raw = [ln.strip() for ln in raw_text.split("\n")]
    # Drop the command line itself (first line)
    prop_lines = [ln for ln in lines_raw[1:] if ln and not ln.startswith("/")]

    if not prop_lines:
        await update.message.reply_text(
            "ℹ️ <b>Usage:</b>\n"
            "<code>/pp_import\n"
            "LeBron James | Points | 25.5 | NBA\n"
            "Mike Trout | Hits | 1.5 | MLB\n"
            "Patrick Mahomes | Pass Yards | 275.5 | NFL | removed</code>\n\n"
            "<i>Supported lifecycle markers: <code>removed</code></i>",
            parse_mode=ParseMode.HTML,
        )
        return

    from datetime import timezone
    from database import PropLineHistory  # noqa: F401 — type check

    results: list[tuple[str, str, str]] = []  # (player, stat, event)
    errors:  list[str]                  = []

    for raw in prop_lines:
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4:
            errors.append(f"⚠️ Skipped (need at least 4 fields): <code>{raw[:60]}</code>")
            continue

        player_name = parts[0]
        stat_type   = parts[1]
        sport       = parts[3].upper()

        try:
            line_value = float(parts[2].replace(",", ""))
        except (ValueError, IndexError):
            errors.append(f"⚠️ Invalid line value: <code>{raw[:60]}</code>")
            continue

        is_removed = len(parts) >= 5 and "removed" in parts[4].lower()

        try:
            _, event = await _db.upsert_prop_line_lifecycle(
                provider    = "PrizePicks",
                player_name = player_name,
                sport       = sport,
                stat_type   = stat_type,
                line_value  = line_value,
                removed     = is_removed,
                fetched_at  = datetime.now(timezone.utc).replace(tzinfo=None),
            )
            results.append((player_name, stat_type, event))
        except Exception as exc:
            errors.append(f"⚠️ Error for <code>{player_name}</code>: {exc}")

    if not results and not errors:
        await update.message.reply_text("⚠️ No valid props found in input.")
        return

    # Build summary grouped by lifecycle event
    event_emoji = {
        "ADDED":     "🆕",
        "CHANGED":   "📊",
        "REMOVED":   "❌",
        "RETURNED":  "↩️",
        "UNCHANGED": "✅",
    }
    event_groups: dict[str, list[str]] = {}
    for player, stat, ev in results:
        event_groups.setdefault(ev, []).append(f"  {player} — {stat}")

    reply_lines = [
        f"📥 <b>PrizePicks Import</b>  ({len(results)} props processed)",
        "",
    ]
    for ev in ("ADDED", "CHANGED", "RETURNED", "REMOVED", "UNCHANGED"):
        if ev in event_groups:
            emoji = event_emoji.get(ev, "•")
            reply_lines.append(f"{emoji} <b>{ev}</b> ({len(event_groups[ev])})")
            reply_lines.extend(event_groups[ev][:10])
            if len(event_groups[ev]) > 10:
                reply_lines.append(f"  … +{len(event_groups[ev]) - 10} more")

    if errors:
        reply_lines.append("")
        reply_lines.extend(errors[:5])

    await update.message.reply_text(
        "\n".join(reply_lines), parse_mode=ParseMode.HTML
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/config — Show active bot configuration (no secrets exposed)."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    ud_sports = ", ".join(sorted(config.ud_alert_sports or [])) or "none"
    try:
        from providers import get_health_monitor
        _config_mon = get_health_monitor()
    except Exception:
        _config_mon = None

    lines: list[str] = [
        "⚙️ <b>Bot Configuration</b>",
        "",
        "<b>Alert Scope</b>",
        f"  Underdog sports:   <code>{ud_sports}</code>",
        f"  Min validation:    <code>{config.UD_VALIDATION_MIN_SAMPLES} snapshots</code>",
        "",
        "<b>Alert Limits</b>",
        f"  Daily UD cap:      <code>{'unlimited' if config.DAILY_UNDERDOG_LIMIT == 0 else config.DAILY_UNDERDOG_LIMIT}</code>",
        "",
        "<b>Player Prop Market Providers</b>",
        *_primary_player_prop_provider_lines(_config_mon),
        *_primary_provider_runtime_lines(_config_mon),
        f"  Coverage: all discovered Underdog player props within pacing/budget",
        "",
        "<b>Historical Enrichment Sources</b>",
        f"  ESPN gamelog:      ✅ active (NBA/NFL/WNBA/MLB/Soccer/NHL)",
        f"  MLB Stats API:     ✅ active",
        f"  NHL API:           ✅ active",
        f"  OpenDota (DOTA):   ✅ active",
        f"  JeffSackmann:      ✅ active (Tennis)",
        f"  Sleeper (NFL):     {'✅ active' if getattr(config, 'UD_SLEEPER_ENABLED', True) else '⏸️ disabled'}",
        "",
        "<b>Poll Intervals</b>",
        f"  Shared Player Props monitor: <code>{config.UNDERDOG_POLL_INTERVAL}s ({config.UNDERDOG_POLL_INTERVAL // 60} min)</code>",
        "  Season check:      <code>retired (PropLine/Underdog feed)</code>",
        "",
        "<i>Secrets are never shown. Restart the bot after changing env vars.</i>",
    ]

    try:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_config: reply_text failed: %s", exc)
        await update.message.reply_text("⚠️ /config failed to send. Check bot logs.")


async def cmd_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tracking — Show persistent monitoring and directional grading results."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not _db:
        await update.message.reply_text("⚠️ Database not ready.")
        return

    try:
        data = await _bounded(
            _db.get_tracking_summary(),
            timeout=_COMMAND_TIMEOUT_SECONDS * _AGGREGATE_REPORT_TIMEOUT_MULTIPLIER,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "cmd_tracking: summary exceeded %.1fs",
            _COMMAND_TIMEOUT_SECONDS * _AGGREGATE_REPORT_TIMEOUT_MULTIPLIER,
        )
        await update.message.reply_text(
            "⚠️ Tracking data is temporarily unavailable because the database is busy. "
            "Please retry shortly.",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as exc:
        logger.exception("cmd_tracking: DB error: %s", exc)
        await update.message.reply_text(
            "⚠️ Tracking data is temporarily unavailable. Please retry shortly.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not isinstance(data, dict):
        logger.warning("cmd_tracking: summary returned an invalid payload")
        await update.message.reply_text(
            "⚠️ Tracking data is temporarily unavailable. Please retry shortly.",
            parse_mode=ParseMode.HTML,
        )
        return

    counts   = data.get("counts", {})
    by_sport = data.get("by_sport", {})
    total    = data.get("total", 0)
    pending  = data.get("pending", 0)
    active_by_sport = data.get("active_by_sport", {}) or {}
    observations_by_sport = data.get("observations_by_sport", {}) or {}

    def _n(d: dict, *keys) -> int:
        """Safely walk nested dict and return int."""
        cur = d
        for k in keys:
            if not isinstance(cur, dict):
                return 0
            cur = cur.get(k, 0)
        return cur if isinstance(cur, int) else 0

    def _hit_rate(hits: int, total_graded: int) -> str:
        if total_graded == 0:
            return "—"
        return f"{hits / total_graded:.1%}"

    lines: list[str] = [
        "📊 <b>Prop Opportunity Tracker</b>",
        f"<i>Historical observations: "
        f"{int(data.get('historical_observations', data.get('tracked_observations', 0)) or 0)}  ·  "
        f"Active tracked props: {int(data.get('active_tracked', data.get('tracked', 0)) or 0)}  ·  "
        f"Directional opportunities: {total}  ·  Pending grading: {pending}</i>",
        "",
    ]

    # Current tracking is shown independently from directional grading. This
    # keeps a sport with observations but no OVER/UNDER rows visible.
    tracking_sports = (
        set(active_by_sport)
        | set(observations_by_sport)
        | set(by_sport)
    )
    if tracking_sports:
        lines += ["<b>Current tracking by sport</b>"]
        for sport in sorted(tracking_sports):
            lines.append(
                f"  <b>{_html.escape(str(sport))}:</b> "
                f"active {int(active_by_sport.get(sport, 0) or 0):,} · "
                f"observations {int(observations_by_sport.get(sport, 0) or 0):,} · "
                f"directional {int((data.get('directional_by_sport', {}) or {}).get(sport, 0) or 0):,}"
            )

    # ── Directional picks (OVER + UNDER) ───────────────────────────────────────
    play_recs = ("OVER", "UNDER")
    play_hit = play_miss = play_push = play_pending = 0
    for rec in play_recs:
        play_hit     += _n(counts, rec, "HIT")
        play_miss    += _n(counts, rec, "MISS")
        play_push    += _n(counts, rec, "PUSH")
        play_pending += _n(counts, rec, "PENDING")
    play_graded = play_hit + play_miss + play_push
    play_unverified = sum(_n(counts, rec, "UNVERIFIED") for rec in play_recs)
    play_void = sum(
        _n(counts, rec, result)
        for rec in play_recs
        for result in ("VOID", "CANCELLED", "INJURY_VOID", "GAME_INTERRUPTED")
    )
    play_total = play_graded + play_pending + play_unverified + play_void

    # Stored HIT/MISS already reflect whether the recorded direction won.
    under_hit_correct  = _n(counts, "UNDER", "HIT")
    over_hit_correct   = _n(counts, "OVER",  "HIT")
    play_correct       = over_hit_correct + under_hit_correct

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "✅ <b>Directional Pick Results</b>",
        f"  Total picks:   <code>{play_total}</code>",
        f"  Graded:        <code>{play_graded}</code>",
    ]
    if play_unverified:
        lines.append(
            f"  Unverified:    <code>{play_unverified}</code> "
            "<i>(excluded from hit rates)</i>"
        )
    if play_graded > 0:
        lines += [
            f"  Correct picks: <code>{play_correct}</code>  "
            f"({_hit_rate(play_correct, play_graded)})",
            f"  ➖ Push:       <code>{play_push}</code>",
            f"  ⏳ Pending:    <code>{play_pending}</code>",
        ]
        if any(_n(counts, "OVER", result) for result in ("HIT", "MISS", "PUSH")):
            o_hit  = _n(counts, "OVER", "HIT")
            o_miss = _n(counts, "OVER", "MISS")
            o_tot  = o_hit + o_miss + _n(counts, "OVER", "PUSH")
            lines.append(
                f"  <i>OVER  picks: {o_hit}/{o_tot} ({_hit_rate(o_hit, o_tot)})</i>"
            )
        if any(_n(counts, "UNDER", result) for result in ("HIT", "MISS", "PUSH")):
            u_hit  = _n(counts, "UNDER", "HIT")
            u_miss = _n(counts, "UNDER", "MISS")
            u_tot  = u_hit + u_miss + _n(counts, "UNDER", "PUSH")
            lines.append(
                f"  <i>UNDER picks: {u_hit}/{u_tot} ({_hit_rate(u_hit, u_tot)})</i>"
            )
    else:
        lines.append("  <i>No graded directional pick results yet.</i>")

    # ── By Sport ──────────────────────────────────────────────────────────────
    if by_sport:
        lines += ["", "━━━━━━━━━━━━━━━━━━", "<b>By Sport (PLAY correct pick rate)</b>"]
        for sport, sport_data in sorted(by_sport.items()):
            s_correct = (
                _n(sport_data, "OVER",  "HIT")
                + _n(sport_data, "UNDER", "HIT")
            )
            s_graded = sum(
                _n(sport_data, rec, res)
                for rec in ("OVER", "UNDER")
                for res in ("HIT", "MISS", "PUSH")
            )
            if s_graded > 0:
                lines.append(
                    f"  <b>{sport}:</b>  {s_correct}/{s_graded} "
                    f"({_hit_rate(s_correct, s_graded)})"
                )

    by_provider = data.get("by_provider", {}) or {}
    _active_by_provider = data.get("active_by_provider", {}) or {}
    _observed_by_provider = data.get(
        "historical_observations_by_provider",
        data.get("tracked_observations_by_provider", {}),
    ) or {}
    _directional_by_provider = data.get("directional_by_provider", {}) or {}
    _delivered_by_provider = data.get(
        "delivered_by_provider", data.get("provider_delivered", {})
    ) or {}
    _pending_by_provider = data.get("pending_by_provider", {}) or {}
    _resolved_by_provider = data.get("resolved_by_provider", {}) or {}
    _provider_names = (
        set(by_provider)
        | set(_active_by_provider)
        | set(_observed_by_provider)
        | set(_directional_by_provider)
    )
    _provider_names = {
        provider for provider in _provider_names
        if str(provider).strip().lower() == "underdog"
    }
    if _provider_names:
        lines += ["", "━━━━━━━━━━━━━━━━━━", "<b>By Provider</b>"]
        for provider in sorted(_provider_names):
            provider_data = by_provider.get(provider, {})
            p_correct = (
                _n(provider_data, "OVER", "HIT")
                + _n(provider_data, "UNDER", "HIT")
            )
            p_resolved = sum(
                _n(provider_data, rec, res)
                for rec in ("OVER", "UNDER")
                for res in ("HIT", "MISS", "PUSH")
            )
            p_pending = sum(
                _n(provider_data, rec, "PENDING")
                for rec in ("OVER", "UNDER")
            )
            lines.append(
                f"  <b>{_html.escape(provider)}:</b> "
                f"active {int(_active_by_provider.get(provider, 0) or 0):,}"
                f" · observed {int(_observed_by_provider.get(provider, 0) or 0):,}"
                f" · directional {int(_directional_by_provider.get(provider, 0) or 0):,}"
                f" · delivered {int(_delivered_by_provider.get(provider, 0) or 0):,}"
                f" · pending {int(_pending_by_provider.get(provider, 0) or 0):,}"
                f" · resolved {int(_resolved_by_provider.get(provider, 0) or 0):,}"
                f" · {p_correct}/{p_resolved} correct"
            )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        "<i>Results graded every 1 h from stored game stats.</i>",
        "<i>HIT means the recorded OVER or UNDER pick won; MISS means it lost.</i>",
    ]

    try:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_tracking: send failed: %s", exc)
        await update.message.reply_text("⚠️ /tracking output too large — check bot logs.")


async def cmd_analyst(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/analyst — Show analyst assessment for the most recent prop picks."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text("⚠️ Database not ready.")
        return

    await update.message.reply_text("⏳ Gathering analyst evidence…")
    try:
        from engine.analyst import (
            build_analyst_narrative,
            build_historical_market_evidence,
        )
        from engine.candidate import candidate_from_ud_decision

        # Retrieve recent alerted Underdog snapshots with bet recommendations
        snaps = await _bounded(_db.get_recent_underdog_snapshots(limit=5))
        alerted = [
            s for s in snaps
            if (
                getattr(s, "alert_sent", False)
                and getattr(s, "bet_recommendation", None) in ("OVER", "UNDER")
                and _is_upcoming_game(
                    getattr(s, "game_time", None),
                    getattr(s, "provider_status", None),
                )
            )
        ]
        # Prefer the durable analyst payload linked to the opportunity row.
        # The narrative below remains a compatibility presentation layer and
        # is never treated as the source of the stored AI prediction.
        stored_analyst: dict[tuple[str, str, str], object] = {}
        get_alerted = getattr(_db, "get_alerted_opportunity_log", None)
        if callable(get_alerted):
            try:
                for row in await _bounded(get_alerted(since_hours=72, limit=10)):
                    stored_analyst[
                        (
                            str(getattr(row, "player_name", "")).lower(),
                            str(getattr(row, "sport", "")).upper(),
                            str(getattr(row, "stat_type", "")).lower(),
                        )
                    ] = row
            except Exception as exc:
                logger.debug("cmd_analyst: stored advisory lookup unavailable: %s", exc)

        if not alerted:
            await update.message.reply_text(
                "🧠 <b>Analyst Assessment</b>\n\n"
                "<i>No recent alerted picks with bet recommendations found.\n"
                "Analyst assessments are generated when props are alerted.</i>",
                parse_mode="HTML",
            )
            return

        lines: list[str] = [
            "🧠 <b>Recent Analyst Assessments</b>",
            "<i>Evidence layers: quantitative · historical/market</i>",
            "",
        ]
        candidate_rows: list[tuple[object, object]] = []
        for snap in alerted[:3]:
            dec_ns = __import__("types", fromlist=["SimpleNamespace"]).SimpleNamespace(
                confidence    = None,
                decision_tier = None,
                recommendation= getattr(snap, "bet_recommendation", None),
                reason        = getattr(snap, "bet_reason", "") or "",
                hit_rates     = {},
                window_agreement = 0,
            )
            score_ns = __import__("types", fromlist=["SimpleNamespace"]).SimpleNamespace(
                total     = None,
                n_history = 10,
            )
            try:
                c = candidate_from_ud_decision(
                    player_name = snap.player_name,
                    sport       = snap.sport,
                    stat_type   = snap.stat_type,
                    line        = snap.line_value,
                    decision    = dec_ns,
                    score       = score_ns,
                )
                # Attach validation data if available
                import json
                if getattr(snap, "validation_json", None):
                    try:
                        vj = json.loads(snap.validation_json)
                        from dataclasses import replace
                        trace = {**c.decision_trace, "validation": vj}
                        c = replace(c, decision_trace=trace)
                    except Exception:
                        pass

                # Keep the stored quantitative/history snapshot separate from
                # the narrative.  No live data is recalculated here.
                from dataclasses import replace
                history_evidence = build_historical_market_evidence(c)
                c = replace(
                    c,
                    decision_trace={
                        **c.decision_trace,
                        "historical_market_evidence": history_evidence,
                    },
                )
                candidate_rows.append((snap, c))
            except Exception as exc:
                logger.debug("cmd_analyst: skip snap %s: %s", snap.player_name, exc)

        for snap, c in candidate_rows:
            narrative = build_analyst_narrative(c)
            c = c.with_analyst_narrative(narrative)

            lines.append(
                f"<b>{_html.escape(snap.player_name)}</b>  ·  "
                f"{_html.escape(snap.sport)}  ·  "
                f"{_html.escape(snap.stat_type)} <code>{snap.line_value:.1f}</code>"
            )
            lines.append(
                f"Direction: <b>{_html.escape(getattr(snap, 'bet_recommendation', ''))}</b>"
            )
            ai_row = stored_analyst.get(
                (
                    str(getattr(snap, "player_name", "")).lower(),
                    str(getattr(snap, "sport", "")).upper(),
                    str(getattr(snap, "stat_type", "")).lower(),
                )
            )
            if ai_row is not None:
                try:
                    import json
                    ai_payload = json.loads(
                        getattr(ai_row, "analyst_payload_json", None) or "{}"
                    )
                except (TypeError, ValueError):
                    ai_payload = {}
                ai_prediction = str(
                    ai_payload.get("prediction")
                    or getattr(ai_row, "analyst_prediction", None)
                    or "UNAVAILABLE"
                )
                ai_grade = getattr(ai_row, "analyst_grade", None) or "PENDING"
                ai_model = str(
                    ai_payload.get("model_version")
                    or getattr(ai_row, "analyst_model_version", None)
                    or "unavailable"
                )
                lines.append(
                    f"🧠 Stored AI: <b>{_html.escape(ai_prediction)}</b> · "
                    f"grade <b>{_html.escape(str(ai_grade))}</b> · "
                    f"model <code>{_html.escape(ai_model)}</code>"
                )
            else:
                lines.append("🧠 Stored AI: <b>UNAVAILABLE</b>")
            lines.append("")
            lines.append(f"✅ {_html.escape(narrative.recommended_because[:200])}")
            lines.append(f"⚠️ {_html.escape(narrative.risk_because[:200])}")
            lines.append(f"🎯 <b>{_html.escape(narrative.final_recommendation[:200])}</b>")
            lines.append("━━━━━━━━━━━━━━━━━━")

        if len(lines) <= 3:
            lines.append("<i>Could not build analyst assessments for recent picks.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except asyncio.TimeoutError:
        logger.warning("cmd_analyst: report exceeded %.1fs", _COMMAND_TIMEOUT_SECONDS)
        await update.message.reply_text(
            "⚠️ Analyst evidence is temporarily unavailable because the database "
            "or research service is busy. Please retry shortly.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("cmd_analyst failed: %s", exc)
        await update.message.reply_text(
            "⚠️ Analyst evidence is temporarily unavailable. Please retry shortly.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_blocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/blocks — List all active player reliability blocks."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text("⚠️ Database not ready.")
        return

    try:
        from engine.player_block import PlayerBlock, blocks_summary_telegram

        db_records = await _report_read(_db.get_active_blocks(), fallback=[])
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        blocks = []
        for rec in db_records:
            try:
                b = PlayerBlock(
                    player_key  = rec.player_key,
                    player_name = rec.player_name,
                    sport       = rec.sport or "",
                    reason_code = rec.reason_code,
                    description = rec.description or "",
                    block_type  = rec.block_type,
                    expires_at  = rec.expires_at,
                    created_at  = rec.created_at or now,
                    review_date = rec.review_date,
                    created_by  = rec.created_by or "system",
                )
                blocks.append(b)
            except Exception as exc:
                logger.debug("cmd_blocks: skip record %s: %s", rec.player_key, exc)

        text = blocks_summary_telegram(blocks)
        text += (
            "\n\n<i>Use /block add &lt;player&gt; &lt;sport&gt; &lt;reason&gt; to add a block.\n"
            "Use /block remove &lt;player&gt; &lt;sport&gt; to remove a block.</i>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as exc:
        logger.exception("cmd_blocks failed: %s", exc)
        await update.message.reply_text("⚠️ /blocks failed. Check bot logs.")


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/block add <player> <sport> <reason> | /block remove <player> [sport]."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text("⚠️ Database not ready.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /block add &lt;player&gt; &lt;sport&gt; &lt;reason&gt;\n"
            "  /block remove &lt;player&gt; [sport]\n\n"
            "Valid reasons: INJURY · MINUTES_RESTRICTION · TENNIS_RETIREMENT · AVAILABILITY\n\n"
            "Example: /block add LeBron James NBA INJURY",
            parse_mode="HTML",
        )
        return

    from engine.player_block import (
        PlayerBlock, BLOCKABLE_REASONS, validate_reason_code,
        reason_code_explanation,
    )
    from engine.identity import player_key as _pk
    from database import PlayerRiskRecord
    from datetime import timedelta

    action = args[0].lower()

    if action == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /block remove <player_key> [sport]")
            return
        pkey   = args[1]
        sport  = args[2].upper() if len(args) >= 3 else ""
        removed = await _db.remove_player_block(pkey, sport)
        if removed:
            await update.message.reply_text(
                f"✅ Block removed for <code>{_html.escape(pkey)}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"ℹ️ No active block found for <code>{_html.escape(pkey)}</code>",
                parse_mode="HTML",
            )
        return

    if action == "add":
        if len(args) < 4:
            await update.message.reply_text(
                "Usage: /block add &lt;player_name&gt; &lt;sport&gt; &lt;reason&gt;\n\n"
                f"Valid reasons: {' · '.join(sorted(BLOCKABLE_REASONS))}",
                parse_mode="HTML",
            )
            return

        reason_code = args[-1].upper()
        sport_code  = args[-2].upper()
        player_name = " ".join(args[1:-2])
        pkey        = _pk(player_name)

        if not validate_reason_code(reason_code):
            await update.message.reply_text(
                f"❌ Invalid reason: <code>{_html.escape(reason_code)}</code>\n\n"
                f"Valid reasons: {' · '.join(sorted(BLOCKABLE_REASONS))}\n\n"
                + "\n".join(
                    f"<b>{r}</b>: {reason_code_explanation(r)}"
                    for r in sorted(BLOCKABLE_REASONS)
                ),
                parse_mode="HTML",
            )
            return

        record = PlayerRiskRecord(
            player_key   = pkey,
            player_name  = player_name,
            sport        = sport_code,
            reason_code  = reason_code,
            description  = f"Manual block via /block command by Telegram admin",
            block_type   = "PERMANENT",
            expires_at   = None,
            review_date  = None,
            created_by   = str(update.effective_user.id if update.effective_user else "admin"),
            is_active    = True,
        )
        try:
            await _db.add_player_block(record)
            await update.message.reply_text(
                f"🚫 Block added:\n"
                f"  Player: <b>{_html.escape(player_name)}</b>\n"
                f"  Sport:  <code>{sport_code}</code>\n"
                f"  Reason: <code>{reason_code}</code>\n"
                f"  Key:    <code>{pkey}</code>\n\n"
                "<i>Use /block remove to remove this block.</i>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.exception("cmd_block add failed: %s", exc)
            await update.message.reply_text(f"⚠️ Failed to add block: {exc}")
        return

    await update.message.reply_text(
        "Unknown action. Use /block add ... or /block remove ...",
    )


async def cmd_refinement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/refinement — Show refinement rules and recent trigger summary."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    try:
        from engine.refinement import get_refinement_engine

        eng = get_refinement_engine()
        text = eng.rules_summary()

        # Evaluate against last-known metrics if available
        note = (
            "\n\n<i>Refinement rules evaluate automatically during /performance and /calibration "
            "commands. Use /performance to see if any rules have fired.</i>"
        )
        await update.message.reply_text(text + note, parse_mode="HTML")

    except Exception as exc:
        logger.exception("cmd_refinement failed: %s", exc)
        await update.message.reply_text("⚠️ /refinement failed. Check bot logs.")


async def cmd_funnel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/funnel — Full pipeline: scanned → filtered → scored → qualified → delivered."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return
    await update.message.reply_text("⏳ Gathering pipeline data…")
    try:
        since_h = 24
        if context.args:
            try:
                since_h = max(1, min(168, int(context.args[0])))
            except ValueError:
                pass

        # Scan-cycle counts are session-scoped for live funnel reporting.
        # Otherwise `/funnel 1` immediately after a restart includes rows
        # written by the previous process and looks like this session already
        # completed several scans.
        _funnel_session_started = None
        try:
            from engine.health import get_health_tracker as _get_ht_session
            _ht_session = _get_ht_session()
            if _ht_session is not None:
                _funnel_session_started = _ht_session.monitoring_session_started()
        except Exception:
            pass

        # Fetch scan pipeline summary (non-fatal: new method may not exist on old schema)
        _scan_result, _summary_result, _observation_result = await _independent_report_reads([
            _db.get_scan_cycle_summary(
                since_hours=since_h,
                session_started_at=_funnel_session_started,
            ),
            _db.get_funnel_summary(since_hours=since_h),
            _db.get_funnel_observation_summary(since_hours=since_h),
        ], timeout=_COMMAND_TIMEOUT_SECONDS * _AGGREGATE_REPORT_TIMEOUT_MULTIPLIER)
        scan_summary: dict = _scan_result if isinstance(_scan_result, dict) else {}
        observation_summary: dict = (
            _observation_result if isinstance(_observation_result, dict) else {}
        )
        if not isinstance(_summary_result, dict):
            # ScanCycleLog is the per-monitoring-cycle source of truth for the
            # pipeline section.  The normalized-history aggregate is larger
            # and can time out while SQLite is draining the current cycle.
            # Do not discard a healthy cycle report just because the optional
            # population aggregate was unavailable.
            if not scan_summary:
                await update.message.reply_text(
                    "⚠️ <b>Scan Pipeline</b>\n\n"
                    "<i>Funnel data is UNAVAILABLE; the database query failed "
                    "or timed out. This is not zero.</i>",
                    parse_mode=ParseMode.HTML,
                )
                return
            logger.warning(
                "cmd_funnel: normalized funnel aggregate unavailable; "
                "rendering ScanCycleLog fallback"
            )
            summary = {
                "since_hours": since_h,
                "evaluated": int(scan_summary.get("analyzed", 0) or 0),
                "directional_total": int(scan_summary.get("qualified", 0) or 0),
                "actionable_total": int(scan_summary.get("qualified", 0) or 0),
                "delivered": int(scan_summary.get("alert_delivered", 0) or 0),
                "pending": 0,
                "resolved": 0,
                "provider_breakdown": {},
                "by_sport": [],
                "source": "scan_cycle_log_fallback",
            }
            # Preserve the first funnel stage even when the heavier population
            # aggregate is unavailable.  Missing fields are explicitly marked
            # partial rather than being interpreted as zero.
            summary.update(observation_summary)
            summary["source"] = "scan_cycle_log_plus_observation_fallback"
        else:
            summary = _summary_result

        tracked   = int(
            summary.get("active_tracked", summary.get("tracked", 0)) or 0
        )
        tracked_observations = int(
            summary.get(
                "historical_observations",
                summary.get("tracked_observations", 0),
            )
            or 0
        )
        evaluated_total = int(summary.get("evaluated", 0) or 0)
        directional_total = int(
            summary.get("directional_total", summary.get("total", 0)) or 0
        )
        # Grading belongs to the universal tracking ledger, not only to
        # directional opportunities. A current feed can have many pending or
        # resolved tracked props while producing zero OVER/UNDER opportunities.
        pending_total = int(
            summary.get("tracked_pending", summary.get("pending", 0)) or 0
        )
        resolved_total = int(
            summary.get("tracked_resolved", summary.get("resolved", 0)) or 0
        )
        tracked_result_counts = (
            summary.get("tracked_result_counts")
            or summary.get("result_counts")
            or {}
        )
        delivered_total = int(summary.get("delivered", 0) or 0)
        evidence_by_source = summary.get("evidence_by_source", {}) or {}
        partial_funnel = summary.get("source") == "scan_cycle_log_plus_observation_fallback"
        observations_available = "observations_by_sport" in summary

        def _fmt_rate(num: int, denom: int) -> str:
            """Format a small percentage with enough precision to stay non-zero."""
            if denom == 0:
                return "—"
            v = num / denom * 100
            if v == 0.0:
                return "0%"
            if v < 0.01:
                return f"{v:.4f}%"
            if v < 0.1:
                return f"{v:.3f}%"
            if v < 1.0:
                return f"{v:.2f}%"
            return f"{v:.1f}%"

        _thick = "━" * 18
        lines: list[str] = [
            _thick,
            "📡 <b>Scan Pipeline</b>",
            _thick,
            "",
        ]
        if partial_funnel:
            lines += [
                "⚠️ <b>Partial funnel report:</b> the heavy population aggregate "
                "was unavailable during this scan.",
                "<i>Observed counts below come from normalized feed history; "
                "missing downstream fields are not zero.</i>",
                "",
            ]

        # ── Section 1: Scan Pipeline (from ScanCycleLog) ─────────────────────────────
        # FPR stats describe the current canonical active by-line universe.
        # Keep this separate from ScanCycleLog.active, which is a cumulative
        # per-scan observation count and must not be presented as the current
        # active-pool denominator.
        fpr_stats: dict = {}
        try:
            from engine.health import get_health_tracker as _get_ht_fpr
            _ht_fpr = _get_ht_fpr()
            if _ht_fpr is not None:
                fpr_stats = _ht_fpr.get_fpr_stats()
        except Exception:
            pass
        # The Sport Funnel must represent actual current FPR processing.
        # PropCandidateLog is a separate candidate/evidence stream and does
        # not receive FPR rows, so it cannot be the source for this metric.
        _fpr_by_sport = fpr_stats.get("fpr_by_sport", {})
        _scan_actionable_by_sport = scan_summary.get("actionable_by_sport", {})
        _scan_removed_by_sport = scan_summary.get("removed_by_sport", {})

        # Reporting is intentionally broader than delivery.  Suppressed or
        # inactive sports still need to be visible so the funnel accounts for
        # the complete monitoring universe.
        def _has_reportable_sport_name(sport: object) -> bool:
            return bool(str(sport or "").strip())

        _breakdown_sports = {
            sport
            for sport in (
                set(_scan_actionable_by_sport) | set(_scan_removed_by_sport)
            )
            if _has_reportable_sport_name(sport)
        }
        by_sport = (
            [
                {
                    "sport": sport,
                    "tracked": _fpr_by_sport.get(sport, 0),
                    "actionable": _scan_actionable_by_sport.get(sport, 0),
                    "removed": _scan_removed_by_sport.get(sport, 0),
                }
                for sport in sorted(
                    {
                        sport
                        for sport in set(_fpr_by_sport) | _breakdown_sports
                        if _has_reportable_sport_name(sport)
                    },
                    key=lambda sport: (
                        -_fpr_by_sport.get(sport, 0),
                        sport,
                    ),
                )
            ]
            if _fpr_by_sport or _breakdown_sports
            # Compatibility fallback for pre-reporting health state. Once an
            # FPR batch has emitted fpr_by_sport, PropCandidateLog is never
            # used for this breakdown.
            else (summary.get("by_sport", []) if not fpr_stats else [])
        )
        # Older callers/tests provide only the legacy `tracked`/`by_sport`
        # shape. Keep its labels available as a compatibility view, while
        # normalized summaries below remain authoritative whenever present.
        has_normalized_funnel = any(
            key in summary
            for key in (
                "active_tracked",
                "historical_observations",
                "tracked_observations",
                "evaluated",
                "directional_total",
                "actionable_total",
                "delivered",
                "pending",
                "resolved",
                "result_counts",
            )
        )

        sc_cycles = int(scan_summary.get("cycles", 0) or 0)
        if sc_cycles > 0:
            sc_fetched   = scan_summary.get("fetched", 0)
            sc_removed   = scan_summary.get("removed", 0)
            sc_active    = scan_summary.get("active", 0)
            sc_unchanged = scan_summary.get("unchanged", 0)
            sc_new       = scan_summary.get("new_props", 0)
            sc_lc        = scan_summary.get("line_changed", 0)
            sc_analyzed  = scan_summary.get("analyzed", 0)
            sc_delivered = scan_summary.get("alert_delivered", 0)
            sc_enrichment_status = str(
                scan_summary.get("enrichment_status", "complete") or "complete"
            )
            sc_enrichment_deferred = int(
                scan_summary.get("enrichment_deferred", 0) or 0
            )
            # A report-window average is not a scan duration.  For one scan it
            # incorrectly reports the entire requested window (for example,
            # "~60min each") even when the bot started only a few minutes ago.
            # Use the observed interval between scan timestamps, and describe
            # a single scan by its age instead of inventing a cadence.
            def _report_dt(value: object) -> Optional[datetime]:
                if isinstance(value, datetime):
                    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
                if not value:
                    return None
                try:
                    text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
                    parsed = datetime.fromisoformat(text)
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    return None

            _first_scan = _report_dt(scan_summary.get("first_scan_ts"))
            _last_scan = _report_dt(scan_summary.get("last_scan_ts"))
            _now_utc = datetime.now(timezone.utc)
            if sc_cycles == 1:
                _scan_age = (
                    max(0.0, (_now_utc - _first_scan).total_seconds() / 60.0)
                    if _first_scan
                    else None
                )
                _scan_timing = (
                    f"1 scan · first scan {_scan_age:.0f}m ago"
                    if _scan_age is not None
                    else "1 scan · cadence pending"
                )
            elif _first_scan and _last_scan:
                _span_minutes = max(
                    0.0,
                    (_last_scan - _first_scan).total_seconds() / 60.0,
                )
                _interval_minutes = _span_minutes / max(sc_cycles - 1, 1)
                _scan_timing = (
                    f"{sc_cycles} scans · ~{_interval_minutes:.1f}min between scans"
                )
            else:
                _scan_timing = f"{sc_cycles} scans · timing unavailable"
            fpr_rescored = fpr_stats.get("fpr_rescored", 0)
            sc_total_scored = sc_analyzed + fpr_rescored
            lines += [
                "",
                 (
                     f"<i>Current session (up to {since_h}h)  ·  {_scan_timing}</i>"
                     if scan_summary.get("session_scoped")
                     else f"<i>Last {since_h}h  ·  {_scan_timing}</i>"
                 ),
                "",
                "📡 <b>Full Monitoring Pipeline</b>",
                "",
                f"📥 Fetched from Underdog:          <b>{sc_fetched:,}</b>",
                f"🚫 Removed from feed:              <b>{sc_removed:,}</b>",
                f"👁 Prop observations (cumulative scan total): <b>{sc_active:,}</b>",
                f"   └─ Not re-alerted / below movement threshold: <b>{sc_unchanged:,}</b>",
                f"   └─ New first-appearance:        <b>{sc_new:,}</b>",
                f"   └─ Line moved (re-scored):      <b>{sc_lc:,}</b>",
                "",
                f"🔬 Scored this period:             <b>{sc_total_scored:,}</b>",
                f"   ├─ New:                         <b>{sc_new:,}</b>",
                f"   ├─ Line moved:                  <b>{sc_lc:,}</b>",
                f"   └─ Full-pool rotation:           <b>{fpr_rescored:,}</b>",
                f"🎯 Qualified directional candidates: <b>{int(scan_summary.get('qualified', 0) or 0):,}</b>",
                f"📤 Delivered to Telegram — current-session scan sends: "
                f"<b>{sc_delivered:,}</b>",
            ]
            if sc_enrichment_status != "complete" or sc_enrichment_deferred:
                lines += [
                    "",
                    "⚠️ <b>Optional evidence enrichment:</b> "
                    f"<b>{_html.escape(sc_enrichment_status)}</b>"
                    + (
                        f" · {sc_enrichment_deferred:,} observations deferred"
                        if sc_enrichment_deferred
                        else ""
                    ),
                    "<i>Complete feed observations were retained; "
                    "directional evidence may be incomplete for this cycle.</i>",
                ]
            # ── Full-Pool Rescan Rotation block ──────────────────────────────────
            try:
                if fpr_stats:
                    _fpr_pool     = fpr_stats.get("pool_size",        0)
                    _fpr_covered  = fpr_stats.get("cursor_end",       0)
                    _fpr_pct      = fpr_stats.get("pct_complete",     0.0)
                    _fpr_rot      = fpr_stats.get("rotation",         1)
                    _fpr_rescored = fpr_stats.get("fpr_rescored",     0)
                    _fpr_sent     = fpr_stats.get("fpr_sent",         0)
                    _fpr_sources  = fpr_stats.get("active_source_ids", _fpr_pool)
                    _fpr_uncovered = fpr_stats.get(
                        "uncovered_props", max(0, _fpr_pool - _fpr_covered)
                    )
                    _fpr_done     = fpr_stats.get("rotation_complete", False)
                    _fpr_next_rot  = fpr_stats.get(
                        "next_rotation", _fpr_rot + (1 if _fpr_done else 0)
                    )
                    _fpr_status   = "✅ complete — next started" if _fpr_done else "🔄 in progress"
                    lines += [
                        "",
                        _thick,
                        "🌐 <b>Full-Pool Rescan Rotation</b>  <i>(last batch)</i>",
                        _thick,
                        "",
                        f"Rotation:           <b>#{_fpr_rot}</b>  ({_fpr_status})",
                        f"Next rotation:      <b>#{_fpr_next_rot}</b>",
                        f"Progress:           <b>{_fpr_pct:.1f}%</b>",
                        f"Coverage:           <b>{_fpr_covered:,} / {_fpr_pool:,}</b> active props",
                        f"Active source IDs:  <b>{_fpr_sources:,}</b>",
                        f"Uncovered remainder: <b>{_fpr_uncovered:,}</b>",
                        f"🔬 Rescored:        <b>{_fpr_rescored:,}</b>",
                        f"📤 Sent:            <b>{_fpr_sent:,}</b>",
                    ]
                    if not has_normalized_funnel:
                        lines += [
                            f"Canonical active props: <b>{int(_fpr_pool or 0):,}</b>",
                            f"Candidate records tracked: <b>{int(summary.get('tracked', 0) or 0):,}</b>",
                        ]
            except Exception:
                pass
        else:
            # No completed scan cycles yet — show job status from HealthTracker
            _scan_status = "awaiting first scan cycle"
            try:
                from engine.health import get_health_tracker as _get_ht_no
                _ht_no = _get_ht_no()
                if _ht_no is not None:
                    _uj = _ht_no.get_job_info("underdog_job")
                    if _uj.get("last_run_ts"):
                        # Job completed at least once but no ScanCycleLog exists —
                        # log write may have failed silently
                        _scan_status = "scan completed but log not recorded (check bot logs)"
                    elif _uj.get("started_at") or _uj.get("last_fail_ts"):
                        _scan_status = "⏳ first scan in progress"
            except Exception:
                pass
            lines += [
                "",
                f"<i>Last {since_h}h  ·  {_scan_status}.</i>",
                "<i>Scan cycle metrics appear here once the first scan completes.</i>",
            ]

        # ── Section 2: Reporting populations ───────────────────────────────────
        # The normalized history is the source of truth for current tracking.
        # Candidate rows are evaluations, not tracked props.
        active = tracked
        # Include the full-pool rotation in the aggregate evaluation count.
        # FPR evaluates unchanged and previously handled active props too; it
        # must not disappear from the funnel merely because it is persisted in
        # HealthTracker rather than ScanCycleLog.
        sc_rescored = (
            scan_summary.get("analyzed", 0)
            + fpr_stats.get("fpr_rescored", 0)
        )
        sc_movers = scan_summary.get("line_changed", 0)
        sc_delivered = scan_summary.get("alert_delivered", 0)
        lines += [
            "",
            _thick,
             "🔭 <b>Tracking &amp; Grading Funnel</b>",
            _thick,
            "",
             f"<i>Scan = feed observations/evaluations. Tracking, delivery, and grading use the "
             f"same durable opportunity records; they are stages, not a second alert path.</i>",
            "",
            f"📥 Active tracked props: <b>{active:,}</b>",
            f"🧾 Historical observations: <b>{tracked_observations:,}</b>",
            f"🔬 Evaluated candidates: <b>{evaluated_total:,}</b>",
            f"🎯 Directional OVER/UNDER opportunities: <b>{directional_total:,}</b>",
            f"📌 Persisted directional records (not send-approved): "
            f"<b>{int(summary.get('actionable_total', directional_total) or 0):,}</b>",
            (
                "   └─ Evidence basis: "
                f"settled results <b>{int(evidence_by_source.get('settled_results', 0) or 0):,}</b> · "
                f"historical-average fallback <b>{int(evidence_by_source.get('historical_average', 0) or 0):,}</b> · "
                f"unavailable <b>{int(evidence_by_source.get('unavailable', 0) or 0):,}</b>"
            ),
            f"🔬 Rescored: <b>{sc_rescored:,}</b>",
            f"📈 Line movers: <b>{sc_movers:,}</b>",
            f"📤 Confirmed Telegram deliveries (last {since_h}h): "
            f"<b>{delivered_total:,}</b>",
            f"⏳ Pending grading (tracked props): <b>{pending_total:,}</b>",
            f"🏁 Resolved tracked outcomes: <b>{resolved_total:,}</b>",
            f"   └─ HIT / MISS / PUSH: <b>{int(tracked_result_counts.get('HIT', 0) or 0):,} / "
            f"{int(tracked_result_counts.get('MISS', 0) or 0):,} / "
            f"{int(tracked_result_counts.get('PUSH', 0) or 0):,}</b>",
            "",
        ]
        if not has_normalized_funnel:
            lines.insert(
                -1,
                f"Candidate records tracked: <b>{int(summary.get('tracked', 0) or 0):,}</b>",
            )

        # Per-sport breakdown
        normalized_by_sport = summary.get("active_by_sport", {}) or {}
        normalized_directional_by_sport = summary.get("directional_by_sport", {}) or {}
        normalized_delivered_by_sport = summary.get("delivered_by_sport", {}) or {}
        if normalized_by_sport or normalized_directional_by_sport or by_sport:
            lines += [
                "",
                "─" * 16,
                "",
                "⚽ <b>Sport Funnel Breakdown</b>",
                "",
            ]
            if not has_normalized_funnel:
                # Legacy health/candidate shape: preserve the old action and
                # removal columns for mocked or pre-normalized responses.
                lines += [
                    "",
                    "<code>Sport        Active   Action  Removed</code>",
                    "<code>──────────  -------  ------  -------</code>",
                ]
                for row in by_sport:
                    sport = row.get("sport")
                    if not _has_reportable_sport_name(sport):
                        continue
                    sp = _html.escape(str(sport)[:10])
                    lines.append(
                        f"<code>{sp:<10}  "
                        f"{int(row.get('tracked', 0) or 0):>7}  "
                        f"{int(row.get('actionable', 0) or 0):>6}  "
                        f"{int(row.get('removed', 0) or 0):>7}</code>"
                    )
            else:
                _sport_table_lines = [
                    f"{'Sport':<6}{'Obs':>7}{'Active':>7}{'Eval':>7}{'Dir':>7}{'Del':>7}",
                    f"{'------':<6}{'-------':>7}{'-------':>7}{'-------':>7}{'-------':>7}{'-------':>7}",
                ]
                normalized_observed_by_sport = (
                    summary.get("observations_by_sport", {}) or {}
                )
                normalized_evaluated_by_sport = (
                    summary.get("evaluated_by_sport", {}) or {}
                )
                # A live session can have FPR sport telemetry while the
                # normalized history aggregate has no per-sport active rows
                # yet (for example, immediately after a restart or when the
                # compatibility candidate fallback supplied the aggregate
                # totals).  Keep those rows visible instead of rendering an
                # empty table beneath populated pipeline totals.
                legacy_by_sport = {
                    str(row.get("sport") or "").strip(): row
                    for row in (by_sport or [])
                    if isinstance(row, dict)
                    and _has_reportable_sport_name(row.get("sport"))
                }
                _sports = (
                    set(normalized_by_sport)
                    | set(normalized_observed_by_sport)
                    | set(normalized_evaluated_by_sport)
                    | set(normalized_directional_by_sport)
                    | set(normalized_delivered_by_sport)
                    | set(_fpr_by_sport)
                    | set(legacy_by_sport)
                )
                for sport in sorted(
                    (sport for sport in _sports if _has_reportable_sport_name(sport)),
                    key=lambda sport: (
                        -int(normalized_by_sport.get(sport, 0) or 0),
                        str(sport).upper(),
                    ),
                ):
                    # Escape dynamic sport name to prevent HTML parse errors
                    sp = _html.escape(str(sport)[:6])
                    legacy_row = legacy_by_sport.get(str(sport), {})
                    sc = normalized_by_sport.get(
                        sport,
                        _fpr_by_sport.get(
                            sport,
                            legacy_row.get("tracked", 0),
                        ),
                    )
                    so = (
                        normalized_observed_by_sport.get(sport, 0)
                        if observations_available
                        else "—"
                    )
                    se = normalized_evaluated_by_sport.get(
                        sport,
                        legacy_row.get(
                            "evaluated",
                            _fpr_by_sport.get(sport, 0),
                        ),
                    )
                    sa = normalized_directional_by_sport.get(sport, 0)
                    sd = normalized_delivered_by_sport.get(sport, 0)
                    _sport_table_lines.append(
                        f"{sp:<6}{str(so):>7}{int(sc or 0):>7}"
                        f"{int(se or 0):>7}{int(sa or 0):>7}{int(sd or 0):>7}"
                    )
                lines += ["", "<pre>" + "\n".join(_sport_table_lines) + "</pre>"]
        else:
            lines += ["", "<i>Sport breakdown accumulates as the bot scans props.</i>"]

        lines.append(f"\n{_thick}")
        # A large all-sport funnel can exceed Telegram's 4096-character limit.
        # Split only at line boundaries so each section remains valid HTML.
        await _send_in_chunks(
            update,
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            sep="\n",
            max_len=3800,
        )

    except Exception as exc:
        logger.exception("cmd_funnel: error: %s", exc)
        await update.message.reply_text("⚠️ Could not load funnel data. Check bot logs.")


async def cmd_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/player NAME — show tracked prop history and hit rate for a player (P11).

    Usage:
      /player LeBron James
      /player Acuña
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/player &lt;player name&gt;</code>\n"
            "Example: <code>/player LeBron James</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name_query = " ".join(args)
    try:
        rows = await _report_read(
            _db.get_player_prop_history(name_query, limit=20),
            fallback=None,
            timeout=8.0,
        )
        if rows is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} Player history is temporarily unavailable. "
                "Please retry shortly."
            )
            return
    except Exception as exc:
        logger.exception("cmd_player: db error: %s", exc)
        await update.message.reply_text(f"{EMOJI['warn']} DB error: {exc}")
        return

    if not rows:
        await update.message.reply_text(
            f"🔍 No tracked picks found matching <b>{_html.escape(name_query)}</b>.\n"
            f"<i>Picks are tracked when the bot generates alerts.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Keep monitoring, directional, delivery, and settlement populations
    # separate. Directional opportunity results already reflect the pick side.
    tracked_rows = [r for r in rows if getattr(r, "tracked_observation", False)]
    directional_rows = [
        r for r in rows
        if str(getattr(r, "recommendation", "") or "").upper() in {"OVER", "UNDER"}
    ]
    delivered_rows = [
        r for r in directional_rows if bool(getattr(r, "alert_sent", False))
    ]
    graded = [
        r for r in directional_rows
        if str(getattr(r, "result", "") or "").upper() in {"HIT", "MISS", "PUSH"}
    ]
    hits = sum(1 for r in graded if str(r.result).upper() == "HIT")
    pushes = sum(1 for r in graded if str(r.result).upper() == "PUSH")
    misses = len(graded) - hits - pushes
    pending = sum(
        1 for r in directional_rows
        if str(getattr(r, "result", "") or "").upper() == "PENDING"
    )
    hit_rate = hits / len(graded) * 100 if graded else None

    # Warning thresholds
    warn = ""
    if hit_rate is not None and len(graded) >= 3:
        if hit_rate < 30:
            warn = "\n⛔ <b>Warning:</b> Very low hit rate — consider fading."
        elif hit_rate < 45:
            warn = "\n⚠️ <b>Caution:</b> Below-average hit rate."
        elif hit_rate >= 70:
            warn = "\n🔥 <b>Hot streak:</b> Strong recent performance."

    # Header
    player_display = _html.escape(rows[0].player_name)
    rate_str = f"{hit_rate:.0f}%" if hit_rate is not None else "—"
    lines = [
        f"👤 <b>Player History — {player_display}</b>",
        "",
        f"Tracked observations: <b>{len(tracked_rows)}</b>",
        f"Directional:          <b>{len(directional_rows)}</b>",
        f"Delivered:            <b>{len(delivered_rows)}</b>",
        f"Graded:               <b>{len(graded)}</b>  ({hits}H / {misses}M / {pushes}P)",
        f"Pending:              <b>{pending}</b>",
        f"Hit rate:             <b>{rate_str}</b>",
    ]
    if warn:
        lines.append(warn)

    # Recent 8 picks
    lines += ["", "<b>Recent picks</b>"]
    result_sym = {"HIT": "✅", "MISS": "❌", "PUSH": "🔁", "PENDING": "⏳"}
    for r in rows[:8]:
        result = str(getattr(r, "result", "PENDING") or "PENDING").upper()
        sym   = result_sym.get(result, "⏳")
        timestamp = getattr(r, "detected_at", None)
        date  = timestamp.strftime("%b %d") if timestamp else "?"
        av    = (
            f" → <i>{r.actual_value:g}</i>"
            if getattr(r, "actual_value", None) is not None else ""
        )
        recommendation = str(getattr(r, "recommendation", "") or "").upper()
        lines.append(
            f"{sym} "
            f"🎯 "
            f"<b>PLAYER PROP {'PICK' if recommendation else 'TRACKED'}</b> · "
            f"<b>{r.stat_type}</b> {recommendation or 'TRACKED'} "
            f"<code>{r.line_value:g}</code>  ·  {r.sport}  ·  {date}{av}"
        )

    lines.append("\n<i>Source: prop_opportunity_log · Player Props combined</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_slipstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/slipstats — Pick accuracy and slip journal performance summary (P12)."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        _grade_auto_slips = getattr(
            _db, "grade_auto_slip_notification_legs", None
        )
        if callable(_grade_auto_slips):
            await _grade_auto_slips()
    except Exception as exc:
        logger.warning("cmd_slipstats: auto-slip settlement refresh failed: %s", exc)

    auto_slip_stats = None
    _auto_stats_reader = getattr(_db, "get_auto_slip_notification_stats", None)
    if callable(_auto_stats_reader):
        try:
            auto_slip_stats = await _auto_stats_reader()
            auto_slip_stats = (
                auto_slip_stats if isinstance(auto_slip_stats, dict) else None
            )
        except Exception as exc:
            logger.warning("cmd_slipstats: auto-slip stats unavailable: %s", exc)

    try:
        slip_stats, sport_rows = await _independent_report_reads([
            _db.get_slip_journal_stats(),
            _db.get_pick_accuracy_by_sport(
                limit_sports=10,
                enabled_sports=config.active_sports,
            ),
        ])
        slip_stats = slip_stats if isinstance(slip_stats, dict) else None
        sport_rows = sport_rows if isinstance(sport_rows, list) else None
        # Pick accuracy per-sport from prop_opportunity_log
    except Exception as exc:
        logger.exception("cmd_slipstats: db error: %s", exc)
        slip_stats = None
        sport_rows = None

    _thick = "━" * 30
    lines = [
        f"📊 <b>Performance Intelligence</b>",
        "",
        f"<b>PICK ACCURACY</b>  (prop_opportunity_log)",
        _thick,
    ]

    if sport_rows:
        for row in sport_rows[:8]:
            sp   = row.get("sport", "?")
            provider = row.get("provider", "Underdog")
            h    = row.get("hits", 0)
            m    = row.get("misses", 0)
            p    = row.get("pushes", 0)
            tot  = h + m + p
            rate = f"{h / tot * 100:.0f}%" if tot > 0 else "—"
            lines.append(
                f"  {provider:<12} {sp:<8} {h}H / {m}M / {p}P  →  {rate}"
            )
    elif sport_rows is None:
        lines.append("  <i>Pick accuracy is temporarily unavailable.</i>")
    else:
        lines.append("  <i>No graded picks yet — data accumulates as games complete.</i>")

    # Slip journal stats
    if slip_stats is None:
        lines += [
            "",
            "<b>SLIP JOURNAL</b>",
            _thick,
            "  <i>Slip journal database error; metrics are temporarily unavailable.</i>",
        ]
    else:
        lines += [
            "",
            f"<b>SLIP JOURNAL</b>  ({slip_stats['total_slips']} graded slips)",
            _thick,
        ]
        by_size = slip_stats.get("by_size", {})
        if by_size:
            for size in sorted(by_size):
                d   = by_size[size]
                w   = d["win"]
                l   = d["loss"]
                tot = w + l
                win_pct = f"{w / tot * 100:.0f}%" if tot > 0 else "—"
                stk = f"${d['staked']:.2f}" if d["staked"] else "—"
                pay = f"${d['payout']:.2f}" if d["payout"] else "—"
                lines.append(
                    f"  {size:<8}  {w}W / {l}L  ({win_pct})  ·  {stk} → {pay}"
                )
        else:
            lines.append("  <i>No graded slips yet. Create one: /slip create</i>")

        # Overall
        if slip_stats["total_staked"] > 0:
            roi_str = (
                f"{slip_stats['overall_roi']:+.1f}%"
                if slip_stats["overall_roi"] is not None
                else "—"
            )
            lines += [
                "",
                f"Total staked:  ${slip_stats['total_staked']:.2f}",
                f"Total payout:  ${slip_stats['total_payout']:.2f}",
                f"Overall ROI:   <b>{roi_str}</b>",
            ]

    lines += ["", "<b>AUTO-SENT SLIPS</b>  (official result evidence)", _thick]
    if auto_slip_stats is None:
        lines.append("  <i>Auto-sent slip results are temporarily unavailable.</i>")
    else:
        auto_by_size = auto_slip_stats.get("by_size", {})
        if auto_by_size:
            for size in sorted(auto_by_size):
                counts = auto_by_size[size]
                hits = int(counts.get("hit", 0))
                misses = int(counts.get("miss", 0))
                pushes = int(counts.get("push", 0))
                pending = int(counts.get("pending", 0))
                voids = int(counts.get("void", 0))
                settled = hits + misses + pushes
                hit_rate = (
                    f"{hits / settled * 100:.0f}% settled hit rate"
                    if settled
                    else "not settled"
                )
                detail = f"{hits}H / {misses}M / {pushes}P"
                if pending:
                    detail += f" · {pending} pending"
                if voids:
                    detail += f" · {voids} void"
                lines.append(f"  {size}-leg: {detail} · {hit_rate}")
        else:
            lines.append("  <i>No auto-sent combinations with saved legs yet.</i>")
        untracked = int(auto_slip_stats.get("untracked_deliveries", 0) or 0)
        if untracked:
            lines.append(
                f"  <i>{untracked} earlier delivery record(s) lack saved legs "
                "and cannot be graded.</i>"
            )
        lines.append(
            "  <i>HIT = every leg hit · MISS = any leg missed · "
            "PUSH = no misses and at least one push. No payout is implied.</i>"
        )

    lines.append(f"\n{_thick}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _is_transient_telegram_read_error(error: object) -> bool:
    """Recognize httpx read failures wrapped by python-telegram-bot."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ReadError):
            return True
        pending.extend(
            cause
            for cause in (
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
            if cause is not None
        )
    return isinstance(error, NetworkError) and "ReadError" in str(error)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors raised by handlers and notify the user if possible."""
    if _is_transient_telegram_read_error(context.error):
        logger.warning(
            "Telegram HTTP read dropped; update handler remains available: %s",
            context.error,
        )
        return
    if isinstance(context.error, Conflict):
        logger.warning(
            "Telegram polling conflict: another process is using this bot "
            "token; stopping this instance",
        )
        application = getattr(context, "application", None)
        stop_running = getattr(application, "stop_running", None)
        if callable(stop_running):
            stop_running()
        return
    logger.error(
        "Update %s caused error: %s",
        update, context.error,
        exc_info=context.error,
    )
    # Try to reply so the user sees a visible failure instead of silence.
    from telegram import Update as _Update
    if isinstance(update, _Update) and update.message:
        try:
            await update.message.reply_text(
                "⚠️ Command failed — please try again. "
                "If the issue persists, the bot may need to restart.",
            )
        except Exception:
            pass
