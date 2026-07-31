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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show bot and market status."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

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
            fail_note = (
                f"  ({h.consecutive_failures} fail{'s' if h.consecutive_failures != 1 else ''})"
                if h.consecutive_failures else ""
            )
            lines.append(
                f"  {h.status_emoji} <b>{name}</b>  {h.status.value}"
                f"  ·  last ✓: {last_ok}{fail_note}"
            )
        # OddsAPI quota/pacing stats hidden — sportsbook polling disabled.
        # To restore: uncomment the block below when re-enabling sportsbook jobs.
        # odds_h = _mon.get_health("OddsAPI")
        # if odds_h.quota_remaining is not None or odds_h.quota_used is not None:
        #     r = f"{odds_h.quota_remaining:,}" if odds_h.quota_remaining is not None else "?"
        #     u = f"{odds_h.quota_used:,}"      if odds_h.quota_used      is not None else "?"
        #     lines.append(f"  ↳ API quota:  {r} remaining  ·  {u} used")
        # try:
        #     from providers.usage_tracker import get_usage_tracker as _get_tracker
        #     _ut = _get_tracker()
        #     if _ut is not None:
        #         _us = _ut.get_stats("OddsAPI")
        #         if _us.month_budget > 0:
        #             _pacing_used = (
        #                 f"{_us.quota_used:,}" if _us.quota_used is not None
        #                 else f"~{_us.month_count:,}"
        #             )
        #             lines.append(
        #                 f"  ↳ Pacing:     {_pacing_used} / {_us.month_budget:,}"
        #                 f"  ({_us.budget_pct:.1f}%)  <code>{_us.budget_bar}</code>"
        #             )
        # except Exception:
        #     pass
    else:
        lines.append("  ⚪ Underdog     not yet tracked")
    lines.append("")

    # ── Odds API cache stats — hidden while sportsbook polling is disabled ────
    # To restore: uncomment when re-enabling sportsbook jobs.
    # if _cache:
    #     st  = _cache.stats()
    #     tot = st["hits"] + st["misses"]
    #     hr  = f"{st['hit_rate'] * 100:.0f}%" if tot > 0 else "—"
    #     lines.append(
    #         f"⛽ <b>Odds API Cache</b>  TTL {st['ttl_seconds']}s"
    #         f"  ·  {st['hits']} hits / {st['misses']} misses  ({hr})"
    #     )
    #     lines.append("")

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
    lines.append("📊 <b>Database Records</b>")
    lines.append(f"  Odds: {db_records:,}  ·  Steam: {total_steam:,}  ·  EV: {total_ev:,}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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

    # Sort: tier rank → game_time ASC (None last) → best_edge DESC
    from engine.timing import format_game_time_label as _fmt_gt
    import datetime as _dt_mod

    def _sort_key(r):
        tier_rank = _TIER_ORDER.get(r.tier or "Low", 3)
        gt = getattr(r, "game_time", None)
        if gt is None:
            gt_sort = _dt_mod.datetime.max
        else:
            gt_sort = gt.replace(tzinfo=None) if gt.tzinfo else gt
        return (tier_rank, gt_sort, -(r.best_edge or 0))

    records.sort(key=_sort_key)

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

        # Game timing label
        gt_label = ""
        gt = getattr(r, "game_time", None)
        if gt is not None:
            _lbl = _fmt_gt(gt)
            if _lbl:
                gt_label = f"  ⏰ {_lbl}"

        # ── Bankroll discipline ─────────────────────────────────────────────
        _dec_str = ""
        try:
            from engine.decision_engine import make_pp_decision
            _dec = make_pp_decision(r)
            _flag = (
                f"  <i>⚠️ {', '.join(_dec.risk_flags)}</i>"
                if _dec.risk_flags else ""
            )
            if _dec.action == "PASS":
                _dec_str = f"\n       {_dec.action_label}{_flag}"
            else:
                _dec_str = (
                    f"\n       {_dec.action_label}  ·  "
                    f"Kelly <code>{_dec.kelly_full * 100:.1f}%</code>  ·  "
                    f"<b>{_dec.suggested_units:.2f}u</b>{_flag}"
                )
        except Exception:
            pass

        lines.append(
            f"  #{rank} <b>{r.player_name}</b> · {r.stat_type}\n"
            f"       PP <code>{r.pp_line_value:g}</code> · <b>{r.best_side}</b> · "
            f"<code>+{r.best_edge:.1f}%</code>{conf_str}{stars_str}{move_str}{result_str}"
            f"{gt_label}"
            f"{_dec_str}\n"
            f"       <i>{r.sport} · vs {r.sportsbook} · "
            f"{r.detected_at.strftime('%H:%M UTC')}</i>"
        )

    if not sport_filter and len(records) >= limit:
        lines.append(f"\n<i>Showing top {limit}. Use /picks [sport] to filter.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testalert [steam|ev] — Send a mock alert to verify delivery end-to-end."""
    uid = getattr(update.effective_user, "id", "?")
    logger.info("cmd_testalert: user_id=%s args=%s", uid, context.args)
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    args          = [a.lower() for a in (context.args or [])]
    send_steam    = not args or "steam"    in args
    send_ev       = not args or "ev"       in args
    send_pp       = not args or "pp"       in args
    send_underdog = not args or "underdog" in args

    if not send_steam and not send_ev and not send_pp and not send_underdog:
        await update.message.reply_text(
            "Usage: /testalert  |  /testalert steam  |  /testalert ev"
            "  |  /testalert pp  |  /testalert underdog"
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

        # ── PP edge opportunity ───────────────────────────────────────────────
        if send_pp:
            from datetime import datetime as _dt
            from prizepicks import PrizePicksLine, PPEdgeOpportunity
            from engine.pp_scoring import score_pp_edge

            _now_ts = _dt.utcnow()
            _pp_line = PrizePicksLine(
                external_id      = "test-pp-001",
                player_name      = "Anthony Edwards",
                team             = "MIN",
                sport            = "NBA",
                league           = "NBA",
                stat_type        = "Points",
                line_value       = 26.5,
                start_time       = None,
                game_description = "MIN vs DEN",
                fetched_at       = _now_ts,
            )
            # Realistic edge: PP line 26.5, DK has 27.0 (-110 / -110).
            # Fair prob ~62.1% OVER at the PP line → +16.8% edge.
            _opp = PPEdgeOpportunity(
                pp_line                   = _pp_line,
                sportsbook                = "DraftKings",
                sportsbook_line           = 27.0,
                sportsbook_over_odds      = -110,
                sportsbook_under_odds     = -110,
                fair_prob_over_at_sb_line = 0.502,
                fair_prob_under_at_sb_line= 0.498,
                line_diff                 = 0.5,   # sb_line − pp_line
                adjusted_fair_prob_over   = 0.621,
                adjusted_fair_prob_under  = 0.379,
                edge_over                 = 16.8,
                edge_under                = 0.0,
                best_side                 = "OVER",
                best_edge                 = 16.8,
                prob_per_unit             = 3.0,
            )
            _pp_score = score_pp_edge(
                _opp, history=[], opening_line=_pp_line.line_value, now=_now_ts,
            )

            # Route through real production pipeline: score → normalize_pp
            # → AlertScopeFilter → AlertDelivery.deliver_pp → broadcast_alert → Telegram.
            delivery = AlertDelivery(_db, context.bot, _alert_chat_ids)
            pp_result = await delivery.deliver_pp(_opp, score=_pp_score)

            await update.message.reply_text(
                f"📋 PP pipeline result: {pp_result}\n"
                f"🏅 Score: {_pp_score.total}/100 — "
                f"tier=<b>{_pp_score.tier}</b> stars={_pp_score.stars}★",
                parse_mode=ParseMode.HTML,
            )

        # ── Underdog line change ──────────────────────────────────────────────
        if send_underdog:
            # Realistic NFL rushing-yards line movement (Barkley +4 yds).
            delivery = AlertDelivery(_db, context.bot, _alert_chat_ids)
            ud_result = await delivery.deliver_underdog(
                player_name = "Saquon Barkley",
                team        = "PHI",
                sport       = "NFL",
                stat_type   = "Rushing Yards",
                old_line    = 85.5,
                new_line    = 89.5,
                game_time   = None,
                removed     = False,
            )
            await update.message.reply_text(
                f"📋 Underdog pipeline result: {ud_result}",
                parse_mode=ParseMode.HTML,
            )

        kinds = " + ".join(filter(None, [
            "steam"    if send_steam    else "",
            "ev"       if send_ev       else "",
            "pp"       if send_pp       else "",
            "underdog" if send_underdog else "",
        ]))
        await update.message.reply_text(f"✅ Test alert(s) sent: {kinds}")

    except Exception as exc:
        logger.exception("cmd_testalert error: %s", exc)
        await update.message.reply_text(f"{EMOJI['warn']} Test alert failed: {exc}")


def _render_slip_section(
    size: int,
    slip_result: "OptimizedSlip",
    label: str = "",
) -> list[str]:
    """Render a single slip size into HTML lines (shared by cmd_slip)."""
    from engine.timing import format_game_time_label as _fmt_gt

    records = slip_result.legs
    actual  = len(records)
    total_conf = 0.0
    conf_count = 0
    total_edge = 0.0

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
        total_edge += r.best_edge or 0.0

        move_note = ""
        if (r.opening_line is not None
                and r.pp_line_value is not None
                and r.opening_line != r.pp_line_value):
            delta = r.pp_line_value - r.opening_line
            move_note = f"  {'▲' if delta > 0 else '▼'}{abs(delta):.1f}"

        gt = getattr(r, "game_time", None)
        gt_label = f"  ⏰ {_fmt_gt(gt)}" if gt is not None and _fmt_gt(gt) else ""

        _dec_leg = ""
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

        section.append(
            f"  <b>Leg {i}</b>  {tier_icon} {r.tier or '—'}  {stars_str}\n"
            f"    <b>{r.player_name}</b> · {r.stat_type}\n"
            f"    {r.best_side} <code>{r.pp_line_value:g}</code>  ·  "
            f"<code>+{r.best_edge:.1f}%</code>  ·  conf {conf_label}"
            f"{move_note}{gt_label}"
            f"{_dec_leg}"
        )

    avg_conf = total_conf / conf_count if conf_count else 0.0
    avg_edge = total_edge / actual     if actual     else 0.0

    section.append(
        f"\n  Avg edge <code>+{avg_edge:.1f}%</code>  ·  "
        f"Avg conf <code>{avg_conf:.0f}/100</code>  ·  "
        f"{actual} legs"
    )

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
    """/slip — Build all prop slip sizes (2–6 legs) from today's top picks.

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

    # Optional single-size argument
    args = context.args or []
    single_size: Optional[int] = None
    if args:
        try:
            single_size = max(2, min(int(args[0]), 6))
        except ValueError:
            pass

    from engine.slip_builder import build_all_slips

    _candidates = await _db.get_top_pp_edges(limit=30, hours=6)
    if not _candidates:
        await update.message.reply_text(
            "🎰 <b>SharpMoney Slip</b>\n\n"
            "No picks detected in the last 6 hours.\n\n"
            "<i>Run /picks to see current edges.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    slips = build_all_slips(_candidates, max_size=6)

    if not slips:
        await update.message.reply_text(
            "🎰 <b>SharpMoney Slip</b>\n\n"
            f"Not enough independent picks to build a slip "
            f"({len(_candidates)} candidate{'s' if len(_candidates) != 1 else ''} "
            f"after correlation filtering).\n\n"
            f"<i>Check back after the next poll cycle.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    today = datetime.now(timezone.utc).strftime("%b %d, %Y")

    if single_size is not None:
        # User requested a specific size
        slip = slips.get(single_size)
        if slip is None:
            await update.message.reply_text(
                f"🎰 <b>SharpMoney Slip</b>\n\n"
                f"Could not build a {single_size}-man slip from today's picks.\n"
                f"<i>Available sizes: {', '.join(str(s) for s in sorted(slips))}.</i>",
                parse_mode=ParseMode.HTML,
            )
            return
        label = "✅ Recommended (Safest)" if single_size == 2 else ""
        lines: list[str] = [
            f"🎰 <b>SharpMoney Slips — {today}</b>",
            "",
        ] + _render_slip_section(single_size, slip, label)
    else:
        # Show all sizes
        lines = [
            f"🎰 <b>SharpMoney Slips — {today}</b>",
            f"<i>{len(slips)} slip size{'s' if len(slips) != 1 else ''} built "
            f"from {len(_candidates)} candidates</i>",
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

    # ── Append PP-specific live section ──────────────────────────────────────
    try:
        edges_6h     = await _db.get_top_pp_edges(limit=50, hours=6)
        resolved_all = await _db.get_all_resolved_pp_edges(limit=200)

        # Top pick
        top = None
        if edges_6h:
            edges_6h.sort(key=lambda r: (
                _TIER_ORDER.get(r.tier or "Low", 3),
                -(r.confidence or 0),
            ))
            top = edges_6h[0]

        pp_lines: list[str] = ["🃏 <b>PrizePicks — Live Picks</b>"]
        if top:
            tier_icon  = _TIER_EMOJI.get(top.tier or "Low", "⚪")
            stars_str  = _stars_from_conf(top.confidence)
            conf_label = f"{top.confidence:.0f}/100" if top.confidence is not None else "—"
            pp_lines += [
                f"  🔝 {tier_icon} {top.tier or '—'}  <b>{top.player_name}</b> · {top.stat_type}",
                f"  {top.best_side} {top.pp_line_value:g}  ·  "
                f"<code>+{top.best_edge:.1f}%</code>  ·  {conf_label}  {stars_str}",
                f"  <i>{top.sport} · {top.sportsbook}</i>",
            ]
        else:
            pp_lines.append("  No PP picks in the last 6 h — use /picks to see older ones")

        # Resolved performance
        pp_lines.append("")
        if resolved_all:
            from engine.decision_engine import compute_tier_performance
            _perf = compute_tier_performance(resolved_all)
            pp_lines.append("  <b>Resolved results by tier</b>")
            for _tier in ("S", "A", "B", "PASS"):
                if _tier not in _perf:
                    continue
                _ts   = _perf[_tier]
                _note = f"  <i>{_ts.sample_size_note}</i>" if _ts.sample_size_note else ""
                pp_lines.append(
                    f"  {_TIER_EMOJI[_tier]} {_tier}  "
                    f"{_ts.picks} resolved  "
                    f"<code>{_ts.hit_rate_pct:.0f}%</code> hit  "
                    f"avg edge <code>+{_ts.avg_edge:.1f}%</code>{_note}"
                )
        else:
            pp_lines.append("  <i>No resolved picks yet</i>")

        # Bot health & uptime
        pp_lines += [
            "",
            "🤖 <b>Bot</b>",
            f"  Uptime: {_uptime_str()}",
            _fmt_provider_status_line("Underdog", "Underdog"),
        ]

        await update.message.reply_text("\n".join(pp_lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("cmd_dashboard: PP section failed: %s", exc)
        # Non-fatal — main dashboard already sent


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

    import os
    pandascore_key = bool(os.environ.get("PANDASCORE_API_KEY", "").strip())

    lines: list[str] = [
        "🔌 <b>Provider Status</b>",
        "",
        "<b>Underdog Alert Sports</b>",
    ]

    # Per-sport provider details
    providers_info = [
        ("MLB",    "⚾", "MLB Stats API",      "statsapi.mlb.com", True,            "free — no key"),
        ("WNBA",   "🏀", "ESPN gamelog",        "espn.com/api",     True,            "free — no key"),
        ("DOTA",   "🎮", "OpenDota API",        "api.opendota.com", True,            "free — no key"),
        ("TENNIS", "🎾", "JeffSackmann CSV",   "github.com/JeffSackmann", True,     "free — no key"),
        ("CS2",    "🖥️", "PandaScore API",     "api.pandascore.co", pandascore_key, "key active" if pandascore_key else "⚠️ PANDASCORE_API_KEY not set"),
    ]

    ud_sports = config.ud_alert_sports

    for sport, icon, provider_name, host, active, note in providers_info:
        in_scope = sport in ud_sports or (sport == "CS2" and "CS" in ud_sports)
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
        f"  <i>Odds API — MLB moneylines + totals only</i>"
    )

    if not pandascore_key:
        lines.append("")
        lines.append(
            "<i>💡 To enable CS2 alerts: add PANDASCORE_API_KEY to environment secrets, "
            "then restart the bot.</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — Alert generation stats, outcomes tracked, performance summary."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if _db is None:
        await update.message.reply_text(f"{EMOJI['warn']} Database not ready.")
        return

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


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/config — Show active bot configuration (no secrets exposed)."""
    if not _check_allowed(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    import os
    pandascore_set = bool(os.environ.get("PANDASCORE_API_KEY", "").strip())
    odds_api_set   = bool(config.ODDS_API_KEY)

    ud_sports = ", ".join(sorted(config.ud_alert_sports)) or "none"
    active_sp = ", ".join(config.active_sports) or "none"

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

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors raised by handlers."""
    logger.error("Update %s caused error: %s", update, context.error, exc_info=context.error)
