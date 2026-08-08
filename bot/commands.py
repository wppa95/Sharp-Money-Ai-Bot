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


def _fmt_user_ts(dt: Optional[datetime]) -> str:
    """Format a UTC datetime for user-facing display.

    Output format: ``Aug 08 · 5:05 PM``  (12-hour clock, no leading zero on hour)

    This is purely a display helper — stored timestamps and internal logic
    remain in UTC.  Returns "—" when dt is None.
    """
    if dt is None:
        return "—"
    # lstrip("0") removes leading zero; "or '12'" handles midnight (12 AM)
    hour_str = dt.strftime("%I").lstrip("0") or "12"
    return dt.strftime(f"%b %d · {hour_str}:%M %p")


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

        # ── Crash forensics — only shown when there is actual crash history ───
        last_cid     = ht.last_crash_id()
        crash_detail = ht.last_crash_detail()
        crash_hist   = ht.crash_history()

        _has_crash_data = last_cid is not None or bool(crash_detail) or len(crash_hist) > 0
        if _has_crash_data:
            lines.append("")
            lines.append("<b>💥 Crash Forensics</b>")
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
        tg_perf = await db.get_telegram_pick_performance()
        total   = rollups.get("total_graded", 0)

        # ── 🎯 Telegram actionable picks block (always shown first) ───────────
        tg_lines = ["📊 <b>Learning Rollups</b>", "", "🎯 <b>TELEGRAM ACTIONABLE PICKS</b>"]
        tg_total = tg_perf.get("total", 0)
        if tg_total == 0:
            tg_lines.append("  <i>No picks sent yet.</i>")
        else:
            tg_h    = tg_perf["hit"]
            tg_m    = tg_perf["miss"]
            tg_p    = tg_perf["push"]
            tg_pend = tg_perf["pending"]
            tg_hr   = tg_perf["hit_rate"]
            tg_lines.append(f"  Total: <b>{tg_total}</b>")
            tg_lines.append(f"  ✅ Hits: {tg_h}   ❌ Misses: {tg_m}   🟡 Pushes: {tg_p}   ⏳ Pending: {tg_pend}")
            if tg_perf["graded"] > 0:
                tg_lines.append(f"  Hit Rate: <b>{tg_hr}%</b>")
        tg_lines.append("")

        if total == 0:
            tg_lines.append("<i>No overall graded plays yet — results are graded after game_time passes.</i>")
            await update.message.reply_text("\n".join(tg_lines), parse_mode=ParseMode.HTML)
            return

        def _tier_row(k: str, v: dict) -> str:
            w, l, p = v.get("W", 0), v.get("L", 0), v.get("P", 0)
            pct = v.get("win_pct", 0.0)
            return f"  <b>{k}:</b>  {w}W-{l}L-{p}P  ({pct:.1f}%)"

        # Extend the Telegram-picks header block with the overall grading section.
        lines = tg_lines + [
            "<b>Overall Grading</b>",
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

        # DB-backed alert count — survives restart, counts only Telegram-delivered picks.
        _alerts_today = await _db.count_today_actionable_alerts() if _db else 0
        _alerts_total = await _db.count_actionable_pick_records() if _db else 0
        lines: list[str] = [
            f"🤖 <b>Sharp Money Bot</b>  ·  Uptime: {_uptime_str()}",
            f"📬 Alerts today: {_alerts_today:,}  ·  All-time: {_alerts_total:,}",
            "",
        ]

        # ── Market provider health (live prop lines) ──────────────────────────────
        lines.append("📡 <b>Market Providers</b>")
        if _mon:
            for name, h in _mon.get_all_health().items():
                # Only show active live-market providers; skip disabled/legacy
                if name in ("PrizePicks", "DraftKings", "FanDuel"):
                    continue
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

        # ── Stat provider health (historical enrichment) ──────────────────────
        lines.append("📊 <b>Stat Providers</b>")
        try:
            from engine.health import get_health_tracker as _get_ht_stat
            _ht_stat = _get_ht_stat()
        except Exception:
            _ht_stat = None
        _sleeper_on = getattr(config, "UD_SLEEPER_ENABLED", True)
        if _ht_stat:
            _slp_info = _ht_stat.get_provider_info("Sleeper")
            if _slp_info:
                _slp_fetch = _ht_stat.provider_last_fetch_str("Sleeper")
                _slp_streak = _slp_info.get("error_streak", 0)
                _slp_icon = "✅" if _slp_streak == 0 else "⚠️"
                lines.append(
                    f"  {_slp_icon} <b>Sleeper</b>  NFL enrichment  ·  last sync: {_html.escape(_slp_fetch)}"
                    + (f"  ·  err streak: {_slp_streak}" if _slp_streak else "")
                )
            elif _sleeper_on:
                lines.append("  ⚪ <b>Sleeper</b>  NFL enrichment  ·  pending first sync")
            else:
                lines.append("  ⏸️ <b>Sleeper</b>  <i>disabled</i>")
        else:
            icon = "⚪" if _sleeper_on else "⏸️"
            lines.append(f"  {icon} <b>Sleeper</b>  NFL enrichment")
        lines.append("  ⚪ <b>ESPN</b>  on-demand gamelog (NBA/NFL/WNBA/MLB)")
        lines.append("  ⚪ <b>NHL API</b>  on-demand skater/goalie logs")
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
                # Filter market-group keys (e.g. "nfl_winner", "ncaaf_championship_winner")
                # that the Odds API returns alongside actual sport keys.
                _MKT_WORDS = frozenset({
                    "winner", "championship", "futures", "preseason",
                    "specials", "h2h", "totals", "alternate", "outright",
                })
                def _is_sport_key(k: str) -> bool:
                    return not any(p in _MKT_WORDS for p in k.lower().split("_"))

                active_sp   = [k for k in active   if _is_sport_key(k)]
                inactive_sp = [k for k in inactive if _is_sport_key(k)]

                if active_sp:
                    short_active = [k.split("_")[-1].upper()[:6] for k in active_sp[:14]]
                    lines.append(
                        f"  In season:  {' · '.join(short_active)}"
                        f"{'…' if len(active_sp) > 14 else ''}"
                    )
                if inactive_sp:
                    short_inactive = [k.split("_")[-1].upper()[:6] for k in inactive_sp[:8]]
                    lines.append(
                        f"  Off season: {' · '.join(short_inactive)}"
                        f"{'…' if len(inactive_sp) > 8 else ''}"
                    )
                lines.append("")
            else:
                lines.append("🏈 <b>Active Sports</b>  <i>(cache not yet populated)</i>")
                lines.append("")

        # ── Database ──────────────────────────────────────────────────────────────
        total_prop_history = await _db.count_prop_line_history() if _db else 0
        lines.append("📊 <b>Database Records</b>")
        if total_prop_history:
            ud_history = await _db.count_prop_line_history(provider="Underdog") if _db else 0
            lines.append(
                f"  PropLineHistory: {total_prop_history:,}"
                + (f"  (UD:{ud_history:,})" if ud_history else "")
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
    """/market — Show line movement and validation summary."""
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
                f"📊 <b>Market Intelligence</b>\n\n"
                f"No market snapshot data available yet.\n"
                f"<i>Data accumulates as Underdog props are polled and scored.</i>",
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
                "<i>No resolved actionable picks yet.  Win/loss results are recorded automatically"
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

    # Enforce strict-sport tier policy — mirrors the actual alert delivery pipeline.
    # MLB and NFL require S-tier; A/B-tier props for those sports are never alerted
    # so they must not appear in an "Actionable Picks" display either.
    from config import config as _picks_cfg
    _strict_sports = {s.upper() for s in _picks_cfg.ud_strict_alert_sports}
    ud_props = [
        p for p in ud_props
        if (p.sport or "").upper() not in _strict_sports
        or (p.score_tier or "") == "S"
    ]

    if sport_filter:
        ud_props = [p for p in ud_props if p.sport.upper() == sport_filter]

    # Display filter: hide season-long futures (stored & tracked as normal).

    ud_props = ud_props[:limit]

    if not ud_props:
        hint = f" for {sport_filter}" if sport_filter else ""
        await update.message.reply_text(
            f"🎯 <b>Actionable Player Prop Picks</b>\n\n"
            f"No qualifying player props available right now{hint}.\n\n"
            f"<i>Season-long futures are excluded from this view.\n"
            f"Ranked through the Sharp Money scoring engine.\n"
            f"Underdog data used as the primary source.</i>",
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

    # Sort within each sport independently — never rank different sports against each other.
    # Each sport forms its own candidate pool: confidence DESC → providers DESC →
    # movement DESC → disagreement DESC → game_time ASC (None last).
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
        if c >= 85: return "S"
        if c >= 70: return "A"
        if c >= 55: return "B"
        return "—"

    _PICK_LABEL: dict[str, str] = {
        "OVER":  "OVER (More) ⬆",
        "UNDER": "UNDER (Less) ⬇",
        "PASS":  "PASS ⚪",
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
    # other. Skip props with no actionable direction (Pick: — or PASS).
    from collections import OrderedDict as _OD
    _sport_groups: _OD = _OD()
    for _flat_idx, (plh, comp) in enumerate(picks):
        _key = plh.sport.upper()
        # Resolve effective recommendation: live snapshot overrides synced column
        _eff_rec = getattr(plh, "bet_recommendation", None)
        _live_r, _ = _rec_map.get(
            (plh.player_name, plh.sport, plh.stat_type), (None, None)
        )
        if _live_r is not None:
            _eff_rec = _live_r
        # Skip PASS / no-direction props — user only wants actionable picks
        if _eff_rec not in ("OVER", "UNDER"):
            logger.debug(
                "cmd_picks: skipping %s/%s — no actionable direction (rec=%s)",
                plh.player_name, plh.stat_type, _eff_rec,
            )
            continue
        # MLB: OVER only for user-facing picks (UNDER is internally tracked but not surfaced)
        if _key == "MLB" and _eff_rec == "UNDER":
            logger.debug(
                "cmd_picks: skipping %s/%s — MLB UNDER blocked from user-facing output",
                plh.player_name, plh.stat_type,
            )
            continue
        if _key not in _sport_groups:
            _sport_groups[_key] = []
        _sport_groups[_key].append((_flat_idx, plh, comp))

    def _render_pick_entry(rank: int, flat_idx: int, plh, comp) -> str:
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
            bet_rec = _live_rec
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
        alt = _alt_lines[flat_idx]
        # Only show when there are ≥2 distinct lines
        if len(alt) >= 2:
            entry_lines.append("")
            entry_lines.append("📊 <b>Available Underdog Lines:</b>")
            alt_labeled = "  ".join(
                f"{_ll(v).split(' ')[0]} <code>{v:.1f}</code>" for v in alt
            )
            entry_lines.append(f"  {alt_labeled}")
            entry_lines.append(f"  Current Selected: 🐶 <code>{ud_v:.1f}</code>")

        return "\n".join(entry_lines)

    # Guard: if all fetched props were PASS/no-direction after filtering, say so.
    if not _sport_groups:
        await update.message.reply_text(
            "No actionable betting picks right now.\n\n"
            "<i>Props are scanned continuously — picks only appear once a prop "
            "has a clear OVER/UNDER direction with sufficient evidence.</i>",
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
    logger.info("cmd_picks: formatted output %d chars, sending (%d props)", len(full_text), len(picks))
    await _send_in_chunks(update, full_text, parse_mode=ParseMode.HTML)
    logger.info("cmd_picks: send complete")


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
            msg,
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
    total_conf = 0.0
    conf_count = 0
    total_move = 0.0   # replaces avg_edge for PropPickAdapter legs
    has_adapters = any(hasattr(r, "comp") for r in records)

    heading = f"🎯 <b>{size}-Man Slip</b>"
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
            tier        = opp.decision_tier,
            confidence  = opp.confidence,
            game_time   = opp.game_time,
        )
        legs = await _db.get_slip_journal_legs(open_slip.slip_code)
        tier_icon = _TIER_EMOJI.get(opp.decision_tier or "", "⚪")
        await update.message.reply_text(
            f"✅ <b>Leg added to {open_slip.slip_code}</b>\n\n"
            f"{tier_icon} <b>{opp.player_name}</b> · {opp.stat_type}\n"
            f"{opp.recommendation} <code>{opp.line_value:g}</code>  ·  "
            f"conf <code>{opp.confidence}/100</code>  ·  {opp.sport}\n\n"
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
            lines.append(
                f"{sym} <b>{leg.player_name}</b> {leg.direction or ''} "
                f"<code>{leg.line_value:g}</code> · {leg.stat_type}{av}"
            )

        lines += [""]
        lines.append(
            f"{'✅ CASH' if summary['all_hit'] else '❌ NO CASH'} — "
            f"{summary['hit']}H / {summary['miss']}M / {summary['push']}P"
            f"{' / ' + str(summary['pending']) + ' pending' if summary['pending'] else ''}"
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
    if not history:
        await update.message.reply_text(
            "📓 <b>Slip Journal</b>\n\n"
            "No slips recorded yet.\n\n"
            "Create one: <code>/slip create [stake]</code>\n"
            "Add picks:  <code>/slip add &lt;player&gt;</code>\n"
            "Grade:      <code>/slip grade [payout]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status_sym = {"OPEN": "🟡", "GRADED": "✅", "VOID": "❌"}
    lines = [f"📓 <b>Slip Journal</b>  ({len(history)} recent)", ""]
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
            lines.append(f"  {sym} {leg.player_name} · {leg.direction or ''} {leg.line_value:g} · {leg.stat_type}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
        await _cmd_slip_journal(update, context, args)
        return

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
            "🎯 <b>Sharp Money Prop Slip</b>\n\n"
            "No qualifying player props available right now.\n\n"
            "<i>Season-long futures are excluded from this view.\n"
            "Underdog props are tracked every 5 min. Run /picks to check current opportunities.</i>",
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

    # Slip eligibility gate — same rules as Telegram alerts:
    #   1. Must have an explicit OVER or UNDER direction (no "—" / PASS props)
    #   2. MLB UNDER blocked from user-facing slips (internally tracked, not surfaced)
    _slip_ineligible = 0
    _candidates_eligible: list[PropPickAdapter] = []
    for _cand in _candidates:
        if _cand.best_side not in ("OVER", "UNDER"):
            _slip_ineligible += 1
            logger.debug(
                "cmd_slip: excluding %s/%s — no direction (best_side=%r)",
                _cand.player_name, _cand.stat_type, _cand.best_side,
            )
            continue
        if _cand.sport.upper() == "MLB" and _cand.best_side == "UNDER":
            _slip_ineligible += 1
            logger.debug(
                "cmd_slip: excluding %s/%s — MLB UNDER blocked from user-facing slips",
                _cand.player_name, _cand.stat_type,
            )
            continue
        _candidates_eligible.append(_cand)
    if _slip_ineligible:
        logger.info(
            "cmd_slip: filtered %d ineligible candidates (no direction or MLB UNDER); "
            "%d eligible remain",
            _slip_ineligible, len(_candidates_eligible),
        )
    _candidates = _candidates_eligible

    slips = build_all_slips(_candidates, max_size=6)

    if not slips:
        await update.message.reply_text(
            "🎯 <b>Sharp Money Prop Slip</b>\n\n"
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
                f"🎯 <b>Sharp Money Prop Slip</b>\n\n"
                f"Could not build a {single_size}-man slip from today's picks.\n"
                f"<i>Available sizes: {', '.join(str(s) for s in sorted(slips))}.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        label = "✅ Recommended (Safest)" if single_size == 2 else ""
        lines: list[str] = [
            f"🎯 <b>Sharp Money Prop Slips — {today}</b>",
            "",
        ] + _render_slip_section(single_size, slip, label)
    else:
        lines = [
            f"🎯 <b>Sharp Money Prop Slips — {today}</b>",
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
        pm_lines: list[str] = ["🐶 <b>Underdog Activity — Live</b>", ""]

        # ── Provider status ───────────────────────────────────────────────────
        from providers import get_health_monitor
        _hmon = get_health_monitor()
        # Market providers (live prop lines)
        pm_lines.append("<b>📡 Market Providers</b>")
        _ud_h = _hmon.get_health("Underdog") if (_hmon and hasattr(_hmon, "get_health")) else None
        if _ud_h is not None:
            _ud_last = _ud_h.format_last_success() if hasattr(_ud_h, "format_last_success") else "—"
            _ud_fail = (
                f"  ({_ud_h.consecutive_failures} fails)"
                if getattr(_ud_h, "consecutive_failures", 0) else ""
            )
            pm_lines.append(
                f"  🐶 {_ud_h.status_emoji} <b>Underdog</b>  {_ud_h.status.value}"
                f"  ·  last ✓: {_ud_last}{_ud_fail}"
            )
        else:
            pm_lines.append("  🐶 ⚪ <b>Underdog</b>  ·  not yet tracked")
        pm_lines.append("")

        # Stat providers (historical enrichment for hit-rate pipeline)
        pm_lines.append("<b>📊 Stat Providers</b>")
        try:
            from engine.health import get_health_tracker as _get_ht_dash
            _ht_dash = _get_ht_dash()
        except Exception:
            _ht_dash = None
        from config import config as _cfg_dash
        _slp_on_dash = getattr(_cfg_dash, "UD_SLEEPER_ENABLED", True)
        if _ht_dash:
            _slp_dash = _ht_dash.get_provider_info("Sleeper")
            if _slp_dash:
                _slp_d_fetch = _ht_dash.provider_last_fetch_str("Sleeper")
                _slp_d_streak = _slp_dash.get("error_streak", 0)
                _slp_d_icon = "✅" if _slp_d_streak == 0 else "⚠️"
                pm_lines.append(
                    f"  {_slp_d_icon} <b>Sleeper</b>  NFL enrichment  ·  last sync: {_html.escape(_slp_d_fetch)}"
                )
            elif _slp_on_dash:
                pm_lines.append("  ⚪ <b>Sleeper</b>  NFL enrichment  ·  pending first sync")
            else:
                pm_lines.append("  ⏸️ <b>Sleeper</b>  <i>disabled</i>")
        else:
            pm_lines.append(f"  {'⚪' if _slp_on_dash else '⏸️'} <b>Sleeper</b>  NFL enrichment")
        pm_lines.append("  ⚪ <b>ESPN</b>  on-demand gamelog (NBA/NFL/WNBA/MLB)")
        pm_lines.append("  ⚪ <b>NHL API</b>  on-demand skater/goalie logs")

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

    # ── Actionable pick alerts (PropOpportunityLog — canonical delivery source) ─
    # PropOpportunityLog.alert_sent=True is set only when broadcast_alert()
    # successfully delivers a 🎯 ACTIONABLE BET PICK to Telegram.  Using
    # PropLineHistory.lifecycle_state was unreliable because new PropLineHistory
    # rows written by subsequent scans reset that field to None/DISCOVERED.
    lines.append("🎯 <b>Actionable Pick Alerts</b>")
    try:
        alerted_opps = await _db.get_alerted_opportunity_log(since_hours=72, limit=8)
        # Also count all alerted picks (not just the display limit)
        _total_alerted = await _db.count_actionable_pick_records()
        if alerted_opps:
            # "sent" = Telegram API accepted the send; no message ID stored.
            lines.append(f"  <i>{len(alerted_opps)} shown (last 72h) · {_total_alerted} all-time sent</i>")
            lines.append("")
            for r in alerted_opps:
                _lv   = float(getattr(r, "line_value", 0) or 0)
                _ts   = getattr(r, "alert_sent_at", None)
                _ts_str = _fmt_user_ts(_ts)
                _rec  = getattr(r, "recommendation", "")
                _tier = getattr(r, "decision_tier", "")
                _rec_icon = "⬆️" if _rec == "OVER" else ("⬇️" if _rec == "UNDER" else "")
                lines.append(
                    f"  🐶 {_rec_icon} <b>{r.player_name}</b>  {r.stat_type}  "
                    f"<code>{_lv:.1f}</code>  ·  <i>{r.sport}</i>"
                    f"  ·  {_tier}  ·  {_ts_str}"
                )
        else:
            lines.append("  <i>No player prop alerts sent in the last 72 h.</i>")
            lines.append("  <i>Alerts fire when props pass all qualification + delivery gates.</i>")
            if _total_alerted:
                lines.append(f"  <i>({_total_alerted} all-time sent — use /performance for history.)</i>")
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
    """/grade — Grade resolved actionable picks by tier (WIN/LOSS/PUSH breakdown)."""
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
    """
    Inner implementation of /grade.

    Source of truth: PropOpportunityLog rows with alert_sent=True.
    These are the only rows that represent actual 🎯 ACTIONABLE BET PICK alerts
    delivered to Telegram.  PPEdgeRecord (legacy PP system) is NOT used here.

    PropOpportunityLog result values: HIT | MISS | PUSH | PENDING
    Mapped to display:  HIT→W  MISS→L  PUSH/REFUND→P
    """
    resolved  = await _db.get_resolved_actionable_picks(limit=200)
    total_all = await _db.count_actionable_pick_records()

    lines: list[str] = ["📊 <b>Sharp Money Pick Grades</b>", ""]

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
            "<i>Use /picks to see active picks.</i>",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    # ── Aggregate by tier ─────────────────────────────────────────────────────
    # PropOpportunityLog uses decision_tier (not .tier); result is HIT/MISS/PUSH (not WIN/LOSS)
    from collections import defaultdict
    stats: dict[str, dict] = defaultdict(lambda: {"W": 0, "L": 0, "P": 0})

    for r in resolved:
        tier = getattr(r, "decision_tier", None) or "—"
        res  = (r.result or "").upper()
        if res == "HIT":
            stats[tier]["W"] += 1
        elif res == "MISS":
            stats[tier]["L"] += 1
        elif res in ("PUSH", "REFUND"):
            stats[tier]["P"] += 1

    overall_w = overall_l = overall_p = 0

    lines.append("─ <b>By Tier</b> " + "─" * 20)
    for tier in ("S", "A", "B", "PASS", "—"):
        if tier not in stats:
            continue
        s   = stats[tier]
        w, l, p = s["W"], s["L"], s["P"]
        total_res = w + l + p
        hit_rate  = w / total_res * 100 if total_res > 0 else 0.0
        icon      = _TIER_EMOJI.get(tier, "⚪")
        lines.append(
            f"  {icon} <b>{tier:<4}</b>  "
            f"{total_res} picks  W:{w}  L:{l}  P:{p}  "
            f"→ <code>{hit_rate:.0f}%</code> hit"
        )
        overall_w += w
        overall_l += l
        overall_p += p

    overall_res = overall_w + overall_l + overall_p
    overall_hit = overall_w / overall_res * 100 if overall_res > 0 else 0.0
    pending_n   = total_all - len(resolved)

    lines += [
        "─ <b>Overall</b> " + "─" * 21,
        f"  <b>{overall_res}</b> resolved  "
        f"W:{overall_w}  L:{overall_l}  P:{overall_p}  "
        f"→ <code>{overall_hit:.0f}%</code> hit",
        "",
        f"<i>{pending_n} picks still PENDING · results set when games finish.</i>",
    ]

    # ── Best/worst tier summary (inline — no PPEdgeRecord dependency) ─────────
    _qual = [
        (t, s) for t, s in stats.items()
        if t in ("S", "A", "B") and (s["W"] + s["L"] + s["P"]) >= 3
    ]
    if _qual:
        best_t  = max(_qual, key=lambda x: x[1]["W"] / max(x[1]["W"] + x[1]["L"] + x[1]["P"], 1))
        worst_t = min(_qual, key=lambda x: x[1]["W"] / max(x[1]["W"] + x[1]["L"] + x[1]["P"], 1))
        lines.append("")
        lines.append("─ <b>Trends</b> " + "─" * 22)
        if best_t[0] != worst_t[0]:
            _bhr = best_t[1]["W"] / max(best_t[1]["W"] + best_t[1]["L"] + best_t[1]["P"], 1) * 100
            _whr = worst_t[1]["W"] / max(worst_t[1]["W"] + worst_t[1]["L"] + worst_t[1]["P"], 1) * 100
            lines.append(
                f"  Best tier:  {_TIER_EMOJI.get(best_t[0], '⚪')} {best_t[0]}  "
                f"{_bhr:.0f}% hit  ({best_t[1]['W']}W / {best_t[1]['L']}L)"
            )
            lines.append(
                f"  Worst tier: {_TIER_EMOJI.get(worst_t[0], '⚪')} {worst_t[0]}  "
                f"{_whr:.0f}% hit  ({worst_t[1]['W']}W / {worst_t[1]['L']}L)"
            )
    elif overall_res > 0:
        lines.append("")
        lines.append(
            f"<i>Trends visible after ≥ 3 resolved picks per tier "
            f"({overall_res} resolved total so far).</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backfill — Fetch latest stats for all pending opportunities and grade them."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    await update.message.reply_text("⏳ Running backfill grading pass…")

    try:
        from providers.player_stats import PlayerStatsProvider
        from engine.calibration import classify_miss

        provider = PlayerStatsProvider()

        # All PENDING opportunities whose game already finished (4+ h ago)
        pending = await _db.get_pending_opportunities(cutoff_hours=4)

        if not pending:
            await update.message.reply_text(
                "✅ No pending opportunities eligible for grading yet.\n"
                "<i>Opportunities become eligible 4 h after game_time.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        # ── Step 1: Fetch stats for all unique (player, sport, stat_type) ────────
        fetched_keys:  set   = set()
        players_found: int   = 0
        for opp in pending:
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
            result_row = await _db.get_game_result_for_grading(
                opp.player_name, opp.sport, opp.stat_type, game_date
            )
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
            await _db.grade_opportunity(opp.id, outcome, actual, error_type=error_type)
            graded += 1

        hit_rate = f"{hits / graded * 100:.0f}%" if graded else "N/A"
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

    except Exception as exc:
        logger.exception("cmd_backfill: unexpected error: %s", exc)
        await update.message.reply_text(f"⚠️ /backfill failed: {exc}")


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

        if not pandascore_key:
            lines.append("")
            lines.append(
                "<i>⚪ CS2 enrichment: optional — not configured.</i>"
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
        ud_today   = await _db.count_today_actionable_alerts()   # Telegram-delivered picks only
        pp_today   = await _db.count_today_pp_alerts()
        total_ud   = await _db.count_underdog_records()
        resolved   = await _db.get_all_resolved_pp_edges(limit=500)
        edges_24h  = await _db.get_top_pp_edges(limit=100, hours=24)

        lines: list[str] = [
            f"📈 <b>Pick Stats</b>  ·  Uptime: {_uptime_str()}",
            "",
            "<b>Today's Actionable Picks</b>",
            f"  Underdog:    <b>{ud_today}</b>",
            "",
            "<b>Pipeline (last 24 h)</b>",
        ]

        # Tier breakdown from 24h edges
        tier_counts: dict[str, int] = {}
        for r in edges_24h:
            t = r.tier or "PASS"
            tier_counts[t] = tier_counts.get(t, 0) + 1

        if tier_counts:
            for tier in ("S", "A", "B", "C", "PASS"):
                n = tier_counts.get(tier, 0)
                if n:
                    icon = {"S": "🔥", "A": "🟢", "B": "🟡", "C": "▪️", "PASS": "⚪"}.get(tier, "⚪")
                    lines.append(f"  {icon} {tier}: <b>{n}</b>")
        else:
            lines.append("  <i>No qualified picks detected in last 24 h</i>")

        lines.append("")
        lines.append(f"<b>All-time</b>  ({total_ud:,} Underdog snapshots)")

        if resolved:
            wins   = sum(1 for r in resolved if (r.result or "").upper() == "WIN")
            losses = sum(1 for r in resolved if (r.result or "").upper() == "LOSS")
            pushes = sum(1 for r in resolved if (r.result or "").upper() in ("PUSH", "REFUND"))
            total_res = wins + losses + pushes
            hit_rate  = wins / total_res * 100 if total_res > 0 else 0.0
            lines += [
                f"  Resolved:    <b>{total_res}</b>  W:{wins}  L:{losses}  P:{pushes}",
                f"  Hit rate:    <code>{hit_rate:.0f}%</code>",
            ]
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
        f"  Min stars (alert): <code>{config.UD_MIN_STARS_TO_ALERT}★</code>",
        f"  Min validation:    <code>{config.UD_VALIDATION_MIN_SAMPLES} snapshots</code>",
        "",
        "<b>Scoring Thresholds</b>",
        f"  Min AI confidence: <code>{config.MIN_AI_CONFIDENCE}/100</code>",
        f"  Min conf S-tier:   <code>{getattr(config, 'UD_MIN_CONF_S', '—')}</code>",
        f"  Min conf A-tier:   <code>{getattr(config, 'UD_MIN_CONF_A', '—')}</code>",
        f"  Market bypass:     <code>score≥70 + |line move|≥2%</code>",
        "",
        "<b>Alert Limits</b>",
        f"  Daily UD cap:      <code>{'unlimited' if config.DAILY_UNDERDOG_LIMIT == 0 else config.DAILY_UNDERDOG_LIMIT}</code>",
        "",
        "<b>Primary Provider</b>",
        f"  🐶 Underdog:       {'✅ enabled' if config.UNDERDOG_ENABLED else '❌ disabled'}",
        "",
        "<b>Stat Data Sources</b>",
        f"  ESPN gamelog:      ✅ active (NBA/NFL/WNBA/MLB/Soccer/NHL)",
        f"  MLB Stats API:     ✅ active",
        f"  NHL API:           ✅ active",
        f"  OpenDota (DOTA):   ✅ active",
        f"  JeffSackmann:      ✅ active (Tennis)",
        f"  PandaScore (CS2):  {'✅ active' if pandascore_set else '⚠️ not set — CS2 alerts suppressed'}",
        f"  Sleeper (NFL):     {'✅ active' if getattr(config, 'UD_SLEEPER_ENABLED', True) else '⏸️ disabled'}",
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

        qual_rate = _fmt_rate(accepted, total)

        _thick = "━" * 18
        lines: list[str] = [
            _thick,
            "🔭 <b>Prop Candidate Funnel</b>",
            _thick,
            "",
            f"<i>Last {since_h}h  ·  Use /funnel 48 for longer window</i>",
            "",
            f"📥 Scanned:              <b>{total}</b>",
            f"✅ Qualified (S/A-tier): <b>{accepted}</b>",
            f"👁 Watchlist (B-tier):   <b>{watchlist}</b>",
            f"❌ Rejected:             <b>{rejected}</b>",
            f"🚫 Removed:              <b>{removed}</b>",
            "",
            f"📊 Qualification rate: <b>{qual_rate}</b>",
            "",
            "<i>ℹ️ Qualified = passed scoring gates (S/A-tier).</i>",
            "<i>   Delivered alerts go through additional gates</i>",
            "<i>   (direction, BQ, conf, dedup, live-game).</i>",
            "<i>   Use /alerts to see Telegram-delivered picks.</i>",
        ]

        # Per-sport breakdown
        by_sport = summary.get("by_sport", [])
        if by_sport:
            lines += [
                "",
                "─" * 16,
                "",
                "⚽ <b>Sport Funnel Breakdown</b>",
                "",
                f"{'Sport':<12} {'Scan':>4} {'Acc':>4} {'Watch':>5} {'Rej':>4} {'Rm':>3}",
                "─" * 36,
            ]
            for row in by_sport:
                sp    = (row.get("sport") or "?")[:11]
                sc    = row.get("scanned",   0)
                acc   = row.get("accepted",  0)
                watch = row.get("watchlist", 0)
                rej   = row.get("rejected",  0)
                rm    = row.get("removed",   0)
                pass_pct = _fmt_rate(acc, sc)
                lines.append(
                    f"<code>{sp:<12} {sc:>4} {acc:>4} {watch:>5} {rej:>4} {rm:>3}</code>"
                    f"  <i>{pass_pct}</i>"
                )
        else:
            lines += ["", "<i>Sport breakdown accumulates as the bot scans props.</i>"]

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
        rows = await _db.get_player_prop_history(name_query, limit=20)
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

    # Aggregate stats
    total  = len(rows)
    graded = [r for r in rows if r.result in ("HIT", "MISS", "PUSH")]
    hits   = sum(1 for r in graded if r.result == "HIT")
    misses = sum(1 for r in graded if r.result == "MISS")
    pushes = sum(1 for r in graded if r.result == "PUSH")
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
        f"Tracked picks:  <b>{total}</b>",
        f"Graded:         <b>{len(graded)}</b>  ({hits}H / {misses}M / {pushes}P)",
        f"Hit rate:       <b>{rate_str}</b>",
    ]
    if warn:
        lines.append(warn)

    # Recent 8 picks
    lines += ["", "<b>Recent picks</b>"]
    result_sym = {"HIT": "✅", "MISS": "❌", "PUSH": "🔁", "PENDING": "⏳"}
    for r in rows[:8]:
        sym   = result_sym.get(r.result, "⏳")
        date  = r.detected_at.strftime("%b %d") if r.detected_at else "?"
        tier  = _TIER_EMOJI.get(r.decision_tier or "", "⚪")
        av    = f" → <i>{r.actual_value:g}</i>" if r.actual_value is not None else ""
        lines.append(
            f"{sym} {tier} <b>{r.stat_type}</b> {r.recommendation or ''} "
            f"<code>{r.line_value:g}</code>  ·  {r.sport}  ·  {date}{av}"
        )

    lines.append("\n<i>Source: prop_opportunity_log</i>")
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
        slip_stats = await _db.get_slip_journal_stats()
        # Pick accuracy per-sport from prop_opportunity_log
        sport_rows = await _db.get_pick_accuracy_by_sport(limit_sports=10)
    except Exception as exc:
        logger.exception("cmd_slipstats: db error: %s", exc)
        await update.message.reply_text(f"{EMOJI['warn']} DB error: {exc}")
        return

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
            h    = row.get("hits", 0)
            m    = row.get("misses", 0)
            tot  = h + m
            rate = f"{h / tot * 100:.0f}%" if tot > 0 else "—"
            lines.append(f"  {sp:<12} {h}H / {m}M  →  {rate}")
    else:
        lines.append("  <i>No graded picks yet — data accumulates as games complete.</i>")

    # Slip journal stats
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
            lines.append(f"  {size:<8}  {w}W / {l}L  ({win_pct})  ·  {stk} → {pay}")
    else:
        lines.append("  <i>No graded slips yet. Create one: /slip create</i>")

    # Overall
    if slip_stats["total_staked"] > 0:
        roi_str = f"{slip_stats['overall_roi']:+.1f}%" if slip_stats["overall_roi"] is not None else "—"
        lines += [
            "",
            f"Total staked:  ${slip_stats['total_staked']:.2f}",
            f"Total payout:  ${slip_stats['total_payout']:.2f}",
            f"Overall ROI:   <b>{roi_str}</b>",
        ]

    lines.append(f"\n{_thick}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
