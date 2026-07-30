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
    select, func, desc
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


# ── Database manager ──────────────────────────────────────────────────────────

class Database:
    """Async database manager. Call `init()` once at startup."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._engine = None
        self._session_factory = None

    async def init(self) -> None:
        """Create engine, run migrations, and ensure tables exist."""
        # Ensure the data directory exists for SQLite
        if self._url.startswith("sqlite"):
            db_path = self._url.replace("sqlite+aiosqlite:///", "")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self._engine = create_async_engine(self._url, echo=False)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            logger.info("Database connection closed.")
