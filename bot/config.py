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
        default_factory=lambda: os.environ.get("TELEGRAM_TOKEN", "")
    )
    # Comma-separated list of Telegram user IDs allowed to use the bot.
    # Leave empty to allow all users (not recommended for production).
    ALLOWED_USER_IDS: list[int] = field(default_factory=list)

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = field(
        default_factory=lambda: os.environ.get("BOT_DATABASE_URL", "sqlite+aiosqlite:///bot/data/sharp_money.db")
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

    # ── External APIs ─────────────────────────────────────────────────────────
    ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "")
    # PrizePicks uses a public API — no key required. Field reserved for future
    # authenticated endpoints.
    PRIZEPICKS_API_KEY: str = os.environ.get("PRIZEPICKS_API_KEY", "")

    # ── PrizePicks monitoring ─────────────────────────────────────────────────
    # Leagues to monitor (comma-separated PrizePicks league names).
    PRIZEPICKS_LEAGUES_RAW: str = os.environ.get("PRIZEPICKS_LEAGUES", "NBA,NFL")
    # Seconds between PrizePicks projection polls (default 5 min).
    PRIZEPICKS_POLL_INTERVAL: int = int(os.environ.get("PRIZEPICKS_POLL_INTERVAL", "300"))
    # Minimum edge % vs sportsbook fair probability to trigger a PP alert.
    MIN_PP_EDGE: float = float(os.environ.get("MIN_PP_EDGE", "5.0"))
    # Minimum fair probability at the PP line to alert (filters razor-thin lines).
    MIN_PP_FAIR_PROB: float = float(os.environ.get("MIN_PP_FAIR_PROB", "0.55"))
    # Dedup window: suppress repeat PP alerts for the same player/stat.
    PP_DEDUP_WINDOW: int = int(os.environ.get("PP_DEDUP_WINDOW", "3600"))  # 60 min

    # ── Sports to monitor (comma-separated Sport enum values) ─────────────────
    # Default: NFL, NBA, MLB — edit via ACTIVE_SPORTS env var
    ACTIVE_SPORTS_RAW: str = os.environ.get("ACTIVE_SPORTS", "NFL,NBA,MLB")

    # ── Alert thresholds ──────────────────────────────────────────────────────
    # Known sharp / respected sportsbooks (comma-separated).
    # Moves at these books carry higher steam conviction.
    SHARP_BOOKS_RAW: str = os.environ.get(
        "SHARP_BOOKS",
        "Pinnacle,Pinnacle Sports,Circa,Circa Sports,Bookmaker,"
        "Bookmaker.eu,Heritage Sports,Heritage,BetOnline,BetOnline.ag,CRIS,5Dimes",
    )

    # Deduplication windows (seconds): suppress repeat alerts for the same
    # event/selection within this window after an alert has been sent.
    EV_DEDUP_WINDOW: int    = int(os.environ.get("EV_DEDUP_WINDOW",    "1800"))  # 30 min
    STEAM_DEDUP_WINDOW: int = int(os.environ.get("STEAM_DEDUP_WINDOW", "3600"))  # 60 min

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

    @property
    def prizepicks_leagues(self) -> list[str]:
        """PrizePicks league names to monitor (e.g. ["NBA", "NFL"])."""
        return [lg.strip() for lg in self.PRIZEPICKS_LEAGUES_RAW.split(",") if lg.strip()]

    @property
    def active_sports(self) -> list[str]:
        """List of Sport enum values to monitor for live odds."""
        return [s.strip() for s in self.ACTIVE_SPORTS_RAW.split(",") if s.strip()]

    @property
    def sharp_books(self) -> frozenset[str]:
        """Set of known sharp / respected sportsbook names."""
        return frozenset(
            b.strip() for b in self.SHARP_BOOKS_RAW.split(",") if b.strip()
        )


# Singleton instance used throughout the project
config = Config()
