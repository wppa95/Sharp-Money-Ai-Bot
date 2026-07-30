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
    chat_id = update.effective_chat.id
    user_id = getattr(update.effective_user, 'id', None)
    logger.info("cmd_start: chat_id=%s user_id=%s", chat_id, user_id)
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


# ── /picks tier constants ─────────────────────────────────────────────────────
# New PPAnalysisScore vocabulary (S/A/B/PASS) plus legacy AlertTier values for
# any records written before the scoring engine was introduced.
_TIER_ORDER: dict[str, int] = {
    "S": 0, "A": 1, "B": 2, "PASS": 3,
    "Critical": 0, "High": 1, "Medium": 2, "Low": 3,
}
_TIER_EMOJI: dict[str, str] = {
    "S": "🔥", "A": "🟢", "B": "🟡", "PASS": "⚪",
    "Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "⚪",
}

# Thresholds mirror PPAnalysisScore._STAR_BANDS — display-only, no scoring logic.
_STAR_BANDS: tuple[tuple[int, int], ...] = ((85, 5), (70, 4), (55, 3), (40, 2))


def _stars_from_conf(conf: Optional[float]) -> str:
    """Return a 5-char star bar (e.g. '★★★★☆') from a 0–100 confidence score."""
    if conf is None:
        return ""
    for threshold, n in _STAR_BANDS:
        if conf >= threshold:
            return "★" * n + "☆" * (5 - n)
    return "★☆☆☆☆"


async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/picks [sport|N] — Ranked PrizePicks edges by confidence tier (S→A→B).

    Usage:
      /picks           — top 10 picks from the last 6 hours
      /picks 5         — top 5 picks
      /picks NBA       — filter to NBA only
      /picks NFL 5     — NFL, top 5
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    args = context.args or []
    limit = 10
    sport_filter: Optional[str] = None
    for arg in args:
        if arg.isdigit():
            limit = min(int(arg), 20)
        else:
            sport_filter = arg.upper()

    records = await _db.get_top_pp_edges(limit=limit, hours=6)

    if sport_filter:
        records = [r for r in records if r.sport.upper() == sport_filter]

    if not records:
        hint = f" for {sport_filter}" if sport_filter else ""
        await update.message.reply_text(
            f"🎯 <b>PrizePicks Picks</b>\n\n"
            f"No edges detected{hint} in the last 6 hours.\n\n"
            f"<i>Edges are found when a PP line diverges from sportsbook fair odds.\n"
            f"Check back after the next poll cycle (every 5 min).</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Sort: tier rank first, then best_edge descending
    records.sort(key=lambda r: (_TIER_ORDER.get(r.tier or "Low", 3), -(r.best_edge or 0)))

    today = datetime.utcnow().strftime("%b %d, %Y")
    lines: list[str] = [f"🎯 <b>PrizePicks Picks — {today}</b>"]
    if sport_filter:
        lines.append(f"<i>Filtered: {sport_filter}</i>")
    lines.append("")

    current_tier: Optional[str] = None
    rank = 0
    for r in records:
        tier = r.tier or "Low"
        if tier != current_tier:
            current_tier = tier
            tier_icon = _TIER_EMOJI.get(tier, "⚪")
            lines.append(f"{tier_icon} <b>{tier}</b>")
        rank += 1

        conf_str  = f"  conf {r.confidence:.0f}/100" if r.confidence else ""
        stars_str = f"  {_stars_from_conf(r.confidence)}" if r.confidence else ""
        result_str = (
            f"  [{r.result}]"
            if r.result and r.result != "PENDING" else ""
        )

        move_str = ""
        if (r.prev_line is not None
                and r.opening_line is not None
                and r.opening_line != r.pp_line_value):
            delta     = r.pp_line_value - r.opening_line
            direction = "▲" if delta > 0 else "▼"
            move_str  = f"  {direction}{abs(delta):.1f} from open"

        lines.append(
            f"  #{rank} <b>{r.player_name}</b> · {r.stat_type}\n"
            f"       PP <code>{r.pp_line_value:g}</code> · <b>{r.best_side}</b> · "
            f"<code>+{r.best_edge:.1f}%</code>{conf_str}{stars_str}{move_str}{result_str}\n"
            f"       <i>{r.sport} · vs {r.sportsbook} · "
            f"{r.detected_at.strftime('%H:%M UTC')}</i>"
        )

    if not sport_filter and len(records) >= limit:
        lines.append(f"\n<i>Showing top {limit}. Use /picks [sport] to filter.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testalert [steam|ev] — Send a mock alert to verify delivery end-to-end."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    args = [a.lower() for a in (context.args or [])]
    send_steam = not args or "steam" in args
    send_ev    = not args or "ev" in args

    if not send_steam and not send_ev:
        await update.message.reply_text(
            "Usage: /testalert  |  /testalert steam  |  /testalert ev"
        )
        return

    await update.message.reply_text("⏳ Generating mock alerts…")

    try:
        from connectors.mock import (
            MockOddsConnector, MockScenario,
            _GAME_A, _BOS_SEL, _LAL_SEL, _DK, _FD, _SP,
        )
        from engine.steam import compute_steam_simple
        from engine.fair_probability import (
            compute_fair_market, FairProbabilityMethod, implied_to_american,
        )
        from engine.ev import compute_ev_from_market, kelly_fraction as kf_fn
        from models import (
            SteamAlert, AlertType, Sport, MarketType,
            EVOpportunity, EVResult, FairOdds, Recommendation,
        )

        # ── Build mock state ──────────────────────────────────────────────
        c = MockOddsConnector()
        await c.fetch()                          # OPENING baseline
        c.tick(MockScenario.STEAM)
        snaps1 = await c.fetch()

        bos_dk1 = next(s for s in snaps1 if s.sportsbook == _DK
                       and s.event == _GAME_A and s.selection == _BOS_SEL
                       and s.market_type == _SP)

        steam_result = compute_steam_simple(
            market=_GAME_A, sport="NBA", market_type=_SP,
            selection=f"{_BOS_SEL} -3.5",
            book_snapshots=[
                {"sportsbook": "Pinnacle",   "open_odds": bos_dk1.opening_odds, "current_odds": bos_dk1.odds},
                {"sportsbook": "DraftKings", "open_odds": bos_dk1.opening_odds, "current_odds": bos_dk1.odds},
            ],
            elapsed_minutes=12.0,
        )

        steam_alert_obj = SteamAlert(
            alert_type=AlertType.STEAM, sport=Sport.NBA, market_type=MarketType.SPREAD,
            event=_GAME_A, selection=f"{_BOS_SEL} -3.5",
            opening_odds=steam_result.opening_odds, current_odds=steam_result.current_odds,
            steam_score=steam_result.steam_score,
            steam_direction=steam_result.movement_direction.value,
            books_moved=steam_result.books_triggered,
        )

        # ── Send steam alert ──────────────────────────────────────────────
        if send_steam:
            steam_msg = format_steam_alert(steam_alert_obj, sharp_books=["Pinnacle"], risk_factors=[])
            await update.message.reply_text(steam_msg, parse_mode=ParseMode.HTML,
                                            disable_web_page_preview=True)

        # ── Build EV window state ─────────────────────────────────────────
        if send_ev:
            c.tick(MockScenario.EV_WINDOW)
            snaps2 = await c.fetch()

            dk_bos2 = next(s for s in snaps2 if s.sportsbook == _DK
                           and s.event == _GAME_A and s.selection == _BOS_SEL
                           and s.market_type == _SP)
            dk_lal2 = next(s for s in snaps2 if s.sportsbook == _DK
                           and s.event == _GAME_A and s.selection == _LAL_SEL
                           and s.market_type == _SP)
            fd_bos2 = next(s for s in snaps2 if s.sportsbook == _FD
                           and s.event == _GAME_A and s.selection == _BOS_SEL
                           and s.market_type == _SP)

            fair      = compute_fair_market(
                [dk_bos2.odds, dk_lal2.odds], labels=[_BOS_SEL, _LAL_SEL],
                method=FairProbabilityMethod.MULTIPLICATIVE,
            )
            engine_ev = compute_ev_from_market(fair, _BOS_SEL, fd_bos2.odds)
            kf        = kf_fn(engine_ev.fair_probability, fd_bos2.odds)

            opp = EVOpportunity(
                ev_result=EVResult(
                    selection=f"{_BOS_SEL} -3.5",
                    fair_odds=FairOdds(
                        selection=f"{_BOS_SEL} -3.5",
                        fair_probability=engine_ev.fair_probability,
                        fair_american_odds=implied_to_american(engine_ev.fair_probability),
                        vig_percentage=engine_ev.vig_pct,
                        market_width=fair.market_width,
                    ),
                    offered_american_odds=fd_bos2.odds,
                    ev_percentage=engine_ev.ev_percentage,
                    edge=engine_ev.edge,
                    kelly_fraction=max(kf, 0.0),
                    half_kelly=max(kf / 2, 0.0),
                ),
                steam_alert=steam_alert_obj,
                sport=Sport.NBA, market_type=MarketType.SPREAD,
                event=_GAME_A, player=None, line=-3.5,
                best_odds=fd_bos2.odds, best_book=_FD,
                fair_probability=engine_ev.fair_probability,
                expected_value=engine_ev.ev_percentage,
                steam_score=steam_result.steam_score,
                ai_confidence=82, recommendation=Recommendation.STRONG_BET, stars=4,
            )

            # Route through the real delivery pipeline (scope filter → dedup → format → send).
            delivery = AlertDelivery(_db, context.bot, _alert_chat_ids)
            ev_result = await delivery.deliver_ev(opp)
            await update.message.reply_text(
                f"📋 EV pipeline result: {ev_result}",
                parse_mode=ParseMode.HTML,
            )

        kinds = " + ".join(filter(None, ["steam" if send_steam else "", "ev" if send_ev else ""]))
        await update.message.reply_text(f"✅ Test alert(s) sent: {kinds}")

    except Exception as exc:
        logger.exception("cmd_testalert error: %s", exc)
        await update.message.reply_text(f"{EMOJI['warn']} Test alert failed: {exc}")


async def cmd_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/slip [N] — Build a prop slip from the top N picks (2–6, default 3).

    Usage:
      /slip      — 3-leg slip from today's top picks
      /slip 4    — 4-leg slip
      /slip 6    — maximum 6-leg slip
    """
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    # Parse leg count: 2–6, default 3
    args = context.args or []
    legs = 3
    if args:
        try:
            legs = int(args[0])
        except ValueError:
            pass
    legs = max(2, min(legs, 6))

    records = await _db.get_top_pp_edges(limit=legs, hours=6)
    records.sort(key=lambda r: (_TIER_ORDER.get(r.tier or "Low", 3), -(r.best_edge or 0)))

    if len(records) < 2:
        hint = f"{len(records)} pick" if records else "no picks"
        await update.message.reply_text(
            f"🎰 <b>SharpMoney Slip</b>\n\n"
            f"Not enough picks to build a slip ({hint} available in last 6 h).\n\n"
            f"<i>A slip needs at least 2 legs.  Run /picks to see what's available.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Trim to requested count if fewer picks came back
    records = records[:legs]
    actual_legs = len(records)

    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    lines: list[str] = [
        f"🎰 <b>SharpMoney Slip — {actual_legs} legs · {today}</b>",
        "",
    ]

    total_conf = 0.0
    conf_count = 0
    total_edge = 0.0

    for i, r in enumerate(records, 1):
        tier_icon = _TIER_EMOJI.get(r.tier or "Low", "⚪")
        stars_str = _stars_from_conf(r.confidence)
        conf_label = f"{r.confidence:.0f}/100" if r.confidence is not None else "—"
        if r.confidence is not None:
            total_conf += r.confidence
            conf_count += 1
        total_edge += r.best_edge or 0.0

        move_note = ""
        if (r.opening_line is not None
                and r.pp_line_value is not None
                and r.opening_line != r.pp_line_value):
            delta = r.pp_line_value - r.opening_line
            move_note = f"  {'▲' if delta > 0 else '▼'}{abs(delta):.1f}"

        lines.append(
            f"<b>Leg {i}</b>  {tier_icon} {r.tier or '—'}  {stars_str}\n"
            f"  <b>{r.player_name}</b> · {r.stat_type}\n"
            f"  {r.best_side} {r.pp_line_value:g}  ·  "
            f"<code>+{r.best_edge:.1f}%</code>  ·  conf {conf_label}{move_note}\n"
            f"  <i>{r.sport} · {r.sportsbook} · {r.detected_at.strftime('%H:%M UTC')}</i>"
        )

    avg_conf = total_conf / conf_count if conf_count else 0.0
    avg_edge = total_edge / actual_legs if actual_legs else 0.0

    lines += [
        "",
        "─" * 28,
        f"Legs: <b>{actual_legs}</b>  ·  "
        f"Avg edge: <code>+{avg_edge:.1f}%</code>  ·  "
        f"Avg conf: <code>{avg_conf:.0f}/100</code>",
        "",
        "<i>⚠️ Research tool — not betting advice.  Verify lines before placing.</i>",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dashboard — Overview: picks health, tier breakdown, top pick, bot uptime."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    today = datetime.now(timezone.utc).strftime("%b %d, %Y  %H:%M UTC")

    # ── Data gathering (all queries independent) ─────────────────────────────
    edges_6h   = await _db.get_top_pp_edges(limit=50, hours=6)
    edges_24h  = await _db.get_top_pp_edges(limit=50, hours=24)
    total_all  = await _db.count_pp_edge_records()

    # Tier breakdown from 6h window
    tier_counts: dict[str, int] = {}
    for r in edges_6h:
        t = r.tier or "—"
        tier_counts[t] = tier_counts.get(t, 0) + 1

    # Top pick (highest conf in 6h, fall back to highest edge)
    top = None
    if edges_6h:
        edges_6h.sort(key=lambda r: (
            _TIER_ORDER.get(r.tier or "Low", 3),
            -(r.confidence or 0),
        ))
        top = edges_6h[0]

    # Freshness: latest detected_at across 24h window
    freshness_str = "No picks yet"
    if edges_24h:
        latest = max(edges_24h, key=lambda r: r.detected_at)
        age_s  = int((datetime.utcnow() - latest.detected_at).total_seconds())
        if age_s < 60:
            freshness_str = f"{age_s}s ago"
        elif age_s < 3600:
            freshness_str = f"{age_s // 60}m ago"
        else:
            freshness_str = f"{age_s // 3600}h {(age_s % 3600) // 60}m ago"

    # ── Build tier breakdown string ───────────────────────────────────────────
    tier_parts = []
    for tier in ("S", "A", "B", "PASS"):
        n = tier_counts.get(tier, 0)
        if n:
            tier_parts.append(f"{_TIER_EMOJI[tier]}{tier}:{n}")
    tier_str = "  " + "  ".join(tier_parts) if tier_parts else "  none"

    # ── Message assembly ──────────────────────────────────────────────────────
    lines: list[str] = [
        f"📊 <b>SharpMoneyBot Dashboard</b>",
        f"<i>{today}</i>",
        "",
        "🃏 <b>PrizePicks Picks</b>",
        f"  Last  6 h:  <b>{len(edges_6h)}</b> detected{tier_str}",
        f"  Last 24 h:  <b>{len(edges_24h)}</b> detected",
        f"  All-time:   <b>{total_all}</b> stored",
        f"  Freshness:  {freshness_str}",
        "",
        "🤖 <b>Bot Health</b>",
        f"  Uptime:    {_uptime_str()}",
        f"  PP API:    ⚠️ Temporarily unavailable (HTTP 403 — bot-detection wall)",
        f"  Odds API:  ⚠️ Monthly quota nearly exhausted",
        "",
    ]

    if top:
        tier_icon  = _TIER_EMOJI.get(top.tier or "Low", "⚪")
        stars_str  = _stars_from_conf(top.confidence)
        conf_label = f"{top.confidence:.0f}/100" if top.confidence is not None else "—"
        lines += [
            "🔝 <b>Top Pick (last 6 h)</b>",
            f"  {tier_icon} {top.tier or '—'}  <b>{top.player_name}</b> · {top.stat_type}",
            f"  {top.best_side} {top.pp_line_value:g}  ·  "
            f"<code>+{top.best_edge:.1f}%</code>  ·  {conf_label}  {stars_str}",
            f"  <i>{top.sport} · {top.sportsbook}</i>",
        ]
    else:
        lines.append("🔝 <b>Top Pick</b>  —  no picks in last 6 h")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts — Recent alert history: PP picks sent, EV and steam alerts."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

    # Gather all alert types
    pp_sent    = await _db.get_recent_pp_alerts(limit=10)
    ev_recent  = await _db.get_recent_ev(limit=5)
    stm_recent = await _db.get_recent_steam(limit=5)

    lines: list[str] = [
        "🔔 <b>Alert History</b>",
        "",
    ]

    # ── PP Pick alerts ────────────────────────────────────────────────────────
    lines.append(f"🎯 <b>PP Pick Alerts</b>")
    if not pp_sent:
        lines.append(
            "  No PP picks have been alerted yet.\n"
            "  <i>Alerts fire automatically when PrizePicks API data resumes.</i>"
        )
    else:
        lines.append(f"  <i>{len(pp_sent)} most recent (alert_sent=True)</i>")
        lines.append("")
        for r in pp_sent:
            tier_icon = _TIER_EMOJI.get(r.tier or "Low", "⚪")
            result_tag = (
                f"  [{r.result}]" if r.result and r.result != "PENDING" else ""
            )
            lines.append(
                f"  {tier_icon} <b>{r.player_name}</b> · {r.stat_type}  "
                f"<code>+{r.best_edge:.1f}%</code>{result_tag}\n"
                f"      <i>{r.sport} · {r.detected_at.strftime('%b %d %H:%M UTC')}</i>"
            )

    lines.append("")

    # ── EV alerts ─────────────────────────────────────────────────────────────
    ev_sent = [r for r in ev_recent if r.alert_sent]
    lines.append(f"{EMOJI['ev']} <b>EV Alerts</b>  ({len(ev_sent)} of last {len(ev_recent)} sent)")
    if not ev_sent:
        lines.append("  <i>No EV alerts sent recently.</i>")
    else:
        for r in ev_sent[:3]:
            lines.append(
                f"  <b>{r.selection}</b> · {r.sport}  "
                f"EV <code>{r.expected_value:+.1f}%</code>\n"
                f"      <i>{r.detected_at.strftime('%b %d %H:%M UTC')}</i>"
            )

    lines.append("")

    # ── Steam alerts ──────────────────────────────────────────────────────────
    stm_sent = [r for r in stm_recent if r.alert_sent]
    lines.append(f"{EMOJI['fire']} <b>Steam Alerts</b>  ({len(stm_sent)} of last {len(stm_recent)} sent)")
    if not stm_sent:
        lines.append("  <i>No steam alerts sent recently.</i>")
    else:
        for r in stm_sent[:3]:
            lines.append(
                f"  <b>{r.selection}</b> · {r.sport}  "
                f"score <code>{r.steam_score}/100</code>\n"
                f"      <i>{r.detected_at.strftime('%b %d %H:%M UTC')}</i>"
            )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grade — Grade resolved PP picks by tier (WIN/LOSS/PUSH breakdown)."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

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

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors raised by handlers."""
    logger.error("Update %s caused error: %s", update, context.error, exc_info=context.error)
