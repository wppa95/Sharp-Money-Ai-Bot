"""
main.py — Sharp Money +EV Detection Bot startup.

Entry point: python bot/main.py

python-telegram-bot v20+ manages its own event loop via run_polling().
Async setup (DB init) is done through the Application's post_init /
post_shutdown lifecycle hooks — do NOT wrap run_polling() in asyncio.run().
"""

from __future__ import annotations

import asyncio
from functools import wraps
import hashlib
import inspect
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler

# Ensure the bot/ directory is on the path for sibling imports
sys.path.insert(0, os.path.dirname(__file__))

from config import config
from database import Database, OddsRecord, get_sqlite_backpressure_metrics
from engine import AnalysisEngine
from engine.season_check import SeasonChecker
from engine.analysis import _SPORT_TO_ODDS_API_KEY
from engine.analysis import (
    _SPORT_PLAYER_PROP_MARKETS,
    _ODDS_MARKET_TO_PROP_STAT,
    SPORTSBOOK_PLAYER_PROP_COVERAGE,
)
from engine.player_props_pipeline import persist_canonical_player_props
from alert_scope_filter import is_ev_line_in_scope
from logging_utils import install_credential_redaction
from models import (
    MarketType,
    OddsLine,
    OddsMovement,
    Sport,
)
from alerts import AlertDelivery, broadcast_alert
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
    cmd_market,
    cmd_performance,
    cmd_backtest,
    cmd_testalert,
    cmd_providers,
    cmd_stats,
    cmd_config,
    cmd_calibration,
    cmd_clv,
    cmd_health,

    cmd_tracking,
    cmd_analyst,
    cmd_rollups,
    cmd_learning,
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
    reset_monitoring_runtime_state,
    shutdown_db_persistence_worker,
    underdog_job,
    _shared_monitoring_cycle,
    _full_pool_rescan_job,
)
from player_history_job import player_history_collector_job
from providers import init_health_monitor
from providers.odds_cache import init_odds_cache
from providers.game_results import OddsApiResultsProvider
from providers.usage_tracker import init_usage_tracker, get_usage_tracker
from providers.quota_pacing import init_quota_pacer, get_quota_pacer
from engine.health import init_health_tracker, get_health_tracker

# ── Logging ───────────────────────────────────────────────────────────────────

_stdout_handler = logging.StreamHandler(sys.stdout)
_log_redaction_filter = install_credential_redaction(
    secrets=(config.TELEGRAM_BOT_TOKEN,)
)
_stdout_handler.addFilter(_log_redaction_filter)
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    handlers=[_stdout_handler],
)
# Some environments install a dedicated handler on HTTP client loggers before
# application startup, so protect those records at the logger boundary too.
for _logger_name in ("httpx", "httpcore", "telegram.request"):
    _http_logger = logging.getLogger(_logger_name)
    _http_logger.addFilter(_log_redaction_filter)
    # HTTPX INFO request lines contain the Telegram bot credential in the URL.
    # Keep warnings/errors available, but do not capture credential-bearing
    # request URLs in workflow logs.
    _http_logger.setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── Telegram command surface ─────────────────────────────────────────────────
# Keep the production command map in one place.  This is the source used by
# the running Application, not just documentation or a test-only inventory.
# ``learning`` is retained as the existing legacy alias for ``rollups``.
TELEGRAM_COMMAND_HANDLERS = (
    ("picks", cmd_picks),
    ("slip", cmd_slip),
    ("dashboard", cmd_dashboard),
    ("alerts", cmd_alerts),
    ("funnel", cmd_funnel),
    ("reconcile", cmd_reconcile),
    ("grade", cmd_grade),
    ("grades", cmd_grade),
    ("player", cmd_player),
    ("slipstats", cmd_slipstats),
    ("analyze", cmd_analyze),
    ("market", cmd_market),
    ("steam", cmd_steam),
    ("ev", cmd_ev),
    ("performance", cmd_performance),
    ("backtest", cmd_backtest),
    ("stats", cmd_stats),
    ("calibration", cmd_calibration),
    ("clv", cmd_clv),
    ("tracking", cmd_tracking),
    ("analyst", cmd_analyst),
    ("rollups", cmd_rollups),
    ("refinement", cmd_refinement),
    ("start", cmd_start),
    ("status", cmd_status),
    ("system", cmd_system),
    ("health", cmd_health),
    ("providers", cmd_providers),
    ("config", cmd_config),
    ("blocks", cmd_blocks),
    ("block", cmd_block),
    ("backfill", cmd_backfill),
    ("testalert", cmd_testalert),
    ("help", cmd_help),
    ("learning", cmd_learning),
)

# Metadata is seeded once into SQLite and then read from there for help,
# Telegram's command menu, and runtime enable/disable checks. Handler code
# remains the tested implementation behind each durable command identity.
TELEGRAM_COMMAND_CATALOG = [
    {"command": "picks", "description": "Current directional player prop picks", "category": "Player Props", "usage": "5 · MLB · TENNIS 5", "sort_order": 10},
    {"command": "slip", "description": "Build a prop slip from top picks", "category": "Player Props", "usage": "[N]", "sort_order": 20},
    {"command": "dashboard", "description": "Player prop activity and provider status", "category": "Player Props", "sort_order": 30},
    {"command": "alerts", "description": "Recent actionable pick alert history", "category": "Player Props", "sort_order": 40},
    {"command": "funnel", "description": "Monitoring and delivery pipeline by provider", "category": "Player Props", "usage": "[hours]", "sort_order": 50},
    {"command": "reconcile", "description": "Reconcile source volume with Telegram delivery", "category": "Player Props", "usage": "[hours]", "sort_order": 60},
    {"command": "grade", "description": "Win/loss breakdown", "category": "Player Props", "sort_order": 70},
    {"command": "grades", "description": "Win/loss breakdown", "category": "Player Props", "alias_of": "grade", "sort_order": 71},
    {"command": "player", "description": "Tracked history and hit rate for one player", "category": "Player Props", "usage": "NAME", "sort_order": 80},
    {"command": "slipstats", "description": "Pick and slip performance", "category": "Player Props", "sort_order": 90},
    {"command": "analyze", "description": "Analyze one line", "category": "Market Analysis", "usage": "ARGS", "sort_order": 100},
    {"command": "market", "description": "Line movement and validation summary", "category": "Market Analysis", "sort_order": 110},
    {"command": "steam", "description": "Recent steam moves", "category": "Market Analysis", "sort_order": 120},
    {"command": "ev", "description": "Recent EV opportunities", "category": "Market Analysis", "sort_order": 130},
    {"command": "performance", "description": "Pick performance by sport", "category": "Performance", "sort_order": 200},
    {"command": "backtest", "description": "Replay resolved picks through the model", "category": "Performance", "sort_order": 210},
    {"command": "stats", "description": "Pick generation and outcome summary", "category": "Performance", "sort_order": 220},
    {"command": "calibration", "description": "Model calibration against outcomes", "category": "Performance", "sort_order": 230},
    {"command": "clv", "description": "Closing-line value performance", "category": "Performance", "sort_order": 235, "enabled": True},
    {"command": "tracking", "description": "Directional pick tracking summary", "category": "Performance", "sort_order": 240},
    {"command": "analyst", "description": "Latest analyst assessment", "category": "Performance", "sort_order": 250},
    {"command": "rollups", "description": "Learning rollups", "category": "Performance", "sort_order": 260},
    {"command": "learning", "description": "Learning rollups", "category": "Performance", "alias_of": "rollups", "sort_order": 261},
    {"command": "refinement", "description": "Refinement rules and triggers", "category": "Performance", "sort_order": 270},
    {"command": "start", "description": "Welcome message and chat ID", "category": "System", "sort_order": 300},
    {"command": "status", "description": "Uptime, provider health, and database counts", "category": "System", "sort_order": 310},
    {"command": "system", "description": "Concise runtime system status", "category": "System", "sort_order": 320},
    {"command": "health", "description": "Background job health", "category": "System", "sort_order": 330},
    {"command": "providers", "description": "Player prop provider status", "category": "System", "sort_order": 340},
    {"command": "config", "description": "Active configuration and alert scope", "category": "System", "sort_order": 350},
    {"command": "blocks", "description": "Active reliability blocks", "category": "System", "sort_order": 360},
    {"command": "block", "description": "Add or remove a reliability block", "category": "System", "usage": "ARGS", "sort_order": 370},
    {"command": "backfill", "description": "Grade eligible pending props", "category": "System", "sort_order": 380},
    {"command": "testalert", "description": "Send a mock alert to verify delivery", "category": "System", "sort_order": 400},
    {"command": "help", "description": "Show the database command catalog", "category": "System", "sort_order": 410},
]


def _telegram_bot_commands(rows) -> list[BotCommand]:
    """Convert catalog rows/definitions into Telegram menu commands."""
    commands: list[BotCommand] = []
    for row in rows:
        if isinstance(row, dict):
            command = row.get("command")
            description = row.get("description")
        else:
            command = getattr(row, "command", None)
            description = getattr(row, "description", None)
        command = str(command or "").strip().lstrip("/")
        if not command or command.lower() in {"propdata", "pp_import"}:
            continue
        commands.append(
            BotCommand(
                command,
                str(description or "Sharp Money Bot command")[:256],
            )
        )
    return commands


async def sync_telegram_command_catalog(
    application: Application,
    db: Database,
) -> tuple[list, int]:
    """Seed and publish the durable command catalog without blocking startup.

    The database controls the normal menu population, including operator
    enable/disable changes.  If the catalog read is unavailable during boot,
    publish the static production definitions as a compatibility fallback so
    a transient SQLite lock cannot prevent the bot from starting with a menu.
    """
    try:
        seeded = await db.ensure_telegram_commands(TELEGRAM_COMMAND_CATALOG)
        # These legacy handlers are intentionally not registered anymore, but
        # older databases may still contain their catalog rows. Disable them
        # before publishing Telegram's menu so retired provider surfaces do
        # not reappear after restart.
        await db.disable_telegram_commands(("pp_import", "propdata"))
        command_rows = await db.get_telegram_commands(enabled_only=True)
    except Exception as exc:
        logger.warning(
            "Telegram command catalog unavailable during startup: %s",
            exc,
        )
        # The fallback is deliberately static and handler-backed; it is only
        # used when the durable catalog cannot be read, never for an empty
        # enabled catalog (which is an intentional operator configuration).
        try:
            await application.bot.set_my_commands(
                _telegram_bot_commands(TELEGRAM_COMMAND_CATALOG)
            )
        except Exception as menu_exc:
            # A Telegram transport outage must not block polling startup either.
            logger.warning(
                "Telegram command menu fallback unavailable during startup: %s",
                menu_exc,
            )
        return [], 0
    try:
        await application.bot.set_my_commands(_telegram_bot_commands(command_rows))
    except Exception as menu_exc:
        # A Telegram transport outage must not block polling startup. Do not
        # retry with static definitions here: the database read succeeded, so
        # the operator's disabled commands must remain excluded.
        logger.warning(
            "Telegram command menu unavailable during startup: %s",
            menu_exc,
        )
    logger.info(
        "Telegram command catalog loaded from database: %d commands (%d seeded)",
        len(command_rows),
        seeded,
    )
    return command_rows, seeded


def register_command_handlers(application: Application) -> Application:
    """Attach the complete production command surface to *application*.

    Registration is deliberately centralized so the same map can be verified
    in tests and used by the actual polling Application.  Callback behavior,
    authorization, argument parsing, and database semantics remain in
    ``commands.py``.
    """
    for command, callback in TELEGRAM_COMMAND_HANDLERS:
        # The durable catalog controls the Telegram menu/help surface. Keep
        # the callback identity stable here so aliases intentionally share the
        # exact same production implementation and authorization path.
        application.add_handler(CommandHandler(command, callback))
    application.add_error_handler(error_handler)
    return application


def build_application() -> Application:
    """Build a polling Application with every command and the shared error path."""
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    return register_command_handlers(application)


class _ScheduledExecutionCoordinator:
    """Run named jobs serially without dropping scheduler triggers.

    JobQueue timers may fire again while an async callback is still running.
    Every trigger gets its own task and waits on the named job lock. This is
    deliberately queueing rather than coalescing: one completed callback
    represents one completed scheduled execution.
    """

    def __init__(self) -> None:
        self._running: set[str] = set()
        self._active_counts: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self._exclusive_locks: dict[str, asyncio.Lock] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._exclusive_waits = 0
        self._exclusive_wait_seconds = 0.0

    async def _execute(self, name: str, callback, context, *args, **kwargs):
        # Track the scheduler-facing name, not only callback-internal aliases.
        # This keeps /health aligned with the jobs the scheduler actually runs.
        try:
            from engine.health import get_health_tracker
            health = get_health_tracker()
        except Exception:
            health = None
        if health is not None:
            health.record_job_started(name)
        try:
            result = await callback(context, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if health is not None:
                health.record_job_fail(name, str(error))
            logger.error(
                "scheduled_execution: task failed (%s): %s",
                name,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            if health is not None:
                health.record_job_run(name)
            return result

    async def _run_guarded(self, name: str, callback, context, *args, **kwargs):
        self._active_counts[name] = self._active_counts.get(name, 0) + 1
        self._running.add(name)
        try:
            return await self._execute(name, callback, context, *args, **kwargs)
        finally:
            remaining = self._active_counts.get(name, 1) - 1
            if remaining > 0:
                self._active_counts[name] = remaining
            else:
                self._active_counts.pop(name, None)
                self._running.discard(name)

    async def _run_reserved(
        self,
        name: str,
        callback,
        context,
        *args,
        exclusive_group: str | None = None,
        **kwargs,
    ):
        """Execute a slot reserved atomically by ``launch``."""
        try:
            job_lock = self._job_locks.setdefault(name, asyncio.Lock())
            job_wait_started = time.monotonic()
            job_was_locked = job_lock.locked()
            async with job_lock:
                if job_was_locked:
                    self._exclusive_waits += 1
                    self._exclusive_wait_seconds += (
                        time.monotonic() - job_wait_started
                    )
                if exclusive_group:
                    lock = self._exclusive_locks.setdefault(
                        exclusive_group,
                        asyncio.Lock(),
                    )
                    wait_started = time.monotonic()
                    was_locked = lock.locked()
                    async with lock:
                        if was_locked:
                            self._exclusive_waits += 1
                            self._exclusive_wait_seconds += (
                                time.monotonic() - wait_started
                            )
                        return await self._execute(
                            name,
                            callback,
                            context,
                            *args,
                            **kwargs,
                        )
                return await self._execute(name, callback, context, *args, **kwargs)
        finally:
            remaining = self._active_counts.get(name, 1) - 1
            if remaining > 0:
                self._active_counts[name] = remaining
            else:
                self._active_counts.pop(name, None)
                self._running.discard(name)

    def wrap(self, name: str, callback):
        @wraps(callback)
        async def _coordinated(context, *args, **kwargs):
            self._active_counts[name] = self._active_counts.get(name, 0) + 1
            self._running.add(name)
            return await self._run_reserved(
                name,
                callback,
                context,
                *args,
                **kwargs,
            )

        return _coordinated

    def launch(
        self,
        name: str,
        callback,
        *,
        exclusive_group: str | None = None,
        allow_overlap: bool = False,
    ):
        """Return a scheduler callback that tracks every real execution."""
        @wraps(callback)
        async def _launcher(context, *args, **kwargs):
            self._active_counts[name] = 1
            self._running.add(name)
            # The named lock serializes later triggers. Keep each trigger as a
            # task so APScheduler cannot discard it while an earlier scan is
            # finishing its database barrier.
            task = asyncio.create_task(
                self._run_reserved(
                    name,
                    callback,
                    context,
                    *args,
                    exclusive_group=exclusive_group,
                    **kwargs,
                ),
                name=f"scheduled:{name}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._task_finished)
            await asyncio.sleep(0)
            return None

        return _launcher

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "scheduled_execution: task failed (%s): %s",
                task.get_name(),
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def shutdown(self) -> None:
        """Cancel and await all real scheduler tasks before runtime reset."""
        tasks = list(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._running.clear()
        self._active_counts.clear()
        self._job_locks.clear()
        self._exclusive_locks.clear()

    def backpressure_metrics(self) -> dict[str, float]:
        """Return scheduler wait counters for runtime/load observations."""
        return {
            "exclusive_group_waits": float(self._exclusive_waits),
            "exclusive_group_wait_seconds": round(
                self._exclusive_wait_seconds,
                3,
            ),
        }

    def reset_backpressure_metrics(self) -> None:
        """Start a fresh scheduler observation window without replacing locks."""
        self._exclusive_waits = 0
        self._exclusive_wait_seconds = 0.0


_scheduled_execution_coordinator = _ScheduledExecutionCoordinator()

# SQLite has one writer slot. Keep database-mutating scheduled jobs in one
# ownership group so they wait before starting instead of competing for the
# process-wide writer gate while a full Underdog persistence drain is active.
_SQLITE_WRITE_JOB_GROUP = "sqlite_write_jobs"
# Let APScheduler coalesce a slow scan instead of queueing overlapping scans.
# The coordinator still serializes named callbacks, but an unbounded scheduler
# backlog can make the next health cycle appear permanently stale.
_SCAN_JOB_KWARGS = {"coalesce": True}


def _coordinated_job(
    name: str,
    callback,
    *,
    exclusive_group: str | None = None,
    allow_overlap: bool = False,
    use_exclusive_group: bool = True,
):
    if not use_exclusive_group:
        exclusive_group = None
    return _scheduled_execution_coordinator.launch(
        name,
        callback,
        exclusive_group=exclusive_group,
        allow_overlap=allow_overlap,
    )


def _configured_odds_api_sports() -> list[tuple[str, Sport]]:
    """Return legacy OddsAPI sports for compatibility callers.

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
            "Legacy OddsAPI scope: ignoring %d unsupported sports: %s",
            len(ignored),
            ", ".join(ignored),
        )
    return supported

# ── Module-level singletons ────────────────────────────────────────────────────
# Initialised inside post_init so they live in the bot's event loop.
_db: Database | None = None
_history_db: Database | None = None
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
    global _db, _history_db, _engine, _season_checker

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Sharp Money +EV Detection Bot — Starting Up")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # ── Health tracker (must be first — jobs reference it) ────────────────────
    _ht = init_health_tracker()
    _ht.record_startup()
    # Every process/application restart gets a disposable monitoring session.
    # Persistent evidence, grading, learning, and PLAY history remain in SQLite.
    await _scheduled_execution_coordinator.shutdown()
    logger.info(
        "Previous scheduler backpressure counters: %s",
        _scheduled_execution_coordinator.backpressure_metrics(),
    )
    _scheduled_execution_coordinator.reset_backpressure_metrics()
    await reset_monitoring_runtime_state()
    logger.info(
        "New monitoring session initialised — session=%s; persistent history preserved",
        _ht.monitoring_session_id(),
    )
    logger.info(
        "HealthTracker initialised — startup #%d", _ht.restart_count()
    )

    _db = Database(config.DATABASE_URL)
    await _db.init()
    logger.info(
        "SQLite backpressure counters reset: %s",
        get_sqlite_backpressure_metrics(config.DATABASE_URL, reset=True),
    )
    # The continuous evidence/history collector owns a separate connection
    # pool so its long full-universe run cannot make command/report reads wait
    # behind storage work.
    _history_db = Database(config.DATABASE_URL)
    await _history_db.init()

    _engine = AnalysisEngine()

    # ── Season / market-status checker ────────────────────────────────────────
    # This checker is an Odds API client, so it must not even be constructed
    # while the provider is disabled.
    _season_checker = None
    if config.ODDS_API_ENABLED:
        _season_checker = SeasonChecker(
            api_key=config.ODDS_API_KEY,
            ttl_seconds=config.SEASON_CHECK_INTERVAL,
        )
        # Eager first load — non-fatal; checker stays fail-open on error.
        if config.SEASON_CHECK_INTERVAL > 0:
            await _season_checker.refresh()

    # ── Provider health monitor + PropLine feed ─────────────────────────────
    _health_monitor = init_health_monitor()
    from providers.propline import PropLinePrizePicksProvider
    _propline_enabled = PropLinePrizePicksProvider(
        bookmaker=config.PROPLINE_UNDERDOG_BOOKMAKER
    ).is_available()
    if config.ODDS_API_ENABLED:
        _health_monitor.register("OddsAPI")
    else:
        _health_monitor.register("OddsAPI", disabled=True)
    _health_monitor.register("Underdog", disabled=not config.UNDERDOG_ENABLED)
    logger.info(
        "Provider health monitor initialised "
        "(Underdog + PropLine feed=%s; all configured sports enabled)",
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

    if config.ODDS_API_ENABLED:
        init_odds_cache(ttl_seconds=config.ODDS_API_CACHE_TTL)
        init_quota_pacer(
            quota_limit=config.ODDS_API_QUOTA_LIMIT,
            daily_limit=config.ODDS_API_DAILY_LIMIT,
            min_interval_seconds=config.ODDS_API_MIN_REQUEST_INTERVAL,
            timezone_name=config.ODDS_API_PACING_TIMEZONE,
            state_path=config.ODDS_API_PACING_STATE_FILE,
        )
        logger.info("Odds API compatibility services enabled")
    else:
        logger.info("Odds API disabled; using PropLine for all player-prop feeds")

    # ── API usage tracker + budget enforcement ────────────────────────────────
    _usage_tracker = init_usage_tracker(
        monthly_budgets=(
            {"OddsAPI": config.ODDS_API_MONTHLY_BUDGET}
            if config.ODDS_API_ENABLED
            else {}
        ),
    )
    _usage_tracker.set_season_checker(_season_checker)
    logger.info(
        "API usage tracker initialised (%s)",
        "OddsAPI budget active"
        if config.ODDS_API_ENABLED
        else "OddsAPI disabled",
    )

    # Game results provider — only construct it when its upstream is enabled.
    _results_provider = (
        OddsApiResultsProvider(api_key=config.ODDS_API_KEY)
        if config.ODDS_API_ENABLED
        else None
    )
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
    # Do not expose the sportsbook engine to confirmation when Odds API is
    # disabled. PropLine confirmation still uses the database/provider path.
    init_odds_confirmation(
        _engine if config.ODDS_API_ENABLED else None,
        _db,
    )

    # Store db reference in bot_data for market engine jobs
    application.bot_data["db"] = _db
    application.bot_data["history_db"] = _history_db
    from providers.player_history import get_player_history_provider
    application.bot_data["player_history_provider"] = (
        get_player_history_provider()
    )
    await sync_telegram_command_catalog(application, _db)

    # Register background jobs
    jq = application.job_queue
    if jq:
        # PropLine owns all player-prop ingestion; no Odds API poller is
        # scheduled while that provider is disabled.
        if config.ODDS_API_ENABLED:
            jq.run_repeating(
                _coordinated_job(
                    "odds_poller",
                    _poll_odds_job,
                    exclusive_group=_SQLITE_WRITE_JOB_GROUP,
                ),
                interval=config.PLAYER_PROP_POLL_INTERVAL,
                first=10,
                name="odds_poller",
            )
        # The scheduler-facing callback is constructed with
        # _shared_monitoring_cycle so PropLine ingestion and Underdog scanning
        # retain one monitoring owner.
        # jq.run_repeating(_steam_check_job,    interval=config.STEAM_CHECK_INTERVAL,      first=15,  name="steam_checker")
        jq.run_repeating(
            _coordinated_job(
                "underdog_monitor",
                _underdog_monitor_cycle,
                exclusive_group=_SQLITE_WRITE_JOB_GROUP,
                # Cadence-critical monitoring has its own full-scan handoff
                # guard in market_engine. Do not make it wait behind FPR,
                # grading, or history work at the scheduler level; SQLite
                # writes are still serialized by the process-wide DB gate.
                use_exclusive_group=False,
            ),
            interval=config.UNDERDOG_POLL_INTERVAL,
            first=45,
            name="underdog_monitor",
            job_kwargs=_SCAN_JOB_KWARGS,
        )
        # The live monitor handles new props and line movement. The full-pool
        # rotation is the complementary data-consumption path: it re-scores
        # the complete eligible active pool so unchanged props are not merely
        # fetched and persisted without receiving fresh evidence evaluation.
        if config.FPR_INTERVAL > 0:
            jq.run_repeating(
                _coordinated_job(
                    "full_pool_rescan",
                    _full_pool_rescan_job,
                    exclusive_group=_SQLITE_WRITE_JOB_GROUP,
                    # FPR defers itself through the monitoring slot when a
                    # live feed scan is active. Keeping it out of the shared
                    # scheduler lock prevents it from starving the next feed
                    # cadence while preserving one-at-a-time FPR execution.
                    use_exclusive_group=False,
                ),
                # Full-pool monitoring is part of the active Underdog cycle;
                # it must not drift to a separate cadence.
                interval=config.UNDERDOG_POLL_INTERVAL,
                first=config.UNDERDOG_POLL_INTERVAL,
                name="full_pool_rescan",
                job_kwargs=_SCAN_JOB_KWARGS,
            )
        # Grading owns an independent one-shot schedule. The next run is only
        # registered after the current pass finishes, so a slow provider cannot
        # trigger APScheduler max-instances skips or leave queued grader runs.
        grader_callback = None

        async def _opportunity_grader_tick(context):
            await _grade_opportunities_job(context)
            queue = getattr(context, "job_queue", None)
            if queue is None:
                application_ref = getattr(context, "application", None)
                queue = getattr(application_ref, "job_queue", None)
            if queue is not None and grader_callback is not None:
                queue.run_once(
                    grader_callback,
                    when=config.OPPORTUNITY_GRADER_INTERVAL,
                    name="opportunity_grader",
                )

        grader_callback = _coordinated_job(
            "opportunity_grader",
            _opportunity_grader_tick,
            exclusive_group=_SQLITE_WRITE_JOB_GROUP,
            # Settlement fetches and grading must not hold the monitoring
            # scheduler slot. The database writer gate protects commits.
            use_exclusive_group=False,
        )
        jq.run_once(
            grader_callback,
            when=75,
            name="opportunity_grader",
            job_kwargs=_SCAN_JOB_KWARGS,
        )
        # CLV seeding/harvesting are independent database maintenance jobs.
        # They remain scheduled when Odds API is disabled so existing seeds are
        # still reconciled without starting any new sportsbook polling.
        jq.run_repeating(
            _coordinated_job(
                "clv_seeder",
                _clv_seed_job,
                exclusive_group=_SQLITE_WRITE_JOB_GROUP,
            ),
            interval=900,
            first=120,
            name="clv_seeder",
        )
        jq.run_repeating(
            _coordinated_job(
                "clv_harvester",
                _clv_harvest_job,
                exclusive_group=_SQLITE_WRITE_JOB_GROUP,
            ),
            interval=3600,
            first=180,
            name="clv_harvester",
        )
        # Underdog, PropLine, full-pool, grading, and player history each own
        # independent scheduler callbacks. None is a child of another cycle.
        # Player-result collection uses the dedicated history pool, but shares
        # the heavy-job ownership group with monitoring. It waits for the live
        # scan and its persistence drain before claiming SQLite.
        jq.run_repeating(
            _coordinated_job(
                "player_history_collector",
                player_history_collector_job,
            ),
            interval=config.PLAYER_HISTORY_POLL_INTERVAL,
            first=90,
            name="player_history_collector",
        )
        # API budget check — every 15 minutes, only when an API budget exists.
        if config.ODDS_API_ENABLED:
            jq.run_repeating(
                _coordinated_job("budget_checker", _budget_check_job),
                interval=900,
                first=30,
                name="budget_checker",
            )
        # PropLineHistory prune — once per day (keeps last 14 days)
        jq.run_repeating(
            _coordinated_job(
                "prop_history_pruner",
                _prune_prop_history_job,
                exclusive_group=_SQLITE_WRITE_JOB_GROUP,
            ),
            interval=86400,
                first=40,
            name="prop_history_pruner",
        )
        # Heartbeat — every 60 s (keeps HealthTracker alive timestamp current)
        jq.run_repeating(_coordinated_job("heartbeat", _heartbeat_job), interval=60, first=30, name="heartbeat")
        # Season / market-status refresh (requires the Odds API provider).
        if config.ODDS_API_ENABLED and config.SEASON_CHECK_INTERVAL > 0:
            jq.run_repeating(
                _coordinated_job("season_checker", _season_check_job),
                interval=config.SEASON_CHECK_INTERVAL,  # default interval=3600
                first=50,
                name="season_checker",
            )
        logger.info(
            "Jobs scheduled — Underdog every %ds with PropLine feed; "
            "full-pool every %ds; "
            "independent grader: every %ds, "
            "history collector: every %ds, "
            "season_check: %s, "
            "heartbeat: every 60s "
             "[Unified Player Props cycle enabled=%s; "
             "PropLine player-prop feed active; Odds API disabled; bookmakers=%s]",
            config.UNDERDOG_POLL_INTERVAL,
            config.UNDERDOG_POLL_INTERVAL,
            config.OPPORTUNITY_GRADER_INTERVAL,
            config.PLAYER_HISTORY_POLL_INTERVAL,
            (
                f"every {config.SEASON_CHECK_INTERVAL}s"
                if config.ODDS_API_ENABLED and config.SEASON_CHECK_INTERVAL > 0
                else "disabled"
            ),
             _propline_enabled,
             ",".join(getattr(config, "propline_bookmakers", [])),
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
    Send a startup or restart notification to all configured chat IDs.

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
    if startup_reason != "first_start":
        parts: list[str] = [
            "🤖 <b>Sharp Money Bot Restarted</b>",
            "",
            "<b>Status:</b>  Monitoring resumed ✅",
            f"Startup #{restart_num}",
            "",
            "<i>Props will be analysed as they are detected.</i>",
        ]
    else:
        parts = [
            "🤖 <b>Sharp Money Bot Online</b>",
            "",
            "<b>Status:</b>  Monitoring active ✅",
            f"Startup #{restart_num}",
            "",
            "<i>Ready. Props will be analysed as they are detected.</i>",
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
    try:
        await _scheduled_execution_coordinator.shutdown()
        await reset_monitoring_runtime_state()
        await shutdown_db_persistence_worker()
    except Exception as _reset_exc:
        logger.warning("Runtime session cleanup failed during shutdown: %s", _reset_exc)
    if _history_db:
        await _history_db.close()
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

# Keep each grading pass bounded while allowing both independent settlement
# populations to make progress.  The old 100+100 split left large backlogs
# growing faster than the grader could drain them.
_GRADE_BATCH_SIZE = 250
_GRADE_FETCH_CONCURRENCY = 12
_GRADE_FETCH_TIMEOUT_SECONDS = 20
# Monitoring and grading have independent owners. Keep each grading pass
# bounded so a slow settlement provider cannot block a live scan or history
# collection while the 30-minute cadence drains the durable backlog.
_GRADE_CYCLE_BUDGET_SECONDS = 50
_GRADE_CYCLE_HEADROOM_SECONDS = 10
_GRADE_PROPLINE_TARGETS_PER_CYCLE = 2


async def _underdog_monitor_cycle(context) -> None:
    """Run only the continuous Underdog monitoring cycle."""
    await underdog_job(context)


# Keep the scheduler-facing callback name stable while retaining the shared
# PropLine preload and Underdog evaluation behavior.
_underdog_monitor_cycle = _shared_monitoring_cycle(_underdog_monitor_cycle)


async def _grade_opportunities_job(context) -> None:
    """
    Every 1–2 minutes: grade completed tracked props from stored game results.
    The job proactively calls ``fetch_results`` for every pending identity
    using the ``_grade_fetched`` identity set before reading persisted result
    rows.

    Finds PENDING rows in prop_opportunity_log whose event is old enough to
    resolve, looks up the actual player stat in player_game_results, and
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
        _tracked_cursor = (
            _ht_start.get_tracking_grader_cursor()
            if _ht_start and hasattr(_ht_start, "get_tracking_grader_cursor")
            else 0
        )
        try:
            pending = await db.get_pending_opportunities(
                cutoff_hours=0.5,
                after_id=_cursor,
                limit=_GRADE_BATCH_SIZE,
            )
        except TypeError:
            # Keep lightweight test doubles and older database adapters
            # compatible while production uses the rotating query.
            pending = await db.get_pending_opportunities(cutoff_hours=0.5)
        # Wrap once the cursor reaches the end of the pending set.
        if not pending and _cursor:
            try:
                pending = await db.get_pending_opportunities(
                    cutoff_hours=0.5,
                    after_id=None,
                    limit=_GRADE_BATCH_SIZE,
                )
            except TypeError:
                pending = await db.get_pending_opportunities(cutoff_hours=0.5)

        # Universal ingestion tracking is a separate settlement population.
        # Keep legacy directional rows in the existing rotating query, then
        # add unresolved neutral observations without changing opportunity
        # reports or strongest-pick behavior.
        _legacy_pending = list(pending or [])
        _tracked_pending = []
        _get_pending_tracked = getattr(db, "get_pending_tracked_props", None)
        if callable(_get_pending_tracked):
            try:
                _tracked_result = _get_pending_tracked(
                    cutoff_hours=0.5,
                    after_id=_tracked_cursor,
                    limit=_GRADE_BATCH_SIZE,
                )
                if hasattr(_tracked_result, "__await__"):
                    _tracked_pending = await _tracked_result
            except TypeError:
                # Compatibility with older adapters that do not accept limit.
                _tracked_result = _get_pending_tracked(cutoff_hours=0.5)
                if hasattr(_tracked_result, "__await__"):
                    _tracked_pending = await _tracked_result
            if not _tracked_pending and _tracked_cursor:
                try:
                    _tracked_result = _get_pending_tracked(
                        cutoff_hours=0.5,
                        after_id=None,
                        limit=_GRADE_BATCH_SIZE,
                    )
                    if hasattr(_tracked_result, "__await__"):
                        _tracked_pending = await _tracked_result
                except TypeError:
                    _tracked_result = _get_pending_tracked(cutoff_hours=0.5)
                    if hasattr(_tracked_result, "__await__"):
                        _tracked_pending = await _tracked_result
        pending = _legacy_pending + list(_tracked_pending or [])
        if not pending:
            if _ht_start:
                _ht_start.record_job_run("_grade_opportunities_job")
            return

        # Reuse durable game evidence before invoking any remote provider.
        # This prevents optional-provider warnings and requests for rows the
        # database can already grade.
        _stored_grade_results = {}
        _bulk_result_loader = getattr(db, "get_player_results_bulk", None)
        if callable(_bulk_result_loader):
            _bulk_result = _bulk_result_loader(
                [
                    (opp.player_name, opp.sport, opp.stat_type)
                    for opp in pending
                ],
                # Keep enough durable history to reconcile opportunities from
                # several weeks ago, not only the most recent 30 games.
                limit=90,
            )
            if hasattr(_bulk_result, "__await__"):
                _stored_grade_results = await _bulk_result
            elif isinstance(_bulk_result, dict):
                _stored_grade_results = _bulk_result

        def _has_stored_grade_result(opp) -> bool:
            _key = (
                str(opp.player_name),
                str(opp.sport),
                str(opp.stat_type or "").lower().strip(),
            )
            _event_date = (
                opp.game_time.strftime("%Y-%m-%d")
                if getattr(opp, "game_time", None)
                else ""
            )
            # Older Underdog rows may have only the source event ID. That ID
            # is not the same namespace as official player-game results, while
            # detected_at is the durable date captured when the opportunity
            # entered the database. Use it only as a dated DB lookup fallback.
            if (
                not _event_date
                and getattr(opp, "game_id", "")
                and getattr(opp, "detected_at", None)
            ):
                _event_date = opp.detected_at.strftime("%Y-%m-%d")
            _game_id = str(getattr(opp, "game_id", "") or "")
            return any(
                (
                    _event_date
                    and str(getattr(row, "game_date", "") or "") == _event_date
                )
                or (
                    _game_id
                    and str(getattr(row, "game_id", "") or "") == _game_id
                )
                for row in _stored_grade_results.get(_key, [])
            )

        # ── Proactively fetch player results so grading works without /backfill ──
        # The grader previously only looked up pre-existing PlayerResult rows.
        # Props removed from Underdog before the game finished would never get
        # a result row from scanning, leaving them PENDING forever.
        # This mirrors the /backfill fetch pattern so auto-grading is self-contained.
        _grade_stat_prov = None
        try:
            from providers.player_stats import PlayerStatsProvider as _GradeStatProvider
            _grade_stat_prov = _GradeStatProvider()
        except Exception as _prov_init_exc:
            logger.warning(
                "_grade_opportunities_job: stat provider initialization failed: %s",
                _prov_init_exc,
            )

        if _grade_stat_prov is not None:
            _grade_fetched: set = set()
            _grade_targets = []
            for _gopp in pending:
                _gkey = (_gopp.player_name, _gopp.sport, (_gopp.stat_type or "").lower().strip())
                if _gkey in _grade_fetched:
                    continue
                _grade_fetched.add(_gkey)
                if _has_stored_grade_result(_gopp):
                    continue
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

            try:
                _grade_batches = await asyncio.gather(
                    *(_fetch_grade_results(_gopp) for _gopp in _grade_targets)
                )
                _all_grade_results = [
                    _gr
                    for _graw in _grade_batches
                    for _gr in (_graw or [])
                ]
                _bulk_grade_writer = getattr(db, "upsert_player_results_bulk", None)
                if callable(_bulk_grade_writer) and _all_grade_results:
                    await _bulk_grade_writer(_all_grade_results)
                else:
                    for _gr in _all_grade_results:
                        await db.upsert_player_result(_gr)
                # Keep the in-memory diagnostic population aligned with the
                # rows just fetched.  Settlement still requires the exact
                # event/date below, but this lets the report distinguish
                # missing settlement evidence from a player with no history.
                for _gr in _all_grade_results:
                    _gr_key = (
                        str(getattr(_gr, "player_name", "") or ""),
                        str(getattr(_gr, "sport", "") or ""),
                        str(getattr(_gr, "stat_type", "") or "").lower().strip(),
                    )
                    _stored_grade_results.setdefault(_gr_key, []).append(_gr)
            except Exception as _prov_fetch_exc:
                logger.warning(
                    "_grade_opportunities_job: stat provider fetch/persist failed: %s",
                    _prov_fetch_exc,
                )

        # If the public stat provider has no row for a completed event, use
        # PropLine's resolved history as a settlement fallback.  This is
        # especially important for CS: PandaScore is optional when PropLine
        # already exposes the event result.  The fallback is event-specific
        # and read-only; it never grades an unresolved or guessed row.
        _propline_grade_provider = None
        # Provider is part of this cache key. Underdog and PrizePicks can use
        # the same player/stat pair while their event/result namespaces differ.
        _propline_grade_cache: dict[tuple[str, str, str, str], list] = {}
        _propline_grade_attempts = 0
        try:
            from providers.player_history import get_player_history_provider
            _propline_grade_provider = get_player_history_provider()
        except Exception as _propline_init_exc:
            logger.debug(
                "_grade_opportunities_job: PropLine grading fallback unavailable: %s",
                _propline_init_exc,
            )

        async def _fetch_propline_grade_results(_gopp):
            nonlocal _propline_grade_attempts
            if _propline_grade_provider is None:
                return []
            _key = (
                str(_gopp.player_name),
                str(_gopp.sport).upper(),
                str(_gopp.stat_type or "").lower().strip(),
                str(
                    getattr(_gopp, "provider", None)
                    or getattr(_gopp, "source_provider", None)
                    or "Underdog"
                ).strip(),
            )
            if _key not in _propline_grade_cache:
                if (
                    _propline_grade_attempts
                    >= _GRADE_PROPLINE_TARGETS_PER_CYCLE
                ):
                    return []
                _propline_grade_attempts += 1
                try:
                    _propline_grade_cache[_key] = list(
                        await asyncio.wait_for(
                            _propline_grade_provider.fetch_resolved_results(
                                _gopp.player_name,
                                _gopp.sport,
                                _gopp.stat_type,
                                source_provider=(
                                    getattr(_gopp, "provider", None)
                                    or getattr(_gopp, "source_provider", None)
                                    or "Underdog"
                                ),
                            ),
                            timeout=_GRADE_FETCH_TIMEOUT_SECONDS,
                        )
                    )
                except Exception as _propline_grade_exc:
                    logger.debug(
                        "_grade_opportunities_job: PropLine settlement lookup "
                        "failed for %s/%s: %s",
                        _gopp.player_name,
                        _gopp.stat_type,
                        _propline_grade_exc,
                    )
                    _propline_grade_cache[_key] = []
            return _propline_grade_cache[_key]

        graded   = 0
        no_data  = 0
        date_shifted = 0
        no_data_by_sport: dict[str, int] = defaultdict(int)
        historical_evidence_available = 0
        no_historical_evidence = 0
        historical_evidence_by_sport: dict[str, int] = defaultdict(int)
        _tracked_grades: list[tuple[int, str, float]] = []
        _opportunity_grades: list[dict[str, Any]] = []

        def _stored_history_for(opp) -> list:
            _key = (
                str(getattr(opp, "player_name", "") or ""),
                str(getattr(opp, "sport", "") or ""),
                str(getattr(opp, "stat_type", "") or "").lower().strip(),
            )
            return _stored_grade_results.get(_key, [])

        for opp in pending:
            game_date = (
                opp.game_time.strftime("%Y-%m-%d")
                if opp.game_time
                else None
            )
            if (
                not game_date
                and getattr(opp, "game_id", "")
                and getattr(opp, "detected_at", None)
            ):
                # Use the database observation date when the source event has
                # no kickoff timestamp. The result lookup remains date-bound;
                # it never falls back to player/stat alone.
                game_date = opp.detected_at.strftime("%Y-%m-%d")
            # Feed event timestamps are UTC, while official player-result
            # providers key their rows by the local game calendar date. A
            # late-night UTC event therefore commonly settles on the prior
            # provider date (for example, 01:40 UTC → the previous MLB date).
            # Try the exact date first, then one previous date; never search
            # by player/stat alone.
            _game_dates = [game_date] if game_date else []
            if game_date and opp.game_time:
                _previous_game_date = (
                    opp.game_time - timedelta(days=1)
                ).strftime("%Y-%m-%d")
                if _previous_game_date != game_date:
                    _game_dates.append(_previous_game_date)
            # A provider may omit game_time, but a source event/game identity
            # still lets the result provider reconcile the completed event.
            # Without either identity, leave the row pending rather than
            # guessing which player game should settle it.
            if not game_date and not getattr(opp, "game_id", ""):
                no_data += 1
                no_data_by_sport[str(getattr(opp, "sport", "UNKNOWN")).upper()] += 1
                if _stored_history_for(opp):
                    historical_evidence_available += 1
                    historical_evidence_by_sport[
                        str(getattr(opp, "sport", "UNKNOWN")).upper()
                    ] += 1
                else:
                    no_historical_evidence += 1
                continue
            _result_lookup_kwargs = {}
            if getattr(opp, "game_id", ""):
                _result_lookup_kwargs["game_id"] = opp.game_id
            if getattr(opp, "provider", None):
                _result_lookup_kwargs["provider"] = opp.provider
            result_row = None
            _matched_game_date = game_date
            for _candidate_game_date in _game_dates:
                result_row = await db.get_game_result_for_grading(
                    opp.player_name,
                    opp.sport,
                    opp.stat_type,
                    _candidate_game_date,
                    **_result_lookup_kwargs,
                )
                if result_row is not None:
                    _matched_game_date = _candidate_game_date
                    break
            # Some PropLine projections have a stable event identity but no
            # game timestamp. Once the official result has been persisted,
            # reconcile by that identity instead of leaving the row pending
            # forever. This remains strict: no date and no game_id still
            # cannot be graded safely.
            if result_row is None and not game_date and getattr(opp, "game_id", ""):
                result_row = await db.get_game_result_for_grading(
                    opp.player_name,
                    opp.sport,
                    opp.stat_type,
                    None,
                    **_result_lookup_kwargs,
                )
            if result_row is None:
                # A date or source event identity is required to safely
                # reconcile a PropLine result to this opportunity. Never fall
                # back by player/stat alone.
                if game_date or getattr(opp, "game_id", ""):
                    for _propline_result in await _fetch_propline_grade_results(opp):
                        await db.upsert_player_result(_propline_result)
                    for _candidate_game_date in _game_dates:
                        result_row = await db.get_game_result_for_grading(
                            opp.player_name,
                            opp.sport,
                            opp.stat_type,
                            _candidate_game_date,
                            **_result_lookup_kwargs,
                        )
                        if result_row is not None:
                            _matched_game_date = _candidate_game_date
                            break
                    if (
                        result_row is None
                        and not game_date
                        and getattr(opp, "game_id", "")
                    ):
                        result_row = await db.get_game_result_for_grading(
                            opp.player_name,
                            opp.sport,
                            opp.stat_type,
                            None,
                            **_result_lookup_kwargs,
                        )
            if result_row is None:
                no_data += 1
                no_data_by_sport[str(getattr(opp, "sport", "UNKNOWN")).upper()] += 1
                if _stored_history_for(opp):
                    historical_evidence_available += 1
                    historical_evidence_by_sport[
                        str(getattr(opp, "sport", "UNKNOWN")).upper()
                    ] += 1
                else:
                    no_historical_evidence += 1
                logger.debug(
                    "_grade_opportunities_job: no result for %s | %s | %s | %s",
                    opp.player_name, opp.sport, opp.stat_type,
                    game_date or "event identity",
                )
                continue
            if _matched_game_date != game_date:
                date_shifted += 1
            if getattr(result_row, "source", "") == "derived_from_db":
                # Persist the composite result after deriving it from durable
                # component rows so future grading/reporting reads use the
                # database directly instead of recomputing it.
                try:
                    await db.upsert_player_result(result_row)
                except Exception as _derived_persist_exc:
                    logger.warning(
                        "_grade_opportunities_job: derived result persistence "
                        "failed for %s/%s/%s: %s",
                        opp.player_name,
                        opp.sport,
                        opp.stat_type,
                        _derived_persist_exc,
                    )
            actual = result_row.actual_value
            line   = opp.line_value
            recommendation = getattr(opp, "recommendation", None)

            # Direction-aware grading: HIT means the bet won.
            #   OVER recommendation → won if actual > line
            #   UNDER recommendation → won if actual < line
            #   PUSH when actual == line (within tolerance)
            _push_tol = 0.01
            if abs(actual - line) < _push_tol:
                outcome = "PUSH"
            elif (recommendation or "OVER").upper() == "UNDER":
                outcome = "HIT" if actual < line else "MISS"
            else:
                # OVER (default) or unknown direction
                outcome = "HIT" if actual > line else "MISS"

            # Classify MISS outcomes for learning — only Model errors update weights
            error_type: Optional[str] = None
            # Neutral tracking rows are graded for market history, but they
            # were never a model recommendation and must not feed calibration
            # learning as if they were a missed pick.
            if (
                outcome == "MISS"
                and (recommendation or "").upper() in ("OVER", "UNDER")
            ):
                from engine.calibration import classify_miss
                error_type = classify_miss(
                    recommendation = recommendation,
                    decision_tier  = opp.decision_tier,
                    confidence     = opp.confidence,
                    actual_value   = actual,
                    line_value     = opp.line_value,
                )
            if recommendation is None:
                _tracked_grades.append((opp.id, outcome, actual))
            else:
                _opportunity_grades.append(
                    {
                        "id": opp.id,
                        "result": outcome,
                        "actual_value": actual,
                        "error_type": error_type,
                    }
                )
            graded += 1

        # Keep the expensive per-row settlement lookups above, but commit each
        # population once.  This materially reduces SQLite writer-gate
        # contention during large history refreshes.  Older adapters and test
        # doubles still use the original row-at-a-time methods.
        _bulk_tracked = getattr(db, "grade_tracked_props_bulk", None)
        if _tracked_grades and inspect.iscoroutinefunction(_bulk_tracked):
            await _bulk_tracked(_tracked_grades)
        elif _tracked_grades:
            _grade_tracked = getattr(db, "grade_tracked_prop", None)
            if callable(_grade_tracked):
                for _tracking_id, _outcome, _actual in _tracked_grades:
                    await _grade_tracked(_tracking_id, _outcome, _actual)

        _bulk_opportunities = getattr(db, "grade_opportunities_bulk", None)
        if _opportunity_grades and inspect.iscoroutinefunction(_bulk_opportunities):
            await _bulk_opportunities(_opportunity_grades)
        elif _opportunity_grades:
            for _grade in _opportunity_grades:
                await db.grade_opportunity(
                    _grade["id"],
                    _grade["result"],
                    _grade["actual_value"],
                    error_type=_grade.get("error_type"),
                )

        if graded or no_data:
            logger.info(
                "_grade_opportunities_job: graded=%d opportunities "
                "(no_data=%d date_shifted=%d historical_evidence_available=%d "
                "no_historical_evidence=%d no_data_by_sport=%s "
                "historical_evidence_by_sport=%s)",
                graded,
                no_data,
                date_shifted,
                historical_evidence_available,
                no_historical_evidence,
                dict(sorted(no_data_by_sport.items())),
                dict(sorted(historical_evidence_by_sport.items())),
            )
        _ht_g = get_health_tracker()
        if _ht_g:
            if hasattr(_ht_g, "set_opportunity_grader_cursor") and _legacy_pending:
                _ht_g.set_opportunity_grader_cursor(
                    max(int(getattr(_opp, "id", 0) or 0) for _opp in _legacy_pending)
                )
            if hasattr(_ht_g, "set_tracking_grader_cursor") and _tracked_pending:
                _ht_g.set_tracking_grader_cursor(
                    max(int(getattr(_tracked, "id", 0) or 0) for _tracked in _tracked_pending)
                )
            _ht_g.record_job_run("_grade_opportunities_job")
    except Exception:
        logger.exception("_grade_opportunities_job: unexpected error")
        _ht_g = get_health_tracker()
        if _ht_g:
            _ht_g.record_job_fail("_grade_opportunities_job", "unexpected error")


async def _heartbeat_job(context) -> None:
    """Run every 60 s for process-health bookkeeping only."""
    ht = get_health_tracker()
    try:
        if ht is not None:
            ht.record_job_started("heartbeat_job")
            ht.update_heartbeat()
            ht.record_job_run("heartbeat_job")
    except Exception as _exc:
        logger.exception("_heartbeat_job: error: %s", _exc)
        if ht is not None:
            ht.record_job_fail("heartbeat_job", str(_exc))


# ── Background jobs ────────────────────────────────────────────────────────────

async def _player_props_job(context) -> None:
    """
    Keep the legacy Odds API player-prop job disabled.

    PropLine is the configured source for PrizePicks, DraftKings, FanDuel, and
    supplemental Underdog observations. Underdog's native connector remains
    active separately. H2H/totals confirmation is a separate path and is not
    enabled by this player-prop job.
    """
    if _db is None or _engine is None:
        logger.debug("_player_props_job: DB or engine not ready, skipping")
        return
    logger.info(
        "_player_props_job: OddsAPI player-prop polling disabled; "
        "PropLine owns PrizePicks/DraftKings/FanDuel/Underdog feed ingestion"
    )
    return

    now      = datetime.utcnow()
    pacer = get_quota_pacer()

    allowed_books = set()
    if config.SPORTSBOOK_CONFIRMATION_ENABLED:
        if config.DRAFTKINGS_ENABLED:
            allowed_books.add("draftkings")
        if config.FANDUEL_ENABLED:
            allowed_books.add("fanduel")
    if not getattr(config, "ODDS_API_ENABLED", True):
        logger.debug("_player_props_job: OddsAPI disabled by configuration")
        return

    configured_sports = _configured_odds_api_sports()
    configured_prop_sports = getattr(config, "player_prop_sports", None)
    if not isinstance(configured_prop_sports, (list, tuple, set, frozenset)):
        raw_prop_sports = getattr(config, "PLAYER_PROP_SPORTS_RAW", "")
        configured_prop_sports = (
            str(raw_prop_sports).split(",") if isinstance(raw_prop_sports, str) else []
        )
    prop_sports = {str(value).strip().upper() for value in configured_prop_sports}
    if not configured_sports:
        aliases = {
            "CFB": Sport.NCAAF,
            "NCAA": Sport.NCAAF,
            "FOOTBALL": Sport.NFL,
            "BASKETBALL": Sport.NBA,
            "BASEBALL": Sport.MLB,
        }
        by_name = {member.value.upper(): member for member in Sport}
        configured_sports = [
            (
                raw,
                aliases.get(raw.upper()) or by_name.get(raw.upper()),
            )
            for raw in configured_prop_sports
            if (aliases.get(raw.upper()) or by_name.get(raw.upper())) is not None
        ]
    persisted_by_provider: dict[str, list[object]] = defaultdict(list)
    seen: set[tuple[str, str, str, str]] = set()
    for sport_str, sport in configured_sports:
        if sport_str.upper() not in {str(value).upper() for value in prop_sports}:
            continue
        if sport not in _SPORT_PLAYER_PROP_MARKETS:
            coverage = SPORTSBOOK_PLAYER_PROP_COVERAGE.get(
                str(getattr(sport, "value", sport)),
                {"status": "unverified", "reason": "sport not mapped"},
            )
            logger.debug(
                "_player_props_job: sportsbook player-prop coverage %s for %s; "
                "skipping (%s)",
                coverage.get("status", "unverified"),
                sport_str,
                coverage.get("reason", "no validated market mapping"),
            )
            continue
        # Skip sports that are out of season / have no active markets.
        # The checker is fail-open: returns True if cache not yet populated.
        if _season_checker is not None:
            odds_key = _SPORT_TO_ODDS_API_KEY.get(sport)
            if odds_key and not _season_checker.is_sport_active(odds_key):
                logger.info(
                    "_player_props_job: skipping %s (%s) — out of season / no active markets",
                    sport_str, odds_key,
                )
                continue
        if pacer is not None and not await pacer.wait_for_slot():
            logger.info(
                "_player_props_job: Odds API request skipped — %s",
                pacer.block_reason(),
            )
            break
        lines = await _engine.fetch_player_prop_odds(sport)
        for line in lines:
            book_key = str(line.sportsbook or "").lower().replace(" ", "")
            if book_key not in allowed_books or line.line is None:
                continue
            provider = "DraftKings" if book_key == "draftkings" else "FanDuel"
            stat_type = _ODDS_MARKET_TO_PROP_STAT.get(line.market_key)
            if not stat_type:
                continue
            event_id = str(getattr(line, "event_id", "") or line.event)
            identity = (
                provider,
                event_id,
                str(line.player_name).strip().lower(),
                stat_type,
            )
            if identity in seen:
                continue
            seen.add(identity)
            source_key = "|".join(str(part) for part in identity)
            external_id = "odds-" + hashlib.sha1(source_key.encode()).hexdigest()[:32]
            persisted_by_provider[provider].append(
                SimpleNamespace(
                    provider=provider,
                    sport=sport.value,
                    player_name=str(line.player_name).strip(),
                    team="",
                    stat_type=stat_type,
                    line_value=float(line.line),
                    game_time=line.event_start,
                    provider_status="active",
                    external_id=external_id,
                    game_id=event_id,
                    event_id=event_id,
                    line_variant="sportsbook",
                    fetched_at=line.observed_at,
                )
            )

    if pacer is not None:
        logger.info("%s", pacer.telemetry().summary)

    persisted = 0
    for provider, props in persisted_by_provider.items():
        try:
            persisted += await persist_canonical_player_props(
                _db,
                provider,
                props,
                append_snapshot=True,
                append_observations=True,
                track=True,
            )
        except Exception as exc:
            logger.exception(
                "_player_props_job: player-prop persistence failed provider=%s: %s",
                provider,
                exc,
            )
    if not persisted:
        logger.debug("_player_props_job: no player-prop lines persisted")
        return

    logger.info(
        "_player_props_job: persisted %d sportsbook player-prop rows "
        "(DraftKings=%d FanDuel=%d); h2h/totals disabled",
        persisted,
        len(persisted_by_provider.get("DraftKings", [])),
        len(persisted_by_provider.get("FanDuel", [])),
    )


async def _poll_odds_job(context) -> None:
    """Compatibility entry point for the retired general Odds API poller."""
    await _player_props_job(context)


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
        clv_records = []
        computed_updates: dict[int, float] = {}
        expired_ids: list[int] = []
        now       = datetime.utcnow()
        grace     = timedelta(hours=4)   # wait this long for closing odds before expiring

        for seed in seeds:
            game_time = seed.game_time
            if game_time is None:
                # No timing info — expire immediately
                expired_ids.append(seed.id)
                expired += 1
                continue

            # Seeds without bet_odds cannot produce CLV% — expire immediately.
            # Underdog seeds seeded via seed_clv_from_ud_confirmation() DO have
            # bet_odds (from OddsAPI avg_odds), so they proceed to closing-line lookup.
            if not seed.bet_odds:
                expired_ids.append(seed.id)
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
                    clv_records.append(rec)
                    computed_updates[seed.id] = clv_result.clv_pct
                    harvested += 1
                except Exception as exc:
                    logger.warning(
                        "_clv_harvest_job: CLV compute failed for seed %d: %s", seed.id, exc
                    )
            elif now - game_time > grace:
                # Grace period elapsed, no odds found → expire
                expired_ids.append(seed.id)
                expired += 1

        # Keep the per-seed read/compute work above, but cross SQLite only
        # once per bounded batch instead of opening a write transaction for
        # every harvested or expired seed.
        if clv_records:
            await _db.save_clv_records_bulk(clv_records)
        if computed_updates:
            await _db.mark_clv_seeds_computed_bulk(computed_updates)
        if expired_ids:
            await _db.mark_clv_seeds_expired_bulk(expired_ids)

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
    if not config.ODDS_API_ENABLED:
        _ht = get_health_tracker()
        if _ht:
            _ht.record_job_run("_budget_check_job")
        return
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
    if not config.ODDS_API_ENABLED or _season_checker is None:
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

    app = build_application()

    logger.info("Starting polling — press Ctrl+C to stop.")

    # run_polling() owns the event loop; do NOT call asyncio.run() around it.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
