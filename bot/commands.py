"""
Telegram command handlers.

Each handler is a standalone async function registered with the Application.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from alerts import (
    format_ev_alert,
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


def init_handlers(db: Database, analysis_engine: AnalysisEngine, alert_chat_ids: list[int]) -> None:
    """Call this once from main.py after the database and engine are ready."""
    global _db, _engine, _alert_chat_ids
    _db = db
    _engine = analysis_engine
    _alert_chat_ids = alert_chat_ids


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


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — welcome message."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await update.message.reply_text(
        format_start_message(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show available commands."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await update.message.reply_text(
        format_help_message(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show bot and market status."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    total_steam = await _db.count_steam_records() if _db else 0
    total_ev    = await _db.count_ev_records() if _db else 0
    db_records  = await _db.count_odds_records() if _db else 0

    msg = format_status_message(
        uptime_str=_uptime_str(),
        total_alerts=_total_alerts_sent,
        total_steam=total_steam,
        total_ev=total_ev,
        books_monitored=0,     # TODO: pull from live odds poller
        active_markets=0,      # TODO: pull from live odds poller
        db_records=db_records,
        last_update=None,      # TODO: pull from live odds poller
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


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


async def cmd_ev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ev — show latest +EV opportunities from the database."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors raised by handlers."""
    logger.error("Update %s caused error: %s", update, context.error, exc_info=context.error)
