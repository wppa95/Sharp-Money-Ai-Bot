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

from telegram import Update
from telegram.ext import Application, CommandHandler

# Ensure the bot/ directory is on the path for sibling imports
sys.path.insert(0, os.path.dirname(__file__))

from config import config
from database import Database
from engine import AnalysisEngine
from commands import (
    cmd_start,
    cmd_help,
    cmd_status,
    cmd_analyze,
    cmd_steam,
    cmd_ev,
    error_handler,
    init_handlers,
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

    # Register background jobs
    jq = application.job_queue
    if jq:
        jq.run_repeating(_poll_odds_job, interval=config.ODDS_POLL_INTERVAL,  first=10,  name="odds_poller")
        jq.run_repeating(_steam_check_job, interval=config.STEAM_CHECK_INTERVAL, first=15, name="steam_checker")
        logger.info(
            "Jobs scheduled — odds: every %ds, steam: every %ds",
            config.ODDS_POLL_INTERVAL,
            config.STEAM_CHECK_INTERVAL,
        )
    else:
        logger.warning("JobQueue not available — background jobs disabled.")

    logger.info("Bot initialised and ready.")


async def post_shutdown(application: Application) -> None:
    """Runs once after polling stops, before the process exits."""
    if _db:
        await _db.close()
    logger.info("Shutdown complete. Goodbye.")


# ── Background job stubs ───────────────────────────────────────────────────────

async def _poll_odds_job(context) -> None:
    """PLACEHOLDER: Fetch live odds from sportsbook APIs on a schedule."""
    logger.debug("Odds polling job triggered (not yet implemented)")


async def _steam_check_job(context) -> None:
    """PLACEHOLDER: Run a steam detection sweep on a schedule."""
    logger.debug("Steam check job triggered (not yet implemented)")


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
    app.add_error_handler(error_handler)

    logger.info("Starting polling — press Ctrl+C to stop.")

    # run_polling() owns the event loop; do NOT call asyncio.run() around it.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
