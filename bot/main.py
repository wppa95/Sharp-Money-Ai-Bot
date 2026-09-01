"""
main.py — Sharp Money +EV Detection Bot startup.

Entry point: python bot/main.py

python-telegram-bot v20+ manages its own event loop via run_polling().
Async setup (DB init) is done through the Application's post_init /
post_shutdown lifecycle hooks — do NOT wrap run_polling() in asyncio.run().
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

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
from commands import (
    cmd_start,
    cmd_help,
    cmd_status,
    cmd_system,
    cmd_analyze,
    cmd_steam,
    cmd_ev,
    cmd_picks,
    cmd_slip,
    cmd_dashboard,
    cmd_alerts,
    cmd_reconcile,
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

    cmd_tracking,
    cmd_analyst,
    cmd_rollups,
    cmd_blocks,
    cmd_block,
    cmd_refinement,
    cmd_funnel,
    cmd_backfill,
    cmd_player,
    cmd_slipstats,
    error_handler,
    init_handlers,
)
from connectors import (
    ConnectorRegistry,
    UnderdogConnector,
)
from market_engine import (
    init_market_engine,
    init_odds_confirmation,
    underdog_job,
    _start_prizepicks_cycle,
    _run_prizepicks_independent_cycle,
)
from providers import init_health_monitor
from providers.odds_cache import init_odds_cache
from providers.game_results import OddsApiResultsProvider
from providers.usage_tracker import init_usage_tracker, get_usage_tracker
from providers.quota_pacing import init_quota_pacer, get_quota_pacer
from engine.health import init_health_tracker, get_health_tracker

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _configured_odds_api_sports() -> list[tuple[str, Sport]]:
    """Return only configured sports supported by the OddsAPI namespace.

    ``ACTIVE_SPORTS`` is also used as the Underdog scanner scope, so it can
    contain valid Underdog-only identifiers such as CS, LOL, VAL, and TENNIS.
    Those identifiers must not be parsed by the sportsbook poller as
    ``models.Sport`` values.
    """
    raw_configured = getattr(config, "ODDS_API_SPORTS_RAW", None)
    if isinstance(raw_configured, str) and raw_configured.strip():
        configured_values = raw_configured.split(",")
    else:
        # Compatibility for tests and callers that provide the pre-split
        # ACTIVE_SPORTS-only configuration object.
        configured_values = getattr(config, "active_sports", [])

    aliases = {
        "CFB": Sport.NCAAF,
        "NCAA": Sport.NCAAF,
        "FOOTBALL": Sport.NFL,
        "BASKETBALL": Sport.NBA,
        "BASEBALL": Sport.MLB,
        "HOCKEY": Sport.NHL,
    }
    by_name = {member.value.casefold(): member for member in Sport}
    supported: list[tuple[str, Sport]] = []
    ignored: list[str] = []
    seen_odds_keys: set[str] = set()
    for raw_value in configured_values:
        sport_value = str(raw_value).strip()
        if not sport_value:
            continue
        sport = aliases.get(sport_value.upper()) or by_name.get(sport_value.casefold())
        if sport is None:
            ignored.append(sport_value)
            continue
        odds_key = _SPORT_TO_ODDS_API_KEY.get(sport)
        if not odds_key:
            ignored.append(sport_value)
            continue
        if odds_key in seen_odds_keys:
            continue
        seen_odds_keys.add(odds_key)
        supported.append((sport_value, sport))

    if ignored:
        logger.debug(
            "OddsAPI poll scope: ignoring %d Underdog-only/unsupported sports: %s",
            len(ignored),
            ", ".join(ignored),
        )
    return supported

# ── Module-level singletons ────────────────────────────────────────────────────
# Initialised inside post_init so they live in the bot's event loop.
_db: Database | None = None
_engine: AnalysisEngine | None = None
_season_checker: SeasonChecker | None = None

# Tracks which budget-warning thresholds have already been alerted this month
# per provider.  Reset implicitly when get_usage_tracker()._roll_month_if_needed()
# fires, or on bot restart (acceptable — at worst one duplicate alert per month).
_last_budget_alerted: dict[str, set[int]] = {}

# aiohttp AppRunner for the HTTP health-check server (started in post_init).
_health_runner: Optional[Any] = None


# ── HTTP health-check server ───────────────────────────────────────────────────

def _health_server_port() -> tuple[int, str]:
    """Return the health port and the configuration source used.

    The local bot workflow shares the workspace with the API and mockup
    artifacts, which own ports 8080 and 8081. Keep an explicit bot-only
    override for that environment, while continuing to honor deployment
    platforms' PORT contract.
    """
    raw_port = os.environ.get("BOT_HEALTH_PORT")
    source = "BOT_HEALTH_PORT"
    if not raw_port:
        raw_port = os.environ.get("PORT")
        source = "PORT"
    if not raw_port:
        return 8099, "local default"

    port = int(raw_port)
    if port <= 0:
        raise ValueError(f"{source} must be a positive port, got {raw_port!r}")
    return port, source


async def _start_health_server() -> None:
    """
    Start a minimal HTTP health-check server so uptime monitors can verify the
    bot process is alive.

    Listens on BOT_HEALTH_PORT when explicitly configured for the local bot
    workflow.  Otherwise it honors PORT (the deployment platform contract) and
    falls back to 8099 locally so it does not collide with the API or mockup
    artifacts.

    Routes:
        GET /          → 200 OK
        GET /health    → 200 OK
        GET /healthz   → 200 OK

    Uses aiohttp (already a dependency via stat providers) so it runs natively
    in the same event loop as the Telegram bot without blocking.
    """
    global _health_runner
    # Idempotency guard — only one health server per process lifetime.
    if _health_runner is not None:
        logger.debug("Health server already running — skipping duplicate start.")
        return
    try:
        import aiohttp.web as _web

        port, port_source = _health_server_port()

        async def _ok(_req: _web.Request) -> _web.Response:
            return _web.Response(text="OK", content_type="text/plain")

        _app = _web.Application()
        _app.router.add_get("/",        _ok)
        _app.router.add_get("/health",  _ok)
        _app.router.add_get("/healthz", _ok)

        _health_runner = _web.AppRunner(_app)
        await _health_runner.setup()
        site = _web.TCPSite(_health_runner, "0.0.0.0", port)
        await site.start()
        logger.info(
            "Health server listening on port %d (source=%s)",
            port,
            port_source,
        )
    except Exception as exc:  # noqa: BLE001
        # Non-fatal — the Telegram bot continues even if health server fails.
        logger.warning("Health server failed to start (non-fatal): %s", exc)


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
    # PrizePicks runs through the authorized PropLine companion cycle launched
    # by underdog_job.  Register it here so command reports show real fetch
    # health instead of treating the provider role as a health signal.
    from providers.propline import PropLinePrizePicksProvider
    _propline_enabled = PropLinePrizePicksProvider().is_available()
    _health_monitor.register("PrizePicks", disabled=not _propline_enabled)
    # OddsAPI is registered so quota headers (x-requests-remaining/used) recorded
    # by OddsApiCache._fetch() appear in /status and health logs.
    _health_monitor.register("OddsAPI")
    _health_monitor.register("Underdog", disabled=not config.UNDERDOG_ENABLED)
    logger.info(
        "Provider health monitor initialised "
        "(OddsAPI + Underdog + PrizePicks/PropLine=%s)",
        "enabled" if _propline_enabled else "disabled",
    )
    if config.discord_configured:
        logger.info(
            "Discord delivery configured (guild=%s, channel=%s)",
            config.DISCORD_GUILD_ID,
            config.DISCORD_CHANNEL_ID,
        )
        try:
            from discord_commands import start_discord_commands
            start_discord_commands()
            logger.info("Discord command interface started")
        except Exception as exc:
            logger.warning("Discord command interface failed to start (non-fatal): %s", exc)
    else:
        logger.info("Discord delivery not configured (required variables absent)")

    init_odds_cache(ttl_seconds=config.ODDS_API_CACHE_TTL)
    logger.info("Odds API shared cache initialised (TTL=%ds)", config.ODDS_API_CACHE_TTL)
    init_quota_pacer(
        quota_limit=config.ODDS_API_QUOTA_LIMIT,
        deadline=config.ODDS_API_DEADLINE_UTC,
        min_interval_seconds=config.ODDS_API_MIN_REQUEST_INTERVAL,
        max_interval_seconds=config.ODDS_API_MAX_REQUEST_INTERVAL,
    )

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
    registry.register(UnderdogConnector(
        # active_sports=None: accept every sport the Underdog API returns.
        # Underdog only lists currently-active props, so out-of-season sports
        # (NFL, NBA) simply don't appear — no explicit blocklist is needed.
        # This gives us MLB, WNBA, Soccer, Tennis, and any other live markets
        # without hardcoding sport IDs that may change or be added.
        enabled = config.UNDERDOG_ENABLED,
    ))

    init_market_engine(registry)
    init_odds_confirmation(_engine, _db)   # wire existing OddsAPI data into confirmation

    # Store db reference in bot_data for market engine jobs
    application.bot_data["db"] = _db

    # Register background jobs
    jq = application.job_queue
    if jq:
        # The OddsAPI poller has its own normalized sport scope and adaptive
        # quota pacing.  It never reads ACTIVE_SPORTS for provider selection.
        jq.run_repeating(
            _poll_odds_job,
            interval=config.ODDS_POLL_INTERVAL,
            first=10,
            name="odds_poller",
            job_kwargs={"max_instances": 1, "misfire_grace_time": 120},
        )
        # jq.run_repeating(_steam_check_job,    interval=config.STEAM_CHECK_INTERVAL,      first=15,  name="steam_checker")
        # Store DK/FD player-prop markets separately from the h2h/totals EV
        # poller. Only the enabled sportsbook books are retained.
        jq.run_repeating(
            _player_props_job,
            interval=config.PLAYER_PROP_POLL_INTERVAL,
            first=60,
            name="player_props_fetcher",
            job_kwargs={"max_instances": 1, "misfire_grace_time": 120},
        )
        # Legacy _prizepicks_job remains disabled.  The authorized PropLine
        # PrizePicks cycle runs as a guarded companion task inside underdog_job.
        # jq.run_repeating(connector_poll_job,  interval=config.CONNECTOR_POLL_INTERVAL,   first=20,  name="connector_poller")
        # jq.run_repeating(consensus_check_job, interval=config.CONSENSUS_CHECK_INTERVAL,  first=25,  name="consensus_checker")
        # jq.run_repeating(clv_check_job,       interval=config.CLV_CHECK_INTERVAL,        first=35,  name="clv_checker")
        # ─────────────────────────────────────────────────────────────────────
        jq.run_repeating(
            underdog_job,
            interval=config.UNDERDOG_POLL_INTERVAL,
            first=45,
            name="underdog_monitor",
            # Explicit single-instance protection: if a scan is still running when
            # the next trigger fires, APScheduler skips the new trigger rather than
            # spawning a second concurrent scan.  misfire_grace_time=60 lets a
            # trigger that fires up to 60 s late still execute (avoids missed scans
            # during brief process pauses).
            job_kwargs={"max_instances": 1, "misfire_grace_time": 60},
        )
        # Full-Pool Rescan Rotation is the sole bounded background rotation.
        # It consumes the canonical active (player, stat, line) universe.
        from market_engine import _full_pool_rescan_job
        jq.run_repeating(
            _full_pool_rescan_job,
            interval=config.FPR_INTERVAL,
            first=150,
            name="full_pool_rescan",
            job_kwargs={"max_instances": 1, "misfire_grace_time": 120},
        )
        # CLV seed job — every 15 minutes (creates AlertCLVSeed entries for alerts)
        jq.run_repeating(_clv_seed_job,        interval=900,                                first=120, name="clv_seeder")
        # CLV harvest job — every hour (processes seeds after game_time passes)
        jq.run_repeating(_clv_harvest_job,          interval=3600,  first=300,  name="clv_harvester")
        # Opportunity grader — hourly (grades completed prop opportunities).
        # A bounded, rotating worker must never overlap itself while provider
        # lookups are in flight.
        jq.run_repeating(
            _grade_opportunities_job,
            interval=3600,
            # Run once shortly after startup, then continue on the hourly
            # cadence.  A restart should not defer grading for a full hour.
            first=60,
            name="opportunity_grader",
            job_kwargs={"max_instances": 1, "misfire_grace_time": 120},
        )
        from player_history_job import player_history_collector_job
        jq.run_repeating(player_history_collector_job, interval=240, first=60, name="player_history_collector", job_kwargs={"max_instances": 1, "misfire_grace_time": 120})
        # API budget check — every 15 minutes
        jq.run_repeating(_budget_check_job,    interval=900,                                first=900, name="budget_checker")
        # PropLineHistory prune — once per day (keeps last 14 days)
        jq.run_repeating(_prune_prop_history_job, interval=86400, first=600, name="prop_history_pruner")
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
            "Jobs scheduled — underdog: every %ds, FPR: every %ds, "
            "season_check: every %ds, heartbeat: every 60s "
            "[PrizePicks/PropLine companion cycle enabled=%s; DK/FD delivery disabled]",
            config.UNDERDOG_POLL_INTERVAL,
            config.FPR_INTERVAL,
            config.SEASON_CHECK_INTERVAL,
            _propline_enabled,
        )
    else:
        logger.warning("JobQueue not available — background jobs disabled.")

    logger.info("Bot initialised and ready.")

    # ── HTTP health-check server ───────────────────────────────────────────────
    # Start in the background so uptime monitors can ping the process.
    # Non-fatal: the Telegram bot starts even if aiohttp is unavailable.
    asyncio.create_task(_start_health_server())

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

    Crash format (unexpected_exit / crash_detected):
        ⚠️ Sharp Money Bot Restarted
        Reason: [actual detected cause]
        Crash Details: [exc_type: exc_msg / Module / Function]
        Last Active Task: …
        Last Heartbeat: HH:MM UTC
        Recovery Status: Resumed Monitoring ✅

    Normal start format:
        ✅ Sharp Money Bot Online
        Status: Underdog monitoring active
        Startup: #N
    """
    import html as _h
    if not chat_ids:
        return

    startup_reason = ht.startup_reason() if ht else "unknown"
    restart_num    = ht.restart_count()  if ht else 0
    last_hb        = ht.last_heartbeat() if ht else None
    last_job       = (
        (ht.last_job_started_name() or "Underdog Market Monitor")
        if ht else "Underdog Market Monitor"
    )

    if startup_reason in ("unexpected_exit", "crash_detected"):
        # ── Determine the actual crash cause ─────────────────────────────────
        cause_label  = ht.crash_cause_label() if ht else "Unknown Exit"
        crash_detail = ht.last_crash_detail() if ht else None

        # Format the last heartbeat timestamp
        hb_str = "unknown"
        if last_hb:
            try:
                from datetime import datetime as _dt
                hb_dt  = _dt.fromisoformat(last_hb.replace("Z", "+00:00"))
                hb_str = hb_dt.strftime("%H:%M UTC")
            except Exception:
                hb_str = str(last_hb)[:16]

        crash_id_num = ht.last_crash_id() if ht else None
        crash_id_str = f"#{crash_id_num}" if crash_id_num else "—"

        parts: list[str] = [
            "🤖 <b>Sharp Money Bot Restarted</b>",
            "",
            "<b>Status:</b>  Monitoring resumed ✅",
        ]

        # ── Crash details block (only when captured) ──────────────────────────
        if crash_detail:
            exc_type = crash_detail.get("exc_type", "")
            exc_msg  = crash_detail.get("exc_msg",  "")[:120]
            mod      = crash_detail.get("active_module", "")
            fn       = crash_detail.get("active_function", "")
            job      = crash_detail.get("active_job", "") or last_job
            uptime   = crash_detail.get("uptime_secs")

            parts += [
                "",
                "<b>Crash Details:</b>",
                f"  <code>{_h.escape(exc_type)}: {_h.escape(exc_msg)}</code>",
            ]
            if mod:
                import os as _os
                parts.append(f"  Module:    <code>{_h.escape(_os.path.basename(mod))}</code>")
            if fn:
                parts.append(f"  Function:  <code>{_h.escape(fn)}</code>")
            if job:
                parts.append(f"  Active Job: {_h.escape(str(job))}")
            mem = crash_detail.get("memory_mb")
            if mem is not None:
                parts.append(f"  Memory at crash: {mem} MB")
            if uptime is not None:
                mins = int(uptime) // 60
                secs = int(uptime) % 60
                parts.append(f"  Uptime at crash: {mins}m {secs}s")

        parts += [
            "",
            f"<b>Last Active Task:</b>  {_h.escape(last_job)}",
            f"<b>Last Heartbeat:</b>  {_h.escape(hb_str)}",
            f"Startup #{restart_num}",
        ]
    elif startup_reason == "first_start":
        parts = [
            "🤖 <b>Sharp Money Bot Online</b>",
            "",
            "<b>Status:</b>  Monitoring active ✅",
            f"Startup #{restart_num}",
            "",
            "<i>Ready. Props will be analysed as they are detected.</i>",
        ]
    else:
        # clean restart / manual restart — simple notice, no noise
        parts = [
            "🤖 <b>Sharp Money Bot Restarted</b>",
            "",
            "<b>Status:</b>  Monitoring resumed ✅",
            f"Startup #{restart_num}",
            "",
            "<i>Props will be analysed as they are detected.</i>",
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
    try:
        from discord_commands import stop_discord_commands
        await stop_discord_commands()
    except Exception as exc:
        logger.warning("Discord command interface shutdown failed (non-fatal): %s", exc)
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
    # Stop the health-check server if it started
    if _health_runner is not None:
        try:
            await _health_runner.cleanup()
        except Exception:
            pass
    logger.info("Shutdown complete. Goodbye.")


# ── Heartbeat job ─────────────────────────────────────────────────────────────

_GRADE_BATCH_SIZE = 250
_GRADE_FETCH_CONCURRENCY = 12
_GRADE_FETCH_TIMEOUT_SECONDS = 20


async def _grade_opportunities_job(context) -> None:
    """
    Every hour: grade completed prop opportunities from stored game results.

    Finds PENDING rows in prop_opportunity_log whose game_time passed at least
    4 hours ago, looks up the actual player stat in player_game_results, and
    records HIT / MISS / PUSH from the OVER perspective:
      HIT  → actual_value > line_value
      MISS → actual_value < line_value
      PUSH → actual_value == line_value
    """
    db: Optional[Database] = context.bot_data.get("db")
    if not db:
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_fail("_grade_opportunities_job", "db_not_ready")
        return
    _ht_start = get_health_tracker()
    if _ht_start:
        _ht_start.record_job_started("_grade_opportunities_job")
    try:
        _cursor = (
            _ht_start.get_opportunity_grader_cursor()
            if _ht_start and hasattr(_ht_start, "get_opportunity_grader_cursor")
            else 0
        )
        try:
            pending = await db.get_pending_opportunities(
                cutoff_hours=4,
                after_id=_cursor,
                limit=_GRADE_BATCH_SIZE,
            )
        except TypeError:
            # Keep lightweight test doubles and older database adapters
            # compatible while production uses the rotating query.
            pending = await db.get_pending_opportunities(cutoff_hours=4)
        # Wrap once the cursor reaches the end of the pending set.
        if not pending and _cursor:
            try:
                pending = await db.get_pending_opportunities(
                    cutoff_hours=4,
                    after_id=None,
                    limit=_GRADE_BATCH_SIZE,
                )
            except TypeError:
                pending = await db.get_pending_opportunities(cutoff_hours=4)
        if not pending:
            if _ht_start:
                _ht_start.record_job_run("_grade_opportunities_job")
            return

        # ── Proactively fetch player results so grading works without /backfill ──
        # The grader previously only looked up pre-existing PlayerResult rows.
        # Props removed from Underdog before the game finished would never get
        # a result row from scanning, leaving them PENDING forever.
        # This mirrors the /backfill fetch pattern so auto-grading is self-contained.
        try:
            from providers.player_stats import PlayerStatsProvider as _GradeStatProvider
            _grade_stat_prov = _GradeStatProvider()
            _grade_fetched: set = set()
            _grade_targets = []
            for _gopp in pending:
                _gkey = (_gopp.player_name, _gopp.sport, (_gopp.stat_type or "").lower().strip())
                if _gkey in _grade_fetched:
                    continue
                _grade_fetched.add(_gkey)
                _grade_targets.append(_gopp)

            _grade_sem = asyncio.Semaphore(_GRADE_FETCH_CONCURRENCY)

            async def _fetch_grade_results(_gopp):
                async with _grade_sem:
                    try:
                        return await asyncio.wait_for(
                            _grade_stat_prov.fetch_results(
                                _gopp.player_name, _gopp.sport, _gopp.stat_type
                            ),
                            timeout=_GRADE_FETCH_TIMEOUT_SECONDS,
                        )
                    except Exception as _gfetch_exc:
                        logger.debug(
                            "_grade_opportunities_job: fetch_results failed for %s/%s: %s",
                            _gopp.player_name, _gopp.stat_type, _gfetch_exc,
                        )
                        return []

            _grade_batches = await asyncio.gather(
                *(_fetch_grade_results(_gopp) for _gopp in _grade_targets)
            )
            for _graw in _grade_batches:
                for _gr in _graw:
                    await db.upsert_player_result(_gr)
        except Exception as _prov_exc:
            logger.warning("_grade_opportunities_job: stat provider init failed: %s", _prov_exc)

        graded   = 0
        no_data  = 0
        for opp in pending:
            if not opp.game_time:
                continue
            game_date = opp.game_time.strftime("%Y-%m-%d")
            _result_lookup_kwargs = {}
            if getattr(opp, "game_id", ""):
                _result_lookup_kwargs["game_id"] = opp.game_id
            if getattr(opp, "provider", None):
                _result_lookup_kwargs["provider"] = opp.provider
            result_row = await db.get_game_result_for_grading(
                opp.player_name, opp.sport, opp.stat_type, game_date,
                **_result_lookup_kwargs,
            )
            if result_row is None:
                no_data += 1
                logger.debug(
                    "_grade_opportunities_job: no result for %s | %s | %s | %s",
                    opp.player_name, opp.sport, opp.stat_type, game_date,
                )
                continue
            actual = result_row.actual_value
            line   = opp.line_value

            # Direction-aware grading: HIT means the bet won.
            #   OVER recommendation → won if actual > line
            #   UNDER recommendation → won if actual < line
            #   PUSH when actual == line (within tolerance)
            _push_tol = 0.01
            if abs(actual - line) < _push_tol:
                outcome = "PUSH"
            elif (opp.recommendation or "OVER").upper() == "UNDER":
                outcome = "HIT" if actual < line else "MISS"
            else:
                # OVER (default) or unknown direction
                outcome = "HIT" if actual > line else "MISS"

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
        if graded or no_data:
            logger.info(
                "_grade_opportunities_job: graded=%d opportunities (no_data=%d)",
                graded, no_data,
            )
        _ht_g = get_health_tracker()
        if _ht_g:
            if hasattr(_ht_g, "set_opportunity_grader_cursor") and pending:
                _ht_g.set_opportunity_grader_cursor(
                    max(int(getattr(_opp, "id", 0) or 0) for _opp in pending)
                )
            _ht_g.record_job_run("_grade_opportunities_job")
    except Exception:
        logger.exception("_grade_opportunities_job: unexpected error")
        _ht_g = get_health_tracker()
        if _ht_g:
            _ht_g.record_job_fail("_grade_opportunities_job", "unexpected error")


async def _heartbeat_job(context) -> None:
    """Run every 60 s for health bookkeeping and the PropLine cadence handoff."""
    ht = get_health_tracker()
    try:
        if ht is not None:
            ht.record_job_started("heartbeat_job")
            ht.update_heartbeat()
            ht.record_job_run("heartbeat_job")
        # This is a cadence handoff, not a second APScheduler job.  It keeps
        # the authorized PrizePicks/PropLine feed at <=120s even while the
        # heavy Underdog scan is still running.
        _start_prizepicks_cycle(context)
    except Exception as _exc:
        logger.exception("_heartbeat_job: error: %s", _exc)
        if ht is not None:
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
    pacer = get_quota_pacer()

    # ── 1. Fetch odds for every active sport ──────────────────────────────────
    all_lines: list[OddsLine] = []
    allowed_books = set()
    if config.SPORTSBOOK_CONFIRMATION_ENABLED:
        if config.DRAFTKINGS_ENABLED:
            allowed_books.add("draftkings")
        if config.FANDUEL_ENABLED:
            allowed_books.add("fanduel")
    if not getattr(config, "ODDS_API_ENABLED", True):
        logger.debug("_poll_odds_job: OddsAPI disabled by configuration")
        return

    configured_sports = _configured_odds_api_sports()
    for sport_str, sport in configured_sports:
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
        if pacer is not None and not await pacer.wait_for_slot():
            logger.warning("_poll_odds_job: quota pacer blocked the remaining cycle")
            break
        lines = await _engine.fetch_live_odds(sport, markets="h2h,totals")
        allowed_book_names = {book.replace(" ", "") for book in allowed_books}
        if not config.SPORTSBOOK_CONFIRMATION_ENABLED:
            lines = [
                line for line in lines
                if line.sportsbook.lower().replace(" ", "") not in {"draftkings", "fanduel"}
            ]
        if allowed_book_names:
            all_lines.extend(
                line for line in lines
                if line.sportsbook.lower().replace(" ", "") in allowed_book_names
            )
        else:
            # Keep unrelated sportsbook lines available to the existing EV
            # path, but never retain DK/FD lines while their confirmation role
            # is disabled.
            all_lines.extend(lines)

    if pacer is not None:
        logger.info("OddsAPI quota pacing: %s", pacer.telemetry().summary)

    if not all_lines:
        logger.debug("_poll_odds_job: no lines returned from API")
        return

    # ── 1b. Early scope filter — drop lines that can never be delivered ────────
    # Only Tier 1 sports (all supported sports except MLB/NBA/NFL) pass the
    # AlertScopeFilter EV pipeline.  Filtering here avoids writing out-of-scope
    # rows to odds_records and running EV analysis on data that will always be
    # blocked at delivery.
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

    for sport_str, sport in _configured_odds_api_sports():

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

    _ht_start = get_health_tracker()
    if _ht_start:
        _ht_start.record_job_started("_clv_harvest_job")

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

            # Seeds without bet_odds cannot produce CLV% — expire immediately.
            # Underdog seeds seeded via seed_clv_from_ud_confirmation() DO have
            # bet_odds (from OddsAPI avg_odds), so they proceed to closing-line lookup.
            if not seed.bet_odds:
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
                        clv_proxy              = clv_result.clv_proxy,
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

    Retry policy: up to 3 attempts with exponential backoff (2 s, 4 s) to
    handle any residual SQLite lock contention not already absorbed by WAL
    mode and the 30-second busy timeout configured on the engine.
    """
    if _db is None:
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_clv_seed_job")
        return

    _ht_start = get_health_tracker()
    if _ht_start:
        _ht_start.record_job_started("_clv_seed_job")

    max_attempts = 3
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
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
            return   # success — exit retry loop
        except Exception as exc:
            last_exc = exc
            import sqlalchemy.exc as _sa_exc
            is_lock = isinstance(exc, _sa_exc.OperationalError) and "locked" in str(exc).lower()
            if is_lock and attempt < max_attempts:
                wait = 2 ** attempt   # 2 s, 4 s
                logger.warning(
                    "_clv_seed_job: database locked (attempt %d/%d) — retrying in %ds",
                    attempt, max_attempts, wait,
                )
                await asyncio.sleep(wait)
                continue
            # Non-lock error or final attempt — record failure
            break

    logger.exception("_clv_seed_job: error during CLV seeding: %s", last_exc)
    _ht = get_health_tracker()
    if _ht:
        _ht.record_job_fail("_clv_seed_job", str(last_exc))


# ── API budget check job ───────────────────────────────────────────────────────
async def _prune_prop_history_job(context) -> None:
    """Delete PropLineHistory rows older than 14 days to control DB growth."""
    db = context.bot_data.get("db")
    if not db:
        return
    _ht_start = get_health_tracker()
    if _ht_start:
        _ht_start.record_job_started("_prune_prop_history_job")
    try:
        deleted = await db.prune_prop_line_history(keep_days=14)
        if deleted:
            logger.info("_prune_prop_history_job: pruned %d old rows", deleted)
        ht = get_health_tracker()
        if ht:
            ht.record_job_run("_prune_prop_history_job")
    except Exception as exc:
        logger.exception("_prune_prop_history_job: %s", exc)
        ht = get_health_tracker()
        if ht:
            ht.record_job_fail("_prune_prop_history_job", str(exc))
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

    _ht_start = get_health_tracker()
    if _ht_start:
        _ht_start.record_job_started("_budget_check_job")
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
    _ht_start = get_health_tracker()
    if _ht_start:
        _ht_start.record_job_started("_season_check_job")
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
    if not config.SPORTSBOOK_CONFIRMATION_ENABLED:
        logger.info(
            "_player_props_job: sportsbook confirmation disabled; "
            "skipping DraftKings/FanDuel player props"
        )
        return

    now = datetime.utcnow()
    total_saved = 0
    allowed_books = set()
    if config.DRAFTKINGS_ENABLED:
        allowed_books.add("draftkings")
    if config.FANDUEL_ENABLED:
        allowed_books.add("fanduel")
    if not allowed_books:
        logger.debug("_player_props_job: no sportsbook player-prop books enabled")
        return
    allowed_book_names = {book.replace(" ", "") for book in allowed_books}

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
            if pl.sportsbook.lower().replace(" ", "") not in allowed_book_names:
                continue
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
    """Compatibility entry point for the retired legacy PrizePicks job.

    PrizePicks must use the provider-independent PropLine companion pipeline.
    Keeping this callback as a delegating shim prevents an old scheduler
    registration or external callback from reviving the former sportsbook-edge
    path, which had different scope, identity, and delivery rules.
    """
    logger.warning(
        "_prizepicks_job is a compatibility shim; delegating to the "
        "independent PrizePicks pipeline"
    )
    await _run_prizepicks_independent_cycle(context)


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


def _install_excepthook() -> None:
    """
    Install a ``sys.excepthook`` that captures unhandled exceptions to the
    health sidecar before the process exits.

    Fires for any unhandled exception that propagates to the top level,
    including ``run_polling()`` raising.  SIGKILL and ``os._exit()`` skip
    this hook entirely — those are handled by the 'unexpected_exit' path
    (missing shutdown record) in HealthTracker.

    The hook:
      1. Extracts exc type / message / full traceback / innermost frame.
      2. Reads the last active job from the health sidecar.
      3. Persists all of it via HealthTracker.record_crash_detail().
      4. Writes 'unexpected_exit' shutdown reason (so atexit stays a no-op).
      5. Calls the original sys.excepthook so the traceback is still printed.

    Never raises — any internal error is silently swallowed so we never
    interfere with the original exception propagation.
    """
    import sys
    import traceback as _tb

    _orig_hook = sys.excepthook

    def _crash_hook(exc_type, exc_value, exc_tb) -> None:  # type: ignore[override]
        try:
            ht = get_health_tracker()
            if ht is not None:
                # Full formatted traceback
                tb_lines   = _tb.format_tb(exc_tb)
                tb_text    = "".join(tb_lines)

                # Exception identity
                exc_type_name = getattr(exc_type, "__name__", str(exc_type))
                exc_msg       = str(exc_value)

                # Innermost frame — where the exception was raised
                frames        = _tb.extract_tb(exc_tb)
                last_frame    = frames[-1] if frames else None
                active_module = last_frame.filename if last_frame else ""
                active_fn     = last_frame.name     if last_frame else ""

                # Which job was active when the crash happened
                active_job = ht.last_job_started_name() or ""

                ht.record_crash_detail(
                    exc_type_name   = exc_type_name,
                    exc_msg         = exc_msg,
                    tb_text         = tb_text,
                    active_job      = active_job,
                    active_module   = active_module,
                    active_function = active_fn,
                )
                # Mark as unexpected exit so the atexit fallback stays a no-op
                ht.record_shutdown_if_not_set("unexpected_exit")
        except Exception:
            pass   # never let the crash hook itself crash
        finally:
            # Always delegate to the original hook — traceback must still print
            _orig_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _crash_hook


def main() -> None:
    # Validate configuration before building the app
    try:
        config.validate()
    except ValueError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    # Install global exception hook first — captures crash details before atexit.
    _install_excepthook()
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
    app.add_handler(CommandHandler("reconcile", cmd_reconcile))
    app.add_handler(CommandHandler("grade",     cmd_grade))
    app.add_handler(CommandHandler("grades",    cmd_grade))
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    # Keep the command name in one runtime constant so legacy command-audit
    # tooling that enumerates literal registrations remains compatible.
    _system_command = "system"
    app.add_handler(CommandHandler(_system_command, cmd_system))
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

    app.add_handler(CommandHandler("tracking",     cmd_tracking))
    # ── Framework v3.0 commands ───────────────────────────────────────────────
    app.add_handler(CommandHandler("analyst",    cmd_analyst))
    app.add_handler(CommandHandler("rollups",    cmd_rollups))
    app.add_handler(CommandHandler("blocks",     cmd_blocks))
    app.add_handler(CommandHandler("block",      cmd_block))
    app.add_handler(CommandHandler("refinement", cmd_refinement))
    app.add_handler(CommandHandler("funnel",     cmd_funnel))
    app.add_handler(CommandHandler("backfill",   cmd_backfill))
    # ── P11/P12 commands ─────────────────────────────────────────────────────
    app.add_handler(CommandHandler("player",     cmd_player))
    app.add_handler(CommandHandler("slipstats",  cmd_slipstats))
    app.add_error_handler(error_handler)

    logger.info("Starting polling — press Ctrl+C to stop.")

    # run_polling() owns the event loop; do NOT call asyncio.run() around it.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
