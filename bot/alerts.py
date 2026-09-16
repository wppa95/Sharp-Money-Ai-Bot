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
import inspect
import re
import time
from threading import Lock
from html import escape, unescape
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional, Union

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import config
from engine.delivery_gates import (
    directional_evidence_gate as _canonical_tier_delivery_gate,
)
_directional_evidence_gate = _canonical_tier_delivery_gate
from engine.score_validation import clamp_score
from engine.delivery_evidence import (
    is_history_optional_sport,
    serialized_evidence_supports_delivery,
)
from models import EVOpportunity, Recommendation, SteamAlert

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)

_DELIVERY_LINE_MAX_AGE = timedelta(hours=6)
_DELIVERY_CLOCK_SKEW = timedelta(minutes=5)


def _is_fresh_delivery_line(fetched_at: object) -> bool:
    """Return whether a provider line is recent enough for a betting alert.

    Missing timestamps remain compatible with older direct callers; live
    provider paths pass their observation timestamp and are rejected when it
    is materially stale.  The current-scan map remains the stronger freshness
    check for the main Underdog job.
    """
    if fetched_at is None:
        return True
    if not isinstance(fetched_at, datetime):
        # Compatibility callers and older records may not expose a typed
        # observation timestamp. Live provider paths always pass datetime.
        return True
    now = datetime.now(fetched_at.tzinfo) if fetched_at.tzinfo else datetime.utcnow()
    age = now - fetched_at
    return -_DELIVERY_CLOCK_SKEW <= age <= _DELIVERY_LINE_MAX_AGE


def _prop_claim_key(
    *,
    provider: str,
    source: str,
    event_id: str,
    player_name: str,
    sport: str,
    stat_type: str,
    direction: str,
) -> tuple[str, ...]:
    """Build the identity used by compatibility delivery claims."""
    return (
        str(provider or "").strip().upper(),
        str(source or "").strip().upper(),
        str(event_id or "").strip(),
        str(player_name or "").strip().casefold(),
        str(sport or "").strip().upper(),
        str(stat_type or "").strip().casefold(),
        str(direction or "").strip().upper(),
    )


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
    provider: str = "Underdog",
) -> str:
    """Format a provider-labeled removal alert for a previously alerted prop."""
    _fmt_dt = lambda dt: dt.strftime("%b %-d %H:%M UTC") if dt else "—"
    provider = str(provider or "Player Props").strip() or "Player Props"
    source_emoji = "🎯"

    duration_str = "—"
    if first_seen and last_seen:
        secs = int((last_seen - first_seen).total_seconds())
        if secs < 3600:
            duration_str = f"{secs // 60}m"
        else:
            duration_str = f"{secs // 3600}h {(secs % 3600) // 60}m"

    return "\n".join([
        f"🚫 <b>PLAYER PROP REMOVED</b>",
        f"",
        f"<b>{player_name}</b>  ·  {sport}  ·  {stat_type}",
        f"{source_emoji} Last Line: <b>{last_line:.1f}</b>",
        f"",
        f"First seen:      {_fmt_dt(first_seen)}",
        f"Last seen:       {_fmt_dt(last_seen)}",
        f"Duration:        {duration_str}",
        f"Originally alerted: {_fmt_dt(first_alert_sent_at)}",
        f"",
        f"Reason: <i>{reason}</i>",
    ])


def format_help_message(commands=None) -> str:
    """Render the persisted command catalog, with a safe legacy fallback."""
    if commands is not None:
        lines = [
            f"{EMOJI['robot']} <b>Sharp Money Bot</b>",
            "",
        ]
        current_category = None
        for row in commands:
            category = str(
                getattr(row, "category", None)
                or (row.get("category") if isinstance(row, dict) else "")
                or "System"
            )
            command = str(
                getattr(row, "command", None)
                or (row.get("command") if isinstance(row, dict) else "")
                or ""
            ).strip().lstrip("/")
            description = str(
                getattr(row, "description", None)
                or (row.get("description") if isinstance(row, dict) else "")
                or ""
            )
            usage = (
                getattr(row, "usage", None)
                if not isinstance(row, dict)
                else row.get("usage")
            )
            alias_of = (
                getattr(row, "alias_of", None)
                if not isinstance(row, dict)
                else row.get("alias_of")
            )
            if not command:
                continue
            if command.lower() in {"propdata", "pp_import"}:
                continue
            if category != current_category:
                if current_category is not None:
                    lines.append("")
                lines.append(f"<b>{escape(category)}</b>")
                current_category = category
            suffix = f"  <i>{escape(str(usage))}</i>" if usage else ""
            if alias_of:
                suffix += f"  <i>alias for /{escape(str(alias_of))}</i>"
            lines.append(
                f"  /{escape(command)} — {escape(description)}{suffix}"
            )
        lines.extend([
            "",
            "<b>Player Props</b> · Underdog",
        ])
        return "\n".join(lines)

    # Compatibility fallback for startup/tests before the database catalog is
    # initialized. Production /help always passes persisted rows.
    legacy_commands = [
        "picks", "slip", "dashboard", "alerts", "funnel", "reconcile",
        "grade", "grades", "player", "slipstats", "analyze", "market",
        "steam", "ev", "clv", "performance", "backtest", "stats",
        "calibration", "tracking", "analyst", "rollups", "learning",
        "refinement", "start", "status", "system", "health", "providers",
        "config", "blocks", "block", "backfill", "testalert", "help",
    ]
    return format_help_message([
        {
            "command": command,
            "description": "Available command",
            "category": "Commands",
        }
        for command in legacy_commands
    ])


def format_start_message() -> str:
    return "\n".join([
        f"{EMOJI['sharp']} <b>Sharp Money Bot</b>",
        "",
        "<b>Player Props</b> · Underdog",
        "Clear OVER and UNDER picks with supporting data.",
        "",
        "Use /help to see all commands.",
    ])


def is_telegram_suppressed_sport(sport: Optional[str]) -> bool:
    """Compatibility hook; all supported sports may reach normal delivery."""
    return False


def _has_strong_underdog_evidence(
    score: object = None,
    validation: object = None,
    decision: object = None,
    intelligence_trace: object = None,
    sport: str = "",
) -> bool:
    """Apply the terminal history/direction rule without importing market_engine."""
    # Polling snapshots are monitoring evidence, not completed player games.
    # Only real game-result evidence can qualify a Telegram PLAY.
    decision_direction = str(
        (
            decision.get("recommendation") or decision.get("rec") or ""
            if isinstance(decision, dict)
            else getattr(decision, "recommendation", "") or ""
        )
    ).upper()
    if decision_direction not in {"OVER", "UNDER"}:
        return False
    if is_history_optional_sport(sport):
        return True

    if bool(getattr(validation, "has_supporting_data", False)):
        source = getattr(validation, "data_source", None)
        # A supporting-data flag without provenance is not enough for a
        # production delivery.  Monitoring snapshots remain non-actionable.
        if not isinstance(source, str):
            return False
        source_lower = source.lower().strip()
        n_real = int(getattr(validation, "n_real_games", 0) or 0)
        n_supplemental = int(
            getattr(validation, "n_supplemental_games", 0) or 0
        )
        if source_lower in {"real", "primary"} and n_real >= 5:
            return True
        if source_lower == "propline" and n_supplemental >= 5:
            return True
        if source_lower in {"real+propline", "primary+propline"}:
            return n_real + n_supplemental >= 5
    for obj in (decision, intelligence_trace):
        if isinstance(obj, dict):
            if serialized_evidence_supports_delivery(
                obj,
                allow_historical_average=False,
            ):
                return True
            continue
        if obj is not None:
            for attr in ("evidence", "bet_evidence", "intelligence_trace"):
                value = getattr(obj, attr, None)
                if serialized_evidence_supports_delivery(
                    value,
                    allow_historical_average=False,
                ):
                    return True
    return False


# ── Low-level Telegram senders ────────────────────────────────────────────────

async def send_alert(
    bot: Bot,
    chat_id: Union[int, str],
    message: str,
    keyboard: Optional[list[list[InlineKeyboardButton]]] = None,
    *,
    sport: Optional[str] = None,
) -> bool:
    """Send a formatted alert message. Returns True on success."""
    # All live Telegram paths eventually pass through this low-level sender.
    # Keep internal scoring vocabulary out of user-facing alerts, including
    # legacy/direct broadcast callers that do not use the current formatters.
    message = _sanitize_user_alert_message(message)
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


def _sanitize_user_alert_message(message: str) -> str:
    """Remove internal classification/quality metadata at the Telegram boundary.

    This is deliberately narrow: evidence, direction, line movement, provider
    identity, and safety disclaimers are preserved. Internal tier, score,
    recommendation, confidence, rating, star, and quality values remain
    available to the engine/database.
    """
    text = str(message or "")
    text = re.sub(r"\b(?:S|A|B|C)[- ]?Tier\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:S/A/B/C|S/A|A/B)[- ]?Tier\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTier(?:[- ]?[12])?\b", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:WATCHLIST(?: ONLY)?|NEAR[- ]MISS)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # These are internal routing/signal labels, never user-facing Telegram
    # classifications.  Keep the actual PICK line intact.
    text = re.sub(
        r"(?:🔥\s*)?<b>\s*(?:STRONG UNDER|UNDER SIGNAL|OVER SIGNAL)\s*</b>"
        r"(?:\s*<i>\s*\(informational signal\)\s*</i>)?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:STRONG UNDER|UNDER SIGNAL|OVER SIGNAL)\b"
        r"\s*(?:\(informational signal\))?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bACTIONABLE(?: BET PICK)?\b", "", text, flags=re.IGNORECASE)
    # Normalize legacy provider-specific PLAY headers at the final boundary.
    # Provider identity remains in internal records; Telegram uses one
    # product-level Player Props label.
    text = re.sub(
        r"(?:🐶|🟣|🎯)\s*<b>\s*(?:UNDERDOG|PRIZEPICKS|PLAYER PROP)\s+PICK\s*</b>",
        "📊 <b>PLAYER PROP</b>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:UNDERDOG|PRIZEPICKS)\s+(?:PROP|LINE|PICK|PLAY)\b",
        "PLAYER PROP",
        text,
        flags=re.IGNORECASE,
    )
    # Quality remains an internal/reporting metric, never a Telegram PLAY
    # field. Strip both the current HTML form and legacy plain-text variants
    # at the final boundary so direct/older formatter callers stay compliant.
    text = re.sub(
        r"(?:^|\n)\s*(?:<b>\s*)?Quality:\s*(?:</b>\s*)?"
        r"(?:<code>\s*)?\d+(?:\.\d+)?\s*/\s*100(?:\s*</code>)?"
        r"(?:\s*[★☆]+)?\s*(?=\n|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Legacy formatters use several equivalent labels for internal quality
    # metadata. Remove complete lines so evidence, direction, and line context
    # are not damaged by substring replacements.
    _metadata_line_patterns = (
        r"\b(?:bet\s+)?quality\b",
        r"\bmarket\s+quality\b",
        r"\b(?:proxy\s+match\s+)?confidence\b",
        r"\b(?:ai\s+)?rating\b",
        r"\bstars?\b",
        r"\b(?:quality\s+)?score\b",
        r"\b(?:decision\s+)?tier\b",
        r"[★☆⭐🌟]{2,}",
    )
    text = "\n".join(
        line
        for line in text.splitlines()
        if not any(
            re.search(
                pattern,
                re.sub(r"<[^>]+>", "", line),
                flags=re.IGNORECASE,
            )
            for pattern in _metadata_line_patterns
        )
    )
    # Market Quality is also internal-only.  Remove the complete legacy
    # section, including its explanatory continuation line, without touching
    # ordinary market/source line text.
    _lines = text.splitlines()
    _clean_lines: list[str] = []
    _skip_market_detail = False
    for _line in _lines:
        _plain = re.sub(r"<[^>]+>", "", _line).strip()
        if _skip_market_detail:
            _skip_market_detail = False
            if re.search(
                r"(?:ELITE|STRONG|HIGH|MEDIUM|LOW|\d+\s*/\s*100|"
                r"How reliable is the market data)",
                _plain,
                flags=re.IGNORECASE,
            ):
                continue
        if re.search(r"\bMarket Quality\s*:", _plain, flags=re.IGNORECASE):
            continue
        if re.search(
            r"\bMarket\s*:\s*(?:ELITE|STRONG|HIGH|MEDIUM|LOW)\b",
            _plain,
            flags=re.IGNORECASE,
        ):
            continue
        if re.search(r"\bMARKET\s+(?:ELITE|STRONG|HIGH|MEDIUM|LOW)\b", _plain, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r"MARKET", _plain, flags=re.IGNORECASE):
            _skip_market_detail = True
            continue
        _clean_lines.append(_line)
    text = "\n".join(_clean_lines)
    # Keep internal routing and lifecycle labels out of Telegram while
    # preserving evidence and provider context. Those two fields explain why
    # the pick exists and are part of the user-facing alert contract.
    _hidden_labels = re.compile(
        r"\b(?:source|reason(?:s| codes)?|status)\b\s*:",
        flags=re.IGNORECASE,
    )
    text = "\n".join(
        line
        for line in text.splitlines()
        if not _hidden_labels.search(unescape(re.sub(r"<[^>]+>", "", line)))
        and "UTC" not in unescape(re.sub(r"<[^>]+>", "", line)).upper()
    )
    text = re.sub(r"\bdecision_pass\b", "no-direction", text, flags=re.IGNORECASE)
    text = re.sub(r"\bnot_immediate\b", "not yet actionable", text, flags=re.IGNORECASE)
    # PASS is an internal recommendation state, not a user-facing outcome.
    # Restrict the replacement to the standalone word so "passed" and
    # ordinary prose remain untouched.
    text = re.sub(r"\bPASS\b", "NO DIRECTION", text, flags=re.IGNORECASE)
    return re.sub(r"[ \t]{2,}", " ", text)


def _has_strong_bet_label(message: str) -> bool:
    """Return whether a rendered alert contains the required pick label."""
    for line in str(message or "").splitlines():
        plain = unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if re.match(r"^💪\s*STRONG\s+BET(?:\b|$)", plain, flags=re.IGNORECASE):
            return True
    return False


async def broadcast_alert(
    bot: Bot,
    chat_ids: list[Union[int, str]],
    message: str,
    *,
    sport: Optional[str] = None,
) -> dict[str, int]:
    """
    Broadcast an alert to multiple chat IDs.
    Returns {"sent": n, "failed": m}.

    Delivery qualification is enforced before this low-level sender.
    """
    # Direct/legacy callers do not carry the structured decision and evidence
    # objects used by AlertDelivery. Retired internal labels therefore make the
    # message ineligible instead of allowing this low-level sender to bypass
    # the actionable-pick boundary.
    if re.search(
        r"\b(?:WATCHLIST|NEAR[- ]?MISS|decision_pass|NO\s+DIRECTION)\b",
        str(message or ""),
        flags=re.IGNORECASE,
    ):
        return {"sent": 0, "failed": 0}

    message = _sanitize_user_alert_message(message)
    sent = failed = 0
    for cid in chat_ids:
        if await send_alert(bot, cid, message, sport=sport):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}


async def send_discord_alert(message: str) -> bool:
    """Send an actionable alert to the configured Discord channel.

    Discord is a best-effort companion channel.  It deliberately has no
    qualification logic of its own: callers invoke it only after the
    canonical Telegram gate has passed.
    """
    if not config.discord_configured:
        return False

    content = unescape(re.sub(r"<[^>]+>", "", _sanitize_user_alert_message(message))).strip()
    if len(content) > 2000:
        content = content[:1997] + "..."

    try:
        import aiohttp

        url = f"https://discord.com/api/v10/channels/{config.DISCORD_CHANNEL_ID}/messages"
        headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json={"content": content},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status not in (200, 201):
                    logger.warning(
                        "Discord alert failed: HTTP %s",
                        response.status,
                    )
                    return False
        return True
    except Exception as exc:  # Discord must never affect the monitoring cycle.
        logger.warning("Discord alert failed (non-fatal): %s", exc)
        return False


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
        f"📊 <b>PLAYER PROP</b>",
        "",
        f"{s_icon} <b>{pp.sport} — {pp.stat_type}</b>",
        f"👤 <b>{pp.player_name}</b> ({pp.team})",
        "",
        "📡 <b>Source:</b>  Player Props · PrizePicks / PropLine",
        f"🎯 <b>LINE:</b>  {pp.line_value} ({pp.stat_type})",
        f"📊 <b>Reference Line:</b>   {opp.sportsbook_line} ({opp.sportsbook}) [{line_note}]",
        f"📉 <b>SB Odds:</b>     O {over_fmt} / U {under_fmt}",
        "",
        f"⚖️  <b>Fair Prob:</b>   {format_probability(adj_prob)} at PP line",
        f"📌 <b>PICK:</b>  {side_icon} <b>{opp.best_side} {pp.line_value}</b>",
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
        f"<i>Line and market context are shown for research.</i>",
    ]

    return "\n".join(parts)


# ── PrizePicks reference alert formatter ──────────────────────────────────────

def format_pp_reference_alert(match: "PPReferenceMatch") -> str:  # noqa: F821
    """
    Format a PrizePicks reference alert derived from an Underdog prop.

    The 🟣 prefix and mandatory disclaimer clearly distinguish this from a
    🚨 Confirmed PrizePicks Alert.  The alert surfaces the Underdog line as
    a PP proxy and includes the ±0.5 uncertainty note. Matching confidence
    remains internal metadata.

    Parameters
    ----------
    match:
        A PPReferenceMatch produced by engine.pp_reference.match_underdog_to_pp().
    """
    # Import here to avoid a circular dependency (pp_reference → alerts → pp_reference)
    from engine.pp_reference import PPReferenceMatch  # noqa: PLC0415

    sport      = match.sport
    s_icon     = _SPORT_EMOJI.get(sport.upper(), "🎯")
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
        f"🎯 <b>PLAYER PROP REFERENCE ALERT</b>",
        "",
        f"{s_icon} <b>{sport}</b>  ·  {match.stat_type}",
        f"👤 <b>{match.player_name}</b>",
        "",
        f"─────────────────────────────",
        f"🎯 <b>LINE:</b>    {ud_line_str}",
        f"🎯 <b>REFERENCE LINE:</b> {pp_line_str}",
        f"    <i>{pp_note}</i>",
        "",
        f"📡 <b>Player Props Source:</b> {source_label}",
        f"",
        f"─────────────────────────────",
        f"⚠️  <b>Reference only — not confirmed provider data.</b>",
        f"    Lines typically match within ±0.5",
        f"    but can diverge.",
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
    rate_limited: bool = False   # True when blocked by global Telegram rate limiter
    recipients_sent: int = 0
    recipients_failed: int = 0
    # True only when an actionable Telegram send has a durable, identity-linked
    # confirmation record. Compatibility callers that construct a result
    # manually remain accepted by engine bookkeeping via getattr(..., True).
    authoritative_recorded: bool = False

    def __str__(self) -> str:
        if self.rate_limited:
            return f"rate-limited({self.filtered_reason})"
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
        # Compatibility delivery methods can be called outside market_engine's
        # shared claim lock. Keep a per-delivery-instance claim store so a
        # concurrent caller cannot send the same provider/source/event pick
        # twice, while avoiding cross-test and cross-bot global state.
        self._prop_delivery_claims: dict[tuple[str, ...], tuple[float, float]] = {}
        self._prop_delivery_claim_lock = Lock()

    def _try_claim_prop_delivery(
        self,
        *,
        provider: str,
        source: str,
        event_id: str,
        player_name: str,
        sport: str,
        stat_type: str,
        direction: str,
        line_value: float,
    ) -> bool:
        """Atomically claim a compatibility prop delivery slot."""
        key = _prop_claim_key(
            provider=provider,
            source=source,
            event_id=event_id,
            player_name=player_name,
            sport=sport,
            stat_type=stat_type,
            direction=direction,
        )
        now = time.time()
        with self._prop_delivery_claim_lock:
            current = self._prop_delivery_claims.get(key)
            if current is not None:
                claimed_at, _claimed_line = current
                if now - claimed_at < config.UD_ALERT_DEDUP_WINDOW:
                    return False

            # Source/event IDs can rotate between provider refreshes. Do not
            # let that turn one logical directional pick into repeated
            # compatibility alerts while the dedup window is active.
            logical_identity = (
                key[0],
                key[3],
                key[4],
                key[5],
                key[6],
            )
            for stored_key, (claimed_at, _claimed_line) in self._prop_delivery_claims.items():
                if len(stored_key) < 7:
                    continue
                stored_identity = (
                    stored_key[0],
                    stored_key[3],
                    stored_key[4],
                    stored_key[5],
                    stored_key[6],
                )
                if (
                    stored_identity == logical_identity
                    and now - claimed_at < config.UD_ALERT_DEDUP_WINDOW
                ):
                    return False
            self._prop_delivery_claims[key] = (now, float(line_value))
            return True

    def _release_prop_delivery_claim(
        self,
        *,
        provider: str,
        source: str,
        event_id: str,
        player_name: str,
        sport: str,
        stat_type: str,
        direction: str,
        line_value: float,
    ) -> None:
        """Release a compatibility claim unless another line replaced it."""
        key = _prop_claim_key(
            provider=provider,
            source=source,
            event_id=event_id,
            player_name=player_name,
            sport=sport,
            stat_type=stat_type,
            direction=direction,
        )
        with self._prop_delivery_claim_lock:
            current = self._prop_delivery_claims.get(key)
            if current is not None and abs(float(current[1]) - float(line_value)) < 1e-9:
                self._prop_delivery_claims.pop(key, None)

    async def _audit_telegram_attempt(
        self,
        *,
        provider: str,
        alert_kind: str,
        delivery_path: str,
        player_name: Optional[str],
        sport: Optional[str],
        stat_type: Optional[str],
        external_id: Optional[str],
        event_id: Optional[str],
        line_value: Optional[float],
        outcome: str,
        recipients_sent: int,
        recipients_failed: int,
    ) -> bool:
        """Persist delivery accounting and report authoritative PLAY success."""
        if (
            str(alert_kind).upper() == "ACTIONABLE_PICK"
            and str(outcome).lower() == "sent"
        ):
            record = getattr(self._db, "record_telegram_play_delivery", None)
            if record is not None:
                try:
                    result = record(
                        provider=provider,
                        delivery_path=delivery_path,
                        player_name=player_name,
                        sport=sport,
                        stat_type=stat_type,
                        external_id=external_id,
                        event_id=event_id,
                        line_value=line_value,
                        recipients_sent=recipients_sent,
                        recipients_failed=recipients_failed,
                    )
                    if inspect.isawaitable(result):
                        result = await result
                    return bool(result)
                except Exception as exc:
                    logger.warning(
                        "Authoritative Telegram PLAY record failed: %s",
                        type(exc).__name__,
                    )
                    return False

        method = getattr(self._db, "log_telegram_delivery", None)
        if method is None:
            return False
        try:
            result = method(
                provider=provider,
                alert_kind=alert_kind,
                delivery_path=delivery_path,
                player_name=player_name,
                sport=sport,
                stat_type=stat_type,
                external_id=external_id,
                event_id=event_id,
                line_value=line_value,
                outcome=outcome,
                recipients_sent=recipients_sent,
                recipients_failed=recipients_failed,
            )
            if inspect.isawaitable(result):
                result = await result
            return bool(result) if isinstance(result, (bool, int)) else True
        except Exception as exc:
            logger.warning("Telegram delivery audit write failed: %s", exc)
            return False

    async def _mark_play_delivered(
        self,
        *,
        provider: str,
        external_id: Optional[str],
        stat_type: Optional[str],
    ) -> None:
        """Mark the exact opportunity immediately after a successful PLAY send."""
        if not external_id or not stat_type:
            logger.warning(
                "PLAY delivery succeeded without source identity: provider=%s stat=%s",
                provider,
                stat_type,
            )
            return
        method = getattr(self._db, "mark_opportunity_alert_sent", None)
        if method is None:
            return
        try:
            result = method(external_id, stat_type, provider=provider)
            if inspect.isawaitable(result):
                result = await result
            if result == 0:
                logger.warning(
                    "PLAY delivery source did not match an opportunity: "
                    "provider=%s external_id=%s stat=%s",
                    provider,
                    external_id,
                    stat_type,
                )
        except Exception as exc:
            logger.warning(
                "PLAY delivery bookkeeping failed: provider=%s external_id=%s "
                "stat=%s error=%s",
                provider,
                external_id,
                stat_type,
                type(exc).__name__,
            )

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
        counts = await broadcast_alert(
            self._bot,
            self._chat_ids,
            message,
            sport=getattr(opp, "sport", None),
        )
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
        _steam_sport = getattr(alert, "sport", None)
        counts = await broadcast_alert(self._bot, self._chat_ids, message, sport=_steam_sport)
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
        *,
        strict_pregame: bool = False,
        evidence_available: bool = False,
    ) -> "DeliveryResult":
        """
        Full PrizePicks alert pipeline:
          0. Scope filter.
          1. Game timing filter — block games already started or outside window.
          2. Require a valid OVER/UNDER direction and legitimate evidence.
          3. format_pp_alert.
          4. Broadcast to all registered chat IDs.

        The active market-engine queue has a shared claim lock; this
        compatibility method also claims atomically before its send.
        """
        # Compatibility hook for non-Underdog alert paths.
        _pp_sport = getattr(getattr(opp, "pp_line", None), "sport", None) or ""
        if is_telegram_suppressed_sport(_pp_sport):
            logger.info(
                "deliver_pp: sport suppressed — sport=%s player=%s stat=%s",
                _pp_sport,
                getattr(getattr(opp, "pp_line", None), "player_name", "?"),
                getattr(getattr(opp, "pp_line", None), "stat_type", "?"),
            )
            return DeliveryResult(sent=False, filtered=True,
                                  filtered_reason=f"Sport suppressed (temporary): {_pp_sport}")

        from alert_normalizer import normalize_pp
        from alert_scope_filter import check
        from engine.timing import is_game_alertable

        if not _is_fresh_delivery_line(getattr(opp.pp_line, "fetched_at", None)):
            logger.debug(
                "PP alert freshness-blocked: %s | %s",
                getattr(opp.pp_line, "player_name", "?"),
                getattr(opp.pp_line, "stat_type", "?"),
            )
            return DeliveryResult(
                sent=False,
                filtered=True,
                filtered_reason="PrizePicks line is stale",
            )

        # 0. Scope filter
        norm_obj = normalize_pp(opp, score=score)
        scope    = check(norm_obj)
        if not scope.allowed:
            return DeliveryResult(sent=False, filtered=True, filtered_reason=scope.reason)

        # 1. Game timing filter
        game_time = getattr(opp.pp_line, "start_time", None)
        allowed, timing_reason = is_game_alertable(
            game_time,
            min_minutes=config.ALERT_WINDOW_MIN_MINUTES,
            max_minutes=config.ALERT_WINDOW_MAX_MINUTES,
            urgent_edge=config.URGENT_EDGE_THRESHOLD,
            edge=opp.best_edge,
            game_status=(
                getattr(opp.pp_line, "provider_status", None)
                or getattr(opp.pp_line, "game_status", None)
            ),
            require_known=strict_pregame,
        )
        if not allowed:
            logger.debug(
                "PP alert timing-blocked: %s | %s | %s",
                opp.pp_line.player_name, opp.pp_line.stat_type, timing_reason,
            )
            return DeliveryResult(sent=False, filtered=True, filtered_reason=timing_reason)

        # 2. Canonical actionable qualification. Scope, timing, deduplication,
        # flood protection, and delivery failures remain independent
        # safety/integrity protections.
        _pp_direction = str(getattr(opp, "best_side", "") or "").strip().upper()
        if not _directional_evidence_gate(_pp_direction, evidence_available):
            return DeliveryResult(
                sent=False,
                filtered=True,
                filtered_reason="Canonical direction/evidence gate",
            )

        _pp_line_value = float(getattr(opp.pp_line, "line_value", 0) or 0)
        _pp_event_id = (
            getattr(opp.pp_line, "event_id", "")
            or getattr(opp.pp_line, "game_id", "")
        )
        if not self._try_claim_prop_delivery(
            provider="PrizePicks",
            source="PrizePicks",
            event_id=str(_pp_event_id or ""),
            player_name=getattr(opp.pp_line, "player_name", ""),
            sport=_pp_sport,
            stat_type=getattr(opp.pp_line, "stat_type", ""),
            direction=_pp_direction,
            line_value=_pp_line_value,
        ):
            return DeliveryResult(sent=False, deduped=True)

        # 3. Format and send
        try:
            message = format_pp_alert(opp)
            counts  = await broadcast_alert(
                self._bot,
                self._chat_ids,
                message,
                sport=_pp_sport,
            )
        except Exception:
            self._release_prop_delivery_claim(
                provider="PrizePicks",
                source="PrizePicks",
                event_id=str(_pp_event_id or ""),
                player_name=getattr(opp.pp_line, "player_name", ""),
                sport=_pp_sport,
                stat_type=getattr(opp.pp_line, "stat_type", ""),
                direction=_pp_direction,
                line_value=_pp_line_value,
            )
            raise
        alert_sent = counts["sent"] > 0
        if not alert_sent:
            self._release_prop_delivery_claim(
                provider="PrizePicks",
                source="PrizePicks",
                event_id=str(_pp_event_id or ""),
                player_name=getattr(opp.pp_line, "player_name", ""),
                sport=_pp_sport,
                stat_type=getattr(opp.pp_line, "stat_type", ""),
                direction=_pp_direction,
                line_value=_pp_line_value,
            )
        _audit_ok = await self._audit_telegram_attempt(
            provider="PrizePicks",
            alert_kind="ACTIONABLE_PICK",
            delivery_path="prizepicks",
            player_name=getattr(opp.pp_line, "player_name", None),
            sport=_pp_sport,
            stat_type=getattr(opp.pp_line, "stat_type", None),
            external_id=getattr(opp.pp_line, "external_id", None),
            event_id=str(
                getattr(opp.pp_line, "event_id", "")
                or getattr(opp.pp_line, "game_id", "")
                or ""
            ),
            line_value=getattr(opp.pp_line, "line_value", None),
            outcome="sent" if alert_sent else "failed",
            recipients_sent=counts["sent"],
            recipients_failed=counts["failed"],
        )
        if (
            alert_sent
            and not hasattr(self._db, "record_telegram_play_delivery")
            and _audit_ok
        ):
            await self._mark_play_delivered(
                provider="PrizePicks",
                external_id=getattr(opp.pp_line, "external_id", None),
                stat_type=getattr(opp.pp_line, "stat_type", None),
            )

        result  = DeliveryResult(
            sent             = alert_sent,
            recipients_sent  = counts["sent"],
            recipients_failed= counts["failed"],
            authoritative_recorded = _audit_ok if alert_sent else False,
        )
        pp     = opp.pp_line
        log_fn = logger.info if alert_sent else logger.warning
        log_fn(
            "PP alert: %s | %s | %s | edge=+%.1f%% → %s",
            pp.player_name, pp.stat_type, opp.best_side, opp.best_edge, result,
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
        high_priority: bool = False,  # Legacy priority-display compatibility flag
        is_moneyline: bool = False,
        game_status: Optional[str] = None,
        strict_pregame: bool = False,
        fetched_at: Optional[datetime] = None,
        source: str = "Underdog",
        source_id: Optional[str] = None,
        event_id: Optional[str] = None,
        delivery_path: Optional[str] = None,
    ) -> "DeliveryResult":
        """
        Full Underdog prop alert pipeline:
          0. normalize_underdog → AlertObject.
          1. Scope check.
          2. Game timing filter — skipped only for removals.
          3. Daily Underdog cap check (disabled by default; set DAILY_UNDERDOG_LIMIT > 0).
          4. Canonical direction-and-evidence gate.
          5. Format and broadcast only after an atomic claim.
        """
        source = source.strip() or "Underdog"
        source_upper = source.upper()
        if source_upper not in {"UNDERDOG", "PRIZEPICKS"}:
            return DeliveryResult(
                sent=False,
                filtered=True,
                filtered_reason=f"Unsupported prop provider/source: {source}",
            )
        from alert_normalizer import normalize_underdog
        from alert_scope_filter import check
        from alerts_multiplatform import (
            format_underdog_change_alert,
            format_underdog_new_prop_alert,
            format_market_move_detected,
        )
        from engine.timing import is_game_alertable

        if not _is_fresh_delivery_line(fetched_at):
            logger.debug(
                "UD alert freshness-blocked: %s | %s | %s",
                player_name,
                stat_type,
                sport,
            )
            return DeliveryResult(
                sent=False,
                filtered=True,
                filtered_reason="Underdog line is stale",
            )

        # 0 & 1. Normalize + scope check
        norm_obj = normalize_underdog(player_name, stat_type, sport, is_removed=removed)
        scope    = check(norm_obj)
        if not scope.allowed:
            return DeliveryResult(sent=False, filtered=True, filtered_reason=scope.reason)

        # 2. Game timing filter.  Removal records may arrive after the event,
        # but no new or re-entry prop may bypass the started-event check.
        if not removed:
            line_change = abs(new_line - old_line)
            # A Telegram pick must have a known pregame state.  Some active
            # feeds omit scheduled_at, but allowing those rows through when
            # status is also blank/stale can send a pick after the event has
            # started or ended.  Monitoring may retain unknown-timing rows;
            # actionable delivery fails closed.
            _strict_pregame_required = strict_pregame
            allowed, timing_reason = is_game_alertable(
                game_time,
                min_minutes=0,
                max_minutes=1440,
                urgent_edge=config.URGENT_EDGE_THRESHOLD,
                edge=line_change * 2.0,
                game_status=game_status,
                require_known=_strict_pregame_required,
            )
            if not allowed:
                logger.debug(
                    "Underdog alert timing-blocked: %s | %s | %s",
                    player_name, stat_type, timing_reason,
                )
                return DeliveryResult(sent=False, filtered=True, filtered_reason=timing_reason)

        # 3. Daily Underdog cap (PrizePicks has its own source identity and
        # must never consume or inherit the Underdog cap).
        if source_upper == "UNDERDOG" and config.DAILY_UNDERDOG_LIMIT > 0:
            today_ud = await self._db.count_today_underdog_alerts()
            if today_ud >= config.DAILY_UNDERDOG_LIMIT:
                reason = (
                    f"Daily Underdog alert cap reached "
                    f"({today_ud}/{config.DAILY_UNDERDOG_LIMIT})"
                )
                logger.info("Underdog alert capped: %s | %s | %s", player_name, stat_type, reason)
                return DeliveryResult(sent=False, filtered=True, filtered_reason=reason)

        # Removal and market-movement records are monitoring observations,
        # never actionable betting picks.  Keep the low-level formatter
        # available for historical/internal callers, but do not let a direct
        # Telegram path turn either event into a user-facing prop alert.
        if removed or market_move_only:
            return DeliveryResult(
                sent=False,
                filtered=True,
                filtered_reason="Monitoring-only event is not Telegram actionable",
            )

        # Every prop Telegram path uses the same canonical gate. Market
        # movement, removal, new-prop, and moneyline labels are monitoring
        # context; none is an actionable direction/evidence substitute.
        _direction = str(getattr(decision, "recommendation", "") or "").upper()
        evidence_available = _has_strong_underdog_evidence(
            score=score,
            validation=validation,
            decision=decision,
            intelligence_trace=intelligence_trace,
            sport=sport,
        )
        if not _canonical_tier_delivery_gate(
            _direction,
            evidence_available=evidence_available,
        ):
            return DeliveryResult(
                sent=False,
                filtered=True,
                filtered_reason="Canonical direction/evidence gate",
            )

        # Telegram is the final edge of the durable tracking/grading funnel.
        # The active scan may evaluate many observations, but only a prop
        # already represented in both ledgers can become a user-facing pick.
        # ``alert_sent`` is a persistent one-alert boundary across restarts and
        # line re-evaluations; it is intentionally checked here, before any
        # formatting or Telegram network call.
        _tracking_gate = getattr(self._db, "get_telegram_delivery_state", None)
        if _tracking_gate is not None:
            try:
                _tracking_state = _tracking_gate(
                    provider=source,
                    external_id=source_id,
                    stat_type=stat_type,
                )
                if inspect.isawaitable(_tracking_state):
                    _tracking_state = await _tracking_state
            except Exception as _tracking_gate_exc:
                logger.warning(
                    "Telegram delivery blocked: tracking funnel lookup failed "
                    "for %s/%s: %s",
                    player_name,
                    stat_type,
                    type(_tracking_gate_exc).__name__,
                )
                return DeliveryResult(
                    sent=False,
                    filtered=True,
                    filtered_reason="Tracking/grading funnel unavailable",
                )
            # Test doubles and legacy adapters may not expose the async
            # database contract.  The real Database always returns this dict;
            # keep compatibility adapters on the existing direction/evidence
            # path rather than treating MagicMock attributes as a decision.
            if isinstance(_tracking_state, dict):
                if not _tracking_state.get("tracked"):
                    return DeliveryResult(
                        sent=False,
                        filtered=True,
                        filtered_reason="Prop is not in tracking ledger",
                    )
                if not _tracking_state.get("directional"):
                    return DeliveryResult(
                        sent=False,
                        filtered=True,
                        filtered_reason="No persisted directional opportunity",
                    )
                if _tracking_state.get("already_delivered"):
                    return DeliveryResult(
                        sent=False,
                        filtered=True,
                        deduped=True,
                    )

        _ud_line_value = float(new_line or 0)
        if not self._try_claim_prop_delivery(
            provider=source,
            source=source,
            event_id=str(event_id or ""),
            player_name=player_name,
            sport=sport,
            stat_type=stat_type,
            direction=_direction,
            line_value=_ud_line_value,
        ):
            return DeliveryResult(sent=False, deduped=True)


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
                source=source,
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
                source=source,
            )

        # 5. Broadcast
        try:
            counts = await broadcast_alert(self._bot, self._chat_ids, message, sport=sport)
        except Exception:
            self._release_prop_delivery_claim(
                provider=source,
                source=source,
                event_id=str(event_id or ""),
                player_name=player_name,
                sport=sport,
                stat_type=stat_type,
                direction=_direction,
                line_value=_ud_line_value,
            )
            raise
        discord_sent = False
        if not removed and not market_move_only and counts["sent"] > 0:
            discord_sent = await send_discord_alert(message)
        alert_sent = counts["sent"] > 0
        if not alert_sent:
            self._release_prop_delivery_claim(
                provider=source,
                source=source,
                event_id=str(event_id or ""),
                player_name=player_name,
                sport=sport,
                stat_type=stat_type,
                direction=_direction,
                line_value=_ud_line_value,
            )
        _alert_kind = (
            "REMOVAL" if removed
            else "MARKET_MOVE" if market_move_only
            else "ACTIONABLE_PICK"
        )
        _audit_ok = await self._audit_telegram_attempt(
            provider=source,
            alert_kind=_alert_kind,
            delivery_path=(
                delivery_path
                or (
                    "prizepicks"
                    if str(source).strip().lower() == "prizepicks"
                    else "underdog"
                )
            ),
            player_name=player_name,
            sport=sport,
            stat_type=stat_type,
            external_id=source_id,
            event_id=event_id,
            line_value=new_line,
            outcome="sent" if alert_sent else "failed",
            recipients_sent=counts["sent"],
            recipients_failed=counts["failed"],
        )
        if (
            alert_sent
            and _alert_kind == "ACTIONABLE_PICK"
            and not hasattr(self._db, "record_telegram_play_delivery")
            and _audit_ok
        ):
            await self._mark_play_delivered(
                provider=source,
                external_id=source_id,
                stat_type=stat_type,
            )

        

        result = DeliveryResult(
            sent             = alert_sent,
            recipients_sent  = counts["sent"],
            recipients_failed= counts["failed"],
            authoritative_recorded = _audit_ok if alert_sent else False,
        )
        if new_prop:
            event_str = f"NEW_PROP line={new_line}"
        elif removed:
            event_str = "REMOVED"
        else:
            event_str = "HIGHER" if new_line > old_line else "LOWER"
        score_tag = (
            f" [stars={score.stars} score={score.total}]"
            if score is not None else ""
        )
        log_fn = logger.info if alert_sent else logger.warning
        log_fn(
            "Underdog alert: %s | %s | %s | %s%s → %s (discord=%s)",
            player_name, stat_type, sport, event_str, score_tag, result,
            discord_sent,
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
