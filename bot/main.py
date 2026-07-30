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
from engine.analysis import _SPORT_TO_ODDS_API_KEY
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
    _health_monitor.register("PrizePicks")
    _health_monitor.register("OddsAPI")
    _health_monitor.register("Underdog")
    logger.info("Provider health monitor initialised (PrizePicks, OddsAPI, Underdog)")

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
        # API budget check — every 15 minutes
        jq.run_repeating(_budget_check_job,    interval=900,                                first=900, name="budget_checker")
        # Season / market-status refresh (skip when interval is 0 = disabled)
        if config.SEASON_CHECK_INTERVAL > 0:
            jq.run_repeating(
                _season_check_job,
                interval=config.SEASON_CHECK_INTERVAL,
                first=config.SEASON_CHECK_INTERVAL,  # first eager load already done above
                name="season_checker",
            )
        logger.info(
            "Jobs scheduled — odds: every %ds, steam: every %ds, prizepicks: every %ds, "
            "connectors: every %ds, consensus: every %ds, clv: every %ds, underdog: every %ds, "
            "season_check: every %ds",
            config.ODDS_POLL_INTERVAL,
            config.STEAM_CHECK_INTERVAL,
            config.PRIZEPICKS_POLL_INTERVAL,
            config.CONNECTOR_POLL_INTERVAL,
            config.CONSENSUS_CHECK_INTERVAL,
            config.CLV_CHECK_INTERVAL,
            config.UNDERDOG_POLL_INTERVAL,
            config.SEASON_CHECK_INTERVAL,
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

        # Skip sports that are out of season / have no active markets.
        if _season_checker is not None:
            odds_key = _SPORT_TO_ODDS_API_KEY.get(sport)
            if odds_key and not _season_checker.is_sport_active(odds_key):
                logger.debug(
                    "_steam_check_job: skipping %s (%s) — out of season / no active markets",
                    sport_str, odds_key,
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


# ── API budget check job ───────────────────────────────────────────────────────

async def _budget_check_job(context) -> None:
    """
    Run every 15 minutes.  Checks API usage against the monthly budget and
    sends a Telegram alert the *first* time each threshold (75 / 90 / 100 %)
    is crossed in a calendar month.

    Alert escalation:
      ≥ 75 % → ⚠️  warning
      ≥ 90 % → ⚠️  serious warning; LOW-priority calls now blocked
      ≥ 100% → 🚨  quota exhausted; only CRITICAL + HIGH calls pass
    """
    _tracker = get_usage_tracker()
    if _tracker is None:
        return

    bot      = context.bot
    chat_ids = list(config.allowed_user_ids)
    if not chat_ids:
        return

    for provider, stats in _tracker.get_all_stats().items():
        if stats.month_budget <= 0:
            continue

        alerted = _last_budget_alerted.setdefault(provider, set())

        for threshold in (75, 90, 100):
            if stats.budget_pct >= threshold and threshold not in alerted:
                alerted.add(threshold)

                if threshold >= 100:
                    icon    = "🚨"
                    heading = "API QUOTA EXHAUSTED"
                    note    = (
                        "All LOW + MEDIUM priority Odds API calls are now blocked.\n"
                        "PrizePicks and player-prop pipelines continue normally."
                    )
                elif threshold >= 90:
                    icon    = "⚠️"
                    heading = f"API USAGE WARNING — {threshold}%"
                    note    = "LOW-priority Odds API calls are now blocked."
                else:
                    icon    = "⚠️"
                    heading = f"API USAGE WARNING — {threshold}%"
                    note    = "Usage is elevated. Monitor before the end of the month."

                used_str = (
                    f"{stats.quota_used:,}"
                    if stats.quota_used is not None
                    else f"~{stats.month_count:,} (tracked)"
                )
                rem_str = (
                    f"{stats.quota_remaining:,}"
                    if stats.quota_remaining is not None
                    else f"~{max(0, stats.month_budget - stats.month_count):,}"
                )

                msg = (
                    f"{icon} <b>{heading}</b>\n"
                    f"\n"
                    f"<b>Provider:</b>   {provider}\n"
                    f"<b>Used:</b>       {used_str} / {stats.month_budget:,}  ({stats.budget_pct:.1f}%)\n"
                    f"<b>Remaining:</b>  {rem_str}\n"
                    f"<b>Budget:</b>     <code>{stats.budget_bar}</code>\n"
                    f"\n"
                    f"<i>{note}</i>"
                )

                await broadcast_alert(bot, chat_ids, msg)
                logger.warning(
                    "_budget_check_job: %s crossed %d%% threshold "
                    "(used=%s / budget=%d)",
                    provider, threshold, used_str, stats.month_budget,
                )


# ── Season / market-status refresh job ────────────────────────────────────────

async def _season_check_job(context) -> None:
    """
    Periodically refresh the season / market-status cache.

    Calls SeasonChecker.refresh() unconditionally (the job scheduler
    already manages the interval).  Failures are logged inside the
    checker and leave the previous cache value intact (fail-open).
    """
    if _season_checker is None:
        return
    await _season_checker.refresh()


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

                # ── 8. Normalise → AlertObject (scope check) ─────────────────
                from alert_normalizer import normalize_pp
                from alert_scope_filter import check
                norm_obj = normalize_pp(opp, score=pp_score)
                scope    = check(norm_obj)

                # ── 9. Store edge record ─────────────────────────────────────
                alert_sent = bool(chat_ids) and scope.allowed
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
                    alert_sent      = alert_sent,
                    tier            = pp_score.tier,
                    confidence      = float(pp_score.total),
                    result          = "PENDING",
                    opening_line    = opening_line,
                    prev_line       = prev_line,
                    detected_at     = now,
                ))

                # ── 10. Alert ────────────────────────────────────────────────
                if chat_ids and scope.allowed:
                    message = format_pp_alert(opp)
                    await broadcast_alert(bot, chat_ids, message)
                    logger.info(
                        "PP edge alert: %s | %s | %s | edge=+%.1f%% | "
                        "score=%d tier=%s stars=%d★",
                        pp_line.player_name, pp_line.stat_type,
                        opp.best_side, opp.best_edge,
                        pp_score.total, pp_score.tier, pp_score.stars,
                    )
                elif not scope.allowed:
                    logger.warning("PP alert skipped — %s", scope.reason)


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
    app.add_error_handler(error_handler)

    logger.info("Starting polling — press Ctrl+C to stop.")

    # run_polling() owns the event loop; do NOT call asyncio.run() around it.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
