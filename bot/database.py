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
    select, func, desc, text
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


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
    alert_outcome = Column(String(64), nullable=True)
    fetched_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)


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

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            logger.info("Database connection closed.")
