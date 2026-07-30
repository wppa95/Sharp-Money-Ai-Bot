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
    # Default: every sport with a verified Odds API key, except NFL
    # (not monitored by default — re-enable via the env var below).
    # Override via the ACTIVE_SPORTS env var (e.g. "NFL,NBA,MLB").
    # Note: "Soccer" is a legacy alias for EPL — don't activate it alongside
    # "EPL" or the EPL feed will be fetched twice.
    ACTIVE_SPORTS_RAW: str = os.environ.get(
        "ACTIVE_SPORTS",
        "NBA,MLB,WNBA,NHL,NCAAF,NCAAB,UFC,"
        "EPL,LaLiga,SerieA,Bundesliga,Ligue1,MLS,UCL",
    )

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

    # ── Multi-platform connector settings ────────────────────────────────────
    # Enable/disable individual connectors via env vars
    DRAFTKINGS_ENABLED: bool = os.environ.get("DRAFTKINGS_ENABLED", "true").lower() == "true"
    FANDUEL_ENABLED:    bool = os.environ.get("FANDUEL_ENABLED",    "true").lower() == "true"
    UNDERDOG_ENABLED:   bool = os.environ.get("UNDERDOG_ENABLED",   "true").lower() == "true"

    # Polling intervals for multi-platform connectors (seconds)
    CONNECTOR_POLL_INTERVAL:  int = int(os.environ.get("CONNECTOR_POLL_INTERVAL",  "90"))
    CONSENSUS_CHECK_INTERVAL: int = int(os.environ.get("CONSENSUS_CHECK_INTERVAL", "120"))
    CLV_CHECK_INTERVAL:       int = int(os.environ.get("CLV_CHECK_INTERVAL",       "300"))
    UNDERDOG_POLL_INTERVAL:   int = int(os.environ.get("UNDERDOG_POLL_INTERVAL",   "300"))

    # Consensus engine thresholds
    # Minimum books for cross-book consensus computation
    CONSENSUS_MIN_BOOKS: int = int(os.environ.get("CONSENSUS_MIN_BOOKS", "2"))
    # American-odds deviation from consensus to flag as market inefficiency
    INEFFICIENCY_THRESHOLD: int = int(os.environ.get("INEFFICIENCY_THRESHOLD", "10"))
    # Minimum inefficiency deviation to trigger an alert
    MIN_INEFFICIENCY_DEVIATION: int = int(os.environ.get("MIN_INEFFICIENCY_DEVIATION", "10"))

    # CLV thresholds
    # Minimum current-vs-projected-close lead (American odds cents) to fire alert
    MIN_CLV_LEAD: int = int(os.environ.get("MIN_CLV_LEAD", "8"))
    # Minimum CLV% for a historical CLV result to be surfaced
    MIN_CLV_PCT: float = float(os.environ.get("MIN_CLV_PCT", "1.0"))

    # Underdog thresholds
    # Minimum absolute line change (units) to trigger an alert
    MIN_UNDERDOG_LINE_CHANGE: float = float(os.environ.get("MIN_UNDERDOG_LINE_CHANGE", "0.5"))

    # Dedup windows for new alert types (seconds)
    INEFFICIENCY_DEDUP_WINDOW: int = int(os.environ.get("INEFFICIENCY_DEDUP_WINDOW", "1800"))
    CLV_DEDUP_WINDOW:          int = int(os.environ.get("CLV_DEDUP_WINDOW",           "3600"))

    # ── Season / market-status check ─────────────────────────────────────────
    # How often to refresh the /v4/sports active-market cache (seconds).
    # Default: 3600 (1 hour).  Set to 0 to disable automatic skipping.
    SEASON_CHECK_INTERVAL: int = int(os.environ.get("SEASON_CHECK_INTERVAL", "3600"))

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
