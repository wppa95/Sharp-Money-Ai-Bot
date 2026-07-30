"""
main.py — Sharp Money +EV Detection Bot startup.

Entry point: python bot/main.py
"""

from __future__ import annotations

import asyncio
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
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ── Application factory ────────────────────────────────────────────────────────

def build_application(db: Database, engine: AnalysisEngine) -> Application:
    """Create and configure the Telegram Application."""
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("steam",   cmd_steam))
    app.add_handler(CommandHandler("ev",      cmd_ev))

    # Global error handler
    app.add_error_handler(error_handler)

    return app


# ── Background jobs (future: live odds polling) ────────────────────────────────

async def _poll_odds_job(context) -> None:
    """
    PLACEHOLDER: Periodic odds polling job.
    Runs every config.ODDS_POLL_INTERVAL seconds once a live API is integrated.
    """
    logger.debug("Odds polling job triggered (not yet implemented)")


async def _steam_check_job(context) -> None:
    """
    PLACEHOLDER: Periodic steam detection sweep.
    Runs every config.STEAM_CHECK_INTERVAL seconds.
    """
    logger.debug("Steam check job triggered (not yet implemented)")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    # Validate configuration
    try:
        config.validate()
    except ValueError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Sharp Money +EV Detection Bot — Starting Up")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Initialise database
    db = Database(config.DATABASE_URL)
    await db.init()

    # Initialise analysis engine
    engine = AnalysisEngine()

    # Wire up command handlers
    allowed_ids = list(config.allowed_user_ids)
    init_handlers(db, engine, allowed_ids)

    # Build the Telegram application
    app = build_application(db, engine)

    # Register background jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(
            _poll_odds_job,
            interval=config.ODDS_POLL_INTERVAL,
            first=10,
            name="odds_poller",
        )
        job_queue.run_repeating(
            _steam_check_job,
            interval=config.STEAM_CHECK_INTERVAL,
            first=15,
            name="steam_checker",
        )
        logger.info(
            "Jobs scheduled — odds: every %ds, steam: every %ds",
            config.ODDS_POLL_INTERVAL,
            config.STEAM_CHECK_INTERVAL,
        )
    else:
        logger.warning("JobQueue not available — background jobs disabled.")

    logger.info("Bot is running. Press Ctrl+C to stop.")
    logger.info("Polling for updates...")

    # Start polling (blocking until KeyboardInterrupt or SIGTERM)
    try:
        await app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    finally:
        logger.info("Shutting down...")
        await db.close()
        logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
