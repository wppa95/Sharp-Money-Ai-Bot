"""
Database module — async SQLAlchemy + SQLite via aiosqlite.
Stores odds history, alerts, and EV opportunities for historical tracking.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float,
    Integer, String, Text,
    UniqueConstraint,
    select, func, desc, text, delete
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


def _tier_from_confidence(ai_confidence: int) -> str:
    """Map an ai_confidence score (0-100) to a tier label for CLV seeding."""
    if ai_confidence >= 95:
        return "S"
    if ai_confidence >= 85:
        return "A"
    if ai_confidence >= 75:
        return "B"
    return "PASS"


# ── ORM base ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── ORM models ────────────────────────────────────────────────────────────────

class OddsRecord(Base):
    __tablename__ = "odds_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sportsbook = Column(String(64), nullable=False, index=True)
    sport = Column(String(32), nullable=False, index=True)
    market_type = Column(String(32), nullable=False)
    event = Column(String(256), nullable=False)
    selection = Column(String(256), nullable=False)
    american_odds = Column(Integer, nullable=False)
    line = Column(Float, nullable=True)
    event_start = Column(DateTime, nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SteamRecord(Base):
    __tablename__ = "steam_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String(32), nullable=False)
    sport = Column(String(32), nullable=False, index=True)
    market_type = Column(String(32), nullable=False)
    event = Column(String(256), nullable=False)
    selection = Column(String(256), nullable=False)
    opening_odds = Column(Integer, nullable=False)
    current_odds = Column(Integer, nullable=False)
    steam_score = Column(Integer, nullable=False)
    steam_direction = Column(String(8), nullable=False)
    books_moved = Column(Text, nullable=False, default="")   # comma-separated
    notes = Column(Text, nullable=False, default="")
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    alert_sent = Column(Boolean, default=False, nullable=False)


class EVRecord(Base):
    __tablename__ = "ev_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sport = Column(String(32), nullable=False, index=True)
    market_type = Column(String(32), nullable=False)
    event = Column(String(256), nullable=False)
    player = Column(String(128), nullable=True)
    selection = Column(String(256), nullable=False)
    line = Column(Float, nullable=True)
    best_odds = Column(Integer, nullable=False)
    best_book = Column(String(64), nullable=False)
    fair_probability = Column(Float, nullable=False)
    expected_value = Column(Float, nullable=False)
    steam_score = Column(Integer, nullable=False)
    ai_confidence = Column(Integer, nullable=False)
    recommendation = Column(String(32), nullable=False)
    stars = Column(Integer, nullable=False)
    reason_codes = Column(Text, nullable=False, default="")  # comma-separated
    result = Column(String(16), nullable=True)               # WIN / LOSS / PUSH / PENDING
    clv = Column(Float, nullable=True)                       # Closing Line Value
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    alert_sent = Column(Boolean, default=False, nullable=False)


# ── PrizePicks models ─────────────────────────────────────────────────────────

class PrizePicksRecord(Base):
    """Raw PrizePicks projection — stores line history over time."""
    __tablename__ = "prizepicks_records"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    external_id      = Column(String(64),  nullable=False, index=True)
    player_name      = Column(String(128), nullable=False, index=True)
    team             = Column(String(64),  nullable=False, default="")
    sport            = Column(String(32),  nullable=False, index=True)
    stat_type        = Column(String(64),  nullable=False)
    line_value       = Column(Float,       nullable=False)
    start_time       = Column(DateTime,    nullable=True)
    game_description = Column(String(256), nullable=False, default="")
    fetched_at       = Column(DateTime,    default=datetime.utcnow, nullable=False)


class PPEdgeRecord(Base):
    """Detected PrizePicks edge opportunity vs sportsbook fair odds."""
    __tablename__ = "pp_edge_records"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    player_name     = Column(String(128), nullable=False, index=True)
    team            = Column(String(64),  nullable=False, default="")
    sport           = Column(String(32),  nullable=False, index=True)
    stat_type       = Column(String(64),  nullable=False)
    pp_line_value   = Column(Float,       nullable=False)
    sportsbook      = Column(String(64),  nullable=False)
    sb_line_value   = Column(Float,       nullable=False)
    sb_over_odds    = Column(Integer,     nullable=False)
    sb_under_odds   = Column(Integer,     nullable=False)
    fair_prob_over  = Column(Float,       nullable=False)
    fair_prob_under = Column(Float,       nullable=False)
    edge_over       = Column(Float,       nullable=False)
    edge_under      = Column(Float,       nullable=False)
    best_side       = Column(String(8),   nullable=False)   # "OVER" | "UNDER"
    best_edge       = Column(Float,       nullable=False)
    alert_sent      = Column(Boolean,     default=False, nullable=False)
    detected_at     = Column(DateTime,    default=datetime.utcnow, nullable=False)
    # ── outcome tracking ──────────────────────────────────────────────────────
    tier            = Column(String(16),  nullable=True)    # AlertTier value
    confidence      = Column(Float,       nullable=True)    # 0–100
    result          = Column(String(16),  nullable=True, default="PENDING")
    # ── line movement tracking ────────────────────────────────────────────────
    opening_line    = Column(Float,       nullable=True)    # first ever line seen
    prev_line       = Column(Float,       nullable=True)    # line from prior record
    # ── timing ───────────────────────────────────────────────────────────────
    game_time       = Column(DateTime,    nullable=True)    # UTC game start time


# ── Multi-platform market snapshot records ────────────────────────────────────

class MarketSnapshotRecord(Base):
    """Normalized cross-platform market snapshot — one record per odds fetch."""
    __tablename__ = "market_snapshots"

    id           = Column(Integer,  primary_key=True, autoincrement=True)
    sportsbook   = Column(String(64),  nullable=False, index=True)
    sport        = Column(String(32),  nullable=False, index=True)
    league       = Column(String(32),  nullable=False, default="")
    event        = Column(String(256), nullable=False)
    market_type  = Column(String(32),  nullable=False)
    selection    = Column(String(256), nullable=False)
    player       = Column(String(128), nullable=True)
    team         = Column(String(64),  nullable=True)
    line         = Column(Float,       nullable=True)
    odds         = Column(Integer,     nullable=False, default=0)
    opening_odds = Column(Integer,     nullable=True)
    is_pickem    = Column(Boolean,     default=False, nullable=False)
    game_time    = Column(DateTime,    nullable=True)
    recorded_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)
    alert_sent   = Column(Boolean,     default=False, nullable=False)


class CLVRecord(Base):
    """Closing Line Value result for an alerted opportunity."""
    __tablename__ = "clv_records"

    id                     = Column(Integer,  primary_key=True, autoincrement=True)
    selection              = Column(String(256), nullable=False, index=True)
    event                  = Column(String(256), nullable=False)
    sport                  = Column(String(32),  nullable=False)
    bet_odds               = Column(Integer,     nullable=False)
    closing_odds           = Column(Integer,     nullable=False)
    clv_pct                = Column(Float,       nullable=False)
    clv_proxy              = Column(Integer,     nullable=False)
    fair_prob_bet          = Column(Float,       nullable=True)
    fair_prob_close        = Column(Float,       nullable=True)
    counterpart_bet_odds   = Column(Integer,     nullable=True)
    counterpart_close_odds = Column(Integer,     nullable=True)
    notes                  = Column(Text,        nullable=False, default="")
    computed_at            = Column(DateTime,    default=datetime.utcnow, nullable=False)


class UnderdogSnapshotRecord(Base):
    """Raw Underdog Fantasy pick'em projection snapshot."""
    __tablename__ = "underdog_snapshots"

    id          = Column(Integer,  primary_key=True, autoincrement=True)
    external_id = Column(String(64),  nullable=False, index=True)
    player_name = Column(String(128), nullable=False, index=True)
    team        = Column(String(64),  nullable=False, default="")
    sport       = Column(String(32),  nullable=False, index=True)
    stat_type   = Column(String(64),  nullable=False)
    line_value  = Column(Float,       nullable=False)
    game_id     = Column(String(64),  nullable=False, default="")
    game_time   = Column(DateTime,    nullable=True)
    line_moved  = Column(Boolean,     default=False, nullable=False)
    prev_line   = Column(Float,       nullable=True)
    # line_delta: new_line - prev_line; positive = line went up, negative = down
    line_delta  = Column(Float,       nullable=True)
    removed     = Column(Boolean,     default=False, nullable=False)
    alert_sent  = Column(Boolean,     default=False, nullable=False)
    # Scoring fields — populated for line-change props that reach the scoring gate
    score_total = Column(Float,       nullable=True)   # composite score (0-100)
    score_tier  = Column(String(8),   nullable=True)   # S / A / B / PASS
    score_stars = Column(Integer,     nullable=True)   # 0-5
    # Delivery outcome: "sent" | "filtered:<reason>" | "skipped" | "failed"
    alert_outcome   = Column(String(64), nullable=True)
    # Validation metrics snapshot — compact JSON produced by player_validator.
    # Keys: n, l5, l10, l20, l30, avg, min, rate_below, season, h2h, has_data
    validation_json = Column(Text,       nullable=True)
    # Betting decision — produced by ud_bet_decision for qualified props
    bet_recommendation = Column(String(8), nullable=True)    # OVER | UNDER | PASS
    bet_confidence     = Column(Integer,   nullable=True)    # 0–95
    bet_reason         = Column(Text,      nullable=True)    # human-readable explanation
    bet_evidence_json  = Column(Text,      nullable=True)    # compact JSON evidence blob
    fetched_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)


class PropLineHistory(Base):
    """
    Provider-agnostic player prop line history.

    Stores normalized PlayerProp snapshots from any pick'em provider
    (PrizePicks, Underdog, or future sources) for line-movement tracking
    and comparison-engine input.

    Designed to complement (not replace) the provider-specific tables
    (prizepicks_records, underdog_snapshots) — this is the shared layer
    that future providers will write to directly.
    """
    __tablename__ = "prop_line_history"

    id          = Column(Integer,  primary_key=True, autoincrement=True)
    provider    = Column(String(64),  nullable=False, index=True)   # "PrizePicks" | "Underdog" | …
    sport       = Column(String(32),  nullable=False, index=True)
    player_name = Column(String(128), nullable=False, index=True)
    team        = Column(String(64),  nullable=False, default="")
    stat_type   = Column(String(64),  nullable=False)
    line_value  = Column(Float,       nullable=False)
    game_time   = Column(DateTime,    nullable=True)
    external_id = Column(String(64),  nullable=False, default="")
    game_id     = Column(String(64),  nullable=False, default="")
    fetched_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)
    # Lifecycle columns (added via migration for existing databases)
    first_seen         = Column(DateTime,   nullable=True)
    last_seen          = Column(DateTime,   nullable=True)
    change_count       = Column(Integer,    default=0,            nullable=True)
    prev_line          = Column(Float,      nullable=True)
    removed            = Column(Boolean,    default=False,        nullable=True)
    # Alert lifecycle state — "DISCOVERED" | "ACTIVE_ALERTED" | "REMOVED"
    lifecycle_state    = Column(String(16), default="DISCOVERED", nullable=True)
    first_alert_sent_at = Column(DateTime, nullable=True)
    # Opening line — set once on first INSERT, never updated.
    # Enables opening-vs-current movement display in alerts.
    opening_line       = Column(Float,     nullable=True)
    # Qualification tier synced from the source provider snapshot.
    # NULL = not yet scored.  S / A / B = qualifying.  PASS = excluded from picks.
    score_tier         = Column(String(8), nullable=True)
    # Bet direction synced from UnderdogSnapshotRecord — OVER | UNDER | PASS | NULL
    bet_recommendation = Column(String(8), nullable=True)
    bet_confidence     = Column(Integer,   nullable=True)


class AlertCLVSeed(Base):
    """
    CLV seed record — captures alert odds at fire time for later CLV computation.

    When an alert is sent, a seed is created with the odds at that moment.
    After the event closes, a harvester job reads these seeds, fetches
    closing odds (via OddsAPI or manual entry), computes CLV, and stores
    the result in clv_records.

    ``clv_computed`` is False until the harvest job processes the seed.
    Seeds for events whose game_time has not yet passed are skipped.
    """
    __tablename__ = "alert_clv_seeds"

    id               = Column(Integer,     primary_key=True, autoincrement=True)
    # Source linkage
    source_table     = Column(String(32),  nullable=False)     # "ev_records" | "underdog_snapshots" | "steam_records"
    source_id        = Column(Integer,     nullable=False, index=True)
    # Alert metadata
    alert_type       = Column(String(32),  nullable=False)     # "EV" | "STEAM" | "UNDERDOG" | "PP"
    sport            = Column(String(32),  nullable=False, index=True)
    market_type      = Column(String(64),  nullable=False, default="")
    event            = Column(String(256), nullable=False, default="")
    selection        = Column(String(256), nullable=False, default="")
    # Odds at alert time
    bet_odds         = Column(Integer,     nullable=True)      # American odds (best_odds for EV)
    counterpart_odds = Column(Integer,     nullable=True)      # opposing side odds
    tier             = Column(String(16),  nullable=True)      # "S" | "A" | "B" | "PASS" | "High" | …
    # Timing
    game_time        = Column(DateTime,    nullable=True)      # UTC game start — when to harvest
    alerted_at       = Column(DateTime,    default=datetime.utcnow, nullable=False)
    # CLV result (populated by harvest job)
    clv_pct          = Column(Float,       nullable=True)      # computed CLV%; None until harvested
    clv_computed     = Column(Boolean,     default=False, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("source_table", "source_id", name="uq_alert_clv_seed_source"),
    )


class PlayerGameResult(Base):
    """
    Per-game stat result for one player × sport × stat_type combination.

    Populated by ``providers.player_stats.PlayerStatsProvider`` from free public
    APIs (MLB Stats API, ESPN unofficial gamelog endpoint).  Used by
    ``engine.player_results.compute_hit_rates()`` to build L5/L10/L20/L30/Season
    and H2H windows for the betting decision engine.

    Unique on (player_name, sport, stat_type, game_date) — re-upserted when the
    API reports a corrected boxscore.
    """
    __tablename__ = "player_game_results"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    player_name  = Column(String(128), nullable=False, index=True)
    sport        = Column(String(32),  nullable=False, index=True)
    stat_type    = Column(String(64),  nullable=False)
    game_date    = Column(String(16),  nullable=False)   # "YYYY-MM-DD"
    opponent     = Column(String(128), nullable=True)
    actual_value = Column(Float,       nullable=False)
    source       = Column(String(32),  nullable=False, default="api")
    fetched_at   = Column(DateTime,    default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "player_name", "sport", "stat_type", "game_date",
            name="uq_player_game_result",
        ),
    )


class PropOpportunityLog(Base):
    """
    Every evaluated player prop opportunity — both PLAY and PASS decisions.

    One row per (external_id, stat_type) pair, recorded immediately after
    ``make_ud_bet_decision()`` runs, regardless of recommendation.

    ``result`` is stored from the OVER perspective for consistent analysis:
      "HIT"     → actual_value > line_value  (over cleared)
      "MISS"    → actual_value < line_value  (over failed)
      "PUSH"    → actual_value == line_value
      "PENDING" → game not yet complete / result not yet available

    Interpretation per recommendation:
      PLAY OVER  + HIT  → ✅ correct pick
      PLAY OVER  + MISS → ❌ incorrect pick
      PLAY UNDER + MISS → ✅ correct pick  (under cleared = over failed)
      PLAY UNDER + HIT  → ❌ incorrect pick
      PASS       + HIT  → 📈 missed OVER opportunity
      PASS       + MISS → ✅ correct pass   (over would have failed)
    """
    __tablename__ = "prop_opportunity_log"

    id             = Column(Integer,     primary_key=True, autoincrement=True)
    external_id    = Column(String(64),  nullable=False, index=True)
    player_name    = Column(String(128), nullable=False, index=True)
    team           = Column(String(64),  nullable=False, default="")
    sport          = Column(String(32),  nullable=False, index=True)
    stat_type      = Column(String(64),  nullable=False)
    line_value     = Column(Float,       nullable=False)
    recommendation = Column(String(8),   nullable=False)   # OVER | UNDER | PASS
    decision_tier  = Column(String(8),   nullable=False)   # S | A | B | PASS
    confidence     = Column(Integer,     nullable=False, default=0)
    game_time      = Column(DateTime,    nullable=True)
    detected_at    = Column(DateTime,    default=datetime.utcnow, nullable=False)
    # Grading — filled by the opportunity_grader job after game_time passes
    result         = Column(String(8),   nullable=False, default="PENDING", index=True)
    actual_value   = Column(Float,       nullable=True)
    graded_at      = Column(DateTime,    nullable=True)
    # Learning label — set by opportunity grader on MISS outcomes
    # Values: "Model" | "Market" | "Settlement" | "Variance" | None (HIT/PUSH/PENDING)
    error_type     = Column(String(16),  nullable=True)
    # Phase 2 enrichment — captured at alert time; added via migration
    stars          = Column(Integer,     nullable=True)          # 0–5 stars at alert time
    risk_level     = Column(String(16),  nullable=True)          # "LOW"|"MEDIUM"|"HIGH"|None
    explanation    = Column(Text,        nullable=True)          # reason / narrative excerpt
    void_reason    = Column(String(64),  nullable=True)          # why result is VOID/CANCELLED
    # Phase 4 Evidence Infrastructure — added via migration
    recommendation_id  = Column(String(64),  nullable=True, index=True)
    # Stable ID derived from (external_id, stat_type); links prop → decision → result.
    provider           = Column(String(32),  nullable=True)               # "Underdog" | "PrizePicks"
    bet_quality_score  = Column(Integer,     nullable=True)               # 0-100 decision confidence
    qualification_path = Column(Text,        nullable=True)               # JSON list of gate outcomes
    reason_codes       = Column(Text,        nullable=True)               # JSON: ["STRONG_L5", …]
    watchlist_state    = Column(String(16),  nullable=True)               # Qualified|Watchlist|Rejected|Removed
    settlement_source  = Column(String(64),  nullable=True)               # "auto_grade"|"manual"|None
    manual_opinion     = Column(String(8),   nullable=True)               # "OVER"|"UNDER"|"PASS"|None

    __table_args__ = (
        UniqueConstraint(
            "external_id", "stat_type",
            name="uq_prop_opportunity_log",
        ),
    )


# ── Prop Candidate Log ────────────────────────────────────────────────────────

class PropCandidateLog(Base):
    """
    Every scored prop candidate — both qualifying and rejected.

    Written once per candidate per scan cycle.  Use this table for:
      • Edge transparency — see exactly where candidates filter out.
      • Qualification calibration — measure false positives / false negatives.
      • Funnel analytics — candidates → qualified → alerted → hit/miss.

    gate_decision values:
      ACCEPTED  — tier S/A, no gate failures, alert was queued
      WATCHLIST — tier B without strong enough component scores to alert
      REJECTED  — tier PASS, or failed confidence/sport/decision gate
      REMOVED   — provider removed the prop during this cycle
    """
    __tablename__ = "prop_candidate_log"

    id                   = Column(Integer,     primary_key=True, autoincrement=True)
    scan_ts              = Column(DateTime,    nullable=False, index=True, default=datetime.utcnow)
    player_name          = Column(String(128), nullable=False, index=True)
    team                 = Column(String(64),  nullable=False, default="")
    sport                = Column(String(32),  nullable=False, index=True)
    stat_type            = Column(String(64),  nullable=False)
    line_value           = Column(Float,       nullable=False, default=0.0)
    provider             = Column(String(32),  nullable=False, default="Underdog")
    score_total          = Column(Float,       nullable=True)
    score_tier           = Column(String(8),   nullable=True)    # S / A / B / PASS
    confidence           = Column(Integer,     nullable=True)
    gate_decision        = Column(String(16),  nullable=False)   # ACCEPTED/WATCHLIST/REJECTED/REMOVED
    rejection_reason     = Column(Text,        nullable=True)    # human-readable rejection label
    reason_codes         = Column(Text,        nullable=True)    # JSON list e.g. ["STRONG_L5", "S_TIER"]
    snapshot_external_id = Column(String(64),  nullable=True, index=True)


# ── Player Risk / Block System ─────────────────────────────────────────────────

class PlayerRiskRecord(Base):
    """
    Player reliability block — prevents alerting on unreliable props.

    Managed via engine.player_block.  One active block per (player_key, sport).
    Expired TEMPORARY blocks are never hard-deleted; set is_active=False instead.
    """
    __tablename__ = "player_risk_records"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    player_key   = Column(String(128), nullable=False, index=True)
    player_name  = Column(String(128), nullable=False)
    sport        = Column(String(32),  nullable=False, default="")   # "" = all sports
    reason_code  = Column(String(32),  nullable=False)               # BLOCKABLE_REASONS
    description  = Column(Text,        nullable=False, default="")
    block_type   = Column(String(16),  nullable=False)               # TEMPORARY | PERMANENT
    expires_at   = Column(DateTime,    nullable=True)                # None = permanent
    review_date  = Column(DateTime,    nullable=True)
    created_by   = Column(String(64),  nullable=False, default="system")
    created_at   = Column(DateTime,    default=datetime.utcnow, nullable=False)
    is_active    = Column(Boolean,     default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "player_key", "sport", "reason_code",
            name="uq_player_risk_active",
        ),
    )


# ── Database manager ──────────────────────────────────────────────────────────

class Database:
    """Async database manager. Call `init()` once at startup."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._engine = None
        self._session_factory = None

    async def init(self) -> None:
        """Create engine, run migrations, and ensure tables exist."""
        # Ensure the data directory exists for file-backed SQLite.
        # Skip for in-memory databases (":memory:" has no parent directory).
        if self._url.startswith("sqlite"):
            db_path = self._url.replace("sqlite+aiosqlite:///", "")
            if db_path and db_path != ":memory:":
                parent = os.path.dirname(db_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)

        # connect_args["timeout"]: SQLite busy timeout — wait up to 30 s before
        # raising OperationalError("database is locked").  Default is 5 s, which
        # is too short when the underdog_job, clv_seed_job, and clv_harvest_job
        # fire within the same scheduler window.
        _connect_args: dict = {}
        if self._url.startswith("sqlite"):
            _connect_args["timeout"] = 30
        self._engine = create_async_engine(
            self._url,
            echo=False,
            connect_args=_connect_args,
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self._engine.begin() as conn:
            # WAL journal mode: allows concurrent reads while a write is in
            # progress, eliminating most "database is locked" errors on SQLite.
            # Setting is persistent — only needs to be applied once per DB file.
            if self._url.startswith("sqlite"):
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                # NORMAL synchronous is safe and faster under WAL.
                await conn.execute(text("PRAGMA synchronous=NORMAL"))
                # busy_timeout (milliseconds): how long SQLite waits when another
                # writer holds the lock before raising "database is locked".
                # The Python-level connect_args["timeout"] (seconds) handles aiosqlite
                # connection-level waits; this PRAGMA covers intra-process lock contention.
                await conn.execute(text("PRAGMA busy_timeout=30000"))
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate_pp_edge_records()
        await self._migrate_underdog_snapshots()
        await self._migrate_clv_records()
        await self._migrate_prop_line_history()
        await self._migrate_prop_line_history_v2()
        await self._migrate_prop_line_history_v3()
        await self._migrate_prop_opportunity_log()
        await self._migrate_prop_opportunity_log_v2()
        await self._migrate_prop_opportunity_log_v3()
        # player_risk_records is created by create_all (new table); no column migration needed
        logger.info("Database initialised at %s", self._url)

    def session(self) -> AsyncSession:
        if self._session_factory is None:
            raise RuntimeError("Database.init() has not been called.")
        return self._session_factory()
    async def prune_prop_line_history(self, keep_days: int = 14) -> int:
        """Delete PropLineHistory rows older than keep_days. Returns rows deleted."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=keep_days)
        async with self.session() as s:
            result = await s.execute(
                delete(PropLineHistory).where(
                    PropLineHistory.fetched_at < cutoff
                )
            )
            await s.commit()
            deleted = result.rowcount or 0
            logger.info(
                "prune_prop_line_history: deleted %d rows older than %d days",
                deleted, keep_days,
            )
            return deleted
    # ── Odds ────────────────────────────────────────────────────────────────

    async def save_odds(self, record: OddsRecord) -> OddsRecord:
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    # ── Steam ────────────────────────────────────────────────────────────────

    async def save_steam(self, record: SteamRecord) -> SteamRecord:
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def get_recent_steam(self, limit: int = 10) -> list[SteamRecord]:
        async with self.session() as s:
            result = await s.execute(
                select(SteamRecord)
                .order_by(desc(SteamRecord.detected_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    # ── EV ───────────────────────────────────────────────────────────────────

    async def save_ev(self, record: EVRecord) -> EVRecord:
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def get_recent_ev(self, limit: int = 10) -> list[EVRecord]:
        async with self.session() as s:
            result = await s.execute(
                select(EVRecord)
                .order_by(desc(EVRecord.detected_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    # ── Stats ────────────────────────────────────────────────────────────────

    async def count_steam_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(select(func.count()).select_from(SteamRecord))
            return result.scalar() or 0

    async def count_ev_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(select(func.count()).select_from(EVRecord))
            return result.scalar() or 0

    async def count_odds_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(select(func.count()).select_from(OddsRecord))
            return result.scalar() or 0

    async def get_prior_odds(
        self,
        event: str,
        selection: str,
        sportsbook: str,
        before: datetime,
    ) -> Optional[OddsRecord]:
        """
        Return the most recent OddsRecord for a specific event/selection/book
        recorded strictly before *before*.  Used for steam movement comparison.
        """
        async with self.session() as s:
            result = await s.execute(
                select(OddsRecord)
                .where(
                    OddsRecord.event == event,
                    OddsRecord.selection == selection,
                    OddsRecord.sportsbook == sportsbook,
                    OddsRecord.recorded_at < before,
                )
                .order_by(desc(OddsRecord.recorded_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_odds_window(self, sport: str, since: datetime) -> list[OddsRecord]:
        """Return all OddsRecords for a sport recorded at or after *since*."""
        async with self.session() as s:
            result = await s.execute(
                select(OddsRecord)
                .where(
                    OddsRecord.sport == sport,
                    OddsRecord.recorded_at >= since,
                )
                .order_by(OddsRecord.recorded_at)
            )
            return list(result.scalars().all())

    async def has_recent_steam_alert(
        self, event: str, selection: str, within_seconds: int = 3600
    ) -> bool:
        """True if a SteamRecord for this event/selection was sent recently."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(seconds=within_seconds)
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(SteamRecord)
                .where(
                    SteamRecord.event == event,
                    SteamRecord.selection == selection,
                    SteamRecord.alert_sent == True,  # noqa: E712
                    SteamRecord.detected_at >= cutoff,
                )
            )
            return (result.scalar() or 0) > 0

    async def has_recent_ev_alert(
        self, event: str, selection: str, within_seconds: int = 1800
    ) -> bool:
        """True if an EVRecord for this event/selection was alerted recently."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(seconds=within_seconds)
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(EVRecord)
                .where(
                    EVRecord.event == event,
                    EVRecord.selection == selection,
                    EVRecord.alert_sent == True,  # noqa: E712
                    EVRecord.detected_at >= cutoff,
                )
            )
            return (result.scalar() or 0) > 0

    # ── PrizePicks ───────────────────────────────────────────────────────────

    async def _migrate_pp_edge_records(self) -> None:
        """Add new columns to pp_edge_records if they don't exist yet (idempotent)."""
        new_cols = [
            "ALTER TABLE pp_edge_records ADD COLUMN tier TEXT",
            "ALTER TABLE pp_edge_records ADD COLUMN confidence REAL",
            "ALTER TABLE pp_edge_records ADD COLUMN result TEXT DEFAULT 'PENDING'",
            "ALTER TABLE pp_edge_records ADD COLUMN opening_line REAL",
            "ALTER TABLE pp_edge_records ADD COLUMN prev_line REAL",
            "ALTER TABLE pp_edge_records ADD COLUMN game_time DATETIME",
        ]
        async with self._engine.begin() as conn:
            for sql in new_cols:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass   # column already exists — safe to ignore

    async def save_pp_line(self, record: "PrizePicksRecord") -> "PrizePicksRecord":
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def get_recent_pp_lines(self, limit: int = 20) -> list["PrizePicksRecord"]:
        async with self.session() as s:
            result = await s.execute(
                select(PrizePicksRecord)
                .order_by(desc(PrizePicksRecord.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def save_pp_edge(self, record: "PPEdgeRecord") -> "PPEdgeRecord":
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def get_recent_pp_edges(self, limit: int = 10) -> list["PPEdgeRecord"]:
        async with self.session() as s:
            result = await s.execute(
                select(PPEdgeRecord)
                .order_by(desc(PPEdgeRecord.detected_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_pp_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(
                select(func.count()).select_from(PrizePicksRecord)
            )
            return result.scalar() or 0

    async def count_pp_edge_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(
                select(func.count()).select_from(PPEdgeRecord)
            )
            return result.scalar() or 0

    async def get_top_pp_edges(
        self, limit: int = 10, hours: int = 6
    ) -> list["PPEdgeRecord"]:
        """Top PP edges from the last ``hours`` hours, deduped by (player, stat).

        Returns at most ``limit`` records sorted by tier rank then edge% descending.
        Deduplication keeps the record with the highest edge for each player/stat pair.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        async with self.session() as s:
            result = await s.execute(
                select(PPEdgeRecord)
                .where(PPEdgeRecord.detected_at >= cutoff)
                .order_by(desc(PPEdgeRecord.best_edge))
                .limit(limit * 4)   # overfetch to allow in-Python dedup
            )
            rows = list(result.scalars().all())
        seen: set[tuple[str, str]] = set()
        deduped: list[PPEdgeRecord] = []
        for r in rows:
            key = (r.player_name, r.stat_type)
            if key not in seen:
                seen.add(key)
                deduped.append(r)
            if len(deduped) >= limit:
                break
        return deduped

    async def get_top_ud_props_for_picks(
        self, limit: int = 10, since_hours: int = 24
    ) -> "list[PropLineHistory]":
        """Return the most-recent Underdog prop snapshot for each (player, stat) within the window.

        Non-removed props only, ordered by fetched_at DESC so freshest data appears first.
        Used as the primary data source for /picks and /slip.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            subq = (
                select(func.max(PropLineHistory.id))
                .where(
                    PropLineHistory.provider   == "Underdog",
                    PropLineHistory.fetched_at >= cutoff,
                    PropLineHistory.removed.isnot(True),
                    # Exclude PASS-tier props from /picks (unscored rows have NULL, shown)
                    PropLineHistory.score_tier != "PASS",
                )
                .group_by(
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(PropLineHistory)
                .where(PropLineHistory.id.in_(subq))
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(limit * 10)  # overfetch to allow sport filtering
            )
        rows = list(result.scalars().all())

        logger.info(
            "UD picks query returned %d rows: %s",
            len(rows),
            [(r.player_name, r.sport, r.stat_type, r.line_value) for r in rows[:5]]
        )

        return rows[:limit]

    async def get_ud_recommendations_bulk(
        self,
        player_stat_triples: "list[tuple[str, str, str]]",
        since_hours: int = 24,
    ) -> "dict[tuple[str, str, str], tuple[str | None, int | None]]":
        """
        Return the most-recent bet_recommendation + bet_confidence from
        UnderdogSnapshotRecord for each (player_name, sport, stat_type) triple.

        Used by /picks to display the OVER/UNDER pick direction without
        changing any scoring or pick-generation logic.

        Returns a dict keyed by (player_name, sport, stat_type) →
        (bet_recommendation, bet_confidence).  Missing props get (None, None).
        """
        if not player_stat_triples:
            return {}
        from datetime import timedelta
        from sqlalchemy import or_, and_
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)

        # Build a filter for each triple so we can do one round-trip.
        conditions = [
            and_(
                UnderdogSnapshotRecord.player_name == pn,
                UnderdogSnapshotRecord.sport       == sp,
                UnderdogSnapshotRecord.stat_type   == st,
            )
            for pn, sp, st in player_stat_triples
        ]

        async with self.session() as s:
            # Subquery: max(id) per (player_name, sport, stat_type), non-removed
            subq = (
                select(func.max(UnderdogSnapshotRecord.id))
                .where(
                    UnderdogSnapshotRecord.removed    == False,  # noqa: E712
                    UnderdogSnapshotRecord.fetched_at >= cutoff,
                    or_(*conditions),
                )
                .group_by(
                    UnderdogSnapshotRecord.player_name,
                    UnderdogSnapshotRecord.sport,
                    UnderdogSnapshotRecord.stat_type,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(UnderdogSnapshotRecord).where(
                    UnderdogSnapshotRecord.id.in_(subq)
                )
            )
            rows = result.scalars().all()

        return {
            (r.player_name, r.sport, r.stat_type): (
                r.bet_recommendation, r.bet_confidence
            )
            for r in rows
        }

    async def get_all_ud_lines_for_prop(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        since_hours: int = 24,
    ) -> "list[float]":
        """
        Return all distinct, active Underdog line_values for a given
        (player_name, sport, stat_type) combination, sorted ascending.

        Used by /picks to show alternate lines (goblin / standard / high).
        Returns a single-element list when only one line is available, so
        callers can skip the multi-line display when len == 1.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            result = await s.execute(
                select(PropLineHistory.line_value)
                .where(
                    PropLineHistory.provider    == "Underdog",
                    PropLineHistory.player_name == player_name,
                    PropLineHistory.sport       == sport,
                    PropLineHistory.stat_type   == stat_type,
                    PropLineHistory.fetched_at  >= cutoff,
                    PropLineHistory.removed.isnot(True),
                )
                .distinct()
                .order_by(PropLineHistory.line_value)
            )
            return [row[0] for row in result.all()]

    async def get_recent_player_prop_lines(
        self, sportsbooks: "list[str]", since_hours: int = 4
    ) -> "list[OddsRecord]":
        """Return all recent OddsRecords with a line value for the given sportsbooks.

        Designed for bulk DK/FD player-prop lookup in the market comparison engine.
        The caller builds a {(player_lower, sportsbook): line} index after parsing
        selection="PlayerName Over" / "PlayerName Under".
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            result = await s.execute(
                select(OddsRecord)
                .where(
                    OddsRecord.sportsbook.in_(sportsbooks),
                    OddsRecord.recorded_at >= cutoff,
                    OddsRecord.line.isnot(None),
                )
                .order_by(desc(OddsRecord.recorded_at))
            )
            return list(result.scalars().all())

    async def get_pp_edge_line_history(
        self, player_name: str, stat_type: str
    ) -> tuple[float | None, float | None]:
        """Return (opening_line, prev_line) for the given player/stat.

        ``opening_line`` is the pp_line_value from the oldest stored record.
        ``prev_line``    is the pp_line_value from the most recent stored record.
        Both are None when no prior records exist (first detection ever).
        """
        async with self.session() as s:
            first_res = await s.execute(
                select(PPEdgeRecord.pp_line_value)
                .where(
                    PPEdgeRecord.player_name == player_name,
                    PPEdgeRecord.stat_type   == stat_type,
                )
                .order_by(PPEdgeRecord.detected_at.asc())
                .limit(1)
            )
            latest_res = await s.execute(
                select(PPEdgeRecord.pp_line_value)
                .where(
                    PPEdgeRecord.player_name == player_name,
                    PPEdgeRecord.stat_type   == stat_type,
                )
                .order_by(PPEdgeRecord.detected_at.desc())
                .limit(1)
            )
        return first_res.scalar(), latest_res.scalar()

    async def get_resolved_pp_history(
        self,
        player_name: str,
        stat_type: str,
        limit: int = 20,
    ) -> list["PPEdgeRecord"]:
        """Return resolved (non-PENDING) PPEdgeRecords for a player/stat, newest first.

        Used by PPAnalysisScore to compute the Hit Rate dimension.
        Records with result=PENDING or result=NULL are excluded.
        """
        from sqlalchemy import not_
        async with self.session() as s:
            result = await s.execute(
                select(PPEdgeRecord)
                .where(
                    PPEdgeRecord.player_name == player_name,
                    PPEdgeRecord.stat_type   == stat_type,
                    PPEdgeRecord.result.isnot(None),
                    not_(PPEdgeRecord.result.in_(["PENDING", ""])),
                )
                .order_by(desc(PPEdgeRecord.detected_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def update_pp_result(self, record_id: int, result: str) -> None:
        """Set the outcome (WIN/LOSS/PUSH/PENDING) on a PPEdgeRecord by id."""
        from sqlalchemy import update as sa_update
        async with self.session() as s:
            await s.execute(
                sa_update(PPEdgeRecord)
                .where(PPEdgeRecord.id == record_id)
                .values(result=result)
            )
            await s.commit()

    async def get_all_resolved_pp_edges(
        self, limit: int = 200
    ) -> list["PPEdgeRecord"]:
        """Return resolved (non-PENDING, non-NULL) PPEdgeRecords, newest first.

        Used by /grade to compute win/loss breakdown by tier.
        """
        from sqlalchemy import not_
        async with self.session() as s:
            result = await s.execute(
                select(PPEdgeRecord)
                .where(
                    PPEdgeRecord.result.isnot(None),
                    not_(PPEdgeRecord.result.in_(["PENDING", ""])),
                )
                .order_by(desc(PPEdgeRecord.detected_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_recent_pp_alerts(self, limit: int = 15) -> list["PPEdgeRecord"]:
        """Return recently alerted PP edges (alert_sent=True), newest first.

        Used by /alerts to show the alert history feed.
        """
        async with self.session() as s:
            result = await s.execute(
                select(PPEdgeRecord)
                .where(PPEdgeRecord.alert_sent == True)   # noqa: E712
                .order_by(desc(PPEdgeRecord.detected_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def has_recent_pp_alert(
        self,
        player_name: str,
        stat_type: str,
        within_seconds: int = 3600,
    ) -> bool:
        """True if a PPEdgeRecord for this player/stat was alerted recently."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(seconds=within_seconds)
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(PPEdgeRecord)
                .where(
                    PPEdgeRecord.player_name == player_name,
                    PPEdgeRecord.stat_type   == stat_type,
                    PPEdgeRecord.alert_sent  == True,   # noqa: E712
                    PPEdgeRecord.detected_at >= cutoff,
                )
            )
            return (result.scalar() or 0) > 0

    async def count_today_pp_alerts(
        self,
        tier: Optional[str] = None,
        in_tiers: Optional[list] = None,
    ) -> int:
        """
        Count PP edge records that were alerted today (UTC midnight to now).

        Parameters
        ----------
        tier:      When provided, only count records with that exact tier.
        in_tiers:  When provided, only count records whose tier is in the list
                   (e.g. ``["A", "B"]`` to count only non-S alerts).
                   Takes precedence over ``tier`` when both are supplied.
        None/None → count all tiers.
        """
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self.session() as s:
            q = (
                select(func.count())
                .select_from(PPEdgeRecord)
                .where(
                    PPEdgeRecord.alert_sent == True,   # noqa: E712
                    PPEdgeRecord.detected_at >= today_start,
                )
            )
            if in_tiers is not None:
                q = q.where(PPEdgeRecord.tier.in_(in_tiers))
            elif tier is not None:
                q = q.where(PPEdgeRecord.tier == tier)
            result = await s.execute(q)
            return result.scalar() or 0

    async def count_today_underdog_alerts(self) -> int:
        """Count Underdog snapshot records that were alerted today (UTC)."""
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.alert_sent == True,   # noqa: E712
                    UnderdogSnapshotRecord.fetched_at >= today_start,
                )
            )
            return result.scalar() or 0

    async def find_player_prop_odds(
        self,
        player_name: str,
        market_type: str,
        since: datetime,
    ) -> list["OddsRecord"]:
        """
        Find sportsbook player-prop OddsRecords for a player + stat combination.

        Matches records whose ``selection`` column contains the player name
        (case-insensitive) and whose ``market_type`` equals the Odds API stat
        string (e.g. "player_points").
        """
        async with self.session() as s:
            result = await s.execute(
                select(OddsRecord)
                .where(
                    OddsRecord.market_type == market_type,
                    OddsRecord.selection.ilike(f"%{player_name}%"),
                    OddsRecord.recorded_at >= since,
                )
                .order_by(desc(OddsRecord.recorded_at))
            )
            return list(result.scalars().all())

    # ── Multi-platform market snapshots ──────────────────────────────────────

    async def save_market_snapshot(self, record: "MarketSnapshotRecord") -> "MarketSnapshotRecord":
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def get_snapshots_since(
        self,
        sport: str,
        since: datetime,
        sportsbook: Optional[str] = None,
    ) -> list["MarketSnapshotRecord"]:
        async with self.session() as s:
            q = select(MarketSnapshotRecord).where(
                MarketSnapshotRecord.sport == sport,
                MarketSnapshotRecord.recorded_at >= since,
            )
            if sportsbook:
                q = q.where(MarketSnapshotRecord.sportsbook == sportsbook)
            result = await s.execute(q.order_by(MarketSnapshotRecord.recorded_at))
            return list(result.scalars().all())

    async def count_snapshot_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(select(func.count()).select_from(MarketSnapshotRecord))
            return result.scalar() or 0

    # ── CLV records ──────────────────────────────────────────────────────────

    async def save_clv_record(self, record: "CLVRecord") -> "CLVRecord":
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def get_recent_clv_records(self, limit: int = 20) -> list["CLVRecord"]:
        async with self.session() as s:
            result = await s.execute(
                select(CLVRecord)
                .order_by(desc(CLVRecord.computed_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_clv_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(select(func.count()).select_from(CLVRecord))
            return result.scalar() or 0

    # ── CLV record migration ─────────────────────────────────────────────────

    async def _migrate_clv_records(self) -> None:
        """Add new columns to clv_records if they don't exist yet (idempotent)."""
        new_cols = [
            "ALTER TABLE clv_records ADD COLUMN sport TEXT DEFAULT ''",
            "ALTER TABLE clv_records ADD COLUMN market_type TEXT DEFAULT ''",
            "ALTER TABLE clv_records ADD COLUMN alert_type TEXT DEFAULT ''",
            "ALTER TABLE clv_records ADD COLUMN tier TEXT DEFAULT ''",
        ]
        async with self._engine.begin() as conn:
            for sql in new_cols:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass  # column already exists — safe to ignore

    # ── Provider-agnostic prop line history ──────────────────────────────────

    async def save_prop_line_history(self, record: "PropLineHistory") -> "PropLineHistory":
        """Store one normalized PlayerProp snapshot from any provider."""
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def save_prop_line_history_bulk(
        self, records: "list[PropLineHistory]"
    ) -> None:
        """Bulk-insert a batch of PropLineHistory records in a single transaction."""
        if not records:
            return
        async with self.session() as s:
            s.add_all(records)
            await s.commit()

    async def get_prop_line_history(
        self,
        provider:    str,
        player_name: str,
        sport:       str,
        stat_type:   str,
        limit:       int = 30,
    ) -> "list[PropLineHistory]":
        """Return recent snapshots for one player+stat, newest first."""
        async with self.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.provider    == provider,
                    PropLineHistory.player_name == player_name,
                    PropLineHistory.sport       == sport,
                    PropLineHistory.stat_type   == stat_type,
                )
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_latest_props_for_provider(
        self, provider: str, since_hours: int = 6
    ) -> "list[PropLineHistory]":
        """Return the most-recent snapshot for each (player, stat) from a provider."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            subq = (
                select(func.max(PropLineHistory.id))
                .where(
                    PropLineHistory.provider   == provider,
                    PropLineHistory.fetched_at >= cutoff,
                )
                .group_by(
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(PropLineHistory).where(PropLineHistory.id.in_(subq))
            )
            return list(result.scalars().all())

    # ── PropLineHistory migration ────────────────────────────────────────────

    async def _migrate_prop_line_history(self) -> None:
        """Add lifecycle columns to prop_line_history if they don't exist (idempotent)."""
        new_cols = [
            "ALTER TABLE prop_line_history ADD COLUMN first_seen DATETIME",
            "ALTER TABLE prop_line_history ADD COLUMN last_seen  DATETIME",
            "ALTER TABLE prop_line_history ADD COLUMN change_count INTEGER DEFAULT 0",
            "ALTER TABLE prop_line_history ADD COLUMN prev_line  REAL",
            "ALTER TABLE prop_line_history ADD COLUMN removed    INTEGER DEFAULT 0",
            # Alert lifecycle state columns (v1.3)
            "ALTER TABLE prop_line_history ADD COLUMN lifecycle_state TEXT DEFAULT 'DISCOVERED'",
            "ALTER TABLE prop_line_history ADD COLUMN first_alert_sent_at DATETIME",
            # Bet direction synced from UnderdogSnapshotRecord (v1.4)
            "ALTER TABLE prop_line_history ADD COLUMN bet_recommendation TEXT",
            "ALTER TABLE prop_line_history ADD COLUMN bet_confidence INTEGER",
        ]
        async with self._engine.begin() as conn:
            for sql in new_cols:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass  # column already exists — safe to ignore

    async def update_prop_lifecycle_state(
        self,
        provider:    str,
        player_name: str,
        sport:       str,
        stat_type:   str,
        new_state:   str,
        first_alert_sent_at: "Optional[datetime]" = None,
    ) -> bool:
        """
        Update the lifecycle_state (and optionally first_alert_sent_at) on the
        most-recent PropLineHistory row for this prop.

        Lifecycle states:
          DISCOVERED     — seen but not yet alerted
          ACTIVE_ALERTED — at least one alert sent to user
          REMOVED        — prop is no longer offered

        Returns True if a row was found and updated, False otherwise.
        """
        from sqlalchemy import update as sa_update

        async with self.session() as s:
            # Find the most-recent row for this prop identity
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.provider    == provider,
                    PropLineHistory.player_name == player_name,
                    PropLineHistory.sport       == sport,
                    PropLineHistory.stat_type   == stat_type,
                )
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False

            vals: dict = {"lifecycle_state": new_state}
            if first_alert_sent_at is not None and row.first_alert_sent_at is None:
                vals["first_alert_sent_at"] = first_alert_sent_at

            await s.execute(
                sa_update(PropLineHistory)
                .where(PropLineHistory.id == row.id)
                .values(**vals)
            )
            await s.commit()
        return True

    async def count_prop_line_history(self, provider: Optional[str] = None) -> int:
        """Count PropLineHistory rows, optionally filtered by provider."""
        async with self.session() as s:
            q = select(func.count()).select_from(PropLineHistory)
            if provider:
                q = q.where(PropLineHistory.provider == provider)
            result = await s.execute(q)
            return result.scalar() or 0

    async def sync_underdog_snapshots_to_prop_history(
        self,
        limit: int = 200,
        since_hours: int = 48,
    ) -> int:
        """
        Bridge Underdog snapshot records into the shared PropLineHistory table
        with full lifecycle tracking (first_seen, last_seen, change_count, removed).

        Algorithm per prop (player_name, sport, stat_type):
          • First appearance  → insert with first_seen=last_seen=fetched_at, change_count=0
          • Same line         → update last_seen only
          • Line changed      → increment change_count, set prev_line, update last_seen
          • Removed snapshot  → set removed=True, update last_seen

        Returns the number of UPSERTED (new or updated) rows.
        """
        from datetime import timedelta
        from sqlalchemy import update as sa_update

        cutoff = datetime.utcnow() - timedelta(hours=since_hours)

        # Load recent Underdog snapshots (all, including removed — we track removals)
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .where(UnderdogSnapshotRecord.fetched_at >= cutoff)
                .order_by(UnderdogSnapshotRecord.fetched_at.asc())  # oldest first
                .limit(limit)
            )
            snaps = list(result.scalars().all())

        if not snaps:
            return 0

        # Load latest PropLineHistory row per (player_name, sport, stat_type) for Underdog
        async with self.session() as s:
            # Subquery: max id per prop identity
            subq = (
                select(func.max(PropLineHistory.id))
                .where(PropLineHistory.provider == "Underdog")
                .group_by(
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(PropLineHistory).where(PropLineHistory.id.in_(subq))
            )
            latest_rows = {
                (r.player_name, r.sport, r.stat_type): r
                for r in result.scalars().all()
            }

        # Track changes to accumulate within this batch
        # ── Build insert/update batches (no DB I/O yet) ──────────────────────
        snap_by_key: dict[tuple, list] = {}
        for snap in snaps:
            key = (snap.player_name or "", snap.sport or "", snap.stat_type or "")
            snap_by_key.setdefault(key, []).append(snap)

        new_rows:    list[PropLineHistory]          = []
        update_jobs: list[tuple[int, dict]]         = []   # (existing_id, update_vals)

        for key, key_snaps in snap_by_key.items():
            player_name, sport, stat_type = key
            # Use the LATEST snapshot for this batch as the authoritative state
            latest_snap = key_snaps[-1]
            existing    = latest_rows.get(key)
            now_ts      = latest_snap.fetched_at or datetime.utcnow()
            is_removed  = bool(latest_snap.removed)
            new_line    = float(latest_snap.line_value) if latest_snap.line_value is not None else 0.0
            snap_tier   = getattr(latest_snap, "score_tier", None)  # may be None for unscored

            if existing is None:
                # First appearance — collect for batch INSERT
                row = PropLineHistory(
                    provider     = "Underdog",
                    sport        = sport,
                    player_name  = player_name,
                    team         = latest_snap.team        or "",
                    stat_type    = stat_type,
                    line_value   = new_line,
                    game_time    = latest_snap.game_time,
                    external_id  = getattr(latest_snap, "external_id", None) or getattr(latest_snap, "id", None) or "",
                    game_id      = latest_snap.game_id     or "",
                    fetched_at   = now_ts,
                )
                # Lifecycle + score_tier columns (migration adds them; ignore on old schema)
                try:
                    row.first_seen   = now_ts
                    row.last_seen    = now_ts
                    row.change_count = 0
                    row.prev_line    = None
                    row.removed      = is_removed
                    row.score_tier   = snap_tier
                    row.bet_recommendation = getattr(latest_snap, "bet_recommendation", None)
                    row.bet_confidence     = getattr(latest_snap, "bet_confidence", None)
                except AttributeError:
                    pass
                new_rows.append(row)
            else:
                # Existing row — collect for batch UPDATE
                old_line     = existing.line_value or 0.0
                line_changed = abs(new_line - old_line) >= 0.01
                change_delta = (getattr(existing, "change_count", 0) or 0)
                if line_changed:
                    change_delta += 1

                uvals: dict = {
                    "line_value": new_line,
                    "fetched_at": now_ts,
                    "last_seen":  now_ts,
                    "change_count": change_delta,
                    "removed":    is_removed,
                }
                if line_changed:
                    uvals["prev_line"] = old_line
                # Always propagate score_tier when the snapshot has a value
                if snap_tier is not None:
                    uvals["score_tier"] = snap_tier
                # Always propagate bet direction from snapshot
                _snap_rec = getattr(latest_snap, "bet_recommendation", None)
                _snap_bc  = getattr(latest_snap, "bet_confidence", None)
                if _snap_rec is not None:
                    uvals["bet_recommendation"] = _snap_rec
                    uvals["bet_confidence"]      = _snap_bc
                update_jobs.append((existing.id, uvals))

        # ── Execute batched INSERTs in a single session ───────────────────────
        upserted = 0
        if new_rows:
            async with self.session() as s:
                for row in new_rows:
                    s.add(row)
                await s.commit()
            upserted += len(new_rows)

        # ── Execute batched UPDATEs in a single session ───────────────────────
        if update_jobs:
            async with self.session() as s:
                for row_id, uvals in update_jobs:
                    await s.execute(
                        sa_update(PropLineHistory)
                        .where(PropLineHistory.id == row_id)
                        .values(**uvals)
                    )
                await s.commit()
            upserted += len(update_jobs)

        return upserted

    async def upsert_prop_line_lifecycle(
        self,
        provider:    str,
        player_name: str,
        sport:       str,
        stat_type:   str,
        line_value:  float,
        *,
        team:        str            = "",
        external_id: str            = "",
        game_id:     str            = "",
        game_time:   "Optional[datetime]" = None,
        removed:     bool           = False,
        fetched_at:  "Optional[datetime]" = None,
    ) -> tuple["PropLineHistory", str]:
        """
        Insert or update a PropLineHistory row with full lifecycle tracking.

        Returns (row, event) where event is one of:
            "ADDED"    — first time this prop is seen for this provider
            "CHANGED"  — same prop, line changed
            "REMOVED"  — prop marked removed
            "RETURNED" — prop was removed then reappeared
            "UNCHANGED"— same prop, same line, not removed
        """
        from sqlalchemy import update as sa_update

        now_ts = fetched_at or datetime.utcnow()

        # Find most-recent existing row for this prop+provider
        async with self.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.provider    == provider,
                    PropLineHistory.player_name == player_name,
                    PropLineHistory.sport       == sport,
                    PropLineHistory.stat_type   == stat_type,
                )
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(1)
            )
            existing = result.scalar_one_or_none()

        if existing is None:
            # First appearance
            row = PropLineHistory(
                provider    = provider,
                sport       = sport,
                player_name = player_name,
                team        = team,
                stat_type   = stat_type,
                line_value  = line_value,
                game_time   = game_time,
                external_id = external_id,
                game_id     = game_id,
                fetched_at  = now_ts,
            )
            try:
                row.first_seen   = now_ts
                row.last_seen    = now_ts
                row.change_count = 0
                row.prev_line    = None
                row.removed      = removed
                row.opening_line = line_value  # set once; never updated on subsequent changes
            except AttributeError:
                pass
            async with self.session() as s:
                s.add(row)
                await s.commit()
                await s.refresh(row)
            return row, "ADDED"

        # Existing — determine event type
        was_removed  = bool(getattr(existing, "removed", False))
        old_line     = existing.line_value or 0.0
        line_changed = abs(line_value - old_line) >= 0.01

        if was_removed and not removed:
            event = "RETURNED"
        elif removed:
            event = "REMOVED"
        elif line_changed:
            event = "CHANGED"
        else:
            event = "UNCHANGED"

        change_count = (getattr(existing, "change_count", 0) or 0)
        if line_changed:
            change_count += 1

        update_vals: dict = {
            "line_value": line_value,
            "fetched_at": now_ts,
            "team":       team or existing.team,
        }
        try:
            update_vals["last_seen"]    = now_ts
            update_vals["change_count"] = change_count
            update_vals["removed"]      = removed
            if line_changed:
                update_vals["prev_line"] = old_line
        except Exception:
            pass

        async with self.session() as s:
            await s.execute(
                sa_update(PropLineHistory)
                .where(PropLineHistory.id == existing.id)
                .values(**update_vals)
            )
            await s.commit()

        # Re-fetch the updated row
        async with self.session() as s:
            result = await s.execute(
                select(PropLineHistory).where(PropLineHistory.id == existing.id)
            )
            row = result.scalar_one()

        return row, event

    # ── Alert CLV seeds ──────────────────────────────────────────────────────

    async def save_alert_clv_seed(self, record: "AlertCLVSeed") -> "AlertCLVSeed":
        """
        Insert a CLV seed record, ignoring duplicates (upsert-on-conflict).

        The unique constraint on (source_table, source_id) prevents double-seeding
        the same alert.  On conflict the existing seed is left unchanged.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        async with self.session() as s:
            stmt = sqlite_insert(AlertCLVSeed).values(
                source_table     = record.source_table,
                source_id        = record.source_id,
                alert_type       = record.alert_type,
                sport            = record.sport,
                market_type      = record.market_type,
                event            = record.event,
                selection        = record.selection,
                bet_odds         = record.bet_odds,
                counterpart_odds = record.counterpart_odds,
                tier             = record.tier,
                game_time        = record.game_time,
                alerted_at       = record.alerted_at,
                clv_pct          = record.clv_pct,
                clv_computed     = record.clv_computed,
            ).on_conflict_do_nothing(
                index_elements=["source_table", "source_id"]
            )
            await s.execute(stmt)
            await s.commit()
        return record

    async def get_pending_clv_seeds(
        self,
        limit: int = 100,
        stale_hours: int = 24,
    ) -> "list[AlertCLVSeed]":
        """
        Return seeds that are ready for harvest — either:
          (a) game_time is set and has passed (normal path), OR
          (b) game_time is None but alerted_at is older than stale_hours
              (seeds from EV records which don't store game_time; expire after cutoff)

        In both cases clv_computed must be False.
        """
        from datetime import timedelta as _td
        from sqlalchemy import or_, and_

        now          = datetime.utcnow()
        stale_cutoff = now - _td(hours=stale_hours)

        async with self.session() as s:
            result = await s.execute(
                select(AlertCLVSeed)
                .where(
                    AlertCLVSeed.clv_computed == False,       # noqa: E712
                    or_(
                        and_(
                            AlertCLVSeed.game_time.isnot(None),
                            AlertCLVSeed.game_time <= now,
                        ),
                        and_(
                            AlertCLVSeed.game_time.is_(None),
                            AlertCLVSeed.alerted_at <= stale_cutoff,
                        ),
                    ),
                )
                .order_by(AlertCLVSeed.alerted_at.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_pending_clv_seeds(self, stale_hours: int = 24) -> int:
        """Count seeds that are ready for harvest (game_time passed or stale)."""
        from datetime import timedelta as _td
        from sqlalchemy import or_, and_

        now          = datetime.utcnow()
        stale_cutoff = now - _td(hours=stale_hours)

        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(AlertCLVSeed)
                .where(
                    AlertCLVSeed.clv_computed == False,       # noqa: E712
                    or_(
                        and_(
                            AlertCLVSeed.game_time.isnot(None),
                            AlertCLVSeed.game_time <= now,
                        ),
                        and_(
                            AlertCLVSeed.game_time.is_(None),
                            AlertCLVSeed.alerted_at <= stale_cutoff,
                        ),
                    ),
                )
            )
            return result.scalar() or 0

    async def mark_clv_seed_computed(self, seed_id: int, clv_pct: float) -> None:
        """Mark a seed as computed and store the resulting CLV%."""
        from sqlalchemy import update as sa_update
        async with self.session() as s:
            await s.execute(
                sa_update(AlertCLVSeed)
                .where(AlertCLVSeed.id == seed_id)
                .values(clv_pct=clv_pct, clv_computed=True)
            )
            await s.commit()

    async def mark_clv_seed_expired(self, seed_id: int) -> None:
        """
        Mark a seed as processed with no CLV data (closing odds unavailable).

        Sets clv_computed=True and clv_pct=None so the harvest job stops
        retrying this seed. Called after the grace period has passed without
        finding closing odds.
        """
        from sqlalchemy import update as sa_update
        async with self.session() as s:
            await s.execute(
                sa_update(AlertCLVSeed)
                .where(AlertCLVSeed.id == seed_id)
                .values(clv_pct=None, clv_computed=True)
            )
            await s.commit()

    async def get_last_odds_for_event(
        self, event: str, selection: str
    ) -> "Optional[OddsRecord]":
        """
        Return the most-recent OddsRecord for a given event+selection.

        Used by the CLV harvest job to find a proxy for closing odds when
        sportsbook polling is active.  Returns None if no odds are found.
        """
        async with self.session() as s:
            result = await s.execute(
                select(OddsRecord)
                .where(
                    OddsRecord.event     == event,
                    OddsRecord.selection == selection,
                )
                .order_by(desc(OddsRecord.recorded_at))  # OddsRecord uses recorded_at
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_clv_seeds_by_tier_stats(self) -> "dict[str, dict]":
        """
        Return avg CLV% grouped by tier from computed seeds.

        Returns: { "S": {"avg_clv": float, "count": int}, ... }
        Used by CalibrationEngine to populate tier CLV averages.
        """
        async with self.session() as s:
            result = await s.execute(
                select(
                    AlertCLVSeed.tier,
                    func.avg(AlertCLVSeed.clv_pct).label("avg_clv"),
                    func.count(AlertCLVSeed.id).label("count"),
                )
                .where(
                    AlertCLVSeed.clv_computed == True,   # noqa: E712
                    AlertCLVSeed.clv_pct.isnot(None),
                    AlertCLVSeed.tier.isnot(None),
                )
                .group_by(AlertCLVSeed.tier)
            )
            rows = result.all()
        return {
            row.tier: {"avg_clv": row.avg_clv, "count": row.count}
            for row in rows
            if row.tier
        }

    async def get_clv_stats_by_dimension(self) -> "dict[str, dict]":
        """
        Return CLV statistics grouped by sport, alert_type, market_type, and tier.

        Returns a dict keyed by dimension name, each containing a list of
        (value, avg_clv, count) tuples.

        Example:
            {
                "by_sport":   [("NFL", 2.1, 10), ...],
                "by_type":    [("EV", 1.8, 5), ...],
                "by_market":  [("h2h", 2.0, 4), ...],
                "by_tier":    [("S", 3.5, 3), ...],
            }
        """
        dims = [
            ("by_sport",  AlertCLVSeed.sport),
            ("by_type",   AlertCLVSeed.alert_type),
            ("by_market", AlertCLVSeed.market_type),
            ("by_tier",   AlertCLVSeed.tier),
        ]
        base_filter = (
            AlertCLVSeed.clv_computed == True,   # noqa: E712
            AlertCLVSeed.clv_pct.isnot(None),
        )
        result: dict[str, list] = {}
        for key, col in dims:
            async with self.session() as s:
                rows = (await s.execute(
                    select(
                        col.label("dim_val"),
                        func.avg(AlertCLVSeed.clv_pct).label("avg_clv"),
                        func.count(AlertCLVSeed.id).label("count"),
                    )
                    .where(*base_filter, col.isnot(None), col != "")
                    .group_by(col)
                    .order_by(func.avg(AlertCLVSeed.clv_pct).desc())
                )).all()
            result[key] = [
                (row.dim_val, round(row.avg_clv or 0, 3), row.count)
                for row in rows
            ]
        return result

    async def get_recent_underdog_snapshots(
        self, limit: int = 500
    ) -> "list[UnderdogSnapshotRecord]":
        """Return the most-recent Underdog snapshot records, newest first."""
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_clv_seed_for_source(
        self, source_table: str, source_id: int
    ) -> "Optional[AlertCLVSeed]":
        """Return the seed for a given source record, or None if not seeded yet."""
        async with self.session() as s:
            result = await s.execute(
                select(AlertCLVSeed).where(
                    AlertCLVSeed.source_table == source_table,
                    AlertCLVSeed.source_id    == source_id,
                )
            )
            return result.scalar_one_or_none()

    async def seed_clv_from_ev_records(self, limit: int = 200) -> int:
        """
        Create AlertCLVSeed entries for EV alerts that haven't been seeded yet.

        Queries ev_records where alert_sent=True and no matching seed exists,
        then bulk-inserts all seeds in a single transaction (ON CONFLICT DO NOTHING).
        Returns the number of new seeds created.

        Safe to call repeatedly — the UNIQUE constraint prevents duplicates.
        """
        from sqlalchemy import not_
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.session() as s:
            seeded_ids_subq = (
                select(AlertCLVSeed.source_id)
                .where(AlertCLVSeed.source_table == "ev_records")
                .scalar_subquery()
            )
            result = await s.execute(
                select(EVRecord)
                .where(
                    EVRecord.alert_sent == True,              # noqa: E712
                    not_(EVRecord.id.in_(seeded_ids_subq)),
                )
                .order_by(desc(EVRecord.detected_at))
                .limit(limit)
            )
            ev_rows = list(result.scalars().all())

        if not ev_rows:
            return 0

        # Build all value dicts first, then insert in ONE transaction.
        # This holds the write lock for the minimum possible time compared
        # with N separate save_alert_clv_seed() calls.
        rows = [
            dict(
                source_table     = "ev_records",
                source_id        = ev.id,
                alert_type       = "EV",
                sport            = ev.sport or "",
                market_type      = ev.market_type or "",
                event            = ev.event or "",
                selection        = ev.selection or "",
                bet_odds         = ev.best_odds,
                counterpart_odds = None,
                tier             = _tier_from_confidence(ev.ai_confidence),
                game_time        = None,
                alerted_at       = ev.detected_at,
                clv_pct          = None,
                clv_computed     = False,
            )
            for ev in ev_rows
        ]
        async with self.session() as s:
            stmt = (
                sqlite_insert(AlertCLVSeed)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source_table", "source_id"])
            )
            result = await s.execute(stmt)
            await s.commit()
            return result.rowcount if result.rowcount >= 0 else len(rows)

    async def seed_clv_from_ud_snapshots(self, limit: int = 200) -> int:
        """
        Create AlertCLVSeed entries for Underdog alerts that haven't been seeded.

        Bulk-inserts all seeds in a single transaction (ON CONFLICT DO NOTHING).
        Returns the number of new seeds created.
        """
        from sqlalchemy import not_
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.session() as s:
            seeded_ids_subq = (
                select(AlertCLVSeed.source_id)
                .where(AlertCLVSeed.source_table == "underdog_snapshots")
                .scalar_subquery()
            )
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.alert_sent == True,    # noqa: E712
                    not_(UnderdogSnapshotRecord.id.in_(seeded_ids_subq)),
                )
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            ud_rows = list(result.scalars().all())

        if not ud_rows:
            return 0

        rows = [
            dict(
                source_table     = "underdog_snapshots",
                source_id        = ud.id,
                alert_type       = "UNDERDOG",
                sport            = ud.sport or "",
                market_type      = ud.stat_type or "",
                event            = ud.game_id or "",
                selection        = f"{ud.player_name} {ud.stat_type} {ud.line_value:g}",
                bet_odds         = None,
                counterpart_odds = None,
                tier             = ud.score_tier or "",
                game_time        = ud.game_time,
                alerted_at       = ud.fetched_at,
                clv_pct          = None,
                clv_computed     = False,
            )
            for ud in ud_rows
        ]
        async with self.session() as s:
            stmt = (
                sqlite_insert(AlertCLVSeed)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source_table", "source_id"])
            )
            result = await s.execute(stmt)
            await s.commit()
            return result.rowcount if result.rowcount >= 0 else len(rows)

    # ── Underdog snapshots ───────────────────────────────────────────────────

    async def _migrate_underdog_snapshots(self) -> None:
        """
        Add analysis columns to underdog_snapshots if they don't exist yet.
        Idempotent — safe to call on every startup.
        """
        new_cols = [
            "ALTER TABLE underdog_snapshots ADD COLUMN line_delta REAL",
            "ALTER TABLE underdog_snapshots ADD COLUMN score_total REAL",
            "ALTER TABLE underdog_snapshots ADD COLUMN score_tier TEXT",
            "ALTER TABLE underdog_snapshots ADD COLUMN score_stars INTEGER",
            "ALTER TABLE underdog_snapshots ADD COLUMN alert_outcome TEXT",
            "ALTER TABLE underdog_snapshots ADD COLUMN validation_json TEXT",
            "ALTER TABLE underdog_snapshots ADD COLUMN bet_recommendation TEXT",
            "ALTER TABLE underdog_snapshots ADD COLUMN bet_confidence INTEGER",
            "ALTER TABLE underdog_snapshots ADD COLUMN bet_reason TEXT",
            "ALTER TABLE underdog_snapshots ADD COLUMN bet_evidence_json TEXT",
        ]
        async with self._engine.begin() as conn:
            for sql in new_cols:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass  # column already exists — safe to ignore

    async def save_underdog_snapshot(self, record: "UnderdogSnapshotRecord") -> "UnderdogSnapshotRecord":
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def save_underdog_snapshots_bulk(
        self,
        records: "list[UnderdogSnapshotRecord]",
    ) -> None:
        """Insert a list of UnderdogSnapshotRecords in a single transaction.

        Used by the cold-start path to avoid opening one SQLite write
        transaction per prop (which causes lock contention with concurrent
        background jobs).
        """
        if not records:
            return
        async with self.session() as s:
            s.add_all(records)
            await s.commit()

    async def get_recent_underdog_snapshots(self, limit: int = 50) -> list["UnderdogSnapshotRecord"]:
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_known_underdog_prop_keys(self) -> "set[tuple[str, str]]":
        """
        Return every (player_name, stat_type) pair ever stored in
        underdog_snapshots, including removed rows.

        Used at the start of each underdog_job cycle to detect genuinely
        new props on their very first appearance.  Including removed rows
        ensures a re-listed prop is NOT re-flagged as new.
        """
        async with self.session() as s:
            result = await s.execute(
                select(
                    UnderdogSnapshotRecord.player_name,
                    UnderdogSnapshotRecord.stat_type,
                ).distinct()
            )
            return {(row[0], row[1]) for row in result.all()}

    async def get_latest_underdog_snapshot_per_prop(
        self,
    ) -> "dict[tuple[str, str], UnderdogSnapshotRecord]":
        """
        Return the single most-recent non-removed snapshot for every active
        (player_name, stat_type) pair, keyed for O(1) lookup.

        Uses MAX(id) per group — one SQL round-trip, no LIMIT, covers the full
        feed regardless of how many props are active.  The autoincrement ``id``
        is a reliable tiebreaker when multiple rows share the same
        ``fetched_at`` (which happens because every job cycle writes an entire
        batch at once).

        Replaces ``get_recent_underdog_snapshots(limit=N)`` in
        ``underdog_job`` where a fixed N was too small to cover all props.
        """
        async with self.session() as s:
            # Subquery: the MAX(id) for each (player, stat) among non-removed rows
            subq = (
                select(func.max(UnderdogSnapshotRecord.id))
                .where(UnderdogSnapshotRecord.removed == False)  # noqa: E712
                .group_by(
                    UnderdogSnapshotRecord.player_name,
                    UnderdogSnapshotRecord.stat_type,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(UnderdogSnapshotRecord).where(
                    UnderdogSnapshotRecord.id.in_(subq)
                )
            )
            rows = result.scalars().all()
        return {(r.player_name, r.stat_type): r for r in rows}

    async def count_underdog_records(self) -> int:
        async with self.session() as s:
            result = await s.execute(select(func.count()).select_from(UnderdogSnapshotRecord))
            return result.scalar() or 0

    async def get_ud_prop_history(
        self,
        player_name: str,
        stat_type: str,
        limit: int = 20,
    ) -> "list[UnderdogSnapshotRecord]":
        """
        Return up to *limit* most-recent records for a specific player + stat
        combination, ordered most-recent-first.  Removal records are excluded
        so they do not distort line-value statistics.
        """
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.player_name == player_name,
                    UnderdogSnapshotRecord.stat_type   == stat_type,
                    UnderdogSnapshotRecord.removed     == False,  # noqa: E712
                )
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def has_recent_ud_alert(
        self,
        player_name: str,
        stat_type: str,
        within_seconds: int = 86400,
    ) -> bool:
        """True if an Underdog alert was sent for this player+stat within *within_seconds*.

        Used by the standing-opportunity scan (4A) to prevent re-alerting
        the same prop more than once per day.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(seconds=within_seconds)
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.player_name == player_name,
                    UnderdogSnapshotRecord.stat_type   == stat_type,
                    UnderdogSnapshotRecord.alert_sent  == True,   # noqa: E712
                    UnderdogSnapshotRecord.fetched_at  >= cutoff,
                )
            )
            return (result.scalar() or 0) > 0

    async def has_recent_inefficiency_alert(
        self,
        event: str,
        selection: str,
        sportsbook: str,
        within_seconds: int = 1800,
    ) -> bool:
        """True if a market inefficiency alert for this market/book was sent recently."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(seconds=within_seconds)
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(MarketSnapshotRecord)
                .where(
                    MarketSnapshotRecord.event == event,
                    MarketSnapshotRecord.selection == selection,
                    MarketSnapshotRecord.sportsbook == sportsbook,
                    MarketSnapshotRecord.alert_sent == True,  # noqa: E712
                    MarketSnapshotRecord.recorded_at >= cutoff,
                )
            )
            return (result.scalar() or 0) > 0

    # ── AI Ranking / Performance tracking ────────────────────────────────────

    async def get_ev_records_with_results(
        self,
        *,
        sport: Optional[str] = None,
        market_type: Optional[str] = None,
        limit: int = 500,
        include_pending: bool = False,
    ) -> list["EVRecord"]:
        """
        Return EVRecord rows that have a resolved result.

        By default, only WIN / LOSS / PUSH rows are returned.
        Set include_pending=True to also include PENDING rows.

        Parameters
        ----------
        sport           Filter to a specific sport (exact match).
        market_type     Filter to a specific market type (exact match).
        limit           Maximum number of rows to return (most recent first).
        include_pending Also include PENDING result rows.
        """
        async with self.session() as s:
            q = select(EVRecord).order_by(desc(EVRecord.detected_at))
            if sport:
                q = q.where(EVRecord.sport == sport)
            if market_type:
                q = q.where(EVRecord.market_type == market_type)
            if not include_pending:
                q = q.where(EVRecord.result.in_(["WIN", "LOSS", "PUSH"]))
            result = await s.execute(q.limit(limit))
            return list(result.scalars().all())

    async def update_ev_record_result(
        self,
        record_id: int,
        result: str,
        clv: Optional[float] = None,
    ) -> None:
        """
        Update the result and optionally the CLV for a specific EVRecord.

        Parameters
        ----------
        record_id   Primary key of the EVRecord to update.
        result      One of WIN / LOSS / PUSH / PENDING.
        clv         Closing Line Value percentage (optional).
        """
        from sqlalchemy import update as sa_update
        values: dict = {"result": result.upper()}
        if clv is not None:
            values["clv"] = clv
        async with self.session() as s:
            await s.execute(
                sa_update(EVRecord)
                .where(EVRecord.id == record_id)
                .values(**values)
            )
            await s.commit()
        logger.debug("Updated EVRecord %d → result=%s clv=%s", record_id, result, clv)

    # ── Player game results ──────────────────────────────────────────────────

    async def upsert_player_result(self, raw: object) -> None:
        """
        Insert or update a PlayerGameResult row.

        *raw* must have attributes matching ``providers.player_stats.RawGameResult``:
        player_name, sport, stat_type, game_date, actual_value, opponent, source.

        If a row already exists for (player_name, sport, stat_type, game_date)
        the actual_value and opponent are refreshed; no duplicate is created.
        """
        player_name  = raw.player_name
        sport        = raw.sport
        stat_type    = raw.stat_type.lower().strip()
        game_date    = raw.game_date
        actual_value = raw.actual_value
        opponent     = raw.opponent
        source       = raw.source

        async with self.session() as s:
            result = await s.execute(
                select(PlayerGameResult).where(
                    PlayerGameResult.player_name == player_name,
                    PlayerGameResult.sport       == sport,
                    PlayerGameResult.stat_type   == stat_type,
                    PlayerGameResult.game_date   == game_date,
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                s.add(PlayerGameResult(
                    player_name  = player_name,
                    sport        = sport,
                    stat_type    = stat_type,
                    game_date    = game_date,
                    opponent     = opponent,
                    actual_value = actual_value,
                    source       = source,
                ))
            else:
                record.actual_value = actual_value
                if opponent:
                    record.opponent = opponent
                record.fetched_at = datetime.utcnow()
            await s.commit()

    async def get_player_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        limit: int = 30,
    ) -> "list[PlayerGameResult]":
        """
        Return up to *limit* most-recent game results for the given player + sport
        + stat combination, ordered newest-first by game_date.
        """
        async with self.session() as s:
            result = await s.execute(
                select(PlayerGameResult)
                .where(
                    PlayerGameResult.player_name == player_name,
                    PlayerGameResult.sport       == sport,
                    PlayerGameResult.stat_type   == stat_type.lower().strip(),
                )
                .order_by(desc(PlayerGameResult.game_date))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_player_results(
        self,
        player_name: Optional[str] = None,
        sport: Optional[str] = None,
    ) -> int:
        """Count PlayerGameResult rows, optionally filtered by player or sport."""
        async with self.session() as s:
            q = select(func.count()).select_from(PlayerGameResult)
            if player_name:
                q = q.where(PlayerGameResult.player_name == player_name)
            if sport:
                q = q.where(PlayerGameResult.sport == sport)
            result = await s.execute(q)
            return result.scalar() or 0

    # ── Prop Opportunity Tracking ────────────────────────────────────────────

    async def _migrate_prop_line_history_v2(self) -> None:
        """Add opening_line column to prop_line_history (Phase 2)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_line_history ADD COLUMN opening_line REAL"
                ))
                logger.info("_migrate_prop_line_history_v2: added opening_line column")
            except Exception:
                pass  # already exists

    async def _migrate_prop_line_history_v3(self) -> None:
        """Add score_tier column to prop_line_history (idempotent)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_line_history ADD COLUMN score_tier TEXT"
                ))
                logger.info("_migrate_prop_line_history_v3: added score_tier column")
            except Exception:
                pass  # already exists

    async def _migrate_prop_opportunity_log(self) -> None:
        """Add error_type column to existing tables (learning label classification)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_opportunity_log ADD COLUMN error_type VARCHAR(16)"
                ))
                logger.info(
                    "_migrate_prop_opportunity_log: added error_type column"
                )
            except Exception:
                pass  # column already exists (SQLite raises OperationalError)

    async def _migrate_prop_opportunity_log_v2(self) -> None:
        """Add Phase 2 enrichment columns to prop_opportunity_log."""
        async with self._engine.begin() as conn:
            for col_sql in [
                "ALTER TABLE prop_opportunity_log ADD COLUMN stars INTEGER",
                "ALTER TABLE prop_opportunity_log ADD COLUMN risk_level VARCHAR(16)",
                "ALTER TABLE prop_opportunity_log ADD COLUMN explanation TEXT",
                "ALTER TABLE prop_opportunity_log ADD COLUMN void_reason VARCHAR(64)",
            ]:
                try:
                    await conn.execute(text(col_sql))
                except Exception:
                    pass  # column already exists
        logger.info("_migrate_prop_opportunity_log_v2: Phase 2 columns ensured")

    async def _migrate_prop_opportunity_log_v3(self) -> None:
        """Add Phase 4 Evidence Infrastructure columns to prop_opportunity_log (idempotent)."""
        async with self._engine.begin() as conn:
            for col_sql in [
                "ALTER TABLE prop_opportunity_log ADD COLUMN recommendation_id VARCHAR(64)",
                "ALTER TABLE prop_opportunity_log ADD COLUMN provider VARCHAR(32)",
                "ALTER TABLE prop_opportunity_log ADD COLUMN bet_quality_score INTEGER",
                "ALTER TABLE prop_opportunity_log ADD COLUMN qualification_path TEXT",
                "ALTER TABLE prop_opportunity_log ADD COLUMN reason_codes TEXT",
                "ALTER TABLE prop_opportunity_log ADD COLUMN watchlist_state VARCHAR(16)",
                "ALTER TABLE prop_opportunity_log ADD COLUMN settlement_source VARCHAR(64)",
                "ALTER TABLE prop_opportunity_log ADD COLUMN manual_opinion VARCHAR(8)",
            ]:
                try:
                    await conn.execute(text(col_sql))
                except Exception:
                    pass  # column already exists
        logger.info("_migrate_prop_opportunity_log_v3: Phase 4 columns ensured")

    async def log_prop_opportunity(
        self,
        *,
        external_id: str,
        player_name: str,
        team: str,
        sport: str,
        stat_type: str,
        line_value: float,
        recommendation: str,            # OVER | UNDER | PASS
        decision_tier: str,             # S | A | B | PASS
        confidence: int,
        game_time: "Optional[datetime]",
        # Phase 2 enrichment — optional, captured at alert time
        stars:       "Optional[int]"   = None,   # 0–5 stars
        risk_level:  "Optional[str]"   = None,   # "LOW" | "MEDIUM" | "HIGH"
        explanation: "Optional[str]"   = None,   # reason / narrative excerpt
        # Phase 4 Evidence Infrastructure — optional
        provider:           "Optional[str]"  = None,   # "Underdog" | "PrizePicks"
        bet_quality_score:  "Optional[int]"  = None,   # 0-100 (== confidence for now)
        qualification_path: "Optional[list]" = None,   # gates passed, JSON-encoded
        reason_codes:       "Optional[list]" = None,   # structured codes, JSON-encoded
        watchlist_state:    "Optional[str]"  = None,   # Qualified|Watchlist|Rejected|Removed
    ) -> None:
        """
        Upsert a prop opportunity at evaluation time (PLAY or PASS).

        On conflict with the same (external_id, stat_type) — i.e. the prop was
        re-evaluated in a later cycle — updates recommendation, tier, confidence,
        line_value, and enrichment to reflect the latest decision.
        Preserves any existing grading (result / actual_value / graded_at) so
        re-evaluation does not wipe a completed outcome.

        Extended result codes accepted by grade_opportunity:
          HIT | MISS | PUSH | PENDING | VOID | CANCELLED | INJURY_VOID | GAME_INTERRUPTED
        """
        import hashlib
        import json as _json
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
        now    = datetime.utcnow()
        rec_id = hashlib.sha256(
            f"{external_id}:{stat_type}".encode()
        ).hexdigest()[:16]
        _qpath = _json.dumps(qualification_path) if qualification_path else None
        _rcodes = _json.dumps(reason_codes) if reason_codes else None
        stmt = (
            _sqlite_insert(PropOpportunityLog)
            .values(
                external_id        = external_id,
                player_name        = player_name,
                team               = team,
                sport              = sport,
                stat_type          = stat_type,
                line_value         = line_value,
                recommendation     = recommendation,
                decision_tier      = decision_tier,
                confidence         = confidence,
                game_time          = game_time,
                detected_at        = now,
                result             = "PENDING",
                actual_value       = None,
                graded_at          = None,
                stars              = stars,
                risk_level         = risk_level,
                explanation        = explanation,
                recommendation_id  = rec_id,
                provider           = provider,
                bet_quality_score  = bet_quality_score,
                qualification_path = _qpath,
                reason_codes       = _rcodes,
                watchlist_state    = watchlist_state,
            )
            .on_conflict_do_update(
                index_elements = ["external_id", "stat_type"],
                set_           = {
                    "recommendation":     recommendation,
                    "decision_tier":      decision_tier,
                    "confidence":         confidence,
                    "line_value":         line_value,
                    "stars":              stars,
                    "risk_level":         risk_level,
                    "explanation":        explanation,
                    "recommendation_id":  rec_id,
                    "provider":           provider,
                    "bet_quality_score":  bet_quality_score,
                    "qualification_path": _qpath,
                    "reason_codes":       _rcodes,
                    "watchlist_state":    watchlist_state,
                }
            )
        )
        async with self.session() as s:
            await s.execute(stmt)
            await s.commit()

    async def log_prop_candidate_batch(
        self,
        candidates: "list[dict]",
    ) -> int:
        """
        Batch-insert PropCandidateLog rows from a list of scored-prop dicts.

        Each dict must have keys matching PropCandidateLog columns.
        Keys: scan_ts, player_name, team, sport, stat_type, line_value,
              provider, score_total, score_tier, confidence, gate_decision,
              rejection_reason, reason_codes (pre-serialised JSON str), snapshot_external_id.

        Returns count of rows inserted.
        """
        if not candidates:
            return 0
        async with self.session() as s:
            for c in candidates:
                row = PropCandidateLog(
                    scan_ts              = c.get("scan_ts") or datetime.utcnow(),
                    player_name          = c.get("player_name", ""),
                    team                 = c.get("team", ""),
                    sport                = c.get("sport", ""),
                    stat_type            = c.get("stat_type", ""),
                    line_value           = float(c.get("line_value") or 0.0),
                    provider             = c.get("provider", "Underdog"),
                    score_total          = c.get("score_total"),
                    score_tier           = c.get("score_tier"),
                    confidence           = c.get("confidence"),
                    gate_decision        = c.get("gate_decision", "REJECTED"),
                    rejection_reason     = c.get("rejection_reason"),
                    reason_codes         = c.get("reason_codes"),
                    snapshot_external_id = c.get("snapshot_external_id"),
                )
                s.add(row)
            await s.commit()
        return len(candidates)

    async def get_funnel_summary(self, since_hours: int = 24) -> dict:
        """
        Aggregate PropCandidateLog gate decisions for the /funnel command.

        Returns:
          {
            "since_hours": int,
            "counts": {"ACCEPTED": n, "WATCHLIST": n, "REJECTED": n, "REMOVED": n},
            "top_rejections": [{"player_name", "sport", "stat_type",
                                 "rejection_reason", "score_tier", "score_total"}],
          }
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        # Gate-decision counts
        async with self.session() as s:
            cnt_rows = await s.execute(
                select(
                    PropCandidateLog.gate_decision,
                    func.count(PropCandidateLog.id).label("n"),
                )
                .where(PropCandidateLog.scan_ts >= cutoff)
                .group_by(PropCandidateLog.gate_decision)
            )
            counts = {r.gate_decision: r.n for r in cnt_rows.all()}

        # Recent rejections — highest score first so near-misses surface first
        async with self.session() as s:
            rej_rows = await s.execute(
                select(
                    PropCandidateLog.player_name,
                    PropCandidateLog.sport,
                    PropCandidateLog.stat_type,
                    PropCandidateLog.rejection_reason,
                    PropCandidateLog.score_tier,
                    PropCandidateLog.score_total,
                )
                .where(
                    PropCandidateLog.scan_ts    >= cutoff,
                    PropCandidateLog.gate_decision == "REJECTED",
                )
                .order_by(desc(PropCandidateLog.score_total))
                .limit(8)
            )
            top_rej = [dict(r._mapping) for r in rej_rows.all()]

        return {
            "since_hours":    since_hours,
            "counts":         counts,
            "top_rejections": top_rej,
        }

    async def get_pending_opportunities(
        self, cutoff_hours: int = 4
    ) -> "list[PropOpportunityLog]":
        """
        Return PENDING opportunities whose game_time was at least cutoff_hours ago.

        Used by the opportunity_grader job to find outcomes that can now be
        looked up in player_game_results.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=cutoff_hours)
        async with self.session() as s:
            result = await s.execute(
                select(PropOpportunityLog)
                .where(
                    PropOpportunityLog.result    == "PENDING",
                    PropOpportunityLog.game_time.isnot(None),
                    PropOpportunityLog.game_time <  cutoff,
                )
                .order_by(PropOpportunityLog.game_time)
                .limit(500)
            )
            return list(result.scalars().all())

    async def grade_opportunity(
        self,
        opp_id:       int,
        result:       str,
        actual_value: float,
        error_type:   Optional[str] = None,
        void_reason:  Optional[str] = None,
    ) -> None:
        """
        Set the outcome (and optional learning label) on a prop opportunity
        after the game completes.

        Parameters
        ----------
        opp_id       : PropOpportunityLog.id
        result       : "HIT" | "MISS" | "PUSH" | "VOID" | "CANCELLED" |
                       "INJURY_VOID" | "GAME_INTERRUPTED"
        actual_value : The recorded stat value from the game result
                       (pass 0.0 for void/cancelled outcomes).
        error_type   : Learning label — "Model" | "Market" | "Settlement" |
                       "Variance" | None.  Should be set on MISS outcomes only.
                       Determines whether this miss should update scoring weights.
        void_reason  : Free-text reason string for VOID/CANCELLED/INJURY_VOID/
                       GAME_INTERRUPTED outcomes (stored in void_reason column).
        """
        from sqlalchemy import update as _sa_update
        values: dict = {
            "result":       result,
            "actual_value": actual_value,
            "graded_at":    datetime.utcnow(),
        }
        if error_type is not None:
            values["error_type"] = error_type
        if void_reason is not None:
            values["void_reason"] = str(void_reason)[:64]
        async with self.session() as s:
            await s.execute(
                _sa_update(PropOpportunityLog)
                .where(PropOpportunityLog.id == opp_id)
                .values(**values)
            )
            await s.commit()

    async def get_learning_rollups(self) -> "dict":
        """
        Return learning-focused performance rollups for the /rollups command.

        Returns a dict with:
          by_tier       — { tier → {W, L, P, total, win_pct} }  (graded PLAY rows only)
          by_sport      — { sport → {W, L, P, total, win_pct} }
          by_stat_type  — { stat_type → {W, L, P, total, win_pct} } (top 15 by volume)
          by_error_type — { error_type → count }   (MISS rows with a label)
          player_trend  — [ {player, sport, stat_type, W, L, P} ] top 10 by volume
          total_graded  — int (all HIT/MISS/PUSH rows)
        """
        def _record(rows_iter) -> dict:
            """Accumulate (rec, result, n) tuples into W/L/P buckets."""
            acc: dict = {}
            for key, rec, res, n in rows_iter:
                entry = acc.setdefault(key, {"W": 0, "L": 0, "P": 0, "total": 0})
                entry["total"] += n
                if rec == "OVER":
                    if res == "HIT":   entry["W"] += n
                    elif res == "MISS": entry["L"] += n
                    else:              entry["P"] += n
                elif rec == "UNDER":
                    if res == "MISS":  entry["W"] += n  # under cleared = over failed
                    elif res == "HIT": entry["L"] += n
                    else:              entry["P"] += n
            for v in acc.values():
                t = v["W"] + v["L"]
                v["win_pct"] = round(v["W"] / t * 100, 1) if t else 0.0
            return acc

        graded_filter = PropOpportunityLog.result.in_(["HIT", "MISS", "PUSH"])
        play_filter   = PropOpportunityLog.recommendation.in_(["OVER", "UNDER"])

        async with self.session() as s:
            # ── by_tier ──────────────────────────────────────────────────────
            tier_rows = await s.execute(
                select(
                    PropOpportunityLog.decision_tier,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).where(graded_filter, play_filter).group_by(
                    PropOpportunityLog.decision_tier,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            by_tier = _record(tier_rows.all())

            # ── by_sport ─────────────────────────────────────────────────────
            sport_rows = await s.execute(
                select(
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).where(graded_filter, play_filter).group_by(
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            by_sport = _record(sport_rows.all())

            # ── by_stat_type (top 15 by volume) ──────────────────────────────
            stat_rows = await s.execute(
                select(
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).where(graded_filter, play_filter).group_by(
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            by_stat_type_raw = _record(stat_rows.all())
            # Sort by volume, keep top 15
            by_stat_type = dict(
                sorted(by_stat_type_raw.items(), key=lambda kv: -kv[1]["total"])[:15]
            )

            # ── by_error_type ─────────────────────────────────────────────────
            err_rows = await s.execute(
                select(
                    PropOpportunityLog.error_type,
                    func.count(PropOpportunityLog.id).label("n"),
                ).where(
                    PropOpportunityLog.result      == "MISS",
                    PropOpportunityLog.error_type.isnot(None),
                ).group_by(PropOpportunityLog.error_type)
            )
            by_error_type = {row.error_type: row.n for row in err_rows.all()}

            # ── player_trend (top 10 by graded volume) ────────────────────────
            player_rows = await s.execute(
                select(
                    PropOpportunityLog.player_name,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).where(graded_filter, play_filter).group_by(
                    PropOpportunityLog.player_name,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            _player_acc: dict = {}
            for player, sport, stat, rec, res, n in player_rows.all():
                key   = (player, sport, stat)
                entry = _player_acc.setdefault(key, {"W": 0, "L": 0, "P": 0, "total": 0})
                entry["total"] += n
                if rec == "OVER":
                    if res == "HIT":    entry["W"] += n
                    elif res == "MISS": entry["L"] += n
                    else:              entry["P"] += n
                elif rec == "UNDER":
                    if res == "MISS":   entry["W"] += n
                    elif res == "HIT":  entry["L"] += n
                    else:              entry["P"] += n
            player_trend = [
                {
                    "player": k[0], "sport": k[1], "stat_type": k[2],
                    "W": v["W"], "L": v["L"], "P": v["P"], "total": v["total"],
                    "win_pct": round(v["W"] / (v["W"] + v["L"]) * 100, 1)
                              if (v["W"] + v["L"]) > 0 else 0.0,
                }
                for k, v in sorted(_player_acc.items(), key=lambda kv: -kv[1]["total"])[:10]
            ]

            # ── total_graded ──────────────────────────────────────────────────
            total_row = await s.execute(
                select(func.count(PropOpportunityLog.id)).where(graded_filter, play_filter)
            )
            total_graded = total_row.scalar() or 0

        return {
            "by_tier":      by_tier,
            "by_sport":     by_sport,
            "by_stat_type": by_stat_type,
            "by_error_type": by_error_type,
            "player_trend": player_trend,
            "total_graded": total_graded,
        }

    async def get_game_result_for_grading(
        self, player_name: str, sport: str, stat_type: str, game_date: str
    ) -> "Optional[PlayerGameResult]":
        """Return the PlayerGameResult for a given player / sport / stat / date, or None."""
        async with self.session() as s:
            result = await s.execute(
                select(PlayerGameResult)
                .where(
                    PlayerGameResult.player_name == player_name,
                    PlayerGameResult.sport       == sport,
                    PlayerGameResult.stat_type   == stat_type,
                    PlayerGameResult.game_date   == game_date,
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_tracking_summary(self) -> "dict":
        """
        Return aggregate tracking statistics for the /tracking command.

        Returns a dict with:
          counts:   { recommendation → { result → count } }
          by_tier:  { tier → { recommendation → { result → count } } }
          by_sport: { sport → { recommendation → { result → count } } }
          total:    int  (all rows ever logged)
          pending:  int  (awaiting grading)
        """
        async with self.session() as s:
            # Overall rec × result
            rows = await s.execute(
                select(
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).group_by(
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            counts: dict = {}
            for rec, res, n in rows.all():
                counts.setdefault(rec, {})[res] = n

            # By decision_tier
            tier_rows = await s.execute(
                select(
                    PropOpportunityLog.decision_tier,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).group_by(
                    PropOpportunityLog.decision_tier,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            by_tier: dict = {}
            for tier, rec, res, n in tier_rows.all():
                by_tier.setdefault(tier, {}).setdefault(rec, {})[res] = n

            # By sport
            sport_rows = await s.execute(
                select(
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).group_by(
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                )
            )
            by_sport: dict = {}
            for sport, rec, res, n in sport_rows.all():
                by_sport.setdefault(sport, {}).setdefault(rec, {})[res] = n

            # Totals
            total_row = await s.execute(
                select(func.count(PropOpportunityLog.id))
            )
            total = total_row.scalar() or 0

            pending_row = await s.execute(
                select(func.count(PropOpportunityLog.id))
                .where(PropOpportunityLog.result == "PENDING")
            )
            pending = pending_row.scalar() or 0

        return {
            "counts":   counts,
            "by_tier":  by_tier,
            "by_sport": by_sport,
            "total":    total,
            "pending":  pending,
        }

    # ── Player Block Management ───────────────────────────────────────────────

    async def get_active_blocks(
        self,
        sport: Optional[str] = None,
    ) -> list[PlayerRiskRecord]:
        """Return all active PlayerRiskRecord rows, optionally filtered by sport."""
        async with self.session() as s:
            stmt = (
                select(PlayerRiskRecord)
                .where(PlayerRiskRecord.is_active == True)  # noqa: E712
            )
            if sport:
                stmt = stmt.where(
                    (PlayerRiskRecord.sport == sport) |
                    (PlayerRiskRecord.sport == "")
                )
            result = await s.execute(stmt.order_by(PlayerRiskRecord.player_name))
            return list(result.scalars().all())

    async def add_player_block(self, record: PlayerRiskRecord) -> PlayerRiskRecord:
        """Upsert a player block (replace existing block for same player/sport/reason)."""
        async with self.session() as s:
            # Deactivate existing active block for this player/sport combo
            await s.execute(
                __import__("sqlalchemy", fromlist=["update"]).update(PlayerRiskRecord)
                .where(
                    PlayerRiskRecord.player_key == record.player_key,
                    PlayerRiskRecord.sport      == record.sport,
                    PlayerRiskRecord.is_active  == True,  # noqa: E712
                )
                .values(is_active=False)
            )
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def remove_player_block(
        self,
        player_key: str,
        sport:      str = "",
    ) -> bool:
        """
        Deactivate all active blocks for *player_key* (optionally filtered by sport).

        Returns True if at least one block was deactivated.
        """
        async with self.session() as s:
            stmt = (
                __import__("sqlalchemy", fromlist=["update"]).update(PlayerRiskRecord)
                .where(
                    PlayerRiskRecord.player_key == player_key,
                    PlayerRiskRecord.is_active  == True,  # noqa: E712
                )
            )
            if sport:
                stmt = stmt.where(PlayerRiskRecord.sport == sport)
            result = await s.execute(stmt.values(is_active=False))
            await s.commit()
            return (result.rowcount or 0) > 0

    async def count_active_blocks(self) -> int:
        """Count currently active player blocks."""
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(PlayerRiskRecord)
                .where(PlayerRiskRecord.is_active == True)  # noqa: E712
            )
            return result.scalar() or 0

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            logger.info("Database connection closed.")
