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
    # Minimum seconds between any two bet-pick alerts for the same prop.
    # Prevents rapid line reversals (e.g. 0.5→1.5→0.5) from each triggering a
    # separate Telegram notification.  Set to 0 to disable.
    UD_FLIP_COOLDOWN: int = int(os.environ.get("UD_FLIP_COOLDOWN", "600"))  # 10 min
    # Sleeper Stats API integration.
    # When enabled, NFL player stats from api.sleeper.app are fetched as a
    # supplement to ESPN gamelog data. One NFL game per week maps cleanly to
    # per-game results, improving hit-rate accuracy for NFL props.
    # Set UD_SLEEPER_ENABLED=false to disable without code changes.
    UD_SLEEPER_ENABLED: bool = os.environ.get("UD_SLEEPER_ENABLED", "true").lower() not in (
        "false", "0", "no", "off"
    )

    # ── Multi-platform connector settings ────────────────────────────────────
    # Enable/disable individual connectors via env vars
    # Temporarily disabled pending full Underdog pipeline validation (doc #9).
    # Re-enable by setting DRAFTKINGS_ENABLED=true / FANDUEL_ENABLED=true env vars.
    DRAFTKINGS_ENABLED: bool = os.environ.get("DRAFTKINGS_ENABLED", "false").lower() == "true"
    FANDUEL_ENABLED:    bool = os.environ.get("FANDUEL_ENABLED",    "false").lower() == "true"
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
    # 3★ = B-tier (moderate confidence, sufficient data quality).
    # B-tier passes only when the per-tier confidence gate (UD_MIN_CONF_B) also clears.
    # Set to 1 to disable star gating entirely; set to 4 to restore A-tier-only behaviour.
    UD_MIN_STARS_TO_ALERT:    int   = int(os.environ.get("UD_MIN_STARS_TO_ALERT", "3"))
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
    #   MLB    — MLB Stats API  (statsapi.mlb.com)
    #   WNBA   — ESPN gamelog   (site.api.espn.com)
    #   NHL    — NHL public API (api-web.nhle.com + api.nhle.com) — free, no key
    #
    # Tracking only (data collected + scored, no Telegram bet alerts):
    #   NBA, NFL — available; muted via ALERT_DISABLED_SPORTS.
    #
    # Supported with real historical data:
    #   MLB    → MLB Stats API (statsapi.mlb.com) — free, no key
    #   WNBA   → ESPN gamelog (site.api.espn.com) — free, no key
    #   NFL    → ESPN gamelog + Sleeper supplement — free, no key
    #   NBA    → ESPN gamelog — free, no key (alerts muted by default)
    #   NHL    → NHL official public API (api-web.nhle.com) — free, no key ✓ verified
    #   DOTA   → OpenDota API (api.opendota.com) — free, no key
    #   TENNIS → JeffSackmann ATP/WTA CSV (github.com/JeffSackmann) — free, no key
    #   CS     → PandaScore API — requires PANDASCORE_API_KEY env var;
    #            returns [] gracefully without it (alerts suppressed by decision engine)
    #
    #   SOCCER → football-data.org API provider is built (bot/providers/soccer_stats.py)
    #            but NOT in the default.  To enable add SOCCER to UD_ALERT_SPORTS env var
    #            AND set FOOTBALL_DATA_API_KEY (free token: football-data.org/client/register).
    #            Disabled by default because the free tier has no lineup/appearance data;
    #            without it the provider cannot distinguish a DNP (injury/bench) from a
    #            zero-stat game, which would produce invalid hit rates.
    #
    # No free provider available (self-suppress via PASS decision):
    #   COD  — no public per-game player stat API exists
    #   LOL  — Riot Games API requires a developer key
    #   NPB/KBO — no accessible public per-game stat API
    UD_ALERT_SPORTS_RAW: str = os.environ.get(
        "UD_ALERT_SPORTS",
        "MLB,WNBA,NFL,NBA,DOTA,TENNIS,CS,NHL,LOL,MMA,GOLF,NCAAF,SOCCER,VALORANT,TT,BADMINTON",
    )

    # ── Sport priority system ─────────────────────────────────────────────────
    # Tier 1 sports are prioritized for alert delivery.  They pass S, A, B, and
    # C-tier alerts.  All other sports (including MLB) follow their own rules.
    # Override via UD_TIER1_SPORTS env var (comma-separated sport codes).
    UD_TIER1_SPORTS_RAW: str = os.environ.get(
        "UD_TIER1_SPORTS",
        "NBA,WNBA,CS,TENNIS,DOTA,LOL,VALORANT,TT,BADMINTON,GOLF,NFL,NCAAF,MMA,SOCCER",
    )

    # MLB alert gate — MLB only sends Telegram alerts when the tier is at or
    # above this value.  "S" = S-tier only (default).  "A" allows A and S.
    # "B" allows B, A, S.  Set to "" to disable the MLB restriction.
    UD_MLB_MIN_TIER: str = os.environ.get("UD_MLB_MIN_TIER", "S")

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

    # ── Per-tier confidence minimums ─────────────────────────────────────────
    # Alerts for each tier only fire when decision.confidence ≥ the minimum
    # for that tier.  Set all to 0 to disable (score-tier gate still applies).
    UD_MIN_CONF_S: int = int(os.environ.get("UD_MIN_CONF_S", "80"))
    UD_MIN_CONF_A: int = int(os.environ.get("UD_MIN_CONF_A", "70"))
    UD_MIN_CONF_B: int = int(os.environ.get("UD_MIN_CONF_B", "55"))

    # ── Alert sport suppression ───────────────────────────────────────────────
    # Sports whose Telegram alerts are temporarily suppressed.
    # Data collection, scoring, and DB writes continue for all suppressed sports.
    # Set ALERT_DISABLED_SPORTS="" to re-enable all sports (current default).
    # Set ALERT_DISABLED_SPORTS="NFL,NBA" to suppress specific sports.
    # NBA and NFL are now Tier 1 priority sports — enabled by default.
    ALERT_DISABLED_SPORTS_RAW: str = os.environ.get("ALERT_DISABLED_SPORTS", "")

    # ── Learning / model update flag ──────────────────────────────────────────
    # When True, learning rollups are surfaced in /rollups output and future
    # weight-adjustment logic is enabled.  Default False — off until validated.
    ENABLE_LEARNING_UPDATES: bool = (
        os.environ.get("ENABLE_LEARNING_UPDATES", "false").lower() == "true"
    )

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
    def ud_tier1_sports(self) -> frozenset[str]:
        """Tier 1 priority sports — pass S/A/B/C tier alerts without restrictions."""
        return frozenset(s.strip() for s in self.UD_TIER1_SPORTS_RAW.split(",") if s.strip())

    @property
    def ud_mlb_alert_tiers(self) -> frozenset[str]:
        """
        Which decision tiers can generate MLB Telegram alerts.

        Controlled by UD_MLB_MIN_TIER:
          "S" → only S-tier (default — prevents MLB from dominating alert volume)
          "A" → S and A
          "B" → S, A, and B
          ""  → no restriction (same as all other sports)
        """
        min_tier = (self.UD_MLB_MIN_TIER or "").upper().strip()
        _all = ("S", "A", "B", "C")
        if not min_tier or min_tier not in _all:
            return frozenset(_all)
        idx = _all.index(min_tier)
        return frozenset(_all[:idx + 1])

    @property
    def alert_disabled_sports(self) -> frozenset[str]:
        """
        Sports whose Telegram alerts are temporarily suppressed (uppercased).

        Data collection, scoring, and DB writes are unaffected — only the
        Telegram broadcast is skipped for matching sports.  Re-enable a sport
        by removing it from the ALERT_DISABLED_SPORTS env var.
        """
        return frozenset(
            s.strip().upper()
            for s in self.ALERT_DISABLED_SPORTS_RAW.split(",")
            if s.strip()
        )

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
