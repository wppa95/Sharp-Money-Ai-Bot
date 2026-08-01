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
    # PandaScore API key for CS2 player stats.  Free tier available at
    # pandascore.co — set PANDASCORE_API_KEY env var to activate CS2 alerts.
    # CS2 props gracefully return [] without this key (decision engine PASS).
    PANDASCORE_API_KEY: str = os.environ.get("PANDASCORE_API_KEY", "")

    # ── PrizePicks monitoring ─────────────────────────────────────────────────
    # Leagues to monitor (comma-separated PrizePicks league names).
    PRIZEPICKS_LEAGUES_RAW: str = os.environ.get("PRIZEPICKS_LEAGUES", "NBA,MLB")
    # Seconds between PrizePicks projection polls (default 5 min).
    PRIZEPICKS_POLL_INTERVAL: int = int(os.environ.get("PRIZEPICKS_POLL_INTERVAL", "300"))
    # Minimum edge % vs sportsbook fair probability to trigger a PP alert.
    MIN_PP_EDGE: float = float(os.environ.get("MIN_PP_EDGE", "5.0"))
    # Minimum fair probability at the PP line to alert (filters razor-thin lines).
    MIN_PP_FAIR_PROB: float = float(os.environ.get("MIN_PP_FAIR_PROB", "0.55"))
    # Dedup window: suppress repeat PP alerts for the same player/stat.
    PP_DEDUP_WINDOW: int = int(os.environ.get("PP_DEDUP_WINDOW", "3600"))  # 60 min
    # Minimum PP line change (in units) to log as a movement signal.
    MIN_PP_LINE_CHANGE: float = float(os.environ.get("MIN_PP_LINE_CHANGE", "0.5"))

    # ── Player prop market fetching ───────────────────────────────────────────
    # Sports for which player-prop odds are fetched from The Odds API and stored
    # in odds_records so the PP crossmatch pipeline can find sportsbook matches.
    # Priority 1: NBA, MLB.  Add soccer leagues (e.g. "NBA,MLB,EPL,MLS") for
    # Priority 2.  NFL excluded by default.
    PLAYER_PROP_SPORTS_RAW: str = os.environ.get("PLAYER_PROP_SPORTS", "MLB,NBA,WNBA,NFL")
    # Seconds between player-prop poll cycles.  Props move slower than game
    # lines so a 10-minute interval is sufficient and keeps credit usage low.
    PLAYER_PROP_POLL_INTERVAL: int = int(os.environ.get("PLAYER_PROP_POLL_INTERVAL", "600"))
    # Seconds between pregame market watch cycles (continuous, all-day).
    # Each cycle runs morning_scan + pregame_scan for all watched entries.
    PREGAME_SCAN_INTERVAL: int = int(os.environ.get("PREGAME_SCAN_INTERVAL", "300"))

    # ── Sports to monitor (comma-separated Sport enum values) ─────────────────
    # Default: MLB only — the only sport whose alerts can be delivered given
    # the current scope rules (MLB Moneyline + MLB Totals on DK/FD).
    # Fetching other sports wastes Odds API quota without producing any alerts.
    # Add sports here only when their alert delivery is also enabled in
    # alert_scope_filter.py — e.g. set ACTIVE_SPORTS=NBA,MLB when NBA is
    # in-season and NBA alerts are approved.
    # Note: "Soccer" is a legacy alias for EPL — don't activate it alongside
    # "EPL" or the EPL feed will be fetched twice.
    ACTIVE_SPORTS_RAW: str = os.environ.get(
        "ACTIVE_SPORTS",
        "MLB",
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
    # Underdog player-prop alert dedup window.
    # Within this window, a second alert for the same player/sport/stat is only
    # sent when the line moves by ≥ MIN_UNDERDOG_LINE_CHANGE (default 0.5 units).
    # Set to 0 to disable time-based dedup (line-only dedup still applies).
    UD_ALERT_DEDUP_WINDOW: int = int(os.environ.get("UD_ALERT_DEDUP_WINDOW", "3600"))  # 60 min

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
    # Minimum absolute line change (units) to pass through to the scoring layer.
    # The grading model then applies a separate quality gate (UD_MIN_STARS_TO_ALERT).
    MIN_UNDERDOG_LINE_CHANGE: float = float(os.environ.get("MIN_UNDERDOG_LINE_CHANGE", "0.5"))
    # Minimum star rating (1–5) required to send an Underdog alert.
    # 3★ corresponds to a score of 55+ (B-tier or better).  Set to 1 to disable.
    UD_MIN_STARS_TO_ALERT:    int   = int(os.environ.get("UD_MIN_STARS_TO_ALERT", "4"))
    # New-prop alert: used for DB qualification tracking (summary inclusion gate).
    # Props at or below this line appear in the end-of-cycle summary regardless
    # of score.  Set to 0.0 to use score-only qualification.
    UD_NEW_PROP_LOW_LINE_THRESHOLD: float = float(
        os.environ.get("UD_NEW_PROP_LOW_LINE_THRESHOLD", "1.0")
    )
    # Strict threshold for IMMEDIATE individual Telegram alerts on new props.
    # Only props at or below this line get a 🚨 PROP LIVE message right away.
    # All other new props are collected into the end-of-cycle digest instead.
    UD_NEW_PROP_IMMEDIATE_LINE_THRESHOLD: float = float(
        os.environ.get("UD_NEW_PROP_IMMEDIATE_LINE_THRESHOLD", "0.5")
    )
    # Minimum DB snapshots required before a prop is allowed an immediate
    # individual alert.  Props with fewer than this many snapshots are sent
    # to the digest only, regardless of line or score.  Prevents "first
    # appearance = alert" for props with no performance evidence.
    UD_VALIDATION_MIN_SAMPLES: int = int(
        os.environ.get("UD_VALIDATION_MIN_SAMPLES", "5")
    )
    # Stat categories that always trigger an immediate individual alert when a
    # new prop is first seen, regardless of line value.  Comma-separated via env
    # var UD_PRIORITY_STAT_CATEGORIES to override.
    UD_PRIORITY_STAT_CATEGORIES: frozenset = field(default_factory=lambda: frozenset(
        s.strip()
        for s in os.environ.get(
            "UD_PRIORITY_STAT_CATEGORIES",
            "Home Runs,Strikeouts,Passing Yards,Rushing Yards,Receiving Yards,"
            "Touchdowns,Points,Rebounds,Assists,3-Pointers,Goals,Shots on Goal,"
            "Rebounds + Assists,Points + Rebounds + Assists",
        ).split(",")
        if s.strip()
    ))

    # Sports whose Underdog bet alerts are delivered to Telegram.
    # ─────────────────────────────────────────────────────────────────────────
    # Primary (real result data available → OVER/UNDER picks enabled):
    #   MLB   — MLB Stats API  (statsapi.mlb.com)
    #   WNBA  — ESPN gamelog   (site.api.espn.com)
    #
    # Tracking only (data collected + scored, no Telegram bet alerts):
    #   NBA, NFL — ESPN gamelog available but user-configured as tracking only.
    #
    # Supported with real historical data:
    #   MLB    → MLB Stats API (statsapi.mlb.com)
    #   WNBA   → ESPN gamelog
    #   DOTA   → OpenDota API (api.opendota.com) — free, no key required
    #   TENNIS → JeffSackmann ATP/WTA CSV (github.com/JeffSackmann) — free, no key
    #   CS     → PandaScore API — requires PANDASCORE_API_KEY env var;
    #            returns [] gracefully without it (alerts suppressed by decision engine)
    #
    # Still unsupported (self-suppress via PASS decision):
    #   Soccer, NPB, KBO — no public per-game stat API integrated.
    UD_ALERT_SPORTS_RAW: str = os.environ.get("UD_ALERT_SPORTS", "MLB,WNBA,DOTA,TENNIS,CS")

    # Dedup windows for new alert types (seconds)
    INEFFICIENCY_DEDUP_WINDOW: int = int(os.environ.get("INEFFICIENCY_DEDUP_WINDOW", "1800"))
    CLV_DEDUP_WINDOW:          int = int(os.environ.get("CLV_DEDUP_WINDOW",           "3600"))

    # ── Season / market-status check ─────────────────────────────────────────
    # How often to refresh the /v4/sports active-market cache (seconds).
    # Default: 3600 (1 hour).  Set to 0 to disable automatic skipping.
    SEASON_CHECK_INTERVAL: int = int(os.environ.get("SEASON_CHECK_INTERVAL", "3600"))

    # ── Odds API shared cache ─────────────────────────────────────────────────
    # TTL for the shared Odds API response cache (seconds).  Default 55 s
    # keeps the cache warm inside the 90 s connector poll cycle.
    # DraftKings and FanDuel share one API call per sport per TTL window,
    # cutting Odds API quota usage by ~50%.
    ODDS_API_CACHE_TTL: int = int(os.environ.get("ODDS_API_CACHE_TTL", "55"))

    # ── API budget management ─────────────────────────────────────────────────
    # Monthly Odds API request cap.  The Odds API free tier allows 500
    # requests/month.  Set to 0 to disable budget enforcement entirely.
    # Telegram warnings fire at 75 %, 90 %, and 100 % of this value.
    ODDS_API_MONTHLY_BUDGET: int = int(os.environ.get("ODDS_API_MONTHLY_BUDGET", "500"))

    # ── Alert limits ─────────────────────────────────────────────────────────
    # Maximum PrizePicks A/B-tier alerts sent per calendar day (UTC).
    # S-tier always bypasses this cap.  Set to 0 to disable.
    DAILY_ALERT_LIMIT: int = int(os.environ.get("DAILY_ALERT_LIMIT", "20"))
    # Maximum Underdog alerts sent per calendar day (UTC).  Set to 0 to disable.
    # Default is 0 (unlimited) — the scoring layer is the primary quality gate.
    # Set DAILY_UNDERDOG_LIMIT env var to a positive integer to re-enable.
    DAILY_UNDERDOG_LIMIT: int = int(os.environ.get("DAILY_UNDERDOG_LIMIT", "0"))

    # ── Game timing filter ───────────────────────────────────────────────────
    # Block alerts for games that start sooner than this (minutes).
    # Exception: allows through if edge ≥ URGENT_EDGE_THRESHOLD.
    ALERT_WINDOW_MIN_MINUTES: int = int(os.environ.get("ALERT_WINDOW_MIN_MINUTES", "30"))
    # Block alerts for games that start later than this (minutes from now).
    ALERT_WINDOW_MAX_MINUTES: int = int(os.environ.get("ALERT_WINDOW_MAX_MINUTES", "120"))
    # Edge % at or above which an alert bypasses the min-minutes gate
    # (i.e. "urgent" edge can fire even if game is <30 min away).
    URGENT_EDGE_THRESHOLD: float = float(os.environ.get("URGENT_EDGE_THRESHOLD", "8.0"))

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
    def ud_alert_sports(self) -> frozenset[str]:
        """Sports for which Underdog bet alerts are delivered (others: tracking only)."""
        return frozenset(s.strip() for s in self.UD_ALERT_SPORTS_RAW.split(",") if s.strip())

    @property
    def prizepicks_leagues(self) -> list[str]:
        """PrizePicks league names to monitor (e.g. ["NBA", "NFL"])."""
        return [lg.strip() for lg in self.PRIZEPICKS_LEAGUES_RAW.split(",") if lg.strip()]

    @property
    def player_prop_sports(self) -> list[str]:
        """Sport enum values for which player-prop odds are fetched."""
        return [s.strip() for s in self.PLAYER_PROP_SPORTS_RAW.split(",") if s.strip()]

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
