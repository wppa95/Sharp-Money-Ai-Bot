"""
main.py — Sharp Money +EV Detection Bot startup.

Entry point: python bot/main.py

python-telegram-bot v20+ manages its own event loop via run_polling().
Async setup (DB init) is done through the Application's post_init /
post_shutdown lifecycle hooks — do NOT wrap run_polling() in asyncio.run().
"""

from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler

# Ensure the bot/ directory is on the path for sibling imports
sys.path.insert(0, os.path.dirname(__file__))

from config import config
from database import Database, OddsRecord
from engine import AnalysisEngine
from engine.season_check import SeasonChecker
from engine.analysis import _SPORT_TO_ODDS_API_KEY, PlayerPropLine
from alert_scope_filter import is_ev_line_in_scope
from models import (
    MarketType,
    OddsLine,
    OddsMovement,
    Sport,
)
from alerts import AlertDelivery, broadcast_alert, format_pp_alert
from database import PrizePicksRecord, PPEdgeRecord
from prizepicks import (
    PrizePicksClient,
    PPEdgeOpportunity,
    PP_LEAGUE_IDS,
    PP_STAT_TO_ODDS_API,
    compare_pp_to_sportsbook,
)
from commands import (
    cmd_start,
    cmd_help,
    cmd_status,
    cmd_analyze,
    cmd_steam,
    cmd_ev,
    cmd_picks,
    cmd_slip,
    cmd_dashboard,
    cmd_alerts,
    cmd_grade,
    cmd_clv,
    cmd_market,
    cmd_performance,
    cmd_backtest,
    cmd_testalert,
    cmd_providers,
    cmd_stats,
    cmd_config,
    cmd_calibration,
    cmd_pp_import,
    cmd_health,
    cmd_restarts,
    cmd_tracking,
    cmd_analyst,
    cmd_blocks,
    cmd_block,
    cmd_refinement,
    error_handler,
    init_handlers,
)
from connectors import (
    ConnectorRegistry,
    DraftKingsConnector,
    FanDuelConnector,
    UnderdogConnector,
)
from market_engine import (
    init_market_engine,
    connector_poll_job,
    consensus_check_job,
    clv_check_job,
    underdog_job,
)
from providers import init_health_monitor
from providers.odds_cache import init_odds_cache
from providers.game_results import OddsApiResultsProvider
from providers.usage_tracker import init_usage_tracker, get_usage_tracker
from engine.health import init_health_tracker, get_health_tracker
from engine.pregame_watch import get_pregame_watch_engine

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Module-level singletons ────────────────────────────────────────────────────
# Initialised inside post_init so they live in the bot's event loop.
_db: Database | None = None
_engine: AnalysisEngine | None = None
_season_checker: SeasonChecker | None = None

# Tracks which budget-warning thresholds have already been alerted this month
# per provider.  Reset implicitly when get_usage_tracker()._roll_month_if_needed()
# fires, or on bot restart (acceptable — at worst one duplicate alert per month).
_last_budget_alerted: dict[str, set[int]] = {}


# ── PTB lifecycle hooks ────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Runs once after the bot is initialised but before polling starts."""
    global _db, _engine, _season_checker

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Sharp Money +EV Detection Bot — Starting Up")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # ── Health tracker (must be first — jobs reference it) ────────────────────
    _ht = init_health_tracker()
    _ht.record_startup()
    logger.info(
        "HealthTracker initialised — startup #%d", _ht.restart_count()
    )

    _db = Database(config.DATABASE_URL)
    await _db.init()

    _engine = AnalysisEngine()

    # ── Season / market-status checker ────────────────────────────────────────
    _season_checker = SeasonChecker(
        api_key=config.ODDS_API_KEY,
        ttl_seconds=config.SEASON_CHECK_INTERVAL,
    )
    # Eager first load — non-fatal; checker stays fail-open on error.
    if config.SEASON_CHECK_INTERVAL > 0:
        await _season_checker.refresh()

    # ── Provider health monitor + shared Odds API cache ──────────────────────
    _health_monitor = init_health_monitor()
    # PrizePicks provider is temporarily disabled — not registered so it never
    # shows as a failed provider in /status or logs.
    # _health_monitor.register("OddsAPI")  # sportsbook polling disabled — Underdog-only mode
    _health_monitor.register("Underdog")
    logger.info("Provider health monitor initialised (Underdog) [sportsbook polling disabled]")

    init_odds_cache(ttl_seconds=config.ODDS_API_CACHE_TTL)
    logger.info("Odds API shared cache initialised (TTL=%ds)", config.ODDS_API_CACHE_TTL)

    # ── API usage tracker + budget enforcement ────────────────────────────────
    _usage_tracker = init_usage_tracker(
        monthly_budgets={"OddsAPI": config.ODDS_API_MONTHLY_BUDGET},
    )
    _usage_tracker.set_season_checker(_season_checker)
    logger.info(
        "API usage tracker initialised (OddsAPI budget: %d requests/month)",
        config.ODDS_API_MONTHLY_BUDGET,
    )

    # Game results provider — not wired to any job yet; framework only
    _results_provider = OddsApiResultsProvider(api_key=config.ODDS_API_KEY)
    application.bot_data["results_provider"] = _results_provider

    allowed_ids = list(config.allowed_user_ids)
    if not allowed_ids:
        logger.warning(
            "⚠️  ALLOWED_USER_IDS is not configured — proactive alerts will NOT "
            "be delivered to anyone. Set ALLOWED_USER_IDS=<your_telegram_chat_id> "
            "in the environment, then restart the bot."
        )
    else:
        logger.info("Alert recipients: %s", allowed_ids)
    init_handlers(_db, _engine, allowed_ids, season_checker=_season_checker)

    # ── Multi-platform connector registry ─────────────────────────────────────
    registry = ConnectorRegistry()
    active_sports = config.active_sports

    registry.register(DraftKingsConnector(
        odds_api_key   = config.ODDS_API_KEY,
        active_sports  = active_sports,
        enabled        = config.DRAFTKINGS_ENABLED,
        season_checker = _season_checker,
    ))
    registry.register(FanDuelConnector(
        odds_api_key   = config.ODDS_API_KEY,
        active_sports  = active_sports,
        enabled        = config.FANDUEL_ENABLED,
        season_checker = _season_checker,
    ))
    registry.register(UnderdogConnector(
        # active_sports=None: accept every sport the Underdog API returns.
        # Underdog only lists currently-active props, so out-of-season sports
        # (NFL, NBA) simply don't appear — no explicit blocklist is needed.
        # This gives us MLB, WNBA, Soccer, Tennis, and any other live markets
        # without hardcoding sport IDs that may change or be added.
        enabled = config.UNDERDOG_ENABLED,
    ))

    init_market_engine(registry)

    # Store db reference in bot_data for market engine jobs
    application.bot_data["db"] = _db

    # Register background jobs
    jq = application.job_queue
    if jq:
        # ── Sportsbook polling disabled — Underdog-only mode ─────────────────
        # To re-enable sportsbook monitoring: uncomment the blocks below and
        # restart the bot.  All logic is preserved; nothing has been deleted.
        # jq.run_repeating(_poll_odds_job,      interval=config.ODDS_POLL_INTERVAL,        first=10,  name="odds_poller")
        # jq.run_repeating(_steam_check_job,    interval=config.STEAM_CHECK_INTERVAL,      first=15,  name="steam_checker")
        # player_props_fetcher disabled — fetches OddsAPI player-prop markets for
        # the PrizePicks crossmatch pipeline which is currently off.  Calling
        # it while PP is disabled causes 422 INVALID_MARKET errors on every cycle
        # and wastes OddsAPI credits.  Re-enable when PrizePicks resumes.
        # jq.run_repeating(_player_props_job,  interval=config.PLAYER_PROP_POLL_INTERVAL, first=60, name="player_props_fetcher")
        jq.run_repeating(_pregame_watch_job,   interval=config.PREGAME_SCAN_INTERVAL,    first=30,  name="pregame_watcher")
        # _prizepicks_job disabled — PrizePicks provider temporarily off
        # jq.run_repeating(connector_poll_job,  interval=config.CONNECTOR_POLL_INTERVAL,   first=20,  name="connector_poller")
        # jq.run_repeating(consensus_check_job, interval=config.CONSENSUS_CHECK_INTERVAL,  first=25,  name="consensus_checker")
        # jq.run_repeating(clv_check_job,       interval=config.CLV_CHECK_INTERVAL,        first=35,  name="clv_checker")
        # ─────────────────────────────────────────────────────────────────────
        jq.run_repeating(underdog_job,         interval=config.UNDERDOG_POLL_INTERVAL,      first=45,  name="underdog_monitor")
        # CLV seed job — every 15 minutes (creates AlertCLVSeed entries for alerts)
        jq.run_repeating(_clv_seed_job,        interval=900,                                first=120, name="clv_seeder")
        # CLV harvest job — every hour (processes seeds after game_time passes)
        jq.run_repeating(_clv_harvest_job,          interval=3600,  first=300,  name="clv_harvester")
        # Opportunity grader — every 6 hours (grades completed prop opportunities)
        jq.run_repeating(_grade_opportunities_job,  interval=21600, first=3600, name="opportunity_grader")
        # API budget check — every 15 minutes
        jq.run_repeating(_budget_check_job,    interval=900,                                first=900, name="budget_checker")
        # Heartbeat — every 60 s (keeps HealthTracker alive timestamp current)
        jq.run_repeating(_heartbeat_job,       interval=60,                                 first=30,  name="heartbeat")
        # Season / market-status refresh (skip when interval is 0 = disabled)
        if config.SEASON_CHECK_INTERVAL > 0:
            jq.run_repeating(
                _season_check_job,
                interval=config.SEASON_CHECK_INTERVAL,
                first=config.SEASON_CHECK_INTERVAL,  # first eager load already done above
                name="season_checker",
            )
        logger.info(
            "Jobs scheduled — underdog: every %ds, pregame: every %ds, "
            "season_check: every %ds, heartbeat: every 60s "
            "[player_props_fetcher disabled — PP pipeline off]",
            config.UNDERDOG_POLL_INTERVAL,
            config.PREGAME_SCAN_INTERVAL,
            config.SEASON_CHECK_INTERVAL,
        )
    else:
        logger.warning("JobQueue not available — background jobs disabled.")

    logger.info("Bot initialised and ready.")

    # ── Startup / crash-recovery notification ─────────────────────────────────
    # Fire-and-forget — non-fatal; network errors are logged at WARNING only.
    try:
        await _send_startup_notification(
            bot      = application.bot,
            chat_ids = list(config.allowed_user_ids),
            ht       = get_health_tracker(),
        )
    except Exception as _notif_exc:
        logger.warning("Startup notification failed (non-fatal): %s", _notif_exc)


async def _send_startup_notification(bot, chat_ids: list[int], ht) -> None:
    """
    Send a startup or crash-recovery notification to all configured chat IDs.

    Crash format (unexpected_exit):
        ⚠️ Sharp Money Bot Restarted
        Reason: Unexpected Exit
        Last Active Task: …
        Last Heartbeat: HH:MM UTC
        Recovery Status: Resumed Monitoring ✅

    Normal start format:
        ✅ Sharp Money Bot Online
        Status: Underdog monitoring active
        Startup: #N
    """
    if not chat_ids:
        return

    startup_reason = ht.startup_reason() if ht else "unknown"
    restart_num    = ht.restart_count()  if ht else 0
    last_hb        = ht.last_heartbeat() if ht else None
    last_error     = ht.last_error()     if ht else None
    last_job       = (
        ht._state.get("last_job_run", {}).get("job", "Underdog Market Monitor")
        if ht and hasattr(ht, "_state") else "Underdog Market Monitor"
    )

    if startup_reason == "unexpected_exit":
        # Format the last heartbeat timestamp
        hb_str = "unknown"
        if last_hb:
            try:
                from datetime import datetime as _dt
                hb_dt  = _dt.fromisoformat(last_hb.replace("Z", "+00:00"))
                hb_str = hb_dt.strftime("%H:%M UTC")
            except Exception:
                hb_str = str(last_hb)[:16]

        parts = [
            "⚠️ <b>Sharp Money Bot Restarted</b>",
            "",
            "<b>Reason:</b>  Unexpected Exit",
            "",
            f"<b>Last Active Task:</b>  {last_job or 'Underdog Market Monitor'}",
            f"<b>Last Heartbeat:</b>  {hb_str}",
        ]
        if last_error:
            import html as _h
            parts += ["", f"<b>Last Error:</b>  <code>{_h.escape(last_error[:200])}</code>"]
        parts += [
            "",
            "<b>Recovery Status:</b>  Resumed Monitoring ✅",
            f"<i>Startup #{restart_num}</i>",
        ]
    elif startup_reason == "first_start":
        parts = [
            "✅ <b>Sharp Money Bot Online</b>",
            "",
            "<b>Status:</b>  Underdog monitoring active",
            "<b>Mode:</b>  S / A / B tier recommendations only",
            "",
            "<i>Ready. Props will be analysed as they are detected.</i>",
        ]
    else:
        # clean restart / manual restart
        parts = [
            "✅ <b>Sharp Money Bot Online</b>",
            "",
            "<b>Status:</b>  Underdog monitoring active",
            f"<b>Startup:</b>  #{restart_num}",
            "",
            "<i>S/A/B tier recommendations will be sent as props are detected.</i>",
        ]

    text = "\n".join(parts)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            logger.info("Startup notification sent to %d", chat_id)
        except Exception as exc:
            logger.warning("Startup notification: failed for %d: %s", chat_id, exc)


async def post_shutdown(application: Application) -> None:
    """Runs once after polling stops, before the process exits."""
    # Record clean shutdown BEFORE closing DB so the sidecar is written
    # even if DB close raises.  This covers SIGTERM, SIGINT, and programmatic
    # stops.  SIGKILL / hard crashes skip this; the atexit fallback handles
    # the crash path, and hard kills leave no record → "unexpected_exit" on
    # next startup.
    ht = get_health_tracker()
    if ht is not None:
        ht.record_shutdown("clean_shutdown")
    if _db:
        await _db.close()
    logger.info("Shutdown complete. Goodbye.")


# ── Heartbeat job ─────────────────────────────────────────────────────────────

async def _grade_opportunities_job(context) -> None:
    """
    Every 6 hours: grade completed prop opportunities from stored game results.

    Finds PENDING rows in prop_opportunity_log whose game_time passed at least
    4 hours ago, looks up the actual player stat in player_game_results, and
    records HIT / MISS / PUSH from the OVER perspective:
      HIT  → actual_value > line_value
      MISS → actual_value < line_value
      PUSH → actual_value == line_value
    """
    db: Optional[Database] = context.bot_data.get("db")
    if not db:
        return
    try:
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        if not pending:
            return
        graded = 0
        for opp in pending:
            if not opp.game_time:
                continue
            game_date = opp.game_time.strftime("%Y-%m-%d")
            result_row = await db.get_game_result_for_grading(
                opp.player_name, opp.sport, opp.stat_type, game_date
            )
            if result_row is None:
                continue
            actual = result_row.actual_value
            line   = opp.line_value
            if actual > line:
                outcome = "HIT"
            elif actual < line:
                outcome = "MISS"
            else:
                outcome = "PUSH"
            # Classify MISS outcomes for learning — only Model errors update weights
            error_type: Optional[str] = None
            if outcome == "MISS":
                from engine.calibration import classify_miss
                error_type = classify_miss(
                    recommendation = opp.recommendation,
                    decision_tier  = opp.decision_tier,
                    confidence     = opp.confidence,
                    actual_value   = actual,
                    line_value     = opp.line_value,
                )
            await db.grade_opportunity(opp.id, outcome, actual, error_type=error_type)
            graded += 1
        if graded:
            logger.info("_grade_opportunities_job: graded=%d opportunities", graded)
    except Exception:
        logger.exception("_grade_opportunities_job: unexpected error")


async def _heartbeat_job(context) -> None:
    """Run every 60 s to keep the HealthTracker heartbeat timestamp current."""
    ht = get_health_tracker()
    if ht is None:
        return
    try:
        ht.update_heartbeat()
        ht.record_job_run("heartbeat_job")
    except Exception as _exc:
        logger.exception("_heartbeat_job: error: %s", _exc)
        ht.record_job_fail("heartbeat_job", str(_exc))


# ── Background jobs ────────────────────────────────────────────────────────────

async def _poll_odds_job(context) -> None:
    """
    Fetch live odds for all active sports, store each line as an OddsRecord,
    then scan for +EV opportunities and alert on any that exceed the threshold.

    EV detection approach:
      1. Group fetched lines by (event, market_type).
      2. Identify the two distinct selections in each market pair.
      3. Find the best (highest) offered odds for each selection across all books.
      4. Use multiplicative vig removal to build the fair probability.
      5. For every book's offered odds on each side, compute EV against that
         fair line and alert if EV ≥ MIN_EV_THRESHOLD.
    """
    if _db is None or _engine is None:
        logger.debug("_poll_odds_job: DB or engine not ready, skipping")
        return

    now      = datetime.utcnow()
    bot      = context.bot
    chat_ids = list(config.allowed_user_ids)
    delivery = AlertDelivery(_db, bot, chat_ids)

    # ── 1. Fetch odds for every active sport ──────────────────────────────────
    all_lines: list[OddsLine] = []
    for sport_str in config.active_sports:
        try:
            sport = Sport(sport_str)
        except ValueError:
            logger.warning("Unknown sport in ACTIVE_SPORTS: %s", sport_str)
            continue
        # Skip sports that are out of season / have no active markets.
        # The checker is fail-open: returns True if cache not yet populated.
        if _season_checker is not None:
            odds_key = _SPORT_TO_ODDS_API_KEY.get(sport)
            if odds_key and not _season_checker.is_sport_active(odds_key):
                logger.info(
                    "_poll_odds_job: skipping %s (%s) — out of season / no active markets",
                    sport_str, odds_key,
                )
                continue
        lines = await _engine.fetch_live_odds(sport)
        all_lines.extend(lines)

    if not all_lines:
        logger.debug("_poll_odds_job: no lines returned from API")
        return

    # ── 1b. Early scope filter — drop lines that can never be delivered ────────
    # Only MLB Moneyline / MLB Totals can pass the AlertScopeFilter for DK/FD EV
    # alerts.  Filtering here avoids writing out-of-scope rows to odds_records
    # and running EV analysis on data that will always be blocked at delivery.
    before_filter = len(all_lines)
    all_lines = [l for l in all_lines if is_ev_line_in_scope(l.sport, l.market_type)]
    dropped = before_filter - len(all_lines)
    if dropped:
        logger.debug(
            "_poll_odds_job: dropped %d out-of-scope lines before analysis (%d remain)",
            dropped, len(all_lines),
        )

    if not all_lines:
        logger.debug("_poll_odds_job: no in-scope lines after scope filter")
        return

    logger.info("_poll_odds_job: storing %d in-scope odds lines", len(all_lines))

    # ── 2. Store every in-scope line to the database ──────────────────────────
    for line in all_lines:
        record = OddsRecord(
            sportsbook=line.sportsbook,
            sport=line.sport.value,
            market_type=line.market_type.value,
            event=line.event,
            selection=line.selection,
            american_odds=line.american_odds,
            line=line.line,
            event_start=line.event_start,
            recorded_at=now,
        )
        await _db.save_odds(record)

    # ── 3. Detect +EV opportunities ───────────────────────────────────────────
    # Group lines by (event, market_type) to find opposing sides.
    market_groups: dict[tuple[str, MarketType], list[OddsLine]] = defaultdict(list)
    for line in all_lines:
        market_groups[(line.event, line.market_type)].append(line)

    for (event, market_type), group in market_groups.items():
        # Collect distinct selections and best odds for each
        best_odds_per_selection: dict[str, tuple[int, str]] = {}  # sel -> (odds, book)
        for line in group:
            sel = line.selection
            if sel not in best_odds_per_selection or line.american_odds > best_odds_per_selection[sel][0]:
                best_odds_per_selection[sel] = (line.american_odds, line.sportsbook)

        selections = list(best_odds_per_selection.keys())
        if len(selections) != 2:
            # Need exactly two sides for vig removal
            continue

        sel_a, sel_b = selections[0], selections[1]
        best_a, book_a = best_odds_per_selection[sel_a]
        best_b, book_b = best_odds_per_selection[sel_b]

        # Evaluate EV for each book's offered odds against the fair line
        for line in group:
            is_side_a = (line.selection == sel_a)
            opp_best_odds = best_b if is_side_a else best_a

            try:
                opp = _engine.analyze_line(
                    sport=line.sport,
                    market_type=line.market_type,
                    event=line.event,
                    selection=line.selection,
                    player=None,
                    line=line.line,
                    side_a_odds=line.american_odds,
                    side_b_odds=opp_best_odds,
                    is_side_a=True,
                    best_book=line.sportsbook,
                )
            except Exception as exc:
                logger.debug("EV analysis failed for %s/%s: %s", event, line.selection, exc)
                continue

            # AlertDelivery handles: filter → dedup → format → send → log
            await delivery.deliver_ev(opp)


async def _steam_check_job(context) -> None:
    """
    Scan the recent odds database for steam / sharp money moves.

    Steam detection approach:
      1. For each active sport, pull all OddsRecords from the last
         5 × ODDS_POLL_INTERVAL seconds.
      2. Group by (event, selection, sportsbook).
      3. For every group with at least two records spanning different poll
         times, compute the odds movement (earliest → latest).
      4. Re-group movements by (event, selection) to count how many
         sportsbooks moved in the same direction.
      5. Build an OddsMovement, score it, and alert if steam_score ≥
         MIN_STEAM_SCORE and the alert hasn't been sent recently.
    """
    if _db is None or _engine is None:
        logger.debug("_steam_check_job: DB or engine not ready, skipping")
        return

    now      = datetime.utcnow()
    bot      = context.bot
    chat_ids = list(config.allowed_user_ids)
    delivery = AlertDelivery(_db, bot, chat_ids)
    window   = timedelta(seconds=config.ODDS_POLL_INTERVAL * 5)
    since = now - window

    for sport_str in config.active_sports:
        try:
            sport = Sport(sport_str)
        except ValueError:
            continue

        # Skip sports that are out of season / have no active markets.
        if _season_checker is not None:
            odds_key = _SPORT_TO_ODDS_API_KEY.get(sport)
            if odds_key and not _season_checker.is_sport_active(odds_key):
                logger.debug(
                    "_steam_check_job: skipping %s (%s) — out of season / no active markets",
                    sport_str, odds_key,
                )
                continue

        # Skip sports whose steam alerts are always blocked by scope.
        # Steam is blocked for all non-MLB sports (and for all steam alert
        # types regardless of sport), so reading their odds records is wasted
        # I/O.  Only MLB records could theoretically produce a deliverable alert
        # if the scope is ever widened; keep MLB here for forward-compatibility.
        if sport != Sport.MLB:
            logger.debug(
                "_steam_check_job: skipping %s — steam outside approved scope", sport_str
            )
            continue

        records = await _db.get_odds_window(sport.value, since)
        if not records:
            continue

        # Group by (event, selection, sportsbook)
        groups: dict[tuple[str, str, str], list[OddsRecord]] = defaultdict(list)
        for r in records:
            groups[(r.event, r.selection, r.sportsbook)].append(r)

        # Compute per-book movements
        # movements_by_market: (event, selection, market_type) -> list of (book, change)
        movements_by_market: dict[tuple[str, str, str], list[tuple[str, int]]] = defaultdict(list)
        earliest_record: dict[tuple[str, str, str], OddsRecord] = {}  # for opening odds
        latest_record: dict[tuple[str, str, str], OddsRecord] = {}    # for current odds

        for (event, selection, book), recs in groups.items():
            if len(recs) < 2:
                continue
            recs_sorted = sorted(recs, key=lambda r: r.recorded_at)
            first, last = recs_sorted[0], recs_sorted[-1]
            change = last.american_odds - first.american_odds
            if change == 0:
                continue
            key = (event, selection, last.market_type)
            movements_by_market[key].append((book, change))
            if key not in earliest_record:
                earliest_record[key] = first
                latest_record[key] = last
            else:
                # keep the earliest opening and latest current
                if first.recorded_at < earliest_record[key].recorded_at:
                    earliest_record[key] = first
                if last.recorded_at > latest_record[key].recorded_at:
                    latest_record[key] = last

        # Evaluate each (event, selection, market_type) for steam
        for key, book_changes in movements_by_market.items():
            event, selection, market_type_str = key
            if not book_changes:
                continue

            # Consensus direction: majority of books
            ups   = [b for b, c in book_changes if c > 0]
            downs = [b for b, c in book_changes if c < 0]
            books_moved = ups if len(ups) >= len(downs) else downs
            if not books_moved:
                continue

            opening_rec = earliest_record[key]
            current_rec = latest_record[key]

            # Build model objects for the engine
            try:
                mtype = MarketType(market_type_str)
            except ValueError:
                mtype = MarketType.MONEYLINE

            opening_line = OddsLine(
                sportsbook=opening_rec.sportsbook,
                sport=sport,
                market_type=mtype,
                event=event,
                selection=selection,
                american_odds=opening_rec.american_odds,
                line=opening_rec.line,
                timestamp=opening_rec.recorded_at,
            )
            current_line = OddsLine(
                sportsbook=current_rec.sportsbook,
                sport=sport,
                market_type=mtype,
                event=event,
                selection=selection,
                american_odds=current_rec.american_odds,
                line=current_rec.line,
                timestamp=current_rec.recorded_at,
            )
            movement = OddsMovement(opening=opening_line, current=current_line)
            steam_alert = _engine._steam.build_steam_alert(
                movement, sport, mtype, event, selection, books_moved
            )

            # AlertDelivery handles: filter → dedup → format → send → log
            await delivery.deliver_steam(steam_alert)


# ── CLV seed job ──────────────────────────────────────────────────────────────

async def _clv_harvest_job(context) -> None:
    """
    Run every hour.  Processes pending AlertCLVSeeds whose game_time has passed:

    - For seeds with bet_odds:  tries to find a closing-odds proxy in OddsRecord;
      if found, computes CLV% and writes a CLVRecord; otherwise marks expired
      after a grace period (4 hours post game_time).
    - For seeds without bet_odds (Underdog pick'em, no sportsbook odds): marks
      expired immediately — CLV% is not applicable to pick'em props.

    When sportsbook polling is re-enabled this job will have real closing-odds
    data; until then it gracefully expires seeds it cannot resolve.
    """
    if _db is None:
        # DB not ready — record as successful no-op so /health never shows "never ran"
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_clv_harvest_job")
        return

    try:
        from engine.clv import compute_clv
        from database import CLVRecord
    except ImportError as _imp_exc:
        logger.warning("_clv_harvest_job: import failed — %s", _imp_exc)
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_fail("_clv_harvest_job", f"ImportError: {_imp_exc}")
        return

    try:
        seeds = await _db.get_pending_clv_seeds(limit=50)
        if not seeds:
            # Idle cycle — nothing to harvest; still counts as a successful run
            _ht = get_health_tracker()
            if _ht:
                _ht.record_job_run("_clv_harvest_job")
            return

        harvested = 0
        expired   = 0
        now       = datetime.utcnow()
        grace     = timedelta(hours=4)   # wait this long for closing odds before expiring

        for seed in seeds:
            game_time = seed.game_time
            if game_time is None:
                # No timing info — expire immediately
                await _db.mark_clv_seed_expired(seed.id)
                expired += 1
                continue

            # Underdog pick'em seeds have no sportsbook bet_odds → expire immediately
            if not seed.bet_odds or seed.alert_type == "UNDERDOG":
                await _db.mark_clv_seed_expired(seed.id)
                expired += 1
                continue

            # Try to find closing odds from the last OddsRecord for this event
            closing_record = await _db.get_last_odds_for_event(
                seed.event or "", seed.selection or ""
            )

            if closing_record is not None and closing_record.american_odds:
                # We have closing odds — compute CLV
                try:
                    clv_result = compute_clv(
                        bet_odds              = seed.bet_odds,
                        closing_odds          = closing_record.american_odds,
                        counterpart_bet_odds  = seed.counterpart_odds,
                        counterpart_close_odds= None,
                        selection             = seed.selection or "",
                        notes                 = f"source={seed.source_table}:{seed.source_id}",
                    )
                    rec = CLVRecord(
                        selection              = seed.selection or "",
                        event                  = seed.event     or "",
                        sport                  = seed.sport     or "",
                        bet_odds               = seed.bet_odds,
                        closing_odds           = closing_record.american_odds,
                        clv_pct                = clv_result.clv_pct,
                        clv_proxy              = clv_result.clv_lead,
                        fair_prob_bet          = clv_result.fair_prob_bet,
                        fair_prob_close        = clv_result.fair_prob_close,
                        counterpart_bet_odds   = seed.counterpart_odds,
                        counterpart_close_odds = None,
                        notes                  = clv_result.notes,
                        computed_at            = now,
                    )
                    # Set migration-added columns if present
                    try:
                        rec.alert_type   = seed.alert_type   or ""
                        rec.market_type  = seed.market_type  or ""
                        rec.tier         = seed.tier         or ""
                    except AttributeError:
                        pass
                    await _db.save_clv_record(rec)
                    await _db.mark_clv_seed_computed(seed.id, clv_result.clv_pct)
                    harvested += 1
                except Exception as exc:
                    logger.warning(
                        "_clv_harvest_job: CLV compute failed for seed %d: %s", seed.id, exc
                    )
            elif now - game_time > grace:
                # Grace period elapsed, no odds found → expire
                await _db.mark_clv_seed_expired(seed.id)
                expired += 1

        if harvested or expired:
            logger.info(
                "_clv_harvest_job: harvested=%d expired=%d (pending remaining=%d)",
                harvested, expired, len(seeds) - harvested - expired,
            )

    except Exception as exc:
        logger.exception("_clv_harvest_job: error: %s", exc)
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_fail("_clv_harvest_job", str(exc))
        return

    _ht = get_health_tracker()
    if _ht:
        _ht.record_job_run("_clv_harvest_job")


async def _clv_seed_job(context) -> None:
    """
    Run every 15 minutes.  Scans alerted EV and Underdog records that have
    not yet been seeded for CLV tracking, and creates AlertCLVSeed entries
    for each.

    This is a lightweight read-then-write operation that never modifies
    existing alert data or fires any Telegram messages.

    When sportsbook polling is re-enabled, a separate harvest job will read
    these seeds, fetch closing odds, compute CLV%, and write clv_records.
    """
    if _db is None:
        # DB not ready — record as successful no-op so /health never shows "never ran"
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_clv_seed_job")
        return
    try:
        ev_seeded = await _db.seed_clv_from_ev_records(limit=100)
        ud_seeded = await _db.seed_clv_from_ud_snapshots(limit=100)
        if ev_seeded or ud_seeded:
            logger.info(
                "_clv_seed_job: seeded %d EV + %d Underdog alerts for CLV tracking",
                ev_seeded, ud_seeded,
            )
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_clv_seed_job")
    except Exception as exc:
        logger.exception("_clv_seed_job: error during CLV seeding: %s", exc)
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_fail("_clv_seed_job", str(exc))


# ── API budget check job ───────────────────────────────────────────────────────

async def _budget_check_job(context) -> None:
    """
    Run every 15 minutes.  Checks API usage against the pacing budget and
    sends a Telegram alert the *first* time each threshold (75 / 90 / 100 %)
    is crossed in a calendar month.

    "Pacing budget" (ODDS_API_MONTHLY_BUDGET) is a self-imposed call cap and
    is separate from the actual OddsAPI plan quota reported by API headers.

    Alert escalation:
      ≥ 75 % → ⚠️  warning
      ≥ 90 % → ⚠️  serious warning; LOW-priority calls now blocked
      ≥ 100% → 🚨  pacing budget exceeded; LOW + MEDIUM blocked; HIGH + CRITICAL pass
    """
    _tracker = get_usage_tracker()
    if _tracker is None:
        # Tracker not ready — record as successful no-op so /health never shows "never ran"
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_budget_check_job")
        return

    bot      = context.bot
    chat_ids = list(config.allowed_user_ids)
    if not chat_ids:
        # No recipients configured — still a successful (no-op) run
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_budget_check_job")
        return

    try:
        for provider, stats in _tracker.get_all_stats().items():
            if stats.month_budget <= 0:
                continue

            alerted = _last_budget_alerted.setdefault(provider, set())

            for threshold in (75, 90, 100):
                if stats.budget_pct >= threshold and threshold not in alerted:
                    alerted.add(threshold)

                    if threshold >= 100:
                        icon    = "🚨"
                        heading = "PACING BUDGET EXCEEDED"
                    elif threshold >= 90:
                        icon    = "⚠️"
                        heading = f"PACING BUDGET WARNING — {threshold}%"
                    else:
                        icon    = "⚠️"
                        heading = f"PACING BUDGET WARNING — {threshold}%"

                    # Pacing budget: how many calls made vs the self-imposed cap
                    pacing_used_str = (
                        f"{stats.quota_used:,}"
                        if stats.quota_used is not None
                        else f"~{stats.month_count:,}"
                    )

                    # Actual OddsAPI plan quota from response headers (separate concept)
                    if stats.quota_remaining is not None or stats.quota_used is not None:
                        r = f"{stats.quota_remaining:,}" if stats.quota_remaining is not None else "?"
                        u = f"{stats.quota_used:,}"      if stats.quota_used      is not None else "?"
                        api_quota_line = f"<b>API quota:</b>     {r} remaining  ·  {u} used\n"
                    else:
                        api_quota_line = ""

                    # Sport breakdown — which UD alert sports are active
                    _sport_icons = {
                        "MLB": "⚾", "WNBA": "🏀", "NBA": "🏀", "NFL": "🏈",
                        "DOTA": "🎮", "CS": "🖥️", "TENNIS": "🎾",
                    }
                    ud_sports     = sorted(config.ud_alert_sports)
                    active_sp     = sorted(config.active_sports)
                    sport_lines   = "  ".join(
                        f"{_sport_icons.get(s, '🔸')}{s}" for s in ud_sports
                    )
                    odds_api_sp   = "  ".join(
                        f"{_sport_icons.get(s, '🔸')}{s}" for s in active_sp
                    ) or "none"

                    if threshold >= 100:
                        blocking_section = (
                            "<b>Blocked:</b>     LOW + MEDIUM priority calls\n"
                            "<b>Protected:</b>   HIGH + CRITICAL — approved active sports\n"
                        )
                        footer = (
                            "This is your self-imposed pacing cap — the actual OddsAPI plan quota "
                            "is not exhausted.\n"
                            "Raise <code>ODDS_API_MONTHLY_BUDGET</code> to allow more calls."
                        )
                    elif threshold >= 90:
                        blocking_section = (
                            "<b>Blocked:</b>     LOW priority calls\n"
                            "<b>Protected:</b>   HIGH + CRITICAL — approved active sports\n"
                        )
                        footer = "Monitor closely before the end of the month."
                    else:
                        blocking_section = ""
                        footer = "Approaching pacing cap. Monitor before the end of the month."

                    msg = (
                        f"{icon} <b>{heading}</b>\n"
                        f"\n"
                        f"<b>Provider:</b>      {provider}\n"
                        f"<b>Pacing budget:</b> {pacing_used_str} / {stats.month_budget:,}"
                        f"  ({stats.budget_pct:.1f}%)\n"
                        f"<b>Pacing bar:</b>    <code>{stats.budget_bar}</code>\n"
                        f"{api_quota_line}"
                        f"\n"
                        f"{blocking_section}"
                        f"\n"
                        f"<b>Alert sports:</b>  {sport_lines}\n"
                        f"<b>Odds API scope:</b> {odds_api_sp}\n"
                        f"<i>(DOTA / TENNIS / CS use external APIs — not affected by this cap)</i>\n"
                        f"\n"
                        f"<i>{footer}</i>"
                    )

                    await broadcast_alert(bot, chat_ids, msg)
                    logger.warning(
                        "_budget_check_job: %s crossed %d%% pacing threshold "
                        "(used=%s / pacing_budget=%d, api_quota_remaining=%s)",
                        provider, threshold, pacing_used_str, stats.month_budget,
                        stats.quota_remaining if stats.quota_remaining is not None else "unknown",
                    )

    except Exception as _exc:
        logger.exception("_budget_check_job: error: %s", _exc)
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_fail("_budget_check_job", str(_exc))
        return

    _ht = get_health_tracker()
    if _ht:
        _ht.record_job_run("_budget_check_job")


# ── Season / market-status refresh job ────────────────────────────────────────

async def _season_check_job(context) -> None:
    """
    Periodically refresh the season / market-status cache.

    Calls SeasonChecker.refresh() unconditionally (the job scheduler
    already manages the interval).  Failures are logged inside the
    checker and leave the previous cache value intact (fail-open).
    """
    if _season_checker is None:
        # Checker not ready — record as successful no-op so /health never shows "never ran"
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_season_check_job")
        return
    try:
        await _season_checker.refresh()
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_season_check_job")
    except Exception as exc:
        logger.exception("_season_check_job: error: %s", exc)
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_fail("_season_check_job", str(exc))


# ── Pregame market watch job (continuous, all-day) ────────────────────────────

async def _pregame_watch_job(context) -> None:
    """
    Continuous pregame market watch — runs every PREGAME_SCAN_INTERVAL seconds.

    Each cycle:
      1. morning_scan: discover new props and record opening lines.
      2. pregame_scan: re-fetch current lines, compute movement, fire alerts
         for qualifying props (conf ≥ 60) not yet alerted this session and
         for any prop whose line moved since the last alert.
      3. clear_stale: remove entries for games that have already started.

    No sport filtering here — the engine accepts every sport Underdog carries.
    """
    if _db is None:
        logger.debug("_pregame_watch_job: DB not ready, skipping")
        return

    _ht = get_health_tracker()
    try:
        bot      = context.application.bot
        chat_ids = list(config.allowed_user_ids)
        engine   = get_pregame_watch_engine()

        n_created = await engine.morning_scan(_db)
        n_alerts  = await engine.pregame_scan(_db, bot, chat_ids)
        n_cleared = engine.clear_stale()

        if n_created or n_alerts or n_cleared:
            logger.info(
                "_pregame_watch_job: watching=%d  new=%d  alerts=%d  cleared=%d",
                engine.watch_count, n_created, n_alerts, n_cleared,
            )
        else:
            logger.debug("_pregame_watch_job: watching=%d  no changes", engine.watch_count)

        if _ht:
            _ht.record_job_run("_pregame_watch_job")

    except Exception as exc:
        logger.exception("_pregame_watch_job: error: %s", exc)
        if _ht:
            _ht.record_job_fail("_pregame_watch_job", str(exc))


# ── Player-prop odds fetching job ─────────────────────────────────────────────

async def _player_props_job(context) -> None:
    """
    Fetch player-prop odds for every configured sport and store them as
    OddsRecord rows so the PrizePicks crossmatch pipeline can find sportsbook
    equivalents when _prizepicks_job runs.

    Runs every PLAYER_PROP_POLL_INTERVAL seconds (default 10 min).  Only
    fetches sports that are active according to the season checker, so credits
    are not wasted on off-season leagues.
    """
    if _db is None or _engine is None:
        logger.debug("_player_props_job: DB or engine not ready, skipping")
        return

    now = datetime.utcnow()
    total_saved = 0

    for sport_str in config.player_prop_sports:
        try:
            sport = Sport(sport_str)
        except ValueError:
            logger.warning("_player_props_job: unknown sport %r in PLAYER_PROP_SPORTS", sport_str)
            continue

        # Skip out-of-season sports to protect API credits
        if _season_checker is not None:
            odds_key = _SPORT_TO_ODDS_API_KEY.get(sport)
            if odds_key and not _season_checker.is_sport_active(odds_key):
                logger.info(
                    "_player_props_job: skipping %s (%s) — out of season",
                    sport_str, odds_key,
                )
                continue

        lines: list[PlayerPropLine] = await _engine.fetch_player_prop_odds(sport)
        if not lines:
            logger.debug("_player_props_job: no player prop lines for %s", sport_str)
            continue

        for pl in lines:
            # selection = "Player Name Over" / "Player Name Under"
            selection = f"{pl.player_name} {pl.description}".strip()
            record = OddsRecord(
                sportsbook=pl.sportsbook,
                sport=pl.sport.value,
                market_type=pl.market_key,   # e.g. "player_points" — matches PP_STAT_TO_ODDS_API
                event=pl.event,
                selection=selection,
                american_odds=pl.american_odds,
                line=pl.line,
                event_start=pl.event_start,
                recorded_at=now,
            )
            await _db.save_odds(record)
            total_saved += 1

    if total_saved:
        logger.info("_player_props_job: saved %d player prop records", total_saved)
    else:
        logger.debug("_player_props_job: no player prop records saved this cycle")


# ── PrizePicks monitoring job ─────────────────────────────────────────────────

async def _prizepicks_job(context) -> None:
    """
    Fetch PrizePicks projections, store line history, compare against any
    available sportsbook player-prop odds, and alert on edges that exceed
    config.MIN_PP_EDGE.

    Pipeline per projection:
      1. Fetch PP lines for each configured league.
      2. Store every line as a PrizePicksRecord (line history).
      3. Look up matching sportsbook player-prop OddsRecords in the DB
         (populated by _poll_odds_job when player-prop markets are fetched).
      4. When a match exists: compute edge via compare_pp_to_sportsbook().
      5. Filter by MIN_PP_EDGE and MIN_PP_FAIR_PROB thresholds.
      6. Deduplicate and alert.
    """
    if _db is None:
        logger.debug("_prizepicks_job: DB not ready, skipping")
        return

    bot      = context.bot
    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()
    since    = now - timedelta(minutes=30)   # sportsbook odds freshness window

    async with PrizePicksClient() as pp_client:
        for league_name in config.prizepicks_leagues:
            league_id = PP_LEAGUE_IDS.get(league_name)
            if league_id is None:
                logger.warning("_prizepicks_job: unknown league %r, skipping", league_name)
                continue

            lines = await pp_client.fetch_projections(league_id)
            if not lines:
                logger.debug("_prizepicks_job: no lines for %s", league_name)
                continue

            logger.info("_prizepicks_job: %d projections for %s", len(lines), league_name)

            for pp_line in lines:
                # ── 1. Persist raw line ──────────────────────────────────────
                await _db.save_pp_line(PrizePicksRecord(
                    external_id=pp_line.external_id,
                    player_name=pp_line.player_name,
                    team=pp_line.team,
                    sport=pp_line.sport,
                    stat_type=pp_line.stat_type,
                    line_value=pp_line.line_value,
                    start_time=pp_line.start_time,
                    game_description=pp_line.game_description,
                    fetched_at=now,
                ))

                # ── 2. Find sportsbook player-prop match ─────────────────────
                odds_api_market = PP_STAT_TO_ODDS_API.get(pp_line.stat_type)
                if not odds_api_market:
                    continue   # stat type not mapped yet

                sb_records = await _db.find_player_prop_odds(
                    player_name=pp_line.player_name,
                    market_type=odds_api_market,
                    since=since,
                )
                if not sb_records:
                    continue   # no sportsbook data for this player/stat yet

                over_rec  = next((r for r in sb_records if "over"  in r.selection.lower()), None)
                under_rec = next((r for r in sb_records if "under" in r.selection.lower()), None)
                if not over_rec or not under_rec:
                    continue   # need both sides for vig removal

                sb_line = over_rec.line if over_rec.line is not None else pp_line.line_value

                # ── 3. Compute edge ──────────────────────────────────────────
                try:
                    opp = compare_pp_to_sportsbook(
                        pp_line,
                        sportsbook=over_rec.sportsbook,
                        sb_line=sb_line,
                        sb_over_odds=over_rec.american_odds,
                        sb_under_odds=under_rec.american_odds,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("_prizepicks_job: edge calc error for %s: %s", pp_line.player_name, exc)
                    continue

                # ── 4. Threshold filter ──────────────────────────────────────
                adj_prob = (
                    opp.adjusted_fair_prob_over
                    if opp.best_side == "OVER"
                    else opp.adjusted_fair_prob_under
                )
                if opp.best_edge < config.MIN_PP_EDGE:
                    continue
                if adj_prob < config.MIN_PP_FAIR_PROB:
                    continue

                # ── 5. Dedup ─────────────────────────────────────────────────
                if await _db.has_recent_pp_alert(
                    pp_line.player_name, pp_line.stat_type,
                    within_seconds=config.PP_DEDUP_WINDOW,
                ):
                    logger.debug(
                        "_prizepicks_job: deduped %s %s", pp_line.player_name, pp_line.stat_type
                    )
                    continue

                # ── 6. Line movement tracking ────────────────────────────────
                opening_line, prev_line = await _db.get_pp_edge_line_history(
                    pp_line.player_name, pp_line.stat_type
                )
                if opening_line is None:
                    opening_line = pp_line.line_value  # first record — current is baseline
                if (prev_line is not None
                        and abs(pp_line.line_value - prev_line) >= config.MIN_PP_LINE_CHANGE):
                    direction = "up" if pp_line.line_value > prev_line else "down"
                    logger.info(
                        "PP line move signal: %s %s %.1f→%.1f (%s, Δ%.1f) — signal, not auto-pick",
                        pp_line.player_name, pp_line.stat_type,
                        prev_line, pp_line.line_value, direction,
                        abs(pp_line.line_value - prev_line),
                    )

                # ── 7. Score (PPAnalysisScore) ───────────────────────────────
                from engine.pp_scoring import score_pp_edge
                resolved_history = await _db.get_resolved_pp_history(
                    pp_line.player_name, pp_line.stat_type
                )
                pp_score = score_pp_edge(
                    opp,
                    history=resolved_history,
                    opening_line=opening_line,
                    now=now,
                )

                # ── 8. Deliver via AlertDelivery (scope + timing + cap + broadcast) ─
                delivery   = AlertDelivery(_db, bot, chat_ids)
                result     = await delivery.deliver_pp(opp, score=pp_score)

                # ── 9. Store edge record (alert_sent reflects actual outcome) ─
                await _db.save_pp_edge(PPEdgeRecord(
                    player_name     = pp_line.player_name,
                    team            = pp_line.team,
                    sport           = pp_line.sport,
                    stat_type       = pp_line.stat_type,
                    pp_line_value   = pp_line.line_value,
                    sportsbook      = opp.sportsbook,
                    sb_line_value   = opp.sportsbook_line,
                    sb_over_odds    = opp.sportsbook_over_odds,
                    sb_under_odds   = opp.sportsbook_under_odds,
                    fair_prob_over  = opp.adjusted_fair_prob_over,
                    fair_prob_under = opp.adjusted_fair_prob_under,
                    edge_over       = opp.edge_over,
                    edge_under      = opp.edge_under,
                    best_side       = opp.best_side,
                    best_edge       = opp.best_edge,
                    alert_sent      = result.sent,
                    tier            = pp_score.tier,
                    confidence      = float(pp_score.total),
                    result          = "PENDING",
                    opening_line    = opening_line,
                    prev_line       = prev_line,
                    detected_at     = now,
                    game_time       = getattr(pp_line, "start_time", None),
                ))

                if result.filtered:
                    logger.debug(
                        "PP alert filtered: %s | %s | %s",
                        pp_line.player_name, pp_line.stat_type, result.filtered_reason,
                    )


# ── Entry point ────────────────────────────────────────────────────────────────

def _register_atexit_fallback() -> None:
    """
    Register an atexit handler that writes 'unexpected_exit' to the health
    sidecar if post_shutdown never ran (e.g. run_polling raised an exception).

    record_shutdown_if_not_set() is a no-op when post_shutdown already wrote
    'clean_shutdown', so this never overwrites a clean exit record.

    NOTE: atexit does NOT run on SIGKILL or os._exit().  In those cases the
    sidecar has no pending_shutdown_reason → next startup infers 'unexpected_exit'
    from the missing field, which is correct.
    """
    import atexit as _atexit

    def _on_exit() -> None:
        ht = get_health_tracker()
        if ht is not None:
            ht.record_shutdown_if_not_set("unexpected_exit")

    _atexit.register(_on_exit)


def main() -> None:
    # Validate configuration before building the app
    try:
        config.validate()
    except ValueError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    # Register crash-path fallback before run_polling so it is active for
    # the entire lifetime of the process.
    _register_atexit_fallback()

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("testalert", cmd_testalert))
    app.add_handler(CommandHandler("picks",     cmd_picks))
    app.add_handler(CommandHandler("slip",      cmd_slip))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("alerts",    cmd_alerts))
    app.add_handler(CommandHandler("grade",     cmd_grade))
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("analyze",   cmd_analyze))
    app.add_handler(CommandHandler("steam",     cmd_steam))
    app.add_handler(CommandHandler("ev",        cmd_ev))
    app.add_handler(CommandHandler("clv",         cmd_clv))
    app.add_handler(CommandHandler("market",      cmd_market))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("backtest",    cmd_backtest))
    app.add_handler(CommandHandler("providers",   cmd_providers))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("config",       cmd_config))
    app.add_handler(CommandHandler("calibration",  cmd_calibration))
    app.add_handler(CommandHandler("pp_import",    cmd_pp_import))
    app.add_handler(CommandHandler("health",       cmd_health))
    app.add_handler(CommandHandler("restarts",     cmd_restarts))
    app.add_handler(CommandHandler("tracking",     cmd_tracking))
    # ── Framework v3.0 commands ───────────────────────────────────────────────
    app.add_handler(CommandHandler("analyst",    cmd_analyst))
    app.add_handler(CommandHandler("blocks",     cmd_blocks))
    app.add_handler(CommandHandler("block",      cmd_block))
    app.add_handler(CommandHandler("refinement", cmd_refinement))
    app.add_error_handler(error_handler)

    logger.info("Starting polling — press Ctrl+C to stop.")

    # run_polling() owns the event loop; do NOT call asyncio.run() around it.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
