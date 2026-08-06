"""
Telegram command handlers.

Each handler is a standalone async function registered with the Application.
"""

from __future__ import annotations

import html as _html
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
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
)
from config import config
from database import Database
from engine import AnalysisEngine
from models import MarketType, Sport

logger = logging.getLogger(__name__)

# ── Bot startup time for uptime tracking ──────────────────────────────────────
_START_TIME = time.monotonic()

# ── Module-level references (set by main.py on startup) ───────────────────────
_db: Optional[Database] = None
_engine: Optional[AnalysisEngine] = None
_alert_chat_ids: list[int] = []
_total_alerts_sent: int = 0
# SeasonChecker instance passed from main.py — typed as object to avoid a
# circular import; duck-typed at call sites via hasattr checks.
_season_checker: Optional[object] = None


def init_handlers(
    db: Database,
    analysis_engine: AnalysisEngine,
    alert_chat_ids: list[int],
    season_checker: Optional[object] = None,
) -> None:
    """Call this once from main.py after the database and engine are ready."""
    global _db, _engine, _alert_chat_ids, _season_checker
    _db             = db
    _engine         = analysis_engine
    _alert_chat_ids = alert_chat_ids
    _season_checker = season_checker


def _uptime_str() -> str:
    seconds = int(time.monotonic() - _START_TIME)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _check_allowed(update: Update) -> bool:
    """Return True if the user is in the allowed list (or if the list is empty)."""
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
        await update.message.reply_text(
            format_help_message(),
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

        # ── Restart / crash diagnostics ───────────────────────────────────────
        reason       = ht.last_startup_reason()
        crash        = ht.was_unexpected_exit()
        prev_session = ht.last_session_duration_str()

        _REASON_LABEL: dict[str, str] = {
            "first_start":     "🆕 First start",
            "clean_restart":   "✅ Clean restart  (SIGTERM / normal stop)",
            "crash_detected":  "❌ Crash detected  (run_polling raised)",
            "unexpected_exit": "⚠️ Unexpected exit  (SIGKILL / OOM / hard crash)",
            "unknown":         "❓ Unknown",
        }
        reason_label = _REASON_LABEL.get(reason, f"❓ {_html.escape(str(reason))}")

        lines: list[str] = [
            "❤️ <b>Bot Health</b>",
            "",
            f"Uptime:           {_uptime_str()}",
            f"Heartbeat:        {ht.heartbeat_age_str()}",
            f"Last startup:     {_html.escape(ht.last_startup() or '—')}",
            f"Restart reason:   {reason_label}",
            f"Previous session: {_html.escape(prev_session)}",
            f"Crash detected:   {'Yes ⚠️' if crash else 'No ✅'}",
            "",
            "<b>📋 Background Jobs</b>",
        ]

        _JOB_LABELS = {
            "underdog_job":        "Underdog monitor",
            "_clv_seed_job":       "CLV seeder",
            "_clv_harvest_job":    "CLV harvester",
            "_budget_check_job":   "Budget checker",
            "_season_check_job":   "Season checker",
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
            if last_err:
                # HTML-escape the error text — Python exception strings may
                # contain angle-brackets (e.g. "<class 'X'>") which break HTML.
                lines.append(f"      ↳ {_html.escape(str(last_err)[:100])}")

        # Provider health
        lines.append("")
        lines.append("<b>📡 Providers</b>")
        for provider in ("Underdog",):
            info = ht.get_provider_info(provider)
            if info:
                last_fetch = ht.provider_last_fetch_str(provider)
                streak = info.get("error_streak", 0)
                icon = "✅" if streak == 0 else "⚠️" if streak < 3 else "🚨"
                lines.append(
                    f"  {icon} <b>{_html.escape(provider)}</b>  ·  last fetch: {_html.escape(last_fetch)}"
                    + (f"  ·  err streak: {streak}" if streak else "")
                )
                last_err_msg = info.get("last_error_msg")
                if last_err_msg and streak:
                    lines.append(f"      ↳ {_html.escape(str(last_err_msg)[:100])}")
            else:
                lines.append(f"  ⚪ <b>{_html.escape(provider)}</b>  ·  not yet fetched")

        last_err_global = ht.last_error()
        if last_err_global:
            lines.append("")
            lines.append(f"⚠️ <b>Last error:</b> {_html.escape(str(last_err_global)[:120])}")
            ts = ht.last_error_ts()
            if ts:
                lines.append(f"   at {_html.escape(str(ts))}")

        # ── Recovery events ───────────────────────────────────────────────────
        recovery = ht.last_recovery_event()
        if recovery:
            lines.append("")
            lines.append(
                f"✅ <b>Last recovery:</b>  {_html.escape(ht.last_recovery_age_str())}"
                f"  ·  job: {_html.escape(recovery.get('job', '?'))}"
            )
            reason_txt = recovery.get("reason", "")
            if reason_txt:
                lines.append(f"   ↳ <i>{_html.escape(str(reason_txt)[:100])}</i>")
        else:
            lines.append("")
            lines.append("✅ <b>Last recovery:</b>  <i>No recovery events recorded</i>")

        # ── Phase 2: extended runtime telemetry ───────────────────────────────
        lines.append("")
        lines.append("<b>📡 Underdog Pipeline</b>")
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
            lines.append("")
            lines.append(
                f"🚨 <b>Last pipeline failure:</b>"
                f"  stage={_html.escape(pf.get('stage','?'))}"
                f"  module={_html.escape(pf.get('module','?'))}"
            )
            lines.append(f"   at {_html.escape(pf.get('ts','?'))}")
            lines.append(f"   ↳ {_html.escape(str(pf.get('error',''))[:100])}")

        # ── Crash forensics ───────────────────────────────────────────────────
        lines.append("")
        lines.append("<b>💥 Crash Forensics</b>")
        last_cid = ht.last_crash_id()
        crash_detail = ht.last_crash_detail()
        crash_hist   = ht.crash_history()

        if last_cid is not None:
            cause = ht.crash_cause_label()
            lines.append(f"  Last crash ID:   <b>#{last_cid}</b>  ({_html.escape(cause)})")
        else:
            lines.append("  Last crash ID:   <i>none recorded</i>")

        if crash_detail:
            cd_ts   = crash_detail.get("ts", "—")
            cd_exc  = crash_detail.get("exc_type", "")
            cd_msg  = crash_detail.get("exc_msg", "")[:80]
            cd_job  = crash_detail.get("active_job") or crash_detail.get("last_job_started") or "—"
            cd_mod  = crash_detail.get("active_module", "")
            cd_fn   = crash_detail.get("active_function", "")
            cd_hb   = crash_detail.get("last_heartbeat", "") or "—"
            cd_ud   = crash_detail.get("last_underdog_scan", "") or "—"
            cd_db   = crash_detail.get("last_db_write", "") or "—"
            cd_mem  = crash_detail.get("memory_mb")
            cd_uptime = crash_detail.get("uptime_secs")

            import os as _os
            mod_base = _os.path.basename(cd_mod) if cd_mod else "—"

            lines.append(f"  Last crash at:   {_html.escape(cd_ts)}")
            if cd_exc:
                lines.append(f"  Exception:       <code>{_html.escape(cd_exc)}: {_html.escape(cd_msg)}</code>")
            if mod_base and mod_base != "—":
                lines.append(f"  Module:          <code>{_html.escape(mod_base)}</code>")
            if cd_fn:
                lines.append(f"  Function:        <code>{_html.escape(cd_fn)}</code>")
            lines.append(f"  Active job:      {_html.escape(str(cd_job))}")
            lines.append(f"  Last heartbeat:  {_html.escape(cd_hb)}")
            lines.append(f"  Last UD scan:    {_html.escape(cd_ud)}")
            lines.append(f"  Last DB write:   {_html.escape(cd_db)}")
            if cd_mem is not None:
                lines.append(f"  Memory at crash: {cd_mem} MB")
            if cd_uptime is not None:
                mins = int(cd_uptime) // 60
                secs = int(cd_uptime) % 60
                lines.append(f"  Uptime at crash: {mins}m {secs}s")

        # Show last 3 crash IDs as a mini-log
        if len(crash_hist) > 1:
            lines.append("")
            lines.append("  <b>Crash log (recent):</b>")
            for rec in crash_hist[-3:]:
                cid   = rec.get("crash_id", "?")
                cts   = rec.get("ts", "—")[:16]
                cexc  = rec.get("exc_type", "—")
                lines.append(f"    #{cid}  {_html.escape(cts)}  {_html.escape(cexc[:40])}")
        elif not crash_detail:
            lines.append("  No crash history recorded.")

        logger.info("cmd_health: sending response (%d lines)", len(lines))
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        logger.info("cmd_health: response sent")
    except Exception as exc:
        logger.exception("cmd_health: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /health failed. Check bot logs.")


async def cmd_rollups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rollups — show learning performance rollups by tier, sport, and stat type."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        db: "Database" = context.bot_data.get("db")
        if db is None:
            await update.message.reply_text("⚠️ Database not available.")
            return

        rollups = await db.get_learning_rollups()
        total   = rollups.get("total_graded", 0)

        if total == 0:
            await update.message.reply_text(
                "📊 <b>Learning Rollups</b>\n\n"
                "<i>No graded plays yet — results are graded after game_time passes.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        def _tier_row(k: str, v: dict) -> str:
            w, l, p = v.get("W", 0), v.get("L", 0), v.get("P", 0)
            pct = v.get("win_pct", 0.0)
            return f"  <b>{k}:</b>  {w}W-{l}L-{p}P  ({pct:.1f}%)"

        lines = [
            "📊 <b>Learning Rollups</b>",
            "",
            f"Total graded plays: <b>{total}</b>",
            "",
        ]

        # ── By tier ───────────────────────────────────────────────────────────
        by_tier = rollups.get("by_tier", {})
        if by_tier:
            lines.append("<b>By Tier</b>")
            for tier in ("S", "A", "B"):
                if tier in by_tier:
                    lines.append(_tier_row(tier, by_tier[tier]))
            lines.append("")

        # ── By sport ──────────────────────────────────────────────────────────
        by_sport = rollups.get("by_sport", {})
        if by_sport:
            lines.append("<b>By Sport</b>")
            for sport, v in sorted(by_sport.items(), key=lambda kv: -kv[1].get("total", 0))[:6]:
                lines.append(_tier_row(sport, v))
            lines.append("")

        # ── Top stat types ────────────────────────────────────────────────────
        by_stat = rollups.get("by_stat_type", {})
        if by_stat:
            lines.append("<b>Top Prop Types</b>")
            for stat, v in list(by_stat.items())[:8]:
                lines.append(_tier_row(stat[:28], v))
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


async def cmd_restarts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/restarts — show bot restart count and recent restart history."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        from engine.health import get_health_tracker
        ht = get_health_tracker()
        if ht is None:
            await update.message.reply_text("⚠️ Health tracker not initialised.")
            return

        count   = ht.restart_count()
        history = ht.restart_history()
        reason  = ht.last_startup_reason()
        crash   = ht.was_unexpected_exit()

        _REASON_ICON: dict[str, str] = {
            "first_start":     "🆕",
            "clean_restart":   "✅",
            "crash_detected":  "❌",
            "unexpected_exit": "⚠️",
            "unknown":         "❓",
        }

        lines: list[str] = [
            "🔄 <b>Bot Restarts</b>",
            "",
            f"Total startups:   <b>{count}</b>",
            f"Last startup:     {ht.last_startup() or '—'}",
            f"Restart reason:   {_REASON_ICON.get(reason, '❓')} {reason}",
            f"Crash detected:   {'Yes ⚠️' if crash else 'No ✅'}",
            f"Prev session:     {ht.last_session_duration_str()}",
        ]

        if history:
            lines.append("")
            lines.append(f"<b>Recent history</b> (last {min(len(history), 10)}):")
            for entry in reversed(history[-10:]):
                ts       = entry.get("ts", "?")
                r        = entry.get("reason", "unknown")
                icon     = _REASON_ICON.get(r, "❓")
                sess     = entry.get("session_secs")
                from engine.health import _secs_to_duration
                dur_str  = f"  session: {_secs_to_duration(sess)}" if sess else ""
                lines.append(f"  {icon} {ts}  <i>{r}</i>{dur_str}")
        else:
            lines.append("")
            lines.append("<i>No restart history recorded yet.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_restarts: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /restarts failed. Check bot logs.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show bot and market status."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        # DB counts
        total_steam = await _db.count_steam_records() if _db else 0
        total_ev    = await _db.count_ev_records() if _db else 0
        db_records  = await _db.count_odds_records() if _db else 0
        # Load provider health monitor and odds cache singletons
        try:
            from providers import get_health_monitor
            from providers.odds_cache import get_odds_cache
            _mon   = get_health_monitor()
            _cache = get_odds_cache()
        except ImportError:
            _mon   = None
            _cache = None

        lines: list[str] = [
            f"🤖 <b>Sharp Money Bot</b>  ·  Uptime: {_uptime_str()}",
            f"📬 Alerts sent: {_total_alerts_sent:,}",
            "",
        ]

        # ── Data provider health ──────────────────────────────────────────────────
        lines.append("📡 <b>Data Providers</b>")
        if _mon:
            for name, h in _mon.get_all_health().items():
                if name == "PrizePicks":
                    continue  # PP provider temporarily disabled
                last_ok   = h.format_last_success()

                # Build a human-readable failure note with the actual reason.
                fail_note = ""
                if h.consecutive_failures:
                    n       = h.consecutive_failures
                    n_label = f"{n} fail{'s' if n != 1 else ''}"
                    # Use failure_type first for well-known categories.
                    ftype = getattr(h, "failure_type", None)
                    ftype_name = ftype.value if ftype is not None and hasattr(ftype, "value") else (str(ftype) if ftype else "")
                    err_msg = getattr(h, "error_msg", "") or ""
                    if ftype_name.upper() == "QUOTA":
                        fail_note = f"  🔸 quota limited  ({n_label})"
                    elif ftype_name.upper() == "BLOCKED":
                        fail_note = f"  🔴 blocked  ({n_label})"
                    elif err_msg:
                        short_err = _html.escape(err_msg[:60])
                        fail_note = f"  ({n_label}: {short_err})"
                    else:
                        fail_note = f"  ({n_label})"

                lines.append(
                    f"  {h.status_emoji} <b>{_html.escape(name)}</b>  {_html.escape(h.status.value)}"
                    f"  ·  last ✓: {_html.escape(last_ok)}{fail_note}"
                )
        else:
            lines.append("  ⚪ Underdog     not yet tracked")
        lines.append("")

        # ── Scheduler / job health ─────────────────────────────────────────────
        try:
            from engine.health import get_health_tracker as _get_ht
            _ht = _get_ht()
            if _ht:
                hb_age = _ht.heartbeat_age_str()
                lines.append(f"⚙️ <b>Scheduler</b>  ·  heartbeat: {hb_age}")
                _JOB_LABELS_SHORT = {
                    "underdog_job":     "UD monitor",
                    "_clv_seed_job":    "CLV seed",
                    "_clv_harvest_job": "CLV harvest",
                }
                for jid, label in _JOB_LABELS_SHORT.items():
                    info = _ht.get_job_info(jid)
                    last_run = _ht.job_last_run_str(jid)
                    streak   = info.get("fail_streak", 0)
                    icon     = "✅" if streak == 0 else "⚠️" if streak < 3 else "🚨"
                    lines.append(f"  {icon} {label}  last: {last_run}")
                lines.append("")
        except Exception:
            pass

        # ── Active sports from season checker ─────────────────────────────────────
        if _season_checker and hasattr(_season_checker, "get_sport_summary"):
            summary = _season_checker.get_sport_summary()  # type: ignore[union-attr]
            if summary:
                active   = [k for k, v in summary.items() if v]
                inactive = [k for k, v in summary.items() if not v]
                lines.append(f"🏈 <b>Active Sports</b>  ({len(active)}/{len(summary)} in season)")
                if active:
                    short_active = [k.split("_")[-1].upper()[:6] for k in active[:14]]
                    lines.append(
                        f"  In season:  {' · '.join(short_active)}"
                        f"{'…' if len(active) > 14 else ''}"
                    )
                if inactive:
                    short_inactive = [k.split("_")[-1].upper()[:6] for k in inactive[:8]]
                    lines.append(
                        f"  Off season: {' · '.join(short_inactive)}"
                        f"{'…' if len(inactive) > 8 else ''}"
                    )
                lines.append("")
            else:
                lines.append("🏈 <b>Active Sports</b>  <i>(cache not yet populated)</i>")
                lines.append("")

        # ── Database ──────────────────────────────────────────────────────────────
        total_prop_history = await _db.count_prop_line_history() if _db else 0
        lines.append("📊 <b>Database Records</b>")
        lines.append(f"  Odds: {db_records:,}  ·  Steam: {total_steam:,}  ·  EV: {total_ev:,}")
        if total_prop_history:
            ud_history  = await _db.count_prop_line_history(provider="Underdog") if _db else 0
            pp_history  = await _db.count_prop_line_history(provider="PrizePicks") if _db else 0
            lines.append(
                f"  PropLineHistory: {total_prop_history:,}"
                + (f"  (UD:{ud_history:,}  PP:{pp_history:,})" if ud_history or pp_history else "")
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_status: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /status failed. Check bot logs.")


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


async def cmd_steam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/steam — show latest detected steam alerts from the database."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        records = await _db.get_recent_steam(limit=5)

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
        records = await _db.get_recent_ev(limit=5)

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
                f"{EMOJI['clock']} <i>{r.detected_at.strftime('%Y-%m-%d %H:%M UTC')}</i>",
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
        records = await _db.get_recent_clv_records(limit=20)
        msg = format_clv_history(records)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as exc:
        logger.exception("cmd_clv error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Could not load CLV data: {exc}",
            parse_mode="HTML",
        )


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/market — Show cross-book consensus and market inefficiencies."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    try:
        from market_engine import _snapshot_cache
        from engine.consensus import compute_consensus
        from alerts_multiplatform import format_consensus_summary
        from config import config

        all_snaps = [s for snaps in _snapshot_cache.values() for s in snaps]

        if not all_snaps:
            await update.message.reply_text(
                f"📊 <b>Market Consensus</b>\n\n"
                f"No cross-book market data available yet.\n"
                f"<i>Data accumulates as the multi-platform connectors poll live odds.</i>",
                parse_mode="HTML",
            )
            return

        results = compute_consensus(
            all_snaps,
            min_books=config.CONSENSUS_MIN_BOOKS,
            outlier_threshold=config.INEFFICIENCY_THRESHOLD,
        )
        msg = format_consensus_summary(results)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as exc:
        logger.exception("cmd_market error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Could not load market data: {exc}",
            parse_mode="HTML",
        )


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/performance — Historical win rate, CLV and ROI broken down by sport, market and tier."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    try:
        from engine.backtesting import BacktestEngine
        from engine.ranking import RankingTier, RankingDecision

        records = await _db.get_ev_records_with_results(limit=500, include_pending=False)

        if not records:
            await update.message.reply_text(
                "📊 <b>Performance History</b>\n\n"
                "<i>No resolved bets yet.  Win/loss results are recorded automatically"
                " once an event's final score is available.</i>",
                parse_mode="HTML",
            )
            return

        engine = BacktestEngine()
        report = engine.run(records)
        msg = report.to_telegram()
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as exc:
        logger.exception("cmd_performance error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Could not load performance data: {exc}",
            parse_mode="HTML",
        )


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backtest [limit] — Run a full backtest on the last N resolved alerts."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
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
        from engine.backtesting import run_backtest

        await update.message.reply_text(
            f"⏳ Running backtest on last <b>{limit}</b> resolved records…",
            parse_mode="HTML",
        )

        records = await _db.get_ev_records_with_results(limit=limit, include_pending=False)

        if not records:
            await update.message.reply_text(
                "📊 <b>Backtest</b>\n\n"
                "<i>No resolved records found.  Results are tracked automatically"
                " once events finish.  Use /performance once results accumulate.</i>",
                parse_mode="HTML",
            )
            return

        report = run_backtest(records)
        msg = report.to_telegram()
        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as exc:
        logger.exception("cmd_backtest error: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Backtest failed: {exc}",
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


# ── /picks tier constants ─────────────────────────────────────────────────────
# New PPAnalysisScore vocabulary (S/A/B/PASS) plus legacy AlertTier values for
# any records written before the scoring engine was introduced.
_TIER_ORDER: dict[str, int] = {
    "S": 0, "A": 1, "B": 2, "PASS": 3,
    "Critical": 0, "High": 1, "Medium": 2, "Low": 3,
}
_TIER_EMOJI: dict[str, str] = {
    "S": "🟩",
    "A": "⬜",
    "B": "🟨",
    "C": "🟧",
    "D": "🟥",
    "PASS": "🟥",

    # legacy compatibility
    "Critical": "🟥",
    "High": "🟧",
    "Medium": "🟨",
    "Low": "🟥",
}

# Thresholds mirror PPAnalysisScore._STAR_BANDS — display-only, no scoring logic.
_STAR_BANDS: tuple[tuple[int, int], ...] = (
    (86, 5),  # S tier
    (70, 4),  # A tier
    (60, 3),  # B tier
    (50, 2),  # C tier
    (0, 1),   # D tier
)


def _stars_from_conf(conf: Optional[float]) -> str:
    """Return a 5-char star bar (e.g. '★★★★☆') from a 0–100 confidence score."""
    if conf is None:
        return ""
    for threshold, n in _STAR_BANDS:
        if conf >= threshold:
            return "★" * n + "☆" * (5 - n)
    return "★☆☆☆☆"


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
        "sportsbook", "result", "detected_at", "comp",
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
        self.game_description = f"vs {plh.team}" if plh.team else ""
        self.detected_at = plh.fetched_at
        self.result      = None
        self.sportsbook  = "Underdog"
        self.comp        = comp

            #Confidence → tier
        conf = comp.proxy_match_confidence if comp else 40
        self.confidence = float(conf)

        if conf >= 86:
            self.tier = "S"
        elif conf >= 70:
            self.tier = "A"
        elif conf >= 60:
            self.tier = "B"
        elif conf >= 50:
            self.tier = "C"
        else:
            self.tier = "D"

        # Best line from comparison or fallback to UD line
        best_line = (comp.best_line if comp else None) or plh.line_value
        self.pp_line_value = float(best_line)
        self.prev_line     = plh.prev_line
        self.opening_line  = None  # not tracked in PropLineHistory; no move note in slip

        # Edge proxy: use absolute line movement scaled to a % signal
        movement = (comp.movement if comp else None) or 0.0
        self.best_edge = abs(movement) * 2.0 if movement else 0.0

        # Direction: market-information label, not a betting call
        # Direction placeholder — updated from DB recommendation in cmd_picks/cmd_slip
        # rendering loop so it reflects the actual OVER/UNDER/PASS from Underdog scoring.
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
      return await _cmd_picks_inner(update, context)
    except Exception as exc:
        logger.exception("cmd_picks: unhandled error: %s", exc)
        await update.message.reply_text(
            "⚠️ /picks failed — check bot logs for details."
        )


async def _cmd_picks_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inner implementation of /picks — wrapped by cmd_picks try/except."""
    args = context.args or []
    limit = 10
    sport_filter: Optional[str] = None
    for arg in args:
        if arg.isdigit():
            limit = min(int(arg), 20)
        else:
            sport_filter = arg.upper()

    from engine.timing import format_game_time_label as _fmt_gt
    from engine.player_prop_market import (
        build_player_prop_market_comparison,
        PROVIDER_EMOJI as _PROV_EMOJI,
    )

    now   = datetime.utcnow()
    today = now.strftime("%b %d, %Y")

    # ── 1. Underdog props ─────────────────────────────────────────────────────
    logger.info("cmd_picks: fetching UD props (limit=%d sport=%s)", limit * 3, sport_filter)
    ud_props = await _db.get_top_ud_props_for_picks(limit=limit * 3, since_hours=24)
    ud_props = [p for p in ud_props if not _is_season_future(p.stat_type)]

    if sport_filter:
        ud_props = [p for p in ud_props if p.sport.upper() == sport_filter]

    # Display filter: hide season-long futures (stored & tracked as normal).
    
    ud_props = ud_props[:limit]

    if not ud_props:
        hint = f" for {sport_filter}" if sport_filter else ""
        await update.message.reply_text(
            f"🟣 <b>Player Prop Picks</b>\n\n"
            f"No qualifying player props available right now{hint}.\n\n"
            f"<i>Season-long futures are excluded from this view.\n"
            f"Player props are ranked through the unified market engine.\n"
            f"Provider data is used when available.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    logger.info("cmd_picks: UD fetch done — %d props after filter (sport=%s)", len(ud_props), sport_filter)

    # ── 2. Cross-provider data (PrizePicks only — DK/FD removed from workflow) ─
    try:
        pp_rows = await _db.get_latest_props_for_provider("PrizePicks", since_hours=24)
    except Exception:
        pp_rows = []

    # ── 3. Build PlayerPropMarketComparison for each prop ────────────────────
    picks: list[tuple] = []   # [(PropLineHistory, PlayerPropMarketComparison|None)]
    for plh in ud_props:
        comp = build_player_prop_market_comparison(
            player_name    = plh.player_name,
            sport          = plh.sport,
            stat_type      = plh.stat_type,
            ud_line        = plh.line_value,
            previous_line  = plh.prev_line,
            fetched_at     = plh.fetched_at,
            pp_rows        = pp_rows,
            now            = now,
            min_confidence = 0,   # show all props; confidence is display info, not a gate
        )
        picks.append((plh, comp))

    # Sort: confidence DESC → provider count DESC → |movement| DESC →
    #        provider disagreement DESC → game_time ASC (None last)
    # No sport preference — value + market quality + confidence ranks first.
    import datetime as _dt_mod

    def _pick_sort(item: tuple) -> tuple:
        plh, comp = item
        conf       = comp.proxy_match_confidence if comp else 0
        n_prov     = sum(1 for pl in comp.lines.values() if pl.available) if comp else 0
        movement   = abs(comp.movement or 0.0) if comp else 0.0
        disagreement = (
            (comp.best_under_line or 0.0) - (comp.best_over_line or 0.0)
            if (comp and comp.best_under_line is not None and comp.best_over_line is not None)
            else 0.0
        )
        gt = plh.game_time
        if gt is None:
            gt_sort = _dt_mod.datetime.max
        else:
            gt_sort = gt.replace(tzinfo=None) if gt.tzinfo else gt
        return (-conf, -n_prov, -movement, -disagreement, gt_sort)

    picks.sort(key=_pick_sort)
    logger.info("cmd_picks: built %d comparisons, sorted", len(picks))

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
            # Infer current opponent from the most recent game result
            # (results are sorted newest-first by the DB query).
            # In series sports (MLB etc.) the last 1-3 games share the same
            # opponent, making this a reliable proxy for the current matchup.
            _opp: Optional[str] = results[0].opponent if results else None
            hr = _compute_hr(results, plh.line_value, opponent=_opp)
            return hr, _opp
        except Exception:
            return None, None

    logger.info("cmd_picks: starting hit-rate gather for %d props", len(picks))
    _hr_raw    = await _asyncio.gather(*[_get_hr(plh) for plh, _ in picks], return_exceptions=True)
    _hit_rates = [(None, None) if isinstance(r, Exception) else r for r in _hr_raw]
    logger.info("cmd_picks: hit-rate gather complete")

    # ── 4b. Fetch bet recommendations + alternate lines concurrently ──────────
    from engine.player_prop_market import _line_label as _ll

    logger.info("cmd_picks: fetching recommendations + alternate lines")
    _rec_map: dict = {}
    try:
        _rec_map = await _db.get_ud_recommendations_bulk(
            [(plh.player_name, plh.sport, plh.stat_type) for plh, _ in picks],
            since_hours=24,
        )
        if not _rec_map and picks:
            logger.warning(
                "cmd_picks: get_ud_recommendations_bulk returned empty for %d props "
                "(stat_type mismatch or no recent snapshots within 24h)",
                len(picks),
            )
        else:
            _missing = [
                (plh.player_name, plh.stat_type)
                for plh, _ in picks
                if (plh.player_name, plh.sport, plh.stat_type) not in _rec_map
            ]
            if _missing:
                logger.warning(
                    "cmd_picks: no recommendation found for %d/%d props: %s",
                    len(_missing), len(picks),
                    "; ".join(f"{p}/{s}" for p, s in _missing[:5]),
                )
    except Exception as _rec_exc:
        logger.warning("cmd_picks: recommendation lookup failed: %s", _rec_exc)

    _alt_raw = await _asyncio.gather(
        *[
            _db.get_all_ud_lines_for_prop(
                plh.player_name, plh.sport, plh.stat_type, since_hours=24
            )
            for plh, _ in picks
        ],
        return_exceptions=True,
    )
    _alt_lines: list[list[float]] = [
        [] if isinstance(r, Exception) else (r or [])
        for r in _alt_raw
    ]
    logger.info("cmd_picks: recs=%d, alt-line queries done", len(_rec_map))

    # ── 5. Format ─────────────────────────────────────────────────────────────
    def _tier_from_conf(c: int) -> str:
        if c >= 90: return "S"
        if c >= 70: return "A"
        if c >= 50: return "B"
        return "—"

    _PICK_LABEL: dict[str, str] = {
        "OVER":  "OVER (More) ⬆",
        "UNDER": "UNDER (Less) ⬇",
        "PASS":  "PASS ⚪",
    }

    header = f"🟣 <b>Player Prop Picks — {today}</b>"
    if sport_filter:
        header += f"  <i>({sport_filter})</i>"
    out: list[str] = [header, ""]

    for rank, (plh, comp) in enumerate(picks, 1):
        conf      = comp.proxy_match_confidence if comp else 0
        tier      = _tier_from_conf(conf)
        tier_icon = _TIER_EMOJI.get(tier, "⚪")
        stars     = _stars_from_conf(float(conf))

        # Underdog line (canonical for this prop)
        ud_pl = comp.lines.get("Underdog") if comp else None
        ud_v  = ud_pl.line_value if (ud_pl and ud_pl.available) else plh.line_value

        # Bet direction — primary: synced column on PropLineHistory (always fresh)
        # Secondary: live UnderdogSnapshotRecord via _rec_map (handles same-cycle updates)
        bet_rec  = getattr(plh, "bet_recommendation", None)
        bet_conf = getattr(plh, "bet_confidence", None)
        rec_key  = (plh.player_name, plh.sport, plh.stat_type)
        _live_rec, _live_conf = _rec_map.get(rec_key, (None, None))
        if _live_rec is None and _rec_map:
            # Case-insensitive fallback (normalisation drift between tables)
            _rec_key_lower = (plh.player_name.lower(), plh.sport.lower(), plh.stat_type.lower())
            for (_rp, _rs, _rst), _rv in _rec_map.items():
                if (_rp.lower(), _rs.lower(), _rst.lower()) == _rec_key_lower:
                    _live_rec, _live_conf = _rv
                    break
        # Prefer live snapshot if available; fall back to synced column
        if _live_rec is not None:
            bet_rec, bet_conf = _live_rec, _live_conf
        if bet_rec is None:
            logger.debug(
                "cmd_picks: no recommendation found for %s/%s — showing PASS",
                plh.player_name, plh.stat_type,
            )
        pick_label = _PICK_LABEL.get(bet_rec or "", "—")

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
            f"Market: {plh.stat_type}",
            f"🐶 Underdog Line: <code>{ud_v:.1f}</code>  {_ll(ud_v)}",
            f"{gt_str}{plh.sport}",
            "",
            f"🎯 Pick: <b>{pick_label}</b>",
            f"Confidence: {conf}/100  {stars}",
            f"Tier: {tier} {tier_icon}{move_str}",
        ]

        # ── Evidence block ────────────────────────────────────────────────────
        hr, _opp = _hit_rates[rank - 1]
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
                entry_lines.append(f"📊 <b>Evidence</b>  <i>(vs {ud_v:.1f} line)</i>")
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
        alt = _alt_lines[rank - 1]
        # Only show when there are ≥2 distinct lines
        if len(alt) >= 2:
            alt_str = "  ".join(_ll(v).split("/")[0].split(" ")[0] + f" <code>{v:.1f}</code>" for v in alt)
            entry_lines.append("")
            entry_lines.append("📊 <b>Available Underdog Lines:</b>")
            alt_labeled = "  ".join(
                f"{_ll(v).split(' ')[0]} <code>{v:.1f}</code>" for v in alt
            )
            entry_lines.append(f"  {alt_labeled}")
            entry_lines.append(f"  Current Selected: 🐶 <code>{ud_v:.1f}</code>")

        out.append("\n".join(entry_lines))

    if len(picks) >= limit and not sport_filter:
        out.append(f"\n<i>Showing top {limit}. Use /picks [sport] to filter.</i>")

    # Telegram HTML message; chunked to stay under the 4096-char API limit.
    full_text = "\n\n".join(out)
    logger.info("cmd_picks: formatted output %d chars, sending (%d props)", len(full_text), len(picks))
    await _send_in_chunks(update, full_text, parse_mode=ParseMode.HTML)
    logger.info("cmd_picks: send complete")


async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testalert — Send a mock Player Prop Market Alert to verify Telegram delivery."""
    uid = getattr(update.effective_user, "id", "?")
    logger.info("cmd_testalert: user_id=%s", uid)
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    await update.message.reply_text("⏳ Generating mock Player Prop Market Alert…")

    try:
        from datetime import datetime as _dt
        from engine.player_prop_market import (
            build_player_prop_market_comparison,
            format_player_prop_market_alert,
        )

        _now = _dt.utcnow()

        # MLB strikeouts — Freddy Peralta, line moved 5.0 → 5.5 (Underdog only;
        # PrizePicks / DK / FD show as Unavailable to demonstrate the multi-provider layout).
        comp = build_player_prop_market_comparison(
            player_name    = "Freddy Peralta",
            sport          = "MLB",
            stat_type      = "strikeouts",
            ud_line        = 5.5,
            previous_line  = 5.0,
            now            = _now,
            min_confidence = 0,  # always render; confidence is shown as info not a gate
        )

        if comp is None:
            await update.message.reply_text(
                f"{EMOJI['warn']} Test comparison could not be built "
                f"(proxy confidence below threshold). Check bot logs."
            )
            return

        msg = format_player_prop_market_alert(comp)
        await update.message.reply_text(
            msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await update.message.reply_text("✅ Player Prop Market Alert test sent.")

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
    total_conf = 0.0
    conf_count = 0
    total_move = 0.0   # replaces avg_edge for PropPickAdapter legs
    has_adapters = any(hasattr(r, "comp") for r in records)

    heading = f"🎰 <b>{size}-Man Slip</b>"
    if label:
        heading += f"  <i>{label}</i>"
    section: list[str] = [heading, ""]

    for i, r in enumerate(records, 1):
        tier_icon  = _TIER_EMOJI.get(r.tier or "Low", "⚪")
        stars_str  = _stars_from_conf(r.confidence)
        conf_label = f"{r.confidence:.0f}/100" if r.confidence is not None else "—"
        if r.confidence is not None:
            total_conf += r.confidence
            conf_count += 1

        best_line = getattr(r, "pp_line_value", None)
        line_str  = f"<code>{best_line:g}</code>" if best_line is not None else "—"

        # Movement note — only for PropPickAdapter (prev_line available)
        move_note = ""
        prev = getattr(r, "prev_line", None)
        if has_adapters:
            comp = getattr(r, "comp", None)
            mv   = comp.movement if comp else None
            if mv is not None and mv != 0:
                sign  = "+" if mv > 0 else ""
                arrow = "↑" if mv > 0 else "↓"
                move_note = f"  <code>{sign}{mv:.1f}{arrow}</code>"
                total_move += abs(mv)
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
                pp_pl = comp.lines.get("PrizePicks")
                pp_v  = pp_pl.line_value if (pp_pl and pp_pl.available) else None
                row_parts = [f"🐶 {_lv2(best_line)}"]
                if pp_v is not None:
                    row_parts.append(f"🟣 {_lv2(pp_v)}")
                provider_row = "\n    " + "  ".join(row_parts)

        section.append(
            f"  <b>Leg {i}</b>  {tier_icon} {r.tier or '—'}  {stars_str}\n"
            f"    <b>{r.player_name}</b> · {r.stat_type}\n"
            f"    {r.best_side} {line_str}  ·  conf {conf_label}"
            f"{move_note}{gt_label}"
            f"{provider_row}"
            f"{_dec_leg}"
        )

    avg_conf = total_conf / conf_count if conf_count else 0.0

    if has_adapters:
        avg_mv = total_move / actual if actual else 0.0
        summary = (
            f"\n  Avg move <code>{avg_mv:.1f}</code>  ·  "
            f"Avg conf <code>{avg_conf:.0f}/100</code>  ·  "
            f"{actual} legs"
        )
    else:
        edges     = [r.best_edge for r in records if getattr(r, "best_edge", None) is not None]
        avg_edge  = sum(edges) / len(edges) if edges else 0.0
        summary   = (
            f"\n  Avg edge <code>+{avg_edge:.1f}%</code>  ·  "
            f"Avg conf <code>{avg_conf:.0f}/100</code>  ·  "
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


async def cmd_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/slip — Build correlation-aware prop slips (2–6 legs) from live market props.

    Usage:
      /slip      — show best 2-man through 6-man slips simultaneously
      /slip 3    — show only the 3-man slip
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    args = context.args or []
    single_size: Optional[int] = None
    if args:
        try:
            single_size = max(2, min(int(args[0]), 6))
        except ValueError:
            pass

    from engine.slip_builder import build_all_slips
    from engine.player_prop_market import build_player_prop_market_comparison

    now   = datetime.utcnow()
    today = now.strftime("%b %d, %Y")

    # ── 1. Fetch Underdog props (same pool as /picks) ─────────────────────────
    ud_props = await _db.get_top_ud_props_for_picks(limit=30, since_hours=6)
    # Display filter: hide season-long futures (stored & tracked as normal).
    ud_props = [p for p in ud_props if not _is_season_future(p.stat_type)]
    if not ud_props:
        await update.message.reply_text(
            "🎰 <b>Player Prop Slip</b>\n\n"
            "No qualifying player props available right now.\n\n"
            "<i>Season-long futures are excluded from this view.\n"
            "Underdog props auto-score every 5 min. Run /picks to check status.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── 2. Cross-provider enrichment (same as /picks) ─────────────────────────
    try:
        pp_rows = await _db.get_latest_props_for_provider("PrizePicks", since_hours=24)
    except Exception:
        pp_rows = []

    # ── 3. Build PropPickAdapter candidates (Underdog + PrizePicks only) ─────
    _candidates: list[PropPickAdapter] = []
    for plh in ud_props:
        comp = build_player_prop_market_comparison(
            player_name    = plh.player_name,
            sport          = plh.sport,
            stat_type      = plh.stat_type,
            ud_line        = plh.line_value,
            previous_line  = plh.prev_line,
            fetched_at     = plh.fetched_at,
            pp_rows        = pp_rows,
            now            = now,
            min_confidence = 0,
        )
        adapter = PropPickAdapter(plh, comp)
        # Populate bet direction from synced PropLineHistory column
        _plh_rec = getattr(plh, "bet_recommendation", None)
        if _plh_rec in ("OVER", "UNDER"):
            adapter.best_side = _plh_rec
        _candidates.append(adapter)

    # Override with fresher live snapshot directions where available
    try:
        _slip_rec_map = await _db.get_ud_recommendations_bulk(
            [(plh.player_name, plh.sport, plh.stat_type) for plh in ud_props],
            since_hours=24,
        )
        for _cand in _candidates:
            _k = (_cand.player_name, _cand.sport, _cand.stat_type)
            _lr, _ = _slip_rec_map.get(_k, (None, None))
            if _lr in ("OVER", "UNDER"):
                _cand.best_side = _lr
    except Exception as _slip_rec_exc:
        logger.debug("cmd_slip: rec_map lookup failed: %s", _slip_rec_exc)

    slips = build_all_slips(_candidates, max_size=6)

    if not slips:
        await update.message.reply_text(
            "🎰 <b>Player Prop Slip</b>\n\n"
            f"Not enough independent picks to build a slip "
            f"({len(_candidates)} candidate{'s' if len(_candidates) != 1 else ''} "
            f"after correlation filtering).\n\n"
            f"<i>Check back after the next poll cycle.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if single_size is not None:
        slip = slips.get(single_size)
        if slip is None:
            await update.message.reply_text(
                f"🎰 <b>Player Prop Slip</b>\n\n"
                f"Could not build a {single_size}-man slip from today's picks.\n"
                f"<i>Available sizes: {', '.join(str(s) for s in sorted(slips))}.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        label = "✅ Recommended (Safest)" if single_size == 2 else ""
        lines: list[str] = [
            f"🎰 <b>Player Prop Slips — {today}</b>",
            "",
        ] + _render_slip_section(single_size, slip, label)
    else:
        lines = [
            f"🎰 <b>Player Prop Slips — {today}</b>",
            f"<i>{len(slips)} slip size{'s' if len(slips) != 1 else ''} built "
            f"from {len(_candidates)} Underdog props</i>",
            "",
        ]
        for size in sorted(slips):
            label = "✅ Recommended (Safest)" if size == 2 else ""
            lines.extend(_render_slip_section(size, slips[size], label))
            lines.append("")

    lines.append("<i>⚠️ Research tool — not betting advice.  Verify lines before placing.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
        report = await DashboardEngine.gather(_db)
        msg    = report.to_telegram()
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_dashboard: DashboardEngine failed: %s", exc)
        await update.message.reply_text(
            f"{EMOJI['warn']} Dashboard unavailable: {exc}", parse_mode=ParseMode.HTML
        )
        return

    # ── Player Prop Market section ────────────────────────────────────────────
    try:
        pm_lines: list[str] = ["🟣 <b>Player Prop Market — Live Activity</b>", ""]

        # ── Provider status ───────────────────────────────────────────────────
        pm_lines.append("<b>📡 Providers</b>")
        from providers import get_health_monitor
        _hmon = get_health_monitor()
        providers_display = [
            ("PrizePicks", "🟣", "manual import via /pp_import"),
            ("Underdog",   "🐶", None),
            ("DraftKings", "🎰", None),
            ("FanDuel",    "🦊", None),
        ]
        for pname, pemoji, override_note in providers_display:
            if override_note:
                pm_lines.append(f"  {pemoji} <b>{pname}</b>  ·  <i>{override_note}</i>")
                continue
            if _hmon:
                h = _hmon.get_health(pname) if hasattr(_hmon, "get_health") else None
                if h is not None:
                    last_ok   = h.format_last_success() if hasattr(h, "format_last_success") else "—"
                    fail_note = (
                        f"  ({h.consecutive_failures} fails)"
                        if getattr(h, "consecutive_failures", 0) else ""
                    )
                    pm_lines.append(
                        f"  {pemoji} {h.status_emoji} <b>{pname}</b>  {h.status.value}"
                        f"  ·  last ✓: {last_ok}{fail_note}"
                    )
                    continue
            # Provider not in health monitor (DK/FD when disabled)
            from config import config as _cfg
            enabled = {
                "DraftKings": getattr(_cfg, "DRAFTKINGS_ENABLED", False),
                "FanDuel":    getattr(_cfg, "FANDUEL_ENABLED", False),
            }.get(pname)
            if enabled is False:
                pm_lines.append(f"  {pemoji} <b>{pname}</b>  ·  <i>disabled</i>")
            else:
                pm_lines.append(f"  {pemoji} <b>{pname}</b>  ·  <i>not yet tracked</i>")

        # ── Underdog prop counts from DB ──────────────────────────────────────
        pm_lines.append("")
        pm_lines.append("<b>📊 Player Prop Activity</b>")
        try:
            total_ud = await _db.count_underdog_records()
            # Recent snapshots (last 6 h) = rough active prop count
            ud_recent = await _db.count_prop_line_history()
            pm_lines.append(
                f"  Underdog snapshots:  <code>{total_ud:,}</code> total"
                f"  ·  PropHistory rows: <code>{ud_recent:,}</code>"
            )
        except Exception:
            pm_lines.append("  <i>Prop counts unavailable</i>")

        # ── Recent Underdog props tracked ─────────────────────────────────────
        pm_lines.append("")
        pm_lines.append("<b>🔔 Recently Tracked Props</b>  <i>(last 6 h)</i>")
        try:
            recent_props = await _db.get_latest_props_for_provider("Underdog", since_hours=6)
            if recent_props:
                for _r in recent_props[:5]:
                    _lc = getattr(_r, "lifecycle_state", None) or "—"
                    _lc_icon = {"ACTIVE_ALERTED": "✅", "DISCOVERED": "🔍", "REMOVED": "🚫"}.get(_lc, "⚪")
                    pm_lines.append(
                        f"  {_lc_icon} <b>{_r.player_name}</b>  {_r.stat_type}  "
                        f"<code>{float(_r.line_value):.1f}</code>  ·  <i>{_r.sport}</i>"
                    )
                if len(recent_props) > 5:
                    pm_lines.append(f"  <i>…and {len(recent_props) - 5} more. Use /alerts for history.</i>")
            else:
                pm_lines.append("  <i>No props tracked yet in this window.</i>")
        except Exception:
            pm_lines.append("  <i>Use /alerts for detailed alert history.</i>")

        # ── Bot health ────────────────────────────────────────────────────────
        pm_lines += [
            "",
            "🤖 <b>Bot</b>",
            f"  Uptime: {_uptime_str()}",
        ]

        await update.message.reply_text("\n".join(pm_lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_dashboard: player prop market section failed: %s", exc)
        # Non-fatal — main dashboard already sent


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

    # ── Player Prop Market alerts (Underdog PropLineHistory) ──────────────────
    lines.append("🟣 <b>Player Prop Alerts</b>")
    try:
        recent_props = await _db.get_latest_props_for_provider("Underdog", since_hours=24)
        alerted_props = [
            r for r in recent_props
            if getattr(r, "lifecycle_state", None) == "ACTIVE_ALERTED"
        ]
        if alerted_props:
            lines.append(f"  <i>{len(alerted_props)} props alerted in last 24 h</i>")
            lines.append("")
            for r in alerted_props[:8]:
                _lv = float(getattr(r, "line_value", 0) or 0)
                _ts = (
                    getattr(r, "first_alert_sent_at", None) or
                    getattr(r, "fetched_at", None)
                )
                _ts_str = _ts.strftime("%b %d %H:%M UTC") if _ts else "—"
                lines.append(
                    f"  🐶 <b>{r.player_name}</b>  {r.stat_type}  "
                    f"<code>{_lv:.1f}</code>  ·  <i>{r.sport}</i>  ·  {_ts_str}"
                )
            if len(alerted_props) > 8:
                lines.append(f"  <i>…{len(alerted_props) - 8} more</i>")
        else:
            lines.append("  <i>No player prop alerts sent in the last 24 h.</i>")
            lines.append("  <i>Alerts fire automatically when qualifying props are detected.</i>")
    except Exception as exc:
        logger.warning("cmd_alerts: prop history lookup failed: %s", exc)
        lines.append("  <i>Alert history unavailable — check /health for job status.</i>")

    _alerts_body = "\n".join(lines)
    try:
        await update.message.reply_text(_alerts_body, parse_mode=ParseMode.HTML)
    except Exception as _send_exc:
        logger.warning("cmd_alerts: HTML send failed (%s), retrying as plain text", _send_exc)
        import re as _re
        await update.message.reply_text(_re.sub(r"<[^>]+>", "", _alerts_body))


async def cmd_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grade — Grade resolved PP picks by tier (WIN/LOSS/PUSH breakdown)."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
      return await _cmd_grade_inner(update, context)
    except Exception as exc:
        logger.exception("cmd_grade: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /grade failed. Check bot logs.")


async def _cmd_grade_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inner implementation of /grade — wrapped by cmd_grade try/except."""
    resolved  = await _db.get_all_resolved_pp_edges(limit=200)
    total_all = await _db.count_pp_edge_records()

    lines: list[str] = ["📈 <b>PP Pick Grades</b>", ""]

    if not resolved:
        pending_count = total_all  # all stored picks must be PENDING
        lines += [
            "No resolved picks yet.",
            "<i>Results update automatically when games finish.</i>",
            "",
            "─ <b>Current snapshot</b> ─",
            f"  Stored picks:  <b>{total_all}</b>",
            f"  Pending:       <b>{pending_count}</b>",
            f"  Resolved:      <b>0</b>",
            "",
            "<i>Use /picks to see active edges.</i>",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # ── Aggregate by tier ─────────────────────────────────────────────────────
    # tier → {W, L, P, edges}
    from collections import defaultdict
    stats: dict[str, dict] = defaultdict(lambda: {"W": 0, "L": 0, "P": 0, "edges": []})

    for r in resolved:
        tier = r.tier or "—"
        res  = (r.result or "").upper()
        if res == "WIN":
            stats[tier]["W"] += 1
        elif res == "LOSS":
            stats[tier]["L"] += 1
        elif res in ("PUSH", "REFUND"):
            stats[tier]["P"] += 1
        if r.best_edge is not None:
            stats[tier]["edges"].append(r.best_edge)

    overall_w = overall_l = overall_p = 0
    all_edges: list[float] = []

    lines.append("─ <b>By Tier</b> " + "─" * 20)
    for tier in ("S", "A", "B", "PASS", "—"):
        if tier not in stats:
            continue
        s   = stats[tier]
        w, l, p = s["W"], s["L"], s["P"]
        total_res = w + l + p
        hit_rate  = w / total_res * 100 if total_res > 0 else 0.0
        avg_edge  = sum(s["edges"]) / len(s["edges"]) if s["edges"] else 0.0
        icon      = _TIER_EMOJI.get(tier, "⚪")
        lines.append(
            f"  {icon} <b>{tier:<4}</b>  "
            f"{total_res} picks  W:{w}  L:{l}  P:{p}  "
            f"→ <code>{hit_rate:.0f}%</code> hit  "
            f"avg edge <code>+{avg_edge:.1f}%</code>"
        )
        overall_w += w
        overall_l += l
        overall_p += p
        all_edges.extend(s["edges"])

    overall_res  = overall_w + overall_l + overall_p
    overall_hit  = overall_w / overall_res * 100 if overall_res > 0 else 0.0
    overall_edge = sum(all_edges) / len(all_edges) if all_edges else 0.0
    pending_n    = total_all - len(resolved)

    lines += [
        "─ <b>Overall</b> " + "─" * 21,
        f"  <b>{overall_res}</b> resolved  "
        f"W:{overall_w}  L:{overall_l}  P:{overall_p}  "
        f"→ <code>{overall_hit:.0f}%</code> hit  "
        f"avg edge <code>+{overall_edge:.1f}%</code>",
        "",
        f"<i>{pending_n} picks still PENDING · results set when games finish.</i>",
    ]

    # ── Trend analysis ────────────────────────────────────────────────────────
    from engine.decision_engine import compute_tier_performance
    _perf_g = compute_tier_performance(resolved)
    _qual   = [(t, s) for t, s in _perf_g.items() if s.picks >= 3 and t in ("S", "A", "B")]
    if _qual:
        best_t  = max(_qual, key=lambda x: x[1].hit_rate)
        worst_t = min(_qual, key=lambda x: x[1].hit_rate)
        lines.append("")
        lines.append("─ <b>Trends</b> " + "─" * 22)
        if best_t[0] != worst_t[0]:
            lines.append(
                f"  Best tier:  {_TIER_EMOJI.get(best_t[0], '⚪')} {best_t[0]}  "
                f"{best_t[1].hit_rate_pct:.0f}% hit  "
                f"({best_t[1].wins}W / {best_t[1].losses}L)"
            )
            lines.append(
                f"  Worst tier: {_TIER_EMOJI.get(worst_t[0], '⚪')} {worst_t[0]}  "
                f"{worst_t[1].hit_rate_pct:.0f}% hit  "
                f"({worst_t[1].wins}W / {worst_t[1].losses}L)"
            )
        if overall_edge > 0 and overall_res >= 5:
            _roi = (overall_edge / 100) * 0.909
            lines.append(
                f"  Implied ROI: <code>{_roi * 100:+.1f}%</code>"
                f" <i>(rough, -110 base)</i>"
            )
    elif overall_res > 0:
        lines.append("")
        lines.append(
            f"<i>Trends visible after ≥ 3 resolved picks per tier "
            f"({overall_res} resolved total so far).</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/providers — Show status of every sport data provider."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    try:
        import os
        pandascore_key = bool(os.environ.get("PANDASCORE_API_KEY", "").strip())

        lines: list[str] = [
            "🔌 <b>Provider Status</b>",
            "",
            "<b>Underdog Alert Sports</b>",
        ]

        # Per-sport provider details
        providers_info = [
            ("MLB",    "⚾", "MLB Stats API",    "statsapi.mlb.com",       True,            "free — no key"),
            ("WNBA",   "🏀", "ESPN gamelog",      "espn.com/api",           True,            "free — no key"),
            ("DOTA",   "🎮", "OpenDota API",      "api.opendota.com",       True,            "free — no key"),
            ("TENNIS", "🎾", "JeffSackmann CSV", "github.com/JeffSackmann", True,            "free — no key"),
            ("CS2",    "🖥️", "PandaScore API",   "api.pandascore.co",       pandascore_key,  "key active" if pandascore_key else "⚠️ PANDASCORE_API_KEY not set"),
        ]

        ud_sports = config.ud_alert_sports

        for sport, icon, provider_name, host, active, note in providers_info:
            in_scope  = sport in ud_sports or (sport == "CS2" and "CS" in ud_sports)
            scope_tag = "✅" if (in_scope and active) else ("⚠️" if in_scope else "⏸️")
            lines.append(
                f"  {scope_tag} {icon} <b>{sport:<6}</b>  {provider_name}\n"
                f"         <i>{note}  ·  {host}</i>"
            )

        lines.append("")
        lines.append("<b>DraftKings / FanDuel</b>")
        lines.append(
            f"  {'✅' if config.DRAFTKINGS_ENABLED else '❌'} DraftKings  ·  "
            f"{'✅' if config.FANDUEL_ENABLED else '❌'} FanDuel\n"
            f"  <i>Odds API — player prop lines (when enabled)</i>"
        )

        if not pandascore_key:
            lines.append("")
            lines.append(
                "<i>💡 To enable CS2 alerts: add PANDASCORE_API_KEY to environment secrets, "
                "then restart the bot.</i>"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_providers: unexpected error: %s", exc)
        await update.message.reply_text("⚠️ /providers failed. Check bot logs.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — Alert generation stats, outcomes tracked, performance summary."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    try:
        # Gather data
        ud_today   = await _db.count_today_underdog_alerts()
        pp_today   = await _db.count_today_pp_alerts()
        total_pp   = await _db.count_pp_edge_records()
        resolved   = await _db.get_all_resolved_pp_edges(limit=500)
        edges_24h  = await _db.get_top_pp_edges(limit=100, hours=24)

        lines: list[str] = [
            f"📈 <b>Alert Stats</b>  ·  Uptime: {_uptime_str()}",
            "",
            "<b>Today's Alerts</b>",
            f"  Underdog:    <b>{ud_today}</b>",
            f"  PrizePicks:  <b>{pp_today}</b>",
            "",
            "<b>Pipeline (last 24 h)</b>",
        ]

        # Tier breakdown from 24h edges
        tier_counts: dict[str, int] = {}
        for r in edges_24h:
            t = r.tier or "PASS"
            tier_counts[t] = tier_counts.get(t, 0) + 1

        if tier_counts:
            for tier in ("S", "A", "B", "PASS"):
                n = tier_counts.get(tier, 0)
                if n:
                    icon = {"S": "🔥", "A": "🟢", "B": "🟡", "PASS": "⚪"}.get(tier, "⚪")
                    lines.append(f"  {icon} {tier}: <b>{n}</b>")
        else:
            lines.append("  <i>No edges detected in last 24 h</i>")

        lines.append("")
        lines.append(f"<b>All-time</b>  ({total_pp:,} edges stored)")

        if resolved:
            wins   = sum(1 for r in resolved if (r.result or "").upper() == "WIN")
            losses = sum(1 for r in resolved if (r.result or "").upper() == "LOSS")
            pushes = sum(1 for r in resolved if (r.result or "").upper() in ("PUSH", "REFUND"))
            total_res = wins + losses + pushes
            hit_rate  = wins / total_res * 100 if total_res > 0 else 0.0
            edges     = [r.best_edge for r in resolved if r.best_edge is not None]
            avg_edge  = sum(edges) / len(edges) if edges else 0.0
            lines += [
                f"  Resolved:    <b>{total_res}</b>  W:{wins}  L:{losses}  P:{pushes}",
                f"  Hit rate:    <code>{hit_rate:.0f}%</code>",
                f"  Avg edge:    <code>+{avg_edge:.1f}%</code>",
            ]
            if total_res >= 5:
                implied_roi = (avg_edge / 100) * 0.909
                lines.append(f"  Implied ROI: <code>{implied_roi * 100:+.1f}%</code> <i>(rough, -110 base)</i>")
        else:
            lines.append("  <i>No resolved picks yet — results recorded after games finish.</i>")

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
        report = await engine.compute(_db)
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

    import os
    pandascore_set = bool(os.environ.get("PANDASCORE_API_KEY", "").strip())
    odds_api_set   = bool(config.ODDS_API_KEY)

    ud_sports = ", ".join(sorted(config.ud_alert_sports or [])) or "none"
    active_sp = ", ".join(config.active_sports or []) or "none"

    lines: list[str] = [
        "⚙️ <b>Bot Configuration</b>",
        "",
        "<b>Alert Scope</b>",
        f"  Underdog sports:   <code>{ud_sports}</code>",
        f"  DK/FD sports:      <code>{active_sp}</code>",
        f"  Min stars (alert): <code>{config.UD_MIN_STARS_TO_ALERT}★</code>",
        f"  Min validation:    <code>{config.UD_VALIDATION_MIN_SAMPLES} snapshots</code>",
        "",
        "<b>Thresholds</b>",
        f"  Min EV:            <code>{config.MIN_EV_THRESHOLD:.1f}%</code>",
        f"  Min steam score:   <code>{config.MIN_STEAM_SCORE}/100</code>",
        f"  Min AI confidence: <code>{config.MIN_AI_CONFIDENCE}/100</code>",
        f"  Min PP edge:       <code>{config.MIN_PP_EDGE:.1f}%</code>",
        "",
        "<b>Alert Limits</b>",
        f"  Daily PP cap:      <code>{'unlimited' if config.DAILY_ALERT_LIMIT == 0 else config.DAILY_ALERT_LIMIT}</code>",
        f"  Daily UD cap:      <code>{'unlimited' if config.DAILY_UNDERDOG_LIMIT == 0 else config.DAILY_UNDERDOG_LIMIT}</code>",
        "",
        "<b>Connectors</b>",
        f"  DraftKings:        {'✅ enabled' if config.DRAFTKINGS_ENABLED else '❌ disabled'}",
        f"  FanDuel:           {'✅ enabled' if config.FANDUEL_ENABLED else '❌ disabled'}",
        f"  Underdog:          {'✅ enabled' if config.UNDERDOG_ENABLED else '❌ disabled'}",
        "",
        "<b>API Keys</b>",
        f"  Odds API:          {'✅ set' if odds_api_set else '❌ not set'}",
        f"  PandaScore (CS2):  {'✅ set' if pandascore_set else '⚠️ not set — CS2 alerts suppressed'}",
        "",
        "<b>Poll Intervals</b>",
        f"  Underdog:          <code>{config.UNDERDOG_POLL_INTERVAL}s ({config.UNDERDOG_POLL_INTERVAL // 60} min)</code>",
        f"  Season check:      <code>{config.SEASON_CHECK_INTERVAL}s</code>",
        "",
        "<i>Secrets are never shown. Restart the bot after changing env vars.</i>",
    ]

    try:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_config: reply_text failed: %s", exc)
        await update.message.reply_text("⚠️ /config failed to send. Check bot logs.")


async def cmd_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tracking — Show PLAY vs PASS opportunity tracking and grading results."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not _db:
        await update.message.reply_text("⚠️ Database not ready.")
        return

    try:
        data = await _db.get_tracking_summary()
    except Exception as exc:
        logger.exception("cmd_tracking: DB error: %s", exc)
        await update.message.reply_text("⚠️ /tracking failed — check bot logs.")
        return

    counts   = data.get("counts", {})
    by_tier  = data.get("by_tier", {})
    by_sport = data.get("by_sport", {})
    total    = data.get("total", 0)
    pending  = data.get("pending", 0)

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
        f"<i>Total evaluated: {total}  ·  Pending grading: {pending}</i>",
        "",
    ]

    # ── PLAY (OVER + UNDER) ────────────────────────────────────────────────────
    play_recs = ("OVER", "UNDER")
    play_hit = play_miss = play_push = play_pending = 0
    for rec in play_recs:
        play_hit     += _n(counts, rec, "HIT")
        play_miss    += _n(counts, rec, "MISS")
        play_push    += _n(counts, rec, "PUSH")
        play_pending += _n(counts, rec, "PENDING")
    play_graded = play_hit + play_miss + play_push
    play_total  = play_graded + play_pending

    # For UNDER picks: HIT from OVER perspective = the UNDER actually failed
    # so we flip for display of "correct picks"
    under_hit_correct  = _n(counts, "UNDER", "MISS")   # over missed = under cleared
    over_hit_correct   = _n(counts, "OVER",  "HIT")    # over hit = over cleared
    play_correct       = over_hit_correct + under_hit_correct

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "✅ <b>PLAY Results</b>",
        f"  Total PLAYs:   <code>{play_total}</code>",
        f"  Graded:        <code>{play_graded}</code>",
    ]
    if play_graded > 0:
        lines += [
            f"  Correct picks: <code>{play_correct}</code>  "
            f"({_hit_rate(play_correct, play_graded)})",
            f"  ➖ Push:       <code>{play_push}</code>",
            f"  ⏳ Pending:    <code>{play_pending}</code>",
        ]
        if _n(counts, "OVER", "HIT") or _n(counts, "OVER", "MISS"):
            o_hit  = _n(counts, "OVER", "HIT")
            o_miss = _n(counts, "OVER", "MISS")
            o_tot  = o_hit + o_miss + _n(counts, "OVER", "PUSH")
            lines.append(
                f"  <i>OVER  picks: {o_hit}/{o_tot} ({_hit_rate(o_hit, o_tot)})</i>"
            )
        if _n(counts, "UNDER", "HIT") or _n(counts, "UNDER", "MISS"):
            u_hit  = _n(counts, "UNDER", "MISS")   # OVER miss = UNDER hit
            u_miss = _n(counts, "UNDER", "HIT")
            u_tot  = u_hit + u_miss + _n(counts, "UNDER", "PUSH")
            lines.append(
                f"  <i>UNDER picks: {u_hit}/{u_tot} ({_hit_rate(u_hit, u_tot)})</i>"
            )
    else:
        lines.append("  <i>No graded PLAY results yet.</i>")

    lines.append("")

    # ── PASS (Missed Opportunity Analysis) ────────────────────────────────────
    pass_hit     = _n(counts, "PASS", "HIT")     # over cleared — missed opportunity
    pass_miss    = _n(counts, "PASS", "MISS")    # over failed  — correct pass
    pass_push    = _n(counts, "PASS", "PUSH")
    pass_pending = _n(counts, "PASS", "PENDING")
    pass_graded  = pass_hit + pass_miss + pass_push
    pass_total   = pass_graded + pass_pending

    lines += [
        "━━━━━━━━━━━━━━━━━━",
        "🚫 <b>PASS Analysis</b>  <i>(missed opportunity check)</i>",
        f"  Total PASSes:       <code>{pass_total}</code>",
        f"  Graded:             <code>{pass_graded}</code>",
    ]
    if pass_graded > 0:
        lines += [
            f"  📈 Would have hit:  <code>{pass_hit}</code>  "
            f"({_hit_rate(pass_hit, pass_graded)})  — missed opportunities",
            f"  📉 Would have miss: <code>{pass_miss}</code>  "
            f"({_hit_rate(pass_miss, pass_graded)})  — correct passes",
            f"  ➖ Push:            <code>{pass_push}</code>",
            f"  ⏳ Pending:         <code>{pass_pending}</code>",
        ]
    else:
        lines.append("  <i>No graded PASS results yet.</i>")

    # ── By Tier ───────────────────────────────────────────────────────────────
    if by_tier:
        lines += ["", "━━━━━━━━━━━━━━━━━━", "<b>By Tier (PLAY correct pick rate)</b>"]
        for tier in ("S", "A", "B"):
            tier_data = by_tier.get(tier, {})
            t_correct = (
                _n(tier_data, "OVER",  "HIT")
                + _n(tier_data, "UNDER", "MISS")
            )
            t_graded = sum(
                _n(tier_data, rec, res)
                for rec in ("OVER", "UNDER")
                for res in ("HIT", "MISS", "PUSH")
            )
            if t_graded > 0:
                lines.append(
                    f"  <b>{tier}:</b>  {t_correct}/{t_graded}  ({_hit_rate(t_correct, t_graded)})"
                )

    # ── By Sport ──────────────────────────────────────────────────────────────
    if by_sport:
        lines += ["", "━━━━━━━━━━━━━━━━━━", "<b>By Sport (PLAY correct pick rate)</b>"]
        for sport, sport_data in sorted(by_sport.items()):
            s_correct = (
                _n(sport_data, "OVER",  "HIT")
                + _n(sport_data, "UNDER", "MISS")
            )
            s_graded = sum(
                _n(sport_data, rec, res)
                for rec in ("OVER", "UNDER")
                for res in ("HIT", "MISS", "PUSH")
            )
            s_passes = (
                _n(sport_data, "PASS", "HIT")
                + _n(sport_data, "PASS", "MISS")
                + _n(sport_data, "PASS", "PUSH")
                + _n(sport_data, "PASS", "PENDING")
            )
            if s_graded > 0 or s_passes > 0:
                lines.append(
                    f"  <b>{sport}:</b>  PLAY {s_correct}/{s_graded} "
                    f"({_hit_rate(s_correct, s_graded)})  ·  PASS {s_passes}"
                )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        "<i>Results graded every 6 h from stored game stats.</i>",
        "<i>OVER perspective: HIT = actual &gt; line, MISS = actual &lt; line.</i>",
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

    try:
        from engine.analyst import build_analyst_narrative, format_analyst_telegram
        from engine.candidate import candidate_from_ud_decision

        # Retrieve recent alerted Underdog snapshots with bet recommendations
        snaps = await _db.get_recent_underdog_snapshots(limit=5)
        alerted = [
            s for s in snaps
            if getattr(s, "alert_sent", False) and getattr(s, "bet_recommendation", None)
        ]

        if not alerted:
            await update.message.reply_text(
                "🧠 <b>Analyst Assessment</b>\n\n"
                "<i>No recent alerted picks with bet recommendations found.\n"
                "Analyst assessments are generated when props are alerted.</i>",
                parse_mode="HTML",
            )
            return

        lines: list[str] = ["🧠 <b>Recent Analyst Assessments</b>", ""]
        for snap in alerted[:3]:
            dec_ns = __import__("types", fromlist=["SimpleNamespace"]).SimpleNamespace(
                confidence    = getattr(snap, "bet_confidence", 70) or 70,
                decision_tier = getattr(snap, "score_tier", "B") or "B",
                recommendation= getattr(snap, "bet_recommendation", "PASS"),
                reason        = getattr(snap, "bet_reason", "") or "",
                hit_rates     = {},
                window_agreement = 0,
            )
            score_ns = __import__("types", fromlist=["SimpleNamespace"]).SimpleNamespace(
                total     = getattr(snap, "score_total", 60) or 60,
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

                narrative = build_analyst_narrative(c)
                c = c.with_analyst_narrative(narrative)

                lines.append(
                    f"<b>{_html.escape(snap.player_name)}</b>  ·  "
                    f"{_html.escape(snap.sport)}  ·  "
                    f"{_html.escape(snap.stat_type)} <code>{snap.line_value:.1f}</code>"
                )
                lines.append(
                    f"Decision: <b>{_html.escape(dec_ns.recommendation)}</b>  "
                    f"Tier: <b>{_html.escape(dec_ns.decision_tier)}</b>"
                )
                lines.append("")
                lines.append(f"✅ {_html.escape(narrative.recommended_because[:200])}")
                lines.append(f"⚠️ {_html.escape(narrative.risk_because[:200])}")
                lines.append(f"🎯 <b>{_html.escape(narrative.final_recommendation[:200])}</b>")
                lines.append("━━━━━━━━━━━━━━━━━━")
            except Exception as exc:
                logger.debug("cmd_analyst: skip snap %s: %s", snap.player_name, exc)

        if len(lines) <= 2:
            lines.append("<i>Could not build analyst assessments for recent picks.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        logger.exception("cmd_analyst failed: %s", exc)
        await update.message.reply_text("⚠️ /analyst failed. Check bot logs.")


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

        db_records = await _db.get_active_blocks()
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
    """/funnel — Edge transparency: how many candidates passed/failed each qualification gate."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    try:
        since_h = 24
        if context.args:
            try:
                since_h = max(1, min(168, int(context.args[0])))
            except ValueError:
                pass

        summary   = await _db.get_funnel_summary(since_hours=since_h)
        counts    = summary.get("counts", {})
        top_rej   = summary.get("top_rejections", [])

        accepted  = counts.get("ACCEPTED",  0)
        watchlist = counts.get("WATCHLIST", 0)
        rejected  = counts.get("REJECTED",  0)
        removed   = counts.get("REMOVED",   0)
        total     = accepted + watchlist + rejected + removed

        qual_rate = f"{accepted / total * 100:.0f}%" if total > 0 else "—"

        _thick = "━" * 18
        lines: list[str] = [
            _thick,
            "🔭 <b>Prop Candidate Funnel</b>",
            _thick,
            "",
            f"<i>Last {since_h}h  ·  Use /funnel 48 for longer window</i>",
            "",
            f"📥 Scanned:           <b>{total}</b>",
            f"✅ Accepted (alerted): <b>{accepted}</b>",
            f"👁 Watchlist (B-tier): <b>{watchlist}</b>",
            f"❌ Rejected:           <b>{rejected}</b>",
            f"🚫 Removed:            <b>{removed}</b>",
            "",
            f"📊 Qualification rate: <b>{qual_rate}</b>",
        ]

        if top_rej:
            lines += [
                "",
                "─" * 16,
                "",
                "📋 <b>Near-Misses (highest-scoring rejected)</b>",
                "",
            ]
            for r in top_rej:
                sc   = f"{r.get('score_total', 0):.0f}" if r.get("score_total") is not None else "?"
                tier = r.get("score_tier") or "?"
                rej  = r.get("rejection_reason") or "?"
                lines.append(
                    f"• <b>{r.get('player_name', '?')}</b> "
                    f"— {r.get('stat_type', '?')} ({r.get('sport', '?')})\n"
                    f"  {sc}/100 [{tier}]  <i>{rej}</i>"
                )
        else:
            lines += ["", "<i>No rejected candidates recorded yet — data accumulates as the bot runs.</i>"]

        lines.append(f"\n{_thick}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    except Exception as exc:
        logger.exception("cmd_funnel: error: %s", exc)
        await update.message.reply_text("⚠️ Could not load funnel data. Check bot logs.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors raised by handlers and notify the user if possible."""
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
