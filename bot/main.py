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
    cmd_clv,
    cmd_market,
    cmd_performance,
    cmd_backtest,
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


# ── PTB lifecycle hooks ────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Runs once after the bot is initialised but before polling starts."""
    global _db, _engine

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Sharp Money +EV Detection Bot — Starting Up")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    _db = Database(config.DATABASE_URL)
    await _db.init()

    _engine = AnalysisEngine()

    allowed_ids = list(config.allowed_user_ids)
    init_handlers(_db, _engine, allowed_ids)

    # ── Multi-platform connector registry ─────────────────────────────────────
    registry = ConnectorRegistry()
    active_sports = config.active_sports

    registry.register(DraftKingsConnector(
        odds_api_key  = config.ODDS_API_KEY,
        active_sports = active_sports,
        enabled       = config.DRAFTKINGS_ENABLED,
    ))
    registry.register(FanDuelConnector(
        odds_api_key  = config.ODDS_API_KEY,
        active_sports = active_sports,
        enabled       = config.FANDUEL_ENABLED,
    ))
    registry.register(UnderdogConnector(
        active_sports = active_sports,
        enabled       = config.UNDERDOG_ENABLED,
    ))

    init_market_engine(registry)

    # Store db reference in bot_data for market engine jobs
    application.bot_data["db"] = _db

    # Register background jobs
    jq = application.job_queue
    if jq:
        jq.run_repeating(_poll_odds_job,       interval=config.ODDS_POLL_INTERVAL,          first=10,  name="odds_poller")
        jq.run_repeating(_steam_check_job,     interval=config.STEAM_CHECK_INTERVAL,        first=15,  name="steam_checker")
        jq.run_repeating(_prizepicks_job,      interval=config.PRIZEPICKS_POLL_INTERVAL,    first=30,  name="prizepicks_monitor")
        # Multi-platform market engine jobs
        jq.run_repeating(connector_poll_job,   interval=config.CONNECTOR_POLL_INTERVAL,     first=20,  name="connector_poller")
        jq.run_repeating(consensus_check_job,  interval=config.CONSENSUS_CHECK_INTERVAL,    first=25,  name="consensus_checker")
        jq.run_repeating(clv_check_job,        interval=config.CLV_CHECK_INTERVAL,          first=35,  name="clv_checker")
        jq.run_repeating(underdog_job,         interval=config.UNDERDOG_POLL_INTERVAL,      first=45,  name="underdog_monitor")
        logger.info(
            "Jobs scheduled — odds: every %ds, steam: every %ds, prizepicks: every %ds, "
            "connectors: every %ds, consensus: every %ds, clv: every %ds, underdog: every %ds",
            config.ODDS_POLL_INTERVAL,
            config.STEAM_CHECK_INTERVAL,
            config.PRIZEPICKS_POLL_INTERVAL,
            config.CONNECTOR_POLL_INTERVAL,
            config.CONSENSUS_CHECK_INTERVAL,
            config.CLV_CHECK_INTERVAL,
            config.UNDERDOG_POLL_INTERVAL,
        )
    else:
        logger.warning("JobQueue not available — background jobs disabled.")

    logger.info("Bot initialised and ready.")


async def post_shutdown(application: Application) -> None:
    """Runs once after polling stops, before the process exits."""
    if _db:
        await _db.close()
    logger.info("Shutdown complete. Goodbye.")


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
        lines = await _engine.fetch_live_odds(sport)
        all_lines.extend(lines)

    if not all_lines:
        logger.debug("_poll_odds_job: no lines returned from API")
        return

    logger.info("_poll_odds_job: storing %d odds lines", len(all_lines))

    # ── 2. Store every line to the database ───────────────────────────────────
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

                # ── 6. Store edge record ─────────────────────────────────────
                alert_sent = bool(chat_ids)
                await _db.save_pp_edge(PPEdgeRecord(
                    player_name=pp_line.player_name,
                    team=pp_line.team,
                    sport=pp_line.sport,
                    stat_type=pp_line.stat_type,
                    pp_line_value=pp_line.line_value,
                    sportsbook=opp.sportsbook,
                    sb_line_value=opp.sportsbook_line,
                    sb_over_odds=opp.sportsbook_over_odds,
                    sb_under_odds=opp.sportsbook_under_odds,
                    fair_prob_over=opp.adjusted_fair_prob_over,
                    fair_prob_under=opp.adjusted_fair_prob_under,
                    edge_over=opp.edge_over,
                    edge_under=opp.edge_under,
                    best_side=opp.best_side,
                    best_edge=opp.best_edge,
                    alert_sent=alert_sent,
                    detected_at=now,
                ))

                # ── 7. Alert ─────────────────────────────────────────────────
                if chat_ids:
                    message = format_pp_alert(opp)
                    await broadcast_alert(bot, chat_ids, message)
                    logger.info(
                        "PP edge alert: %s | %s | %s | edge=+%.1f%%",
                        pp_line.player_name, pp_line.stat_type,
                        opp.best_side, opp.best_edge,
                    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    # Validate configuration before building the app
    try:
        config.validate()
    except ValueError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("steam",   cmd_steam))
    app.add_handler(CommandHandler("ev",      cmd_ev))
    app.add_handler(CommandHandler("clv",         cmd_clv))
    app.add_handler(CommandHandler("market",      cmd_market))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("backtest",    cmd_backtest))
    app.add_error_handler(error_handler)

    logger.info("Starting polling — press Ctrl+C to stop.")

    # run_polling() owns the event loop; do NOT call asyncio.run() around it.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
