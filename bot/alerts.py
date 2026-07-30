"""
Alert formatting and Telegram message dispatch.

All alert messages use HTML parse mode for rich formatting.
The alert templates are designed to be immediately scannable on mobile.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Union

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from models import EVOpportunity, Recommendation, SteamAlert

logger = logging.getLogger(__name__)

# ── Emoji constants ────────────────────────────────────────────────────────────

EMOJI = {
    "sharp":    "🚨",
    "steam":    "🔥",
    "ev":       "💰",
    "up":       "📈",
    "down":     "📉",
    "star":     "★",
    "star_e":   "☆",
    "check":    "✅",
    "warn":     "⚠️",
    "info":     "ℹ️",
    "robot":    "🤖",
    "chart":    "📊",
    "clock":    "🕐",
    "lock":     "🔒",
    "target":   "🎯",
    "money":    "💵",
    "fire":     "🔥",
}

RECOMMENDATION_EMOJI = {
    Recommendation.STRONG_BET: "🟢 STRONG BET",
    Recommendation.BET:        "🟡 BET",
    Recommendation.LEAN:       "🔵 LEAN",
    Recommendation.PASS:       "⚪ PASS",
    Recommendation.FADE:       "🔴 FADE",
}


# ── Alert formatters ───────────────────────────────────────────────────────────

def format_odds(american: int) -> str:
    return f"+{american}" if american > 0 else str(american)


def format_probability(p: float) -> str:
    return f"{p * 100:.1f}%"


def format_ev(ev: float) -> str:
    sign = "+" if ev >= 0 else ""
    return f"{sign}{ev:.2f}%"


def format_steam_alert(alert: SteamAlert) -> str:
    """Format a steam / sharp money move alert."""
    direction_emoji = EMOJI["up"] if alert.steam_direction == "UP" else EMOJI["down"]
    books = ", ".join(alert.books_moved) if alert.books_moved else "Multiple books"

    lines = [
        f"{EMOJI['sharp']} <b>SHARP MONEY ALERT</b> {EMOJI['sharp']}",
        "",
        f"<b>Sport:</b>   {alert.sport}",
        f"<b>Market:</b>  {alert.market_type}",
        f"<b>Event:</b>   {alert.event}",
        f"<b>Line:</b>    {alert.selection}",
        "",
        f"{direction_emoji} <b>Odds Movement</b>",
        f"  Opening:  <code>{format_odds(alert.opening_odds)}</code>",
        f"  Current:  <code>{format_odds(alert.current_odds)}</code>",
        f"  Change:   <code>{format_odds(alert.current_odds - alert.opening_odds)}</code>",
        "",
        f"{EMOJI['fire']} <b>Steam Score:</b>  <code>{alert.steam_score}/100</code>",
        f"{EMOJI['chart']} <b>Books Moved:</b> {books}",
        "",
    ]

    if alert.notes:
        lines += [f"{EMOJI['info']} {alert.notes}", ""]

    lines += [
        f"{EMOJI['clock']} <i>{alert.timestamp.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]

    return "\n".join(lines)


def format_ev_alert(opp: EVOpportunity) -> str:
    """Format a full +EV opportunity alert."""
    rec_display = RECOMMENDATION_EMOJI.get(opp.recommendation, str(opp.recommendation))
    star_bar = EMOJI["star"] * opp.stars + EMOJI["star_e"] * (5 - opp.stars)
    reason_block = "\n".join(f"  • {r}" for r in opp.reason_codes)
    player_line = f"\n<b>Player:</b>   {opp.player}" if opp.player else ""
    line_str = f" ({opp.line:+g})" if opp.line is not None else ""

    lines = [
        f"{EMOJI['ev']} <b>+EV OPPORTUNITY DETECTED</b> {EMOJI['ev']}",
        "",
        f"<b>Sport:</b>   {opp.sport}",
        f"<b>Market:</b>  {opp.market_type}",
        f"<b>Event:</b>   {opp.event}",
        f"{player_line}<b>Line:</b>    {opp.ev_result.selection}{line_str}",
        f"<b>Odds:</b>    <code>{format_odds(opp.best_odds)}</code>  @ {opp.best_book}",
        "",
        f"{EMOJI['target']} <b>Fair Probability:</b>  <code>{format_probability(opp.fair_probability)}</code>",
        f"{EMOJI['money']} <b>Expected Value:</b>    <code>{format_ev(opp.expected_value)}</code>",
        f"{EMOJI['fire']} <b>Steam Score:</b>        <code>{opp.steam_score}/100</code>",
        f"{EMOJI['robot']} <b>AI Confidence:</b>     <code>{opp.ai_confidence}/100</code>",
        "",
        f"<b>Recommendation:</b>  {rec_display}",
        f"<b>Rating:</b>          {star_bar}",
        "",
        f"<b>Reason Codes:</b>",
        reason_block,
        "",
        f"{EMOJI['clock']} <i>{opp.timestamp.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]

    return "\n".join(lines)


def format_status_message(
    uptime_str: str,
    total_alerts: int,
    total_steam: int,
    total_ev: int,
    books_monitored: int,
    active_markets: int,
    db_records: int,
    last_update: Optional[datetime],
) -> str:
    """Format the /status response."""
    last_upd = last_update.strftime("%H:%M UTC") if last_update else "N/A"
    return "\n".join([
        f"{EMOJI['robot']} <b>Sharp Money Bot — Status</b>",
        "",
        f"{EMOJI['check']} <b>Uptime:</b>           {uptime_str}",
        f"{EMOJI['chart']} <b>Books Monitored:</b>  {books_monitored}",
        f"{EMOJI['chart']} <b>Active Markets:</b>   {active_markets}",
        f"{EMOJI['clock']} <b>Last Odds Update:</b> {last_upd}",
        "",
        f"{EMOJI['sharp']} <b>Steam Alerts Sent:</b>  {total_steam}",
        f"{EMOJI['ev']}   <b>+EV Alerts Sent:</b>    {total_ev}",
        f"{EMOJI['info']} <b>DB Records:</b>         {db_records}",
        "",
        f"<i>All systems operational</i>",
    ])


def format_help_message() -> str:
    return "\n".join([
        f"{EMOJI['robot']} <b>Sharp Money +EV Bot — Commands</b>",
        "",
        "<b>📋 Available Commands:</b>",
        "",
        f"  /start    — Welcome message and quick overview",
        f"  /help     — Show this help menu",
        f"  /status   — Bot status, uptime, and market stats",
        f"  /analyze  — Analyze a betting line",
        f"             <i>Usage: /analyze SPORT SELECTION ODDS OPP_ODDS</i>",
        f"             <i>Example: /analyze NBA Lakers+3.5 -110 -110</i>",
        f"  /steam    — Show the {EMOJI['fire']} latest steam / sharp moves",
        f"  /ev       — Show the {EMOJI['ev']} latest +EV opportunities",
        "",
        "<b>🔍 Analyze Format:</b>",
        "  <code>/analyze [sport] [selection] [your_odds] [opp_odds]</code>",
        "  <code>/analyze NFL Chiefs-3 -110 -110</code>",
        "",
        "<b>📡 Data Sources (coming soon):</b>",
        "  • Live sportsbook odds via The Odds API",
        "  • PrizePicks player props monitoring",
        "  • CLV (Closing Line Value) tracking",
        "",
        f"<i>Built for sharp bettors. Use responsibly.</i>",
    ])


def format_start_message() -> str:
    return "\n".join([
        f"{EMOJI['sharp']} <b>Welcome to the Sharp Money +EV Detection Bot</b>",
        "",
        "I monitor betting markets, detect sharp money moves, and surface",
        f"<b>+Expected Value opportunities</b> before the line closes.",
        "",
        "<b>What I do:</b>",
        f"  {EMOJI['fire']} Detect steam moves and line changes across books",
        f"  {EMOJI['target']} Remove vig to calculate fair probabilities",
        f"  {EMOJI['money']} Calculate Expected Value (+EV) for any line",
        f"  {EMOJI['robot']} Score AI confidence using multi-signal analysis",
        f"  {EMOJI['ev']} Alert you to high-value opportunities automatically",
        "",
        "Use /help to see all available commands.",
        "Use /analyze to manually analyze any line right now.",
        "",
        f"<i>Sharp money finds value. This bot finds sharp money.</i>",
    ])


# ── Telegram sender ────────────────────────────────────────────────────────────

async def send_alert(
    bot: Bot,
    chat_id: Union[int, str],
    message: str,
    keyboard: Optional[list[list[InlineKeyboardButton]]] = None,
) -> bool:
    """
    Send a formatted alert message. Returns True on success.
    Silently logs errors and returns False so the caller can handle them.
    """
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        return True
    except TelegramError as exc:
        logger.error("Failed to send alert to %s: %s", chat_id, exc)
        return False


async def broadcast_alert(
    bot: Bot,
    chat_ids: list[Union[int, str]],
    message: str,
) -> dict[str, int]:
    """
    Broadcast an alert to multiple chat IDs.
    Returns {"sent": n, "failed": m}.
    """
    sent = failed = 0
    for cid in chat_ids:
        if await send_alert(bot, cid, message):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}
