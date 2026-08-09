"""
Alert formatting and Telegram message dispatch.

Public API
──────────
  format_ev_alert(opp)           — rich HTML for a +EV opportunity
  format_steam_alert(alert)      — rich HTML for a steam / sharp move
  AlertDelivery                  — filter → dedup → format → send → log
  send_alert / broadcast_alert   — low-level Telegram senders
  compute_ev_risk_factors(opp)   — list of RiskFactor for an EV alert
  compute_steam_risk_factors(a)  — list of RiskFactor for a steam alert
  identify_sharp_books(books)    — filter to known sharp sportsbooks

All Telegram messages use HTML parse mode and are designed to be
immediately scannable on a mobile screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Union

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import config
from engine.score_validation import clamp_score
from models import EVOpportunity, Recommendation, SteamAlert

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


# ── Known sharp / respected sportsbooks ───────────────────────────────────────

#: Default sharp book list — override via SHARP_BOOKS env var (comma-separated).
SHARP_BOOKS: frozenset[str] = frozenset({
    "Pinnacle", "Pinnacle Sports",
    "Circa", "Circa Sports",
    "Bookmaker", "Bookmaker.eu",
    "Heritage Sports", "Heritage",
    "BetOnline", "BetOnline.ag",
    "CRIS", "5Dimes",
})


def identify_sharp_books(books: list[str]) -> list[str]:
    """Return the subset of *books* that are known sharp / respected books."""
    sharp = config.sharp_books  # configurable via env var
    return [b for b in books if b in sharp]


# ── Sport display helpers ─────────────────────────────────────────────────────

_SPORT_EMOJI: dict[str, str] = {
    "NFL":    "🏈",
    "NBA":    "🏀",
    "MLB":    "⚾",
    "NHL":    "🏒",
    "NCAAF":  "🏈",
    "NCAAB":  "🏀",
    "UFC":    "🥊",
    "WNBA":   "🏀",
    "Soccer": "⚽",
    "EPL":        "⚽",
    "LaLiga":     "⚽",
    "SerieA":     "⚽",
    "Bundesliga": "⚽",
    "Ligue1":     "⚽",
    "MLS":        "⚽",
    "UCL":        "⚽",
    "Other":  "🎯",
}


def sport_icon(sport) -> str:
    key = sport.value if hasattr(sport, "value") else str(sport)
    return _SPORT_EMOJI.get(key, "🎯")


# ── Risk factors ──────────────────────────────────────────────────────────────

@dataclass
class RiskFactor:
    """A single risk warning with severity level and human-readable description."""
    level: str          # "HIGH" | "MEDIUM" | "LOW"
    description: str

    @property
    def icon(self) -> str:
        return {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}.get(self.level, "⚪")


def compute_ev_risk_factors(opp: EVOpportunity) -> list[RiskFactor]:
    """
    Derive risk warnings from an EVOpportunity.

    Checks: vig level, steam confirmation, odds extremity, AI confidence,
    edge thinness, and over-sized Kelly.
    """
    factors: list[RiskFactor] = []
    vig = opp.ev_result.fair_odds.vig_percentage

    if vig > 7.0:
        factors.append(RiskFactor("HIGH",   f"Very high vig ({vig:.1f}%) — market quality poor"))
    elif vig > 5.0:
        factors.append(RiskFactor("MEDIUM", f"Elevated vig ({vig:.1f}%) — shop for better lines"))

    if opp.steam_score < 25:
        factors.append(RiskFactor("MEDIUM", "No steam confirmation — EV signal only"))
    elif opp.steam_score < 50:
        factors.append(RiskFactor("LOW",    "Weak steam — edge not fully confirmed by sharp action"))

    if abs(opp.best_odds) > 300:
        factors.append(RiskFactor("MEDIUM", "Extreme odds — reduced market liquidity"))

    if opp.ai_confidence < 65:
        factors.append(RiskFactor("MEDIUM", f"Moderate AI confidence ({opp.ai_confidence}/100)"))

    if 0 < opp.expected_value < 3.0:
        factors.append(RiskFactor("LOW",    "Thin edge — susceptible to line movement before bet"))

    kelly = opp.ev_result.kelly_fraction
    if kelly > 0.15:
        factors.append(RiskFactor("LOW",    f"High Kelly ({kelly:.0%}) — consider quarter-Kelly sizing"))

    return factors


def compute_steam_risk_factors(alert: SteamAlert) -> list[RiskFactor]:
    """
    Derive risk warnings from a SteamAlert.

    Checks: book count, movement size, sharp book presence, and steam score.
    """
    factors: list[RiskFactor] = []
    num_books = len(alert.books_moved)

    if num_books < 2:
        factors.append(RiskFactor("HIGH",   "Single-book move — no cross-book confirmation"))
    elif num_books < 3:
        factors.append(RiskFactor("MEDIUM", "Only 2 books moved — limited confirmation"))

    change = abs(alert.current_odds - alert.opening_odds)
    if change < 8:
        factors.append(RiskFactor("MEDIUM", "Small movement — may be noise, not sharp action"))

    if not identify_sharp_books(alert.books_moved):
        factors.append(RiskFactor("MEDIUM", "No sharp books in movers (Pinnacle, Circa, etc.)"))

    if alert.steam_score < 50:
        factors.append(RiskFactor("LOW",    f"Moderate steam score ({alert.steam_score}/100)"))

    return factors


# ── Emoji and label constants ─────────────────────────────────────────────────

EMOJI = {
    "sharp":   "🚨",
    "steam":   "🔥",
    "ev":      "💰",
    "up":      "📈",
    "down":    "📉",
    "star":    "★",
    "star_e":  "☆",
    "check":   "✅",
    "warn":    "⚠️",
    "info":    "ℹ️",
    "robot":   "🤖",
    "chart":   "📊",
    "clock":   "🕐",
    "lock":    "🔒",
    "target":  "🎯",
    "money":   "💵",
    "fire":    "🔥",
}

RECOMMENDATION_EMOJI = {
    Recommendation.STRONG_BET: "🟢 STRONG BET",
    Recommendation.BET:        "🟡 BET",
    Recommendation.LEAN:       "🔵 LEAN",
    Recommendation.PASS:       "⚪ PASS",
    Recommendation.FADE:       "🔴 FADE",
}


# ── Formatting helpers ────────────────────────────────────────────────────────

def format_odds(american: int) -> str:
    return f"+{american}" if american > 0 else str(american)


def format_probability(p: float) -> str:
    return f"{p * 100:.1f}%"


def format_ev(ev: float) -> str:
    sign = "+" if ev >= 0 else ""
    return f"{sign}{ev:.2f}%"


def _div() -> str:
    return "─" * 30


def _risk_section(factors: list[RiskFactor]) -> list[str]:
    lines = [f"{EMOJI['warn']} <b>Risk Factors</b>"]
    if not factors:
        lines.append("  ✅ None identified")
    else:
        for f in factors:
            lines.append(f"  {f.icon} {f.description}")
    return lines


# ── Alert formatters ──────────────────────────────────────────────────────────

def format_ev_alert(
        opp: EVOpportunity,
        *,
        risk_factors: Optional[list[RiskFactor]] = None,
        ranking_result: Optional[object] = None,
    ) -> str:
    """
    Format a +EV opportunity as a rich Telegram HTML message.

    Covers all required fields:
      alert type · sport/league · event · player/market · sportsbook · line
      odds · fair probability · EV% · steam score · books moving · sharp books
      AI confidence · star rating · risk factors
    """
    if risk_factors is None:
        risk_factors = compute_ev_risk_factors(opp)

    icon        = sport_icon(opp.sport)
    rec_display = RECOMMENDATION_EMOJI.get(opp.recommendation, str(opp.recommendation))
    star_bar    = EMOJI["star"] * opp.stars + EMOJI["star_e"] * (5 - opp.stars)
    line_str    = f" ({opp.line:+g})" if opp.line is not None else ""
    player_line = f"\n👤 <b>Player:</b>    {opp.player}" if opp.player else ""
    kelly       = opp.ev_result.kelly_fraction
    half_kelly  = opp.ev_result.half_kelly

    # Decide the header label
    has_steam  = opp.steam_alert is not None or opp.steam_score >= 50
    alert_type = "STEAM + EV ALERT" if has_steam else "+EV OPPORTUNITY"

    # Steam sub-section
    steam_section: list[str] = []
    if opp.steam_alert:
        sa            = opp.steam_alert
        dir_icon      = EMOJI["down"] if sa.steam_direction == "DOWN" else EMOJI["up"]
        dir_label     = "FALLING" if sa.steam_direction == "DOWN" else "RISING"
        books_all_str = ", ".join(sa.books_moved) if sa.books_moved else "—"
        sharp_found   = identify_sharp_books(sa.books_moved)
        sharp_str     = ", ".join(sharp_found) if sharp_found else "None detected"
        steam_section = [
            "",
            f"{EMOJI['fire']} <b>Steam Move  ({dir_label})</b>",
            f"  Opening:     <code>{format_odds(sa.opening_odds)}</code>",
            f"  Current:     <code>{format_odds(sa.current_odds)}</code>",
            f"  Change:      <code>{format_odds(sa.current_odds - sa.opening_odds)}</code>  {dir_icon}",
            f"  Books moved: {books_all_str}",
            f"  ⚡ Sharp:    {sharp_str}",
        ]
    elif opp.steam_score > 0:
        steam_section = [
            "",
            f"{EMOJI['fire']} <b>Steam Score:</b>  <code>{opp.steam_score}/100</code>",
        ]

    # Reason codes (positive signals)
    reason_block = (
        "\n".join(f"  • {r}" for r in opp.reason_codes)
        if opp.reason_codes else "  • (none)"
    )

    parts: list[str] = [
        # ── Header ────────────────────────────────────────────────────────────
        f"{EMOJI['ev']} <b>{alert_type}</b>",
        "",
        # ── Event context ─────────────────────────────────────────────────────
        f"{icon} <b>{opp.sport.value}</b>  ·  {opp.market_type.value}",
        f"📋 <b>{opp.event}</b>{player_line}",
        "",
        _div(),
        # ── Selection / odds ──────────────────────────────────────────────────
        f"⚡ <b>{opp.ev_result.selection}{line_str}</b>",
        f"  Sportsbook:  {opp.best_book}",
        f"  Offered:     <code>{format_odds(opp.best_odds)}</code>",
        f"  Fair Odds:   <code>{format_odds(opp.ev_result.fair_odds.fair_american_odds)}</code>",
        f"  Fair Prob:   <code>{format_probability(opp.fair_probability)}</code>",
        f"  Market Vig:  <code>{opp.ev_result.fair_odds.vig_percentage:.2f}%</code>",
        _div(),
        # ── Edge analysis ─────────────────────────────────────────────────────
        f"{EMOJI['chart']} <b>Edge Analysis</b>",
        f"  Expected Value:  <code>{format_ev(opp.expected_value)}</code>",
        f"  Raw Edge:        <code>{opp.ev_result.edge:+.4f}</code>",
        f"  Full Kelly:      <code>{kelly:.1%}</code>   Half-Kelly: <code>{half_kelly:.1%}</code>",
        "",
        # ── Confidence ────────────────────────────────────────────────────────
        f"{EMOJI['fire']} <b>Steam Score:</b>     <code>{opp.steam_score}/100</code>",
        f"{EMOJI['robot']} <b>AI Confidence:</b>  <code>{opp.ai_confidence}/100</code>",
           f"⭐ <b>Rating:</b>          {star_bar}  {rec_display}",
    ]
    parts += steam_section

    parts += [
        "",
        _div(),
        # ── Sharp signals ─────────────────────────────────────────────────────
        f"{EMOJI['up']} <b>Sharp Signals</b>",
        reason_block,
        "",
        # ── Risk factors ──────────────────────────────────────────────────────
        *_risk_section(risk_factors),
        "",
        f"{EMOJI['clock']} <i>{opp.timestamp.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]

    # ── Optional AI Ranking block ─────────────────────────────────────────────
    if ranking_result is not None:
        try:
            # ranking_result is a RankingResult – use duck-typing to avoid
            # a hard import here (engine imports alerts, not vice versa)
            ranking_block = getattr(ranking_result, "to_telegram_block")()
            if ranking_block:
                parts += ["", ranking_block]
        except Exception:
            pass  # never let ranking formatting break the main alert

    return "\n".join(parts)
    """
    Format a steam / sharp money move alert as a rich Telegram HTML message.

    Covers all required fields:
      alert type · sport/league · event · market · selection · odds movement
      steam score · books moving · sharp books · risk factors
    """
    if sharp_books is None:
        sharp_books = identify_sharp_books(alert.books_moved)
    if risk_factors is None:
        risk_factors = compute_steam_risk_factors(alert)

    icon        = sport_icon(alert.sport)
    dir_icon    = EMOJI["down"] if alert.steam_direction == "DOWN" else EMOJI["up"]
    dir_label   = "FALLING" if alert.steam_direction == "DOWN" else "RISING"
    books_str   = ", ".join(alert.books_moved) if alert.books_moved else "—"
    sharp_str   = ", ".join(sharp_books) if sharp_books else "None detected"
    num_books   = len(alert.books_moved)
    change      = alert.current_odds - alert.opening_odds

    # Steam score bar (visual 0–10 scale)
    filled = round(alert.steam_score / 10)
    score_bar = "█" * filled + "░" * (10 - filled)

    parts: list[str] = [
        # ── Header ────────────────────────────────────────────────────────────
        f"{EMOJI['sharp']} <b>SHARP MONEY ALERT</b> {EMOJI['sharp']}",
        "",
        # ── Event context ─────────────────────────────────────────────────────
        f"{icon} <b>{alert.sport.value}</b>  ·  {alert.market_type.value}",
        f"📋 <b>{alert.event}</b>",
        "",
        _div(),
        # ── Odds movement ─────────────────────────────────────────────────────
        f"{dir_icon} <b>{alert.selection}</b>  —  odds {dir_label}",
        f"  Opening:   <code>{format_odds(alert.opening_odds)}</code>",
        f"  Current:   <code>{format_odds(alert.current_odds)}</code>",
        f"  Change:    <code>{format_odds(change)}</code>",
        _div(),
        "",
        # ── Steam score ───────────────────────────────────────────────────────
        f"{EMOJI['fire']} <b>Steam Score:  {alert.steam_score}/100</b>",
        f"  <code>[{score_bar}]</code>",
        "",
        # ── Books ─────────────────────────────────────────────────────────────
        f"📚 <b>Books Moved</b>  ({num_books})",
        f"  All:      {books_str}",
        f"  ⚡ Sharp:  {sharp_str}",
    ]

    if alert.notes:
        parts += ["", f"{EMOJI['info']} <i>{alert.notes}</i>"]

        parts += [
        "",
        _div(),
        # ── Risk factors ──────────────────────────────────────────────────────
        *_risk_section(risk_factors),
        "",
        f"{EMOJI['clock']} <i>{alert.timestamp.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]

        return "\n".join(parts)


def format_steam_alert(
    alert,
    *,
    sharp_books=None,
    risk_factors=None,
) -> str:
    """Temporary stub — full implementation pending."""
    sport = getattr(alert, "sport", None)
    sport_str = getattr(sport, "value", str(sport)) if sport else "N/A"
    market = getattr(alert, "market_type", None)
    market_str = getattr(market, "value", str(market)) if market else "N/A"
    event = getattr(alert, "event", "N/A")
    selection = getattr(alert, "selection", "N/A")
    score = getattr(alert, "steam_score", 0)
    direction = getattr(alert, "steam_direction", "")
    books = getattr(alert, "books_moved", None) or []
    books_str = ", ".join(books) if books else "None"
    sharp_str = ", ".join(sharp_books) if sharp_books else books_str
    opening = getattr(alert, "opening_odds", None)
    current = getattr(alert, "current_odds", None)
    return (
        f"{EMOJI.get('fire', '🔥')} <b>SHARP MONEY ALERT</b>\n"
        f"<b>{sport_str}</b> {event}\n"
        f"Market: {market_str}\n"
        f"Selection: {selection}\n"
        f"Opening: {opening}  Current: {current}\n"
        f"Steam Score: {score}/100 {direction}\n"
        f"Books: {books_str}\n"
        f"Sharp: {sharp_str}\n"
        f"Risk factors: none\n"
        f"<code>[{'█' * (score // 10)}{'░' * (10 - score // 10)}]</code>"
    )


    # ── Status / help / start formatters ─────────────────────────────────────────

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


def format_ud_removal_alert(
    player_name: str,
    sport: str,
    stat_type: str,
    last_line: float,
    first_seen: "Optional[datetime]" = None,
    last_seen: "Optional[datetime]" = None,
    first_alert_sent_at: "Optional[datetime]" = None,
    reason: str = "pulled from board",
) -> str:
    """Format a removal alert for an Underdog prop that was previously alerted."""
    _fmt_dt = lambda dt: dt.strftime("%b %-d %H:%M UTC") if dt else "—"

    duration_str = "—"
    if first_seen and last_seen:
        secs = int((last_seen - first_seen).total_seconds())
        if secs < 3600:
            duration_str = f"{secs // 60}m"
        else:
            duration_str = f"{secs // 3600}h {(secs % 3600) // 60}m"

    return "\n".join([
        f"🚫 <b>UNDERDOG PROP REMOVED</b>",
        f"",
        f"<b>{player_name}</b>  ·  {sport}  ·  {stat_type}",
        f"Last line:       <b>{last_line:.1f}</b>",
        f"",
        f"First seen:      {_fmt_dt(first_seen)}",
        f"Last seen:       {_fmt_dt(last_seen)}",
        f"Duration:        {duration_str}",
        f"Originally alerted: {_fmt_dt(first_alert_sent_at)}",
        f"",
        f"Reason: <i>{reason}</i>",
    ])


def format_help_message() -> str:
    return "\n".join([
        f"{EMOJI['robot']} <b>Sharp Money Bot — Commands</b>",
        "",
        "<b>🐶 Player Prop Monitoring</b>",
        f"  /picks          — Actionable player prop picks by tier (S→A→B→C)",
        f"                    <i>/picks 5 · /picks MLB · /picks TENNIS 5</i>",
        f"  /slip [N]       — Build a prop slip from top N picks (2–6)",
        f"                    <i>/slip 3 · /slip 5</i>",
        f"  /dashboard      — Player prop activity, provider status, alert history",
        f"  /alerts         — Recent actionable pick alert history",
        f"  /grade          — Win/loss breakdown by tier for resolved picks",
        "",
        "<b>📊 Market Analysis</b>",
        f"  /market   — Line movement and validation summary",
        f"  /clv      — Closing Line Value history",
        "",
        "<b>📈 Performance</b>",
        f"  /performance    — Tier accuracy and pick performance by sport",
        f"  /backtest       — Replay resolved picks through the model",
        f"  /stats          — Pick generation stats and outcome summary",
        f"  /calibration    — Model calibration: tier accuracy vs outcomes",
        "",
        "<b>⚙️ System</b>",
        f"  /status     — Bot uptime, provider health, database counts",
        f"  /health     — Background job health: last run / last error",
        f"  /restarts   — Bot restart count and history",
        f"  /providers  — Player prop provider status",
        f"  /config     — Active configuration and alert scope",
        f"  /testalert  — Send a mock Actionable Pick Alert to verify delivery",
        f"  /help       — This menu",
        "",
        "<b>Primary Provider</b>",
        "  🐶 Underdog Fantasy (live monitoring)",
        "",
        f"<i>Monitoring Underdog Fantasy player props across multiple sports. Not betting advice.</i>",
    ])


def format_start_message() -> str:
    return "\n".join([
        f"{EMOJI['sharp']} <b>Sharp Money Bot — Player Prop Monitor</b>",
        "",
        "I monitor <b>Underdog Fantasy</b> player props across multiple sports",
        "and surface <b>actionable picks with clear direction and tier confidence</b>.",
        "",
        "<b>What I do:</b>",
        f"  🐶 Monitor Underdog Fantasy live (every 5 min)",
        f"  📈 Detect line movement and score each prop S / A / B / C tier",
        f"  🎯 Alert when a qualified OVER or UNDER pick is confirmed",
        f"  🗃️ Collect multi-sport data for grading and model learning",
        "",
        "<b>Alert format:</b>  🎯 ACTIONABLE BET PICK",
        "  Shows direction, tier, confidence score, and supporting data.",
        "  Only S / A / B / C tier props with a clear direction reach Telegram.",
        "",
        "Use /help to see all commands.",
        "",
        f"<i>Monitoring Underdog Fantasy player props. Not betting advice.</i>",
    ])


# ── Low-level Telegram senders ────────────────────────────────────────────────

async def send_alert(
    bot: Bot,
    chat_id: Union[int, str],
    message: str,
    keyboard: Optional[list[list[InlineKeyboardButton]]] = None,
) -> bool:
    """Send a formatted alert message. Returns True on success."""
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


# ── PrizePicks alert formatting ───────────────────────────────────────────────

def compute_pp_risk_factors(opp: "PPEdgeOpportunity") -> list[RiskFactor]:  # noqa: F821
    """
    Derive risk warnings for a PrizePicks edge opportunity.

    Checks: edge thinness, line discrepancy magnitude, fair probability
    vs threshold, and whether the probability adjustment is large.
    """
    from prizepicks import PPEdgeOpportunity  # local import to avoid circular
    factors: list[RiskFactor] = []

    if opp.best_edge < 7.0:
        factors.append(RiskFactor("MEDIUM", f"Thin edge ({opp.best_edge:.1f}%) — susceptible to line movement"))

    adj_prob = (
        opp.adjusted_fair_prob_over
        if opp.best_side == "OVER"
        else opp.adjusted_fair_prob_under
    )
    if adj_prob < 0.55:
        factors.append(RiskFactor("MEDIUM", f"Moderate fair probability ({adj_prob:.1%}) — not a clear edge"))

    if abs(opp.line_diff) > 2.0:
        factors.append(RiskFactor(
            "MEDIUM" if abs(opp.line_diff) > 3.0 else "LOW",
            f"Lines differ by {abs(opp.line_diff):.1f} units — probability model applied",
        ))

    if abs(opp.line_diff) == 0:
        factors.append(RiskFactor("LOW", "PP line matches sportsbook — direct fair-odds comparison"))

    return factors


def format_pp_alert(
    opp: "PPEdgeOpportunity",  # noqa: F821
    *,
    risk_factors: Optional[list[RiskFactor]] = None,
) -> str:
    """
    Format a PrizePicks edge opportunity as a rich Telegram HTML message.

    Covers: player · sport · stat type · PP line · sportsbook reference line ·
    odds · fair probability · edge % · best side · risk factors.
    """
    from prizepicks import PPEdgeOpportunity  # local import to avoid circular

    if risk_factors is None:
        risk_factors = compute_pp_risk_factors(opp)

    pp      = opp.pp_line
    s_icon  = _SPORT_EMOJI.get(pp.sport, "🎯")
    side_icon = EMOJI["up"] if opp.best_side == "OVER" else EMOJI["down"]

    adj_prob = (
        opp.adjusted_fair_prob_over
        if opp.best_side == "OVER"
        else opp.adjusted_fair_prob_under
    )

    lines_match = abs(opp.line_diff) < 0.5
    line_note   = (
        "exact match"
        if lines_match
        else f"diff {opp.line_diff:+.1f} units"
    )

    over_fmt  = format_odds(opp.sportsbook_over_odds)
    under_fmt = format_odds(opp.sportsbook_under_odds)

    parts = [
        f"🎯 <b>PRIZEPICKS EDGE DETECTED</b>",
        "",
        f"{s_icon} <b>{pp.sport} — {pp.stat_type}</b>",
        f"👤 <b>{pp.player_name}</b> ({pp.team})",
        "",
        f"📋 <b>PP Line:</b>     {pp.line_value} ({pp.stat_type})",
        f"📊 <b>SB Line:</b>     {opp.sportsbook_line} ({opp.sportsbook}) [{line_note}]",
        f"📉 <b>SB Odds:</b>     O {over_fmt} / U {under_fmt}",
        "",
        f"⚖️  <b>Fair Prob:</b>   {format_probability(adj_prob)} at PP line",
        f"{side_icon} <b>Best Side:</b>  {opp.best_side}",
        f"💰 <b>Edge:</b>        {opp.best_edge:+.2f}%",
    ]

    if opp.line_diff != 0:
        parts.append(
            f"🔢 <b>Line Model:</b>  {opp.prob_per_unit:.1f}% per unit "
            f"({opp.line_diff:+.1f} unit adj)"
        )

    if pp.game_description:
        parts.append(f"🏟️  <b>Game:</b>       {pp.game_description}")

    if pp.start_time:
        parts.append(f"🕐 <b>Start:</b>      {pp.start_time.strftime('%b %d %H:%M')} UTC")

    parts += [
        "",
        f"─────────────────────────────",
        *_risk_section(risk_factors),
        "",
        f"<i>Compare line at prizepicks.com before placing.</i>",
    ]

    return "\n".join(parts)


# ── PrizePicks reference alert formatter ──────────────────────────────────────

def format_pp_reference_alert(match: "PPReferenceMatch") -> str:  # noqa: F821
    """
    Format a PrizePicks reference alert derived from an Underdog prop.

    The 🟣 prefix and mandatory disclaimer clearly distinguish this from a
    🚨 Confirmed PrizePicks Alert.  The alert surfaces the Underdog line as
    a PP proxy, includes the ±0.5 uncertainty note, and the confidence score.

    Parameters
    ----------
    match:
        A PPReferenceMatch produced by engine.pp_reference.match_underdog_to_pp().
    """
    # Import here to avoid a circular dependency (pp_reference → alerts → pp_reference)
    from engine.pp_reference import PPReferenceMatch  # noqa: PLC0415

    sport      = match.sport
    s_icon     = _SPORT_EMOJI.get(sport.upper(), "🎯")
    conf_bar   = _pp_conf_bar(match.confidence)

    # Inferred PP line may equal UD line (proxy) or differ slightly (DB match)
    pp_line_str = f"{match.inferred_pp_line:.1f}"
    ud_line_str = f"{match.ud_line:.1f}"

    if match.pp_source == "prop_history_match" and match.pp_line_from_db is not None:
        pp_note = f"confirmed from PP history (UD: {ud_line_str})"
    else:
        pp_note = f"inferred from Underdog proxy (±0.5 uncertainty)"

    source_label = (
        "📋 PP History Match"
        if match.pp_source == "prop_history_match"
        else "🔍 Underdog Proxy"
    )

    lines = [
        f"🟣 <b>PRIZEPICKS REFERENCE ALERT</b>",
        "",
        f"{s_icon} <b>{sport}</b>  ·  {match.stat_type}",
        f"👤 <b>{match.player_name}</b>",
        "",
        f"─────────────────────────────",
        f"📊 <b>Underdog Line:</b>    {ud_line_str}",
        f"🎯 <b>Inferred PP Line:</b> {pp_line_str}",
        f"    <i>{pp_note}</i>",
        "",
        f"🔬 <b>Confidence:</b>  {match.confidence}/100  {conf_bar}",
        f"📡 <b>Source:</b>      {source_label}",
        f"",
        f"─────────────────────────────",
        f"⚠️  <b>Reference only — not confirmed PrizePicks data.</b>",
        f"    Underdog and PrizePicks lines typically match within ±0.5",
        f"    but can diverge. Always verify at prizepicks.com before",
        f"    placing a pick.",
        f"",
        f"{EMOJI['clock']} <i>{match.matched_at.strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]

    return "\n".join(lines)


def _pp_conf_bar(confidence: int) -> str:
    """Return a visual 0–10 scale bar for a PP reference confidence score."""
    filled = max(0, min(10, round(confidence / 10)))
    return f"<code>[{'█' * filled}{'░' * (10 - filled)}]</code>"


# ── AlertDelivery ─────────────────────────────────────────────────────────────

@dataclass
class DeliveryResult:
    """Result of a single alert delivery attempt."""
    sent: bool
    filtered: bool = False
    filtered_reason: str = ""
    deduped: bool = False
    recipients_sent: int = 0
    recipients_failed: int = 0

    def __str__(self) -> str:
        if self.filtered:
            return f"filtered({self.filtered_reason})"
        if self.deduped:
            return "deduped"
        return f"sent={self.sent} ({self.recipients_sent} ok, {self.recipients_failed} fail)"


class AlertDelivery:
    """
    Central alert delivery pipeline: filter → deduplicate → format → send → log.

    Usage
    ─────
    ::

        delivery = AlertDelivery(db, bot, chat_ids)
        result = await delivery.deliver_ev(opp)
        result = await delivery.deliver_steam(steam_alert)

    The class reads threshold defaults from ``config`` but accepts per-instance
    overrides so tests can inject tighter or looser filters without touching
    environment variables.
    """

    def __init__(
        self,
        db: "Database",
        bot: Bot,
        chat_ids: list[Union[int, str]],
        *,
        min_ev: Optional[float] = None,
        min_confidence: Optional[int] = None,
        min_steam: Optional[int] = None,
        ev_dedup_window: Optional[int] = None,
        steam_dedup_window: Optional[int] = None,
    ) -> None:
        self._db          = db
        self._bot         = bot
        self._chat_ids    = list(chat_ids)
        self._min_ev      = min_ev         if min_ev         is not None else config.MIN_EV_THRESHOLD
        self._min_conf    = min_confidence if min_confidence is not None else config.MIN_AI_CONFIDENCE
        self._min_steam   = min_steam      if min_steam      is not None else config.MIN_STEAM_SCORE
        self._ev_dedup    = ev_dedup_window    if ev_dedup_window    is not None else config.EV_DEDUP_WINDOW
        self._steam_dedup = steam_dedup_window if steam_dedup_window is not None else config.STEAM_DEDUP_WINDOW

    # ── EV delivery ───────────────────────────────────────────────────────────

    async def deliver_ev(self, opp: EVOpportunity) -> DeliveryResult:
        """
        Full EV alert pipeline:
          0. Scope filter (sport/market allowlist).
          1. Filter by min EV% and min AI confidence.
          2. Deduplicate against recently sent alerts in the DB.
          3. Compute risk factors, format message.
          4. Broadcast to all registered chat IDs.
          5. Log the alert (with alert_sent flag) to the database.
        """
        # 0. Scope filter
        from alert_normalizer import normalize_ev
        from alert_scope_filter import check
        scope = check(normalize_ev(opp))
        if not scope.allowed:
            return DeliveryResult(sent=False, filtered=True, filtered_reason=scope.reason)

        # 1. Filter
        if opp.expected_value < self._min_ev:
            return DeliveryResult(
                sent=False, filtered=True,
                filtered_reason=f"EV {opp.expected_value:.2f}% < min {self._min_ev:.2f}%",
            )
        if opp.ai_confidence < self._min_conf:
            return DeliveryResult(
                sent=False, filtered=True,
                filtered_reason=f"AI confidence {opp.ai_confidence} < min {self._min_conf}",
            )

        selection = opp.ev_result.selection

        # 2. Deduplicate
        if await self._db.has_recent_ev_alert(
            opp.event, selection, within_seconds=self._ev_dedup
        ):
            logger.debug("EV alert deduped: %s / %s", opp.event, selection)
            return DeliveryResult(sent=False, deduped=True)

        # 3. Format
        risk_factors = compute_ev_risk_factors(opp)
        message = format_ev_alert(opp, risk_factors=risk_factors)

        # 4. Send
        counts = await broadcast_alert(self._bot, self._chat_ids, message)
        alert_sent = counts["sent"] > 0

        # 5. Log
        await self._log_ev(opp, alert_sent=alert_sent)

        result = DeliveryResult(
            sent=alert_sent,
            recipients_sent=counts["sent"],
            recipients_failed=counts["failed"],
        )
        log_fn = logger.info if alert_sent else logger.warning
        log_fn(
            "EV alert: %s | %s | EV=%.2f%% conf=%d stars=%d → %s",
            opp.event, selection, opp.expected_value,
            opp.ai_confidence, opp.stars, result,
        )
        return result

    # ── Steam delivery ────────────────────────────────────────────────────────

    async def deliver_steam(self, alert: SteamAlert) -> DeliveryResult:
        """
        Full steam alert pipeline:
          0. Scope filter (sportsbook sharp money alerts are outside allowed scope).
          1. Filter by min steam score.
          2. Deduplicate against recently sent alerts in the DB.
          3. Identify sharp books, compute risk factors, format message.
          4. Broadcast to all registered chat IDs.
          5. Log the alert (with alert_sent flag) to the database.
        """
        # 0. Scope filter
        from alert_normalizer import normalize_steam
        from alert_scope_filter import check
        scope = check(normalize_steam(alert))
        if not scope.allowed:
            return DeliveryResult(sent=False, filtered=True, filtered_reason=scope.reason)

        # 1. Filter
        if alert.steam_score < self._min_steam:
            return DeliveryResult(
                sent=False, filtered=True,
                filtered_reason=f"Steam score {alert.steam_score} < min {self._min_steam}",
            )

        # 2. Deduplicate
        if await self._db.has_recent_steam_alert(
            alert.event, alert.selection, within_seconds=self._steam_dedup
        ):
            logger.debug("Steam alert deduped: %s / %s", alert.event, alert.selection)
            return DeliveryResult(sent=False, deduped=True)

        # 3. Format
        sharp_books  = identify_sharp_books(alert.books_moved)
        risk_factors = compute_steam_risk_factors(alert)
        message = format_steam_alert(alert, sharp_books=sharp_books, risk_factors=risk_factors)

        # 4. Send
        counts = await broadcast_alert(self._bot, self._chat_ids, message)
        alert_sent = counts["sent"] > 0

        # 5. Log
        await self._log_steam(alert, alert_sent=alert_sent)

        result = DeliveryResult(
            sent=alert_sent,
            recipients_sent=counts["sent"],
            recipients_failed=counts["failed"],
        )
        log_fn = logger.info if alert_sent else logger.warning
        log_fn(
            "Steam alert: %s | %s | score=%d → %s",
            alert.event, alert.selection, alert.steam_score, result,
        )
        return result

    # ── PP delivery ───────────────────────────────────────────────────────────

    async def deliver_pp(
        self,
        opp: "PPEdgeOpportunity",
        score: "Optional[PPAnalysisScore]" = None,
    ) -> "DeliveryResult":
        """
        Full PrizePicks alert pipeline:
          0. Scope filter.
          1. Game timing filter — block games already started or outside window.
          2. Daily alert cap check — S-tier always bypasses; A/B count against cap.
          3. format_pp_alert.
          4. Broadcast to all registered chat IDs.

        No DB-level dedup is applied here — dedup is handled upstream in
        _prizepicks_job by checking for recent PPEdgeRecords in the DB.
        """
        from alert_normalizer import normalize_pp
        from alert_scope_filter import check
        from engine.timing import is_game_alertable

        # 0. Scope filter
        norm_obj = normalize_pp(opp, score=score)
        scope    = check(norm_obj)
        if not scope.allowed:
            return DeliveryResult(sent=False, filtered=True, filtered_reason=scope.reason)

        # 1. Game timing filter
        game_time = getattr(opp.pp_line, "start_time", None)
        tier_str  = score.tier if score else None
        allowed, timing_reason = is_game_alertable(
            game_time,
            min_minutes=config.ALERT_WINDOW_MIN_MINUTES,
            max_minutes=config.ALERT_WINDOW_MAX_MINUTES,
            urgent_edge=config.URGENT_EDGE_THRESHOLD,
            edge=opp.best_edge,
        )
        if not allowed:
            logger.debug(
                "PP alert timing-blocked: %s | %s | %s",
                opp.pp_line.player_name, opp.pp_line.stat_type, timing_reason,
            )
            return DeliveryResult(sent=False, filtered=True, filtered_reason=timing_reason)

        # 2. Daily alert cap
        # S-tier always passes.  A/B tiers count against DAILY_ALERT_LIMIT.
        # Only A/B-tier records count toward the budget — S-tier volume never
        # reduces the A/B quota.
        if config.DAILY_ALERT_LIMIT > 0 and tier_str in ("A", "B"):
            today_sent = await self._db.count_today_pp_alerts(in_tiers=["A", "B"])
            if today_sent >= config.DAILY_ALERT_LIMIT:
                reason = (
                    f"Daily PP alert cap reached ({today_sent}/{config.DAILY_ALERT_LIMIT}) "
                    f"— tier {tier_str} suppressed (S-tier bypasses cap)"
                )
                logger.info(
                    "PP alert capped: %s | %s | %s",
                    opp.pp_line.player_name, opp.pp_line.stat_type, reason,
                )
                return DeliveryResult(sent=False, filtered=True, filtered_reason=reason)

        # 3. Format and send
        message = format_pp_alert(opp)
        counts  = await broadcast_alert(self._bot, self._chat_ids, message)
        alert_sent = counts["sent"] > 0

        result  = DeliveryResult(
            sent             = alert_sent,
            recipients_sent  = counts["sent"],
            recipients_failed= counts["failed"],
        )
        pp     = opp.pp_line
        log_fn = logger.info if alert_sent else logger.warning
        log_fn(
            "PP alert: %s | %s | %s | edge=+%.1f%% | tier=%s → %s",
            pp.player_name, pp.stat_type, opp.best_side, opp.best_edge, tier_str, result,
        )
        return result

    # ── Underdog delivery ─────────────────────────────────────────────────────

    async def deliver_underdog(
        self,
        player_name: str,
        team: str,
        sport: str,
        stat_type: str,
        old_line: float,
        new_line: float,
        game_time: "Optional[datetime]" = None,
        score: "Optional[object]" = None,           # UDPropScore — typed as object to avoid import
        validation: "Optional[object]" = None,      # PlayerPropValidation — typed as object
        decision: "Optional[object]" = None,        # UDBetDecision — typed as object
        market_quality: "Optional[object]" = None,  # MarketQuality — display context
        market_pressure: "Optional[object]" = None, # MarketPressureFlag — warning only
        pp_line: "Optional[float]" = None,          # PrizePicks line if available
        dk_line: "Optional[float]" = None,          # DraftKings line if available
        fd_line: "Optional[float]" = None,          # FanDuel line if available
        *,
        removed: bool = False,
        new_prop: bool = False,
        standing: bool = False,  # True for evidence-driven alerts without line movement
        removal_reason: Optional[str] = None,  # Why prop was removed (removal alerts only)
        opponent: Optional[str] = None,        # Opponent team/player (when available)
        intelligence_trace: Optional[dict] = None,  # prop_intelligence trace
        opening_line: Optional[float] = None,  # First ever line from PropLineHistory
        market_move_only: bool = False,  # True → send lightweight market-move alert only
        market_confirmation: Optional[dict] = None,  # OddsAPI sportsbook confirmation data
        high_priority: bool = False,  # True for 85–94/100 S-tier (4★+) → prepend 🔥 header
    ) -> "DeliveryResult":
        """
        Full Underdog prop alert pipeline:
          0. normalize_underdog → AlertObject.
          1. Scope check.
          2. Game timing filter — skipped for removals and new-prop alerts.
          3. Daily Underdog cap check (disabled by default; set DAILY_UNDERDOG_LIMIT > 0).
          4. Format and broadcast.
             - market_move_only=True → format_market_move_detected (📈 MARKET MOVE)
             - new_prop=True         → format_underdog_new_prop_alert (🚨 PROP LIVE)
             - removed=True          → format_underdog_change_alert   (🚫 REMOVED)
             - default               → format_underdog_change_alert   (🎯 BET PICK)
          5. Broadcast to all registered chat IDs.
        """
        from alert_normalizer import normalize_underdog
        from alert_scope_filter import check
        from alerts_multiplatform import (
            format_underdog_change_alert,
            format_underdog_new_prop_alert,
            format_market_move_detected,
        )
        from engine.timing import is_game_alertable

        # 0 & 1. Normalize + scope check
        norm_obj = normalize_underdog(player_name, stat_type, sport, is_removed=removed)
        scope    = check(norm_obj)
        if not scope.allowed:
            return DeliveryResult(sent=False, filtered=True, filtered_reason=scope.reason)

        # 2. Game timing filter
        # Skipped for removals (game may have ended) and new-prop alerts
        # (opportunity is now — timing gate must not suppress first-appearance alerts).
        if not removed and not new_prop:
            line_change = abs(new_line - old_line)
            allowed, timing_reason = is_game_alertable(
                game_time,
                min_minutes=config.ALERT_WINDOW_MIN_MINUTES,
                max_minutes=config.ALERT_WINDOW_MAX_MINUTES,
                urgent_edge=config.URGENT_EDGE_THRESHOLD,
                edge=line_change * 2.0,
            )
            if not allowed:
                logger.debug(
                    "Underdog alert timing-blocked: %s | %s | %s",
                    player_name, stat_type, timing_reason,
                )
                return DeliveryResult(sent=False, filtered=True, filtered_reason=timing_reason)

        # 3. Daily Underdog cap (disabled by default — DAILY_UNDERDOG_LIMIT=0)
        if config.DAILY_UNDERDOG_LIMIT > 0:
            today_ud = await self._db.count_today_underdog_alerts()
            if today_ud >= config.DAILY_UNDERDOG_LIMIT:
                reason = (
                    f"Daily Underdog alert cap reached "
                    f"({today_ud}/{config.DAILY_UNDERDOG_LIMIT})"
                )
                logger.info("Underdog alert capped: %s | %s | %s", player_name, stat_type, reason)
                return DeliveryResult(sent=False, filtered=True, filtered_reason=reason)

        # 4. Format
        if market_move_only:
            message = format_market_move_detected(
                player_name, team, sport, stat_type,
                old_line, new_line,
                game_time,
            )
        elif new_prop:
            message = format_underdog_new_prop_alert(
                player_name, team, sport, stat_type,
                new_line,
                game_time,
                score=score,
                validation=validation,
                decision=decision,
                market_quality=market_quality,
                market_pressure=market_pressure,
                pp_line=pp_line,
                dk_line=dk_line,
                fd_line=fd_line,
                low_line_threshold=config.UD_NEW_PROP_LOW_LINE_THRESHOLD,
                opponent=opponent,
                intelligence_trace=intelligence_trace,
                market_confirmation=market_confirmation,
            )
        else:
            message = format_underdog_change_alert(
                player_name, team, sport, stat_type,
                old_line, new_line,
                game_time,
                score=score,
                validation=validation,
                decision=decision,
                market_quality=market_quality,
                market_pressure=market_pressure,
                pp_line=pp_line,
                dk_line=dk_line,
                fd_line=fd_line,
                removed=removed,
                standing=standing,
                removal_reason=removal_reason,
                opponent=opponent,
                intelligence_trace=intelligence_trace,
                opening_line=opening_line,
                market_confirmation=market_confirmation,
            )

        # 5. Broadcast
        # 85–94/100 S-tier priority (4★+): prepend a priority header to the formatted message.
        # The header is prepended AFTER formatting so the existing alert body is unchanged.
        # 95+/100 props use the V3.3 override path (broadcast_alert directly) and never reach here.
        if high_priority and message and not removed:
            _hp_score = int(score.total) if score is not None else "??"
            message = (
                f"🔥 <b>S-TIER HIGH PRIORITY — {_hp_score}/100</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                + message
            )
        counts     = await broadcast_alert(self._bot, self._chat_ids, message)
        alert_sent = counts["sent"] > 0

        result = DeliveryResult(
            sent             = alert_sent,
            recipients_sent  = counts["sent"],
            recipients_failed= counts["failed"],
        )
        if new_prop:
            event_str = f"NEW_PROP line={new_line}"
        elif removed:
            event_str = "REMOVED"
        else:
            event_str = "HIGHER" if new_line > old_line else "LOWER"
        score_tag = (
            f" [tier={score.tier} stars={score.stars} score={score.total}]"
            if score is not None else ""
        )
        log_fn = logger.info if alert_sent else logger.warning
        log_fn(
            "Underdog alert: %s | %s | %s | %s%s → %s",
            player_name, stat_type, sport, event_str, score_tag, result,
        )
        return result

    # ── DB logging ────────────────────────────────────────────────────────────

    async def _log_ev(self, opp: EVOpportunity, *, alert_sent: bool) -> None:
        """Persist an EVRecord to the database."""
        from database import EVRecord
        record = EVRecord(
            sport=opp.sport.value,
            market_type=opp.market_type.value,
            event=opp.event,
            player=opp.player,
            selection=opp.ev_result.selection,
            line=opp.line,
            best_odds=opp.best_odds,
            best_book=opp.best_book,
            fair_probability=opp.fair_probability,
            expected_value=opp.expected_value,
            steam_score=opp.steam_score,
            ai_confidence=clamp_score(opp.ai_confidence, "ev.ai_confidence", 0, 100),
            recommendation=opp.recommendation.value,
            stars=opp.stars,
            reason_codes=",".join(opp.reason_codes),
            alert_sent=alert_sent,
            detected_at=opp.timestamp,
        )
        await self._db.save_ev(record)

    async def _log_steam(self, alert: SteamAlert, *, alert_sent: bool) -> None:
        """Persist a SteamRecord to the database."""
        from database import SteamRecord
        record = SteamRecord(
            alert_type=alert.alert_type.value,
            sport=alert.sport.value,
            market_type=alert.market_type.value,
            event=alert.event,
            selection=alert.selection,
            opening_odds=alert.opening_odds,
            current_odds=alert.current_odds,
            steam_score=alert.steam_score,
            steam_direction=alert.steam_direction,
            books_moved=",".join(alert.books_moved),
            notes=alert.notes,
            alert_sent=alert_sent,
            detected_at=alert.timestamp,
        )
        await self._db.save_steam(record)
