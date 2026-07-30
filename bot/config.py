"""
Configuration module for the Sharp Money +EV Detection Bot.
Loads settings from environment variables / .env file.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    # Comma-separated list of Telegram user IDs allowed to use the bot.
    # Leave empty to allow all users (not recommended for production).
    ALLOWED_USER_IDS: list[int] = field(default_factory=list)

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///bot/data/sharp_money.db")
    )

    # ── Analysis Engine ───────────────────────────────────────────────────────
    # Minimum EV% to flag an opportunity
    MIN_EV_THRESHOLD: float = float(os.environ.get("MIN_EV_THRESHOLD", "3.0"))
    # Minimum steam score (0–100) to fire a steam alert
    MIN_STEAM_SCORE: int = int(os.environ.get("MIN_STEAM_SCORE", "70"))
    # Minimum AI confidence score (0–100) to include in alert
    MIN_AI_CONFIDENCE: int = int(os.environ.get("MIN_AI_CONFIDENCE", "60"))

    # ── Polling / Job intervals (seconds) ─────────────────────────────────────
    ODDS_POLL_INTERVAL: int = int(os.environ.get("ODDS_POLL_INTERVAL", "60"))
    STEAM_CHECK_INTERVAL: int = int(os.environ.get("STEAM_CHECK_INTERVAL", "30"))

    # ── External APIs (placeholders — fill in as services are integrated) ─────
    ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "")
    PRIZEPICKS_API_KEY: str = os.environ.get("PRIZEPICKS_API_KEY", "")

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        """Raise ValueError if required settings are missing."""
        if not self.TELEGRAM_BOT_TOKEN:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not set. "
                "Add it to your environment or .env file."
            )

    @property
    def allowed_user_ids(self) -> set[int]:
        raw = os.environ.get("ALLOWED_USER_IDS", "")
        if not raw:
            return set()
        return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}


# Singleton instance used throughout the project
config = Config()
