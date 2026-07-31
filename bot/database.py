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
    Boolean, Column, DateTime, Float, Integer, String, Text,
    UniqueConstraint,
    select, func, desc, text
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

        self._engine = create_async_engine(self._url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate_pp_edge_records()
        await self._migrate_underdog_snapshots()
        await self._migrate_clv_records()
        logger.info("Database initialised at %s", self._url)

    def session(self) -> AsyncSession:
        if self._session_factory is None:
            raise RuntimeError("Database.init() has not been called.")
        return self._session_factory()

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
        Bridge Underdog snapshot records into the shared PropLineHistory table.

        Reads recent, non-removed UnderdogSnapshotRecord rows whose
        (player_name, sport, stat_type, fetched_at) combination does not yet
        exist in PropLineHistory, then bulk-inserts them.

        This ensures Underdog data flows into the same normalized model as
        PrizePicks data, enabling cross-provider comparison and line history.

        Returns the number of new PropLineHistory rows written.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)

        # Load recent Underdog snapshots (non-removed only)
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.fetched_at >= cutoff,
                    UnderdogSnapshotRecord.removed    == False,   # noqa: E712
                )
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            snaps = list(result.scalars().all())

        if not snaps:
            return 0

        # Find already-bridged combinations so we don't duplicate
        # Key: (player_name, sport, stat_type, fetched_at rounded to minute)
        cutoff_dt = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            existing_result = await s.execute(
                select(
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                    PropLineHistory.external_id,
                )
                .where(
                    PropLineHistory.provider   == "Underdog",
                    PropLineHistory.fetched_at >= cutoff_dt,
                )
            )
            existing_keys = {
                (row.player_name, row.sport, row.stat_type, row.external_id)
                for row in existing_result.all()
            }

        # Build new records for snapshots not yet bridged
        new_records: list[PropLineHistory] = []
        for snap in snaps:
            key = (
                snap.player_name or "",
                snap.sport       or "",
                snap.stat_type   or "",
                snap.external_id or "",
            )
            if key in existing_keys:
                continue
            new_records.append(PropLineHistory(
                provider    = "Underdog",
                sport       = snap.sport       or "",
                player_name = snap.player_name or "",
                team        = snap.team        or "",
                stat_type   = snap.stat_type   or "",
                line_value  = float(snap.line_value) if snap.line_value is not None else 0.0,
                game_time   = snap.game_time,
                external_id = snap.external_id or "",
                game_id     = snap.game_id     or "",
                fetched_at  = snap.fetched_at  or datetime.utcnow(),
            ))
            existing_keys.add(key)   # prevent duplicates within this batch

        if new_records:
            await self.save_prop_line_history_bulk(new_records)

        return len(new_records)

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

    async def get_pending_clv_seeds(self, limit: int = 100) -> "list[AlertCLVSeed]":
        """
        Return seeds where game_time has passed but CLV has not been computed.

        These are ready for the harvest job to fetch closing odds and compute CLV.
        """
        now = datetime.utcnow()
        async with self.session() as s:
            result = await s.execute(
                select(AlertCLVSeed)
                .where(
                    AlertCLVSeed.clv_computed == False,       # noqa: E712
                    AlertCLVSeed.game_time.isnot(None),
                    AlertCLVSeed.game_time <= now,
                )
                .order_by(AlertCLVSeed.game_time.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_pending_clv_seeds(self) -> int:
        """Count seeds where game_time has passed but CLV not yet computed."""
        now = datetime.utcnow()
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(AlertCLVSeed)
                .where(
                    AlertCLVSeed.clv_computed == False,       # noqa: E712
                    AlertCLVSeed.game_time.isnot(None),
                    AlertCLVSeed.game_time <= now,
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
        then creates seeds.  Returns the number of new seeds created.

        Safe to call repeatedly — the UNIQUE constraint prevents duplicates.
        """
        from sqlalchemy import not_

        async with self.session() as s:
            # Find ev_records that are alerted but not yet seeded
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

        created = 0
        for ev in ev_rows:
            seed = AlertCLVSeed(
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
                game_time        = None,    # ev_records don't store game_time currently
                alerted_at       = ev.detected_at,
                clv_computed     = False,
            )
            await self.save_alert_clv_seed(seed)
            created += 1

        return created

    async def seed_clv_from_ud_snapshots(self, limit: int = 200) -> int:
        """
        Create AlertCLVSeed entries for Underdog alerts that haven't been seeded.

        Returns the number of new seeds created.
        """
        from sqlalchemy import not_

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

        created = 0
        for ud in ud_rows:
            seed = AlertCLVSeed(
                source_table     = "underdog_snapshots",
                source_id        = ud.id,
                alert_type       = "UNDERDOG",
                sport            = ud.sport or "",
                market_type      = ud.stat_type or "",
                event            = ud.game_id or "",
                selection        = f"{ud.player_name} {ud.stat_type} {ud.line_value:g}",
                bet_odds         = None,   # Underdog is pick'em — no American odds
                counterpart_odds = None,
                tier             = ud.score_tier or "",
                game_time        = ud.game_time,
                alerted_at       = ud.fetched_at,
                clv_computed     = False,
            )
            await self.save_alert_clv_seed(seed)
            created += 1

        return created

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

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            logger.info("Database connection closed.")
