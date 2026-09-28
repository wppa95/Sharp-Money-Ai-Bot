"""
Database module — async SQLAlchemy + SQLite via aiosqlite.
Stores odds history, alerts, and EV opportunities for historical tracking.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import time
import weakref
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float,
    Integer, String, Text,
    Index,
    UniqueConstraint,
    select, func, desc, text, delete, or_, and_, case, tuple_, update
)
from sqlalchemy import insert as sa_insert
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

_UNDERDOG_BULK_MAX_RETRIES = 4
_UNDERDOG_BULK_RETRY_DELAYS = (1.0, 3.0, 7.0, 15.0)
_UNDERDOG_CANTOPEN_RETRY_DELAYS = (1.0, 3.0, 10.0, 30.0)
# SQLite has one writer slot. Keep monitoring commits short enough that
# grading/history/command work can acquire that slot between feed chunks.
# The caller still receives one aggregate count, so chunking never changes
# the durability contract or drops a record.
_UNDERDOG_BULK_BATCH_SIZE = 200
_PROP_HISTORY_BATCH_SIZE = 200
_TRACKING_BATCH_SIZE = 200
_CLV_BULK_BATCH_SIZE = 100
PLAYER_PROPS_PRODUCT = "Player Props"
_PLAYER_PROP_PROVIDERS = ("Underdog", "PrizePicks", "DraftKings", "FanDuel")
_UNDERDOG_SETTLEMENT_RESULT_SOURCES = frozenset(
    {"propline_event_results", "propline_history"}
)
_UNDERDOG_VOID_RESULT_SOURCES = frozenset({"propline_event_results_void"})
_DIRECTIONAL_GRADE_RESULTS = frozenset({"HIT", "MISS", "PUSH"})
_VOID_GRADE_RESULTS = frozenset({
    "VOID", "CANCELLED", "INJURY_VOID", "GAME_INTERRUPTED",
})
_TERMINAL_GRADE_RESULTS = frozenset({
    "HIT", "MISS", "PUSH", "VOID", "CANCELLED",
    "INJURY_VOID", "GAME_INTERRUPTED", "UNRESOLVED", "OUTLIER",
    "PLATFORM_DIFF",
})
_UNTRUSTED_PLAYER_RESULT_SOURCES = frozenset({"", "graded_prop", "manual", "unknown"})
_DB_WRITE_MAX_RETRIES = 4
_DB_WRITE_RETRY_DELAYS = (1.0, 3.0, 7.0, 15.0)
_PRIZEPICKS_EXTERNAL_PREFIX = "PrizePicks::"
_TELEGRAM_DELIVERY_KEY_VERSION = "v1"


def is_official_underdog_settlement_source(source: object) -> bool:
    """Return whether a stored result came from an explicit PropLine settlement."""
    return str(source or "").strip().casefold() in _UNDERDOG_SETTLEMENT_RESULT_SOURCES


def is_gradeable_player_result_source(provider: object, source: object) -> bool:
    """Reject self-generated and unproven rows before they can grade a pick."""
    source_key = str(source or "").strip().casefold()
    provider_key = str(provider or "").strip().casefold()
    if not provider_key or provider_key == "underdog":
        return source_key in _UNDERDOG_SETTLEMENT_RESULT_SOURCES
    return source_key not in _UNTRUSTED_PLAYER_RESULT_SOURCES


def is_trusted_directional_grade_source(
    provider: object,
    result: object,
    source: object,
) -> bool:
    """Return whether a terminal opportunity grade has reportable provenance.

    Underdog-native (including legacy provider-less) rows require explicit
    PropLine settlement evidence. Other provider rows require an external
    result source; self-generated ``graded_prop`` rows and legacy/manual rows
    are not independent evidence.
    """
    outcome = str(result or "").strip().upper()
    if outcome not in _TERMINAL_GRADE_RESULTS:
        return False
    source_key = str(source or "").strip().casefold()
    provider_key = str(provider or "").strip().casefold()
    if not provider_key or provider_key == "underdog":
        if outcome in _VOID_GRADE_RESULTS:
            return (
                source_key in _UNDERDOG_SETTLEMENT_RESULT_SOURCES
                or source_key in _UNDERDOG_VOID_RESULT_SOURCES
            )
        return source_key in _UNDERDOG_SETTLEMENT_RESULT_SOURCES
    return is_gradeable_player_result_source(provider, source)


def _trusted_directional_grade_filter():
    """SQL predicate for verified HIT/MISS/PUSH opportunity results."""
    provider_key = func.lower(func.trim(func.coalesce(PropOpportunityLog.provider, "")))
    source_key = func.lower(
        func.trim(func.coalesce(PropOpportunityLog.grade_result_source, ""))
    )
    underdog_provider = or_(provider_key == "", provider_key == "underdog")
    return and_(
        PropOpportunityLog.result.in_(tuple(_DIRECTIONAL_GRADE_RESULTS)),
        or_(
            and_(
                underdog_provider,
                source_key.in_(tuple(_UNDERDOG_SETTLEMENT_RESULT_SOURCES)),
            ),
            and_(
                ~underdog_provider,
                source_key != "",
                source_key.notin_(tuple(_UNTRUSTED_PLAYER_RESULT_SOURCES)),
            ),
        ),
    )


def _trusted_terminal_grade_filter():
    """SQL predicate for terminal grades with provider-appropriate provenance."""
    provider_key = func.lower(func.trim(func.coalesce(PropOpportunityLog.provider, "")))
    source_key = func.lower(
        func.trim(func.coalesce(PropOpportunityLog.grade_result_source, ""))
    )
    result_key = func.upper(func.trim(func.coalesce(PropOpportunityLog.result, "")))
    underdog_provider = or_(provider_key == "", provider_key == "underdog")
    underdog_settlement = and_(
        result_key.in_(tuple(_TERMINAL_GRADE_RESULTS - _VOID_GRADE_RESULTS)),
        source_key.in_(tuple(_UNDERDOG_SETTLEMENT_RESULT_SOURCES)),
    )
    underdog_void = and_(
        result_key.in_(tuple(_VOID_GRADE_RESULTS)),
        source_key.in_(
            tuple(_UNDERDOG_SETTLEMENT_RESULT_SOURCES | _UNDERDOG_VOID_RESULT_SOURCES)
        ),
    )
    return and_(
        result_key.in_(tuple(_TERMINAL_GRADE_RESULTS)),
        or_(
            and_(underdog_provider, or_(underdog_settlement, underdog_void)),
            and_(
                ~underdog_provider,
                source_key != "",
                source_key.notin_(tuple(_UNTRUSTED_PLAYER_RESULT_SOURCES)),
            ),
        ),
    )


def _analyst_grade(
    prediction: Optional[str],
    result: Optional[str],
    recommendation: Optional[str],
) -> Optional[str]:
    """Derive analyst outcome from the authoritative stored result."""
    from engine.ai_analyst import analyst_grade

    return analyst_grade(prediction, result, recommendation)

# Keep both Database objects (monitoring and history) on an explicit, bounded
# SQLite pool.  The process-wide session gate below makes that capacity a
# shared budget across both pools, so one lane cannot exhaust its own pool
# while the other lane is idle.
_SQLITE_POOL_SIZE = 8
_SQLITE_MAX_OVERFLOW = 0
_SQLITE_POOL_TIMEOUT_SECONDS = 30
_SQLITE_SESSION_LIMIT = _SQLITE_POOL_SIZE
_SQLITE_BACKPRESSURE_THRESHOLD_SECONDS = 0.001

# SQLite has one writer slot per database file.  The bot intentionally keeps a
# second Database object for the long-running player-history collector, but
# both objects point at the same SQLite URL.  A per-instance lock would
# therefore leave a cross-pool race.  Keep one lock per URL and event loop so
# every session from every in-process pool crosses the same gate.
_SQLITE_WRITER_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
] = weakref.WeakKeyDictionary()
_SQLITE_SESSION_GATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()
_SQLITE_BACKPRESSURE_METRICS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, dict[str, float]]
] = weakref.WeakKeyDictionary()
_SQLITE_BACKPRESSURE_KEYS = (
    "session_acquires",
    "session_waits",
    "session_wait_seconds",
    "writer_acquires",
    "writer_waits",
    "writer_wait_seconds",
    "write_retries",
)


def _sqlite_writer_lock(url: str) -> asyncio.Lock | None:
    """Return the shared session gate for one SQLite URL."""
    if not str(url).startswith("sqlite"):
        return None
    loop = asyncio.get_running_loop()
    locks_for_loop = _SQLITE_WRITER_LOCKS.setdefault(loop, {})
    return locks_for_loop.setdefault(str(url), asyncio.Lock())


def _sqlite_session_gate(url: str) -> asyncio.Semaphore | None:
    """Return the shared connection budget for one SQLite URL."""
    if not str(url).startswith("sqlite"):
        return None
    loop = asyncio.get_running_loop()
    gates_for_loop = _SQLITE_SESSION_GATES.setdefault(loop, {})
    return gates_for_loop.setdefault(
        str(url),
        asyncio.Semaphore(_SQLITE_SESSION_LIMIT),
    )


def _sqlite_backpressure_metrics(url: str) -> dict[str, float] | None:
    """Return the per-loop metrics bucket shared by all pools for one URL."""
    if not str(url).startswith("sqlite"):
        return None
    loop = asyncio.get_running_loop()
    metrics_for_loop = _SQLITE_BACKPRESSURE_METRICS.setdefault(loop, {})
    return metrics_for_loop.setdefault(
        str(url),
        {key: 0.0 for key in _SQLITE_BACKPRESSURE_KEYS},
    )


def _record_sqlite_backpressure(
    url: str,
    key: str,
    amount: float = 1.0,
) -> None:
    metrics = _sqlite_backpressure_metrics(url)
    if metrics is not None:
        metrics[key] = metrics.get(key, 0.0) + float(amount)


def get_sqlite_backpressure_metrics(
    url: str,
    *,
    reset: bool = False,
) -> dict[str, float]:
    """Read shared session/writer wait counters without adding a DB table."""
    metrics = _sqlite_backpressure_metrics(url)
    if metrics is None:
        return {key: 0.0 for key in _SQLITE_BACKPRESSURE_KEYS}
    snapshot = {
        key: float(metrics.get(key, 0.0))
        for key in _SQLITE_BACKPRESSURE_KEYS
    }
    if reset:
        for key in _SQLITE_BACKPRESSURE_KEYS:
            metrics[key] = 0.0
    return snapshot


def _statement_is_write(statement) -> bool:
    """Return whether a SQLAlchemy statement can change SQLite state."""
    for attribute in ("is_insert", "is_update", "is_delete", "is_dml"):
        if bool(getattr(statement, attribute, False)):
            return True
    text_value = str(statement).lstrip().lower()
    return text_value.startswith((
        "insert ", "update ", "delete ", "replace ", "create ",
        "alter ", "drop ", "vacuum ",
    ))


class _WriteAwareAsyncSession:
    """Proxy that acquires the shared gate only for write activity."""

    def __init__(
        self,
        session: AsyncSession,
        lock: asyncio.Lock | None,
        owner: "_LockedAsyncSession",
    ) -> None:
        self._session = session
        self._lock = lock
        self._owner = owner

    async def execute(self, statement, *args, **kwargs):
        if _statement_is_write(statement):
            await self._owner._acquire_writer()
        return await self._session.execute(statement, *args, **kwargs)

    async def flush(self, *args, **kwargs):
        await self._owner._acquire_writer()
        return await self._session.flush(*args, **kwargs)

    async def commit(self):
        await self._owner._acquire_writer()
        try:
            return await self._session.commit()
        finally:
            self._owner._release_writer()

    async def rollback(self):
        try:
            return await self._session.rollback()
        finally:
            self._owner._release_writer()

    def __getattr__(self, name: str):
        return getattr(self._session, name)


class _LockedAsyncSession:
    """AsyncSession context manager with a cross-pool SQLite write gate.

    Reads do not acquire the gate, which keeps Telegram reports and target
    discovery responsive while a large monitoring write is in progress.
    ``flush``/DML/commit acquire the gate and hold it through the transaction.
    """

    def __init__(
        self,
        session: AsyncSession,
        lock: asyncio.Lock | None,
        session_gate: asyncio.Semaphore | None = None,
        url: str = "",
    ) -> None:
        self._session = session
        self._lock = lock
        self._session_gate = session_gate
        self._url = url
        self._session_gate_acquired = False
        self._acquired = False

    async def _acquire_writer(self) -> None:
        if self._lock is not None and not self._acquired:
            _record_sqlite_backpressure(self._url, "writer_acquires")
            started = time.monotonic()
            await self._lock.acquire()
            self._acquired = True
            waited = time.monotonic() - started
            if waited >= _SQLITE_BACKPRESSURE_THRESHOLD_SECONDS:
                _record_sqlite_backpressure(self._url, "writer_waits")
                _record_sqlite_backpressure(
                    self._url,
                    "writer_wait_seconds",
                    waited,
                )
            if waited >= 0.05:
                logger.info(
                    "SQLite writer gate backpressure: waited %.3fs",
                    waited,
                )

    def _release_writer(self) -> None:
        if self._acquired:
            self._lock.release()
            self._acquired = False

    async def __aenter__(self) -> _WriteAwareAsyncSession:
        if self._session_gate is not None:
            _record_sqlite_backpressure(self._url, "session_acquires")
            started = time.monotonic()
            await self._session_gate.acquire()
            self._session_gate_acquired = True
            waited = time.monotonic() - started
            if waited >= _SQLITE_BACKPRESSURE_THRESHOLD_SECONDS:
                _record_sqlite_backpressure(self._url, "session_waits")
                _record_sqlite_backpressure(
                    self._url,
                    "session_wait_seconds",
                    waited,
                )
            if waited >= 0.05:
                logger.info(
                    "SQLite session budget backpressure: waited %.3fs",
                    waited,
                )
        try:
            await self._session.__aenter__()
        except BaseException:
            if self._session_gate_acquired:
                self._session_gate.release()
                self._session_gate_acquired = False
            raise
        return _WriteAwareAsyncSession(self._session, self._lock, self)

    async def __aexit__(self, exc_type, exc_value, traceback) -> object:
        try:
            return await self._session.__aexit__(exc_type, exc_value, traceback)
        finally:
            self._release_writer()
            if self._session_gate_acquired:
                self._session_gate.release()
                self._session_gate_acquired = False

    def __getattr__(self, name: str):
        return getattr(self._session, name)


def _stored_opportunity_external_id(external_id: str, provider: Optional[str]) -> str:
    """Keep legacy SQLite uniqueness compatible while separating providers."""
    value = str(external_id or "")
    if str(provider or "").strip().lower() == "prizepicks":
        if not value.startswith(_PRIZEPICKS_EXTERNAL_PREFIX):
            value = f"{_PRIZEPICKS_EXTERNAL_PREFIX}{value}"
    return value


def _dedup_player_key(player_name: str, provider: Optional[str]) -> str:
    """Provider-scope the legacy three-part in-memory dedup key."""
    if str(provider or "").strip().lower() == "prizepicks":
        return f"{_PRIZEPICKS_EXTERNAL_PREFIX}{player_name}"
    return player_name


def _telegram_delivery_key(
    *,
    provider: Optional[str],
    alert_kind: Optional[str],
    delivery_path: Optional[str],
    external_id: Optional[str],
    event_id: Optional[str],
    stat_type: Optional[str],
    line_value: Optional[float],
) -> Optional[str]:
    """Build the immutable identity for one successful PLAY version.

    Missing identity is deliberately represented by ``None`` so legacy rows
    remain preserved but cannot be mistaken for confirmed Telegram deliveries.
    The line is part of the version because a legitimate line-change PLAY is a
    distinct delivery from the previous line.
    """
    values = {
        "provider": str(provider or "").strip(),
        "alert_kind": str(alert_kind or "").strip().upper(),
        "delivery_path": str(delivery_path or "").strip(),
        "external_id": str(external_id or "").strip(),
        "event_id": str(event_id or "").strip(),
        "stat_type": str(stat_type or "").strip(),
    }
    if values["alert_kind"] != "ACTIONABLE_PICK":
        return None
    if not all(values.values()) or line_value is None:
        return None
    try:
        line = f"{float(line_value):.6f}"
    except (TypeError, ValueError):
        return None
    payload = "|".join(
        [f"{key}={values[key]}" for key in sorted(values)] + [f"line={line}"]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_TELEGRAM_DELIVERY_KEY_VERSION}:{digest}"


def _sqlite_error_kind(exc: BaseException) -> Optional[str]:
    """Classify the two transient SQLite failures handled by this module."""
    message = str(exc).lower()
    if "unable to open database file" in message:
        return "cantopen"
    if "database is locked" in message or "database table is locked" in message:
        return "lock"
    return None


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    """Return whether an exception is a transient SQLite writer lock."""
    return _sqlite_error_kind(exc) == "lock"


def _is_sqlite_cantopen_error(exc: BaseException) -> bool:
    """Return whether SQLite could not open the database file."""
    return _sqlite_error_kind(exc) == "cantopen"


def _log_sqlite_failure(
    *,
    operation: str,
    stage: str,
    exc: BaseException,
) -> None:
    """Log a SQLite failure with its original traceback intact."""
    kind = _sqlite_error_kind(exc) or "other"
    logger.error(
        "SQLite access failure: operation=%s stage=%s kind=%s error=%s",
        operation,
        stage,
        kind,
        exc,
        exc_info=True,
    )


def _install_sqlite_diagnostics(engine) -> None:
    """Attach narrow SQLAlchemy lifecycle diagnostics to one SQLite engine.

    These listeners observe the existing engine without changing its pooling,
    transaction, or journal behavior.  The ``handle_error`` listener retains
    SQLAlchemy's original exception/traceback; it only adds the stage context
    to the log.
    """
    sync_engine = engine.sync_engine

    def _on_connect(dbapi_connection, connection_record) -> None:
        connection_record.info["sqlite_access_stage"] = "connection_creation"
        logger.debug("SQLite access stage=connection_creation")

    def _on_checkout(dbapi_connection, connection_record, connection_proxy) -> None:
        connection_record.info["sqlite_access_stage"] = "pool_checkout"
        logger.debug("SQLite access stage=pool_checkout")

    def _on_invalidate(dbapi_connection, connection_record, exception) -> None:
        if exception is not None and _sqlite_error_kind(exception) is not None:
            _log_sqlite_failure(
                operation="pool",
                stage=connection_record.info.get(
                    "sqlite_access_stage", "pool_checkout"
                ),
                exc=exception,
            )
        elif exception is not None:
            logger.debug(
                "SQLite connection invalidated: stage=%s exception_type=%s",
                connection_record.info.get(
                    "sqlite_access_stage", "pool_checkout"
                ),
                type(exception).__name__,
            )
        connection_record.info["sqlite_access_stage"] = "connection_invalidated"

    def _on_begin(connection) -> None:
        connection.info["sqlite_access_stage"] = "transaction_begin"
        logger.debug("SQLite access stage=transaction_begin")

    def _on_commit(connection) -> None:
        connection.info["sqlite_access_stage"] = "commit"
        logger.debug("SQLite access stage=commit")

    def _on_handle_error(exception_context):
        original = exception_context.original_exception
        kind = _sqlite_error_kind(original)
        if kind is None:
            return

        connection = exception_context.connection
        stage = "connection_creation"
        if connection is not None:
            try:
                stage = connection.info.get("sqlite_access_stage", "database_operation")
            except Exception:
                stage = "database_operation"
        statement = exception_context.statement or "<no statement>"
        _log_sqlite_failure(
            operation=f"sql:{statement}",
            stage=stage,
            exc=original,
        )

    event.listen(sync_engine, "connect", _on_connect)
    event.listen(sync_engine, "checkout", _on_checkout)
    event.listen(sync_engine, "invalidate", _on_invalidate)
    event.listen(sync_engine, "begin", _on_begin)
    event.listen(sync_engine, "commit", _on_commit)
    event.listen(sync_engine, "handle_error", _on_handle_error)


def _retry_sqlite_write(func):
    """Retry a complete write operation after a transient SQLite lock.

    The wrapped method owns its session, so each retry starts with a fresh
    transaction and cannot reuse a failed session/transaction state.
    """
    @functools.wraps(func)
    async def _wrapped(self, *args, **kwargs):
        for attempt in range(_DB_WRITE_MAX_RETRIES + 1):
            try:
                return await func(self, *args, **kwargs)
            except Exception as exc:
                if not _is_sqlite_lock_error(exc) or attempt >= _DB_WRITE_MAX_RETRIES:
                    raise
                _record_sqlite_backpressure(self._url, "write_retries")
                delay = _DB_WRITE_RETRY_DELAYS[
                    min(attempt, len(_DB_WRITE_RETRY_DELAYS) - 1)
                ]
                logger.warning(
                    "%s: SQLite lock; retry %d/%d in %.2fs",
                    func.__name__,
                    attempt + 1,
                    _DB_WRITE_MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
    return _wrapped


def _is_supported_tracking_sport(sport: Optional[object]) -> bool:
    """Return whether a normalized sport can appear in tracking reports.

    Every normalized sport is retained for monitoring, persistence, evidence,
    and the normal evidence-backed delivery path.
    """
    return bool(str(sport or "").strip())


def _tier_from_confidence(ai_confidence: int) -> str:
    """Map an ai_confidence score (0-100) to a tier label for CLV seeding."""
    if ai_confidence >= 95:
        return "S"
    if ai_confidence >= 85:
        return "A"
    if ai_confidence >= 75:
        return "B"
    return "PASS"


def _bounded_score(value: object, *, maximum: float) -> float | None:
    """Keep diagnostic score values numeric and within their display range."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, min(float(maximum), float(value)))
    except (TypeError, ValueError):
        return None


def _normalized_score_tier(value: object) -> str | None:
    if value is None:
        return None
    tier = str(value).strip().upper()
    return tier if tier in {"S", "A", "B", "C", "PASS"} else None


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


class PropLineEvidence(Base):
    """Durable, provider-agnostic evidence for one PropLine endpoint target."""

    __tablename__ = "propline_evidence"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    family          = Column(String(64), nullable=False)
    target_key      = Column(String(256), nullable=False)
    official_sport  = Column(String(64), nullable=False)
    canonical_sport = Column(String(64), nullable=True, index=True)
    event_id        = Column(String(256), nullable=True, index=True)
    player_id       = Column(String(256), nullable=True)
    player_name     = Column(String(256), nullable=True, index=True)
    market          = Column(String(128), nullable=True, index=True)
    outcome_id      = Column(String(256), nullable=True)
    bookmaker       = Column(String(128), nullable=True)
    source          = Column(String(128), nullable=True)
    fetched_at      = Column(DateTime, nullable=False, index=True)
    expires_at      = Column(DateTime, nullable=True, index=True)
    complete        = Column(Boolean, nullable=False, default=True)
    redacted        = Column(Boolean, nullable=False, default=False)
    payload_json    = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "family", "official_sport", "target_key",
            name="uq_propline_evidence_target",
        ),
        Index(
            "ix_propline_evidence_family_sport_target",
            "family", "official_sport", "target_key",
        ),
        Index(
            "ix_propline_evidence_event_player_market",
            "event_id", "player_name", "market",
        ),
    )


# ── Telegram delivery audit ───────────────────────────────────────────────────

class TelegramDeliveryAudit(Base):
    """Durable audit row for one outbound Telegram alert attempt.

    This is separate from opportunity rows because one opportunity can produce
    multiple legitimate alerts, while one alert can fan out to many recipients.
    """
    __tablename__ = "telegram_delivery_audit"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    provider          = Column(String(32), nullable=False, default="Unknown", index=True)
    alert_kind        = Column(String(32), nullable=False, default="ACTIONABLE_PICK")
    delivery_path     = Column(String(32), nullable=False, default="unknown")
    player_name       = Column(String(128), nullable=True)
    sport             = Column(String(32), nullable=True, index=True)
    stat_type         = Column(String(64), nullable=True)
    external_id       = Column(String(128), nullable=True, index=True)
    event_id          = Column(String(128), nullable=True, index=True)
    # Non-null only for identity-complete successful PLAY deliveries.  The
    # partial unique index is the storage-level retry/concurrency guard.
    delivery_key     = Column(String(256), nullable=True)
    line_value        = Column(Float, nullable=True)
    outcome           = Column(String(24), nullable=False)  # sent|failed|rate_limited
    recipients_sent   = Column(Integer, nullable=False, default=0)
    recipients_failed = Column(Integer, nullable=False, default=0)
    evidence_snapshot_json = Column(Text, nullable=True)
    attempted_at      = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index(
            "uq_telegram_delivery_audit_key",
            "delivery_key",
            unique=True,
            sqlite_where=text("delivery_key IS NOT NULL"),
        ),
    )


class TelegramCommand(Base):
    """Durable command catalog used by the Telegram UI and help output."""

    __tablename__ = "telegram_commands"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    command     = Column(String(64), nullable=False, unique=True, index=True)
    description = Column(String(256), nullable=False, default="")
    category    = Column(String(64), nullable=False, default="System")
    usage       = Column(String(256), nullable=True)
    alias_of    = Column(String(64), nullable=True)
    enabled     = Column(Boolean, nullable=False, default=True)
    sort_order  = Column(Integer, nullable=False, default=0)


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
    provider_status = Column(String(32), nullable=True)
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
    provider    = Column(String(64),  nullable=False, index=True)   # Source provenance
    product     = Column(String(32),  nullable=False, default=PLAYER_PROPS_PRODUCT, index=True)
    sport       = Column(String(32),  nullable=False, index=True)
    player_name = Column(String(128), nullable=False, index=True)
    team        = Column(String(64),  nullable=False, default="")
    stat_type   = Column(String(64),  nullable=False)
    line_value  = Column(Float,       nullable=False)
    game_time   = Column(DateTime,    nullable=True)
    provider_status = Column(String(32), nullable=True)
    external_id = Column(String(64),  nullable=False, default="")
    game_id     = Column(String(64),  nullable=False, default="")
    line_variant = Column(String(16), nullable=False, default="")
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
    bet_recommendation   = Column(String(8), nullable=True)
    bet_confidence       = Column(Integer,   nullable=True)
    # Market quality score (0–100) stored at scoring time; NULL = not yet computed.
    market_quality_score = Column(Integer,   nullable=True)
    # Composite score (BQ, 0–100) stored at scoring time for tier-aware /picks and /slip gating.
    # Tier 2 (NBA/MLB/NFL) requires score_total ≥ 75 for Telegram eligibility.
    score_total          = Column(Float,     nullable=True)

    __table_args__ = (
        # Current monitoring is bounded by provider and reporting-day
        # timestamps.  Keep that slice indexable even after the append-only
        # history table becomes large.
        Index(
            "ix_prop_line_history_provider_fetched_at",
            "provider",
            "fetched_at",
        ),
    )


class PropLineObservation(Base):
    """Immutable normalized observations from a provider feed snapshot.

    ``PropLineHistory`` is intentionally a current-state/lifecycle view.  This
    table is the append-only observation ledger used to prove what the feed
    actually returned at each fetch without making an unresolved live quote
    look like a settled player result.
    """

    __tablename__ = "prop_line_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(96), nullable=False, index=True)
    observation_key = Column(String(64), nullable=False, index=True)
    provider = Column(String(64), nullable=False, index=True)
    product = Column(String(32), nullable=False, default=PLAYER_PROPS_PRODUCT, index=True)
    sport = Column(String(32), nullable=False, index=True)
    player_name = Column(String(128), nullable=False)
    team = Column(String(64), nullable=False, default="")
    stat_type = Column(String(64), nullable=False)
    line_value = Column(Float, nullable=False)
    game_time = Column(DateTime, nullable=True)
    provider_status = Column(String(32), nullable=True)
    external_id = Column(String(128), nullable=False, default="")
    game_id = Column(String(128), nullable=False, default="")
    line_variant = Column(String(32), nullable=False, default="")
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index(
            "ix_prop_line_observations_provider_fetched_at",
            "provider",
            "fetched_at",
        ),
    )


class PropLineCatalogAudit(Base):
    """Durable state and change timestamps for observed catalog identities.

    Only identities that were first observed as unmapped are retained.  A
    later mapped observation updates that same row, making the table useful for
    incident review without turning the full known catalog into noise.
    """

    __tablename__ = "propline_catalog_audit"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    identity_key     = Column(String(512), nullable=False, unique=True, index=True)
    kind             = Column(String(16), nullable=False, index=True)
    provider_key     = Column(String(256), nullable=False, index=True)
    canonical_sport  = Column(String(64), nullable=False, default="")
    canonical_market = Column(String(128), nullable=False, default="")
    provider_label   = Column(String(256), nullable=False, default="")
    sample_count     = Column(Integer, nullable=False, default=0)
    status           = Column(String(16), nullable=False, default="unmapped", index=True)
    first_seen_at    = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at     = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_changed_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    resolved_at      = Column(DateTime, nullable=True)


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

    Unique on (player_name, sport, stat_type, game_date, game_id) — re-upserted
    when the API reports a corrected boxscore. ``game_id`` is optional for
    legacy/stat-provider rows that do not expose an event ID.
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
    game_id      = Column(String(128), nullable=False, default="", index=True)
    fetched_at   = Column(DateTime,    default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "player_name", "sport", "stat_type", "game_date", "game_id",
            name="uq_player_game_result",
        ),
    )


class PropOpportunityLog(Base):
    """
    Directional player prop opportunities used for evidence and grading.

    One row per (external_id, stat_type) pair, recorded immediately after
    evaluation produces an OVER or UNDER direction. Universal ingestion
    tracking is stored separately in PropTrackingLog.

    ``result`` is stored from the recorded recommendation's perspective:
      "HIT"     → the recommended OVER or UNDER cleared the line
      "MISS"    → the recommended OVER or UNDER failed to clear the line
      "PUSH"    → actual_value == line_value
      "PENDING" → game not yet complete / result not yet available

    Recommendation-less neutral observations live in PropTrackingLog and use
    their own OVER-baseline settlement labels.
    """
    __tablename__ = "prop_opportunity_log"

    id             = Column(Integer,     primary_key=True, autoincrement=True)
    external_id    = Column(String(64),  nullable=False, index=True)
    player_name    = Column(String(128), nullable=False, index=True)
    team           = Column(String(64),  nullable=False, default="")
    sport          = Column(String(32),  nullable=False, index=True)
    stat_type      = Column(String(64),  nullable=False)
    line_value     = Column(Float,       nullable=False)
    recommendation = Column(String(8),   nullable=False)   # OVER | UNDER
    decision_tier  = Column(String(8),   nullable=False)   # Legacy score metadata
    confidence     = Column(Integer,     nullable=False, default=0)
    game_time      = Column(DateTime,    nullable=True)
    game_id        = Column(String(128), nullable=False, default="", index=True)
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
    provider           = Column(String(32),  nullable=True)               # Source provenance
    product            = Column(String(32),  nullable=False, default=PLAYER_PROPS_PRODUCT, index=True)
    bet_quality_score  = Column(Integer,     nullable=True)               # 0-100 decision confidence
    qualification_path = Column(Text,        nullable=True)               # JSON list of gate outcomes
    reason_codes       = Column(Text,        nullable=True)               # JSON: ["STRONG_L5", …]
    watchlist_state    = Column(String(16),  nullable=True)               # Legacy compatibility; current writes null
    settlement_source  = Column(String(64),  nullable=True)               # "auto_grade"|"manual"|None
    grade_result_source = Column(String(64), nullable=True)               # Provider row used for this outcome
    manual_opinion     = Column(String(8),   nullable=True)               # Legacy compatibility
    # Downstream intelligence — additive and never used as a delivery gate.
    downstream_intelligence_json = Column(Text, nullable=True)
    evidence_completeness        = Column(Integer, nullable=True)
    sharp_confidence             = Column(Integer, nullable=True)
    sharp_confidence_components  = Column(Text, nullable=True)
    # Telegram actionable pick tracking — set True only when the 🎯 ACTIONABLE BET PICK
    # alert was successfully delivered to Telegram. Directional opportunities
    # that were not delivered remain False.
    alert_sent         = Column(Boolean,     nullable=False, default=False)
    alert_sent_at      = Column(DateTime,    nullable=True)               # UTC timestamp of delivery
    # Advisory AI analyst output. These fields are never used as a delivery,
    # qualification, or grading gate.
    analyst_prediction      = Column(String(16), nullable=True)             # OVER/UNDER/UNAVAILABLE
    analyst_confidence      = Column(Integer, nullable=True)
    analyst_risk_level      = Column(String(16), nullable=True)
    analyst_model           = Column(String(64), nullable=True)
    analyst_model_version   = Column(String(32), nullable=True)
    analyst_payload_json    = Column(Text, nullable=True)
    analyst_grade           = Column(String(8), nullable=True)              # HIT/MISS/PUSH
    analyst_graded_at       = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "external_id", "stat_type",
            name="uq_prop_opportunity_log",
        ),
    )


class PropTrackingLog(Base):
    """
    Universal prop-ingestion ledger used only for settlement tracking.

    One row represents the latest observation for a provider-owned prop
    identity. It is intentionally separate from PropOpportunityLog so
    non-actionable observations can be graded without appearing in actionable
    opportunity reports, calibration samples, or Telegram history.
    """
    __tablename__ = "prop_tracking_log"

    id           = Column(Integer,     primary_key=True, autoincrement=True)
    provider     = Column(String(32),  nullable=False, index=True)  # Source provenance
    product      = Column(String(32),  nullable=False, default=PLAYER_PROPS_PRODUCT, index=True)
    external_id  = Column(String(128), nullable=False, index=True)
    player_name  = Column(String(128), nullable=False, index=True)
    team         = Column(String(64),  nullable=False, default="")
    sport        = Column(String(32),  nullable=False, index=True)
    stat_type    = Column(String(64),  nullable=False)
    line_value   = Column(Float,       nullable=False)
    game_time    = Column(DateTime,    nullable=True)
    game_id      = Column(String(128), nullable=False, default="", index=True)
    line_variant = Column(String(16),  nullable=False, default="")
    detected_at  = Column(DateTime,    default=datetime.utcnow, nullable=False)
    result       = Column(String(16),  nullable=False, default="PENDING", index=True)
    actual_value = Column(Float,       nullable=True)
    graded_at    = Column(DateTime,    nullable=True)
    settlement_source = Column(String(64), nullable=True)
    void_reason = Column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider", "external_id", "stat_type",
            name="uq_prop_tracking_log_identity",
        ),
    )


class AdaptiveLearningRun(Base):
    """Immutable audit record for one adaptive-learning rebuild."""

    __tablename__ = "adaptive_learning_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    activated_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=False, nullable=False, index=True)
    min_sample_size = Column(Integer, nullable=False)
    min_model_errors = Column(Integer, nullable=False)
    max_delta = Column(Float, nullable=False)
    eligible_groups = Column(Integer, nullable=False, default=0)
    total_groups = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="DESCRIPTIVE")


class AdaptiveLearningWeight(Base):
    """Provider/sport/market weights belonging to one audited run."""

    __tablename__ = "adaptive_learning_weights"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(64), nullable=False, index=True)
    provider = Column(String(64), nullable=False, index=True)
    sport = Column(String(32), nullable=False, index=True)
    market_type = Column(String(64), nullable=False, index=True)
    sample_size = Column(Integer, nullable=False, default=0)
    eligible_samples = Column(Integer, nullable=False, default=0)
    model_errors = Column(Integer, nullable=False, default=0)
    hit_rate = Column(Float, nullable=True)
    multiplier = Column(Float, nullable=False, default=1.0)
    status = Column(String(32), nullable=False, default="INSUFFICIENT_SAMPLE")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "version", "provider", "sport", "market_type",
            name="uq_adaptive_learning_weight",
        ),
        Index(
            "ix_adaptive_learning_weight_lookup",
            "version", "provider", "sport", "market_type",
        ),
    )


# ── Prop Candidate Log ────────────────────────────────────────────────────────

class PropCandidateLog(Base):
    """
    Directional scored-candidate evidence.

    Written once per directional candidate per scan cycle. The legacy
    gate_decision and rejection_reason columns remain for compatibility with
    existing databases, but current writes use EVALUATED with no rejection
    reason. Alert delivery is tracked separately.
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
    score_tier           = Column(String(8),   nullable=True)    # Scoring metadata
    confidence           = Column(Integer,     nullable=True)
    gate_decision        = Column(String(16),  nullable=False)   # Current writes: EVALUATED
    rejection_reason     = Column(Text,        nullable=True)    # Legacy compatibility; current writes null
    reason_codes         = Column(Text,        nullable=True)    # JSON list e.g. ["STRONG_L5", "S_TIER"]
    snapshot_external_id = Column(String(64),  nullable=True, index=True)


# ── Scan Cycle Log ────────────────────────────────────────────────────────────

class ScanCycleLog(Base):
    """
    Per-scan-cycle pipeline metrics written at the end of every underdog_job run.

    Provides evidence that the FULL active prop pool is monitored every poll,
    and shows exactly where the funnel narrows from raw feed to alert delivery.

    stage order:
      fetched → removed → futures → active (non-removed, non-futures)
      active → unchanged (stable lines, skipped) + new + line_changed + cold_start
      new + line_changed + cold_start → analyzed (scored)
      analyzed → qualified (passed alert gates) → alert_delivered (Telegram sent)
    """
    __tablename__ = "scan_cycle_log"

    id              = Column(Integer,  primary_key=True, autoincrement=True)
    scan_ts         = Column(DateTime, nullable=False, index=True, default=datetime.utcnow)
    # Stage 1 — raw feed
    fetched         = Column(Integer,  nullable=False, default=0)  # snapshots from Underdog API
    # Stage 2 — active filter
    removed         = Column(Integer,  nullable=False, default=0)  # [REMOVED] markers in feed
    futures         = Column(Integer,  nullable=False, default=0)  # futures/awards skipped
    active          = Column(Integer,  nullable=False, default=0)  # non-removed, non-futures
    # Stage 3 — known_keys gate
    unchanged       = Column(Integer,  nullable=False, default=0)  # stable line → skipped (no re-score)
    new_props       = Column(Integer,  nullable=False, default=0)  # first appearance
    line_changed    = Column(Integer,  nullable=False, default=0)  # line moved ≥ MIN_LINE_CHANGE
    cold_start      = Column(Integer,  nullable=False, default=0)  # scored during cold-start pass
    # Stage 4+ — scoring & delivery
    analyzed        = Column(Integer,  nullable=False, default=0)  # total scored this cycle
    qualified       = Column(Integer,  nullable=False, default=0)  # passed all alert gates
    alert_delivered = Column(Integer,  nullable=False, default=0)  # successfully sent to Telegram
    actionable_by_sport = Column(Text, nullable=True)  # JSON: sport -> scan-actionable count
    removed_by_sport    = Column(Text, nullable=True)  # JSON: sport -> feed-removal count
    # Optional player-history enrichment is allowed to defer after the complete
    # feed is persisted. Keep that degraded state with the cycle so reports do
    # not mistake observation-only work for a zero or failed scan.
    enrichment_status   = Column(String(16), nullable=False, default="complete")
    enrichment_deferred = Column(Integer, nullable=False, default=0)


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


# ── Slip Journal (P9/P10) ─────────────────────────────────────────────────────

class SlipJournal(Base):
    """User betting journal — one row per slip the user records.

    Created via /slip create, graded via /slip grade.
    status: OPEN → GRADED | VOID
    """
    __tablename__ = "slip_journal"

    id         = Column(Integer,   primary_key=True, autoincrement=True)
    slip_code  = Column(String(12), nullable=False, unique=True, index=True)  # e.g. SLP-001
    created_at = Column(DateTime,  default=datetime.utcnow, nullable=False)
    stake      = Column(Float,     nullable=True)    # $ staked (optional)
    slip_type  = Column(String(8),  nullable=True)   # "2-man" / "3-man" / etc.
    status     = Column(String(12), nullable=False, default="OPEN")  # OPEN/GRADED/VOID
    payout     = Column(Float,     nullable=True)    # $ returned
    roi_pct    = Column(Float,     nullable=True)    # (payout/stake - 1)*100
    notes      = Column(Text,      nullable=True)
    graded_at  = Column(DateTime,  nullable=True)


class SlipJournalLeg(Base):
    """One leg (prop pick) within a slip journal entry."""
    __tablename__ = "slip_journal_legs"

    id           = Column(Integer,    primary_key=True, autoincrement=True)
    slip_code    = Column(String(12),  nullable=False, index=True)   # → SlipJournal.slip_code
    opp_id       = Column(Integer,    nullable=True,  index=True)    # → PropOpportunityLog.id
    provider     = Column(String(32),  nullable=True)                # Underdog | PrizePicks
    player_name  = Column(String(128), nullable=False)
    team         = Column(String(64),  nullable=False, default="")
    sport        = Column(String(32),  nullable=False, default="")
    stat_type    = Column(String(64),  nullable=False, default="")
    line_value   = Column(Float,      nullable=True)
    direction    = Column(String(8),  nullable=True)   # OVER / UNDER
    tier         = Column(String(8),  nullable=True)   # S / A / B / PASS
    confidence   = Column(Integer,    nullable=True)
    game_time    = Column(DateTime,   nullable=True)
    result       = Column(String(8),  nullable=False, default="PENDING")  # HIT/MISS/PUSH/PENDING
    actual_value = Column(Float,      nullable=True)
    graded_at    = Column(DateTime,   nullable=True)


class AutoSlipNotification(Base):
    """Durable deduplication record for proactively delivered slip combinations."""
    __tablename__ = "auto_slip_notifications"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    signature  = Column(String(64), nullable=False, unique=True, index=True)
    slip_size  = Column(Integer, nullable=False)
    status     = Column(String(12), nullable=False, default="SENDING")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    sent_at    = Column(DateTime, nullable=True)


class AutoSlipNotificationLeg(Base):
    """Persist each leg so automatic slips can be graded from official results."""
    __tablename__ = "auto_slip_notification_legs"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    notification_signature = Column(String(64), nullable=False, index=True)
    combination_signature  = Column(String(64), nullable=False, index=True)
    leg_index              = Column(Integer, nullable=False)
    provider               = Column(String(32), nullable=False, default="Underdog")
    external_id            = Column(String(128), nullable=False, default="")
    game_id                = Column(String(128), nullable=False, default="")
    player_name            = Column(String(128), nullable=False)
    sport                  = Column(String(32), nullable=False, default="")
    stat_type              = Column(String(64), nullable=False, default="")
    line_value             = Column(Float, nullable=True)
    direction              = Column(String(8), nullable=False, default="")
    game_time              = Column(DateTime, nullable=True)
    result                 = Column(String(16), nullable=False, default="PENDING")
    actual_value           = Column(Float, nullable=True)
    grade_result_source    = Column(String(64), nullable=True)
    graded_at              = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "notification_signature",
            "leg_index",
            name="uq_auto_slip_notification_leg",
        ),
        Index(
            "ix_auto_slip_notification_legs_combo",
            "combination_signature",
            "leg_index",
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

        # connect_args["timeout"]: SQLite busy timeout — wait up to two minutes
        # before raising OperationalError("database is locked"). The monitoring
        # ledger is large enough that a bounded 30-second wait can expire while
        # a legitimate 200-row write is still committing.
        _connect_args: dict = {}
        if self._url.startswith("sqlite"):
            _connect_args["timeout"] = 120
        _engine_kwargs = {
            "echo": False,
            "connect_args": _connect_args,
        }
        # SQLAlchemy's in-memory SQLite dialect uses StaticPool and rejects
        # QueuePool arguments.  Production uses the file-backed path, where
        # the explicit bounded pool prevents unmeasured connection growth.
        if self._url.startswith("sqlite") and ":memory:" not in self._url:
            _engine_kwargs.update(
                pool_size=_SQLITE_POOL_SIZE,
                max_overflow=_SQLITE_MAX_OVERFLOW,
                pool_timeout=_SQLITE_POOL_TIMEOUT_SECONDS,
            )
        self._engine = create_async_engine(self._url, **_engine_kwargs)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        _install_sqlite_diagnostics(self._engine)
        async with self._engine.begin() as conn:
            # WAL journal mode: allows concurrent reads while a write is in
            # progress, eliminating most "database is locked" errors on SQLite.
            # Setting is persistent — only needs to be applied once per DB file.
            if self._url.startswith("sqlite"):
                try:
                    await conn.execute(text("PRAGMA journal_mode=WAL"))
                except Exception as exc:
                    _log_sqlite_failure(
                        operation="database.init",
                        stage="wal_shared_memory_access",
                        exc=exc,
                    )
                    raise
                # NORMAL synchronous is safe and faster under WAL.
                await conn.execute(text("PRAGMA synchronous=NORMAL"))
                # busy_timeout (milliseconds): how long SQLite waits when another
                # writer holds the lock before raising "database is locked".
                # The Python-level connect_args["timeout"] (seconds) handles aiosqlite
                # connection-level waits; this PRAGMA covers intra-process lock contention.
                await conn.execute(text("PRAGMA busy_timeout=120000"))
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
        await self._migrate_prop_opportunity_log_v4()
        await self._migrate_prop_opportunity_log_v5()
        await self._migrate_prop_line_history_v4()
        await self._migrate_prop_line_history_v5()
        await self._migrate_prop_line_history_v6()
        await self._migrate_prop_line_history_v7()
        await self._migrate_prop_line_history_v8()
        await self._migrate_prop_line_variants_v1()
        await self._migrate_player_props_product_v1()
        await self._migrate_player_game_results_v1()
        await self._migrate_prop_opportunity_log_v6()
        await self._migrate_prop_opportunity_log_v7()
        await self._migrate_prop_opportunity_log_v8()
        await self._migrate_telegram_delivery_audit_v1()
        # v2 reads mapped audit rows, so ensure any newer mapped columns exist
        # before that ORM query runs against an older database file.
        await self._migrate_telegram_delivery_audit_v3()
        await self._migrate_telegram_delivery_audit_v2()
        await self._migrate_slip_journal_leg_v1()
        await self._migrate_scan_cycle_log_v1()
        await self._migrate_scan_cycle_log_v2()
        # player_risk_records is created by create_all (new table); no column migration needed
        logger.info("Database initialised at %s", self._url)

    def session(self) -> AsyncSession:
        if self._session_factory is None:
            raise RuntimeError("Database.init() has not been called.")
        session = self._session_factory()
        return _LockedAsyncSession(
            session,
            _sqlite_writer_lock(self._url),
            _sqlite_session_gate(self._url),
            self._url,
        )

    @staticmethod
    def _propline_evidence_datetime(value: object) -> datetime | None:
        """Normalize evidence timestamps to naive UTC for SQLite."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _propline_evidence_value(record: object, name: str, default=None):
        if isinstance(record, dict):
            return record.get(name, default)
        return getattr(record, name, default)

    @staticmethod
    def _propline_evidence_dict(row: PropLineEvidence) -> dict:
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, ValueError):
            payload = None
        return {
            "family": row.family,
            "target_key": row.target_key,
            "official_sport": row.official_sport,
            "canonical_sport": row.canonical_sport,
            "event_id": row.event_id,
            "player_id": row.player_id,
            "player_name": row.player_name,
            "market": row.market,
            "outcome_id": row.outcome_id,
            "bookmaker": row.bookmaker,
            "source": row.source,
            "fetched_at": row.fetched_at,
            "expires_at": row.expires_at,
            "complete": bool(row.complete),
            "redacted": bool(row.redacted),
            "payload": payload,
        }

    @_retry_sqlite_write
    async def upsert_propline_evidence_batch(self, records: "Iterable[object]") -> int:
        """Upsert complete, non-redacted endpoint evidence in one transaction."""
        prepared: dict[tuple[str, str, str], dict] = {}
        for record in records or ():
            family = str(self._propline_evidence_value(record, "family", "") or "").strip()
            target_key = str(
                self._propline_evidence_value(record, "target_key", "") or ""
            ).strip()
            official_sport = str(
                self._propline_evidence_value(record, "official_sport", "") or ""
            ).strip()
            if not family or not target_key or not official_sport:
                continue
            if not bool(self._propline_evidence_value(record, "complete", False)):
                continue
            if bool(self._propline_evidence_value(record, "redacted", False)):
                continue
            fetched_at = self._propline_evidence_datetime(
                self._propline_evidence_value(record, "fetched_at")
            ) or datetime.utcnow()
            expires_at = self._propline_evidence_datetime(
                self._propline_evidence_value(record, "expires_at")
            )
            payload = self._propline_evidence_value(record, "payload", None)
            if payload is None:
                payload = self._propline_evidence_value(record, "payload_json", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError):
                        payload = {"raw": payload}
            try:
                payload_json = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), default=str
                )
            except (TypeError, ValueError):
                continue
            values = {
                "family": family,
                "target_key": target_key,
                "official_sport": official_sport,
                "canonical_sport": self._propline_evidence_value(record, "canonical_sport"),
                "event_id": self._propline_evidence_value(record, "event_id"),
                "player_id": self._propline_evidence_value(record, "player_id"),
                "player_name": self._propline_evidence_value(record, "player_name"),
                "market": self._propline_evidence_value(record, "market"),
                "outcome_id": self._propline_evidence_value(record, "outcome_id"),
                "bookmaker": self._propline_evidence_value(record, "bookmaker"),
                "source": self._propline_evidence_value(record, "source"),
                "fetched_at": fetched_at,
                "expires_at": expires_at,
                "complete": True,
                "redacted": False,
                "payload_json": payload_json,
            }
            prepared[(family, official_sport, target_key)] = values
        if not prepared:
            return 0
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert_stmt = sqlite_insert(PropLineEvidence).values(list(prepared.values()))
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["family", "official_sport", "target_key"],
            set_={
                key: insert_stmt.excluded[key]
                for key in (
                    "canonical_sport", "event_id", "player_id", "player_name",
                    "market", "outcome_id", "bookmaker", "source", "fetched_at",
                    "expires_at", "complete", "redacted", "payload_json",
                )
            },
        )
        async with self.session() as s:
            await s.execute(stmt)
            await s.commit()
        return len(prepared)

    async def get_propline_evidence(
        self,
        family: str,
        target_key: str,
        official_sport: str | None = None,
        *,
        fresh_only: bool = False,
        now: datetime | None = None,
    ) -> dict | None:
        filters = [
            PropLineEvidence.family == str(family),
            PropLineEvidence.target_key == str(target_key),
        ]
        if official_sport is not None:
            filters.append(PropLineEvidence.official_sport == str(official_sport))
        if fresh_only:
            at = self._propline_evidence_datetime(now) or datetime.utcnow()
            filters.append(
                or_(
                    PropLineEvidence.expires_at.is_(None),
                    PropLineEvidence.expires_at > at,
                )
            )
        async with self.session() as s:
            result = await s.execute(
                select(PropLineEvidence)
                .where(*filters)
                .order_by(desc(PropLineEvidence.fetched_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return self._propline_evidence_dict(row) if row else None

    async def get_fresh_propline_evidence_target_keys(
        self,
        family: str,
        target_keys: Iterable[str],
        official_sport: str | None = None,
        now: datetime | None = None,
    ) -> set[str]:
        keys = {str(key) for key in target_keys if str(key)}
        if not keys:
            return set()
        at = self._propline_evidence_datetime(now) or datetime.utcnow()
        filters = [
            PropLineEvidence.family == str(family),
            PropLineEvidence.target_key.in_(keys),
            or_(
                PropLineEvidence.expires_at.is_(None),
                PropLineEvidence.expires_at > at,
            ),
        ]
        if official_sport is not None:
            filters.append(PropLineEvidence.official_sport == str(official_sport))
        async with self.session() as s:
            result = await s.execute(
                select(PropLineEvidence.target_key).where(*filters)
            )
            return {str(key) for key in result.scalars().all()}

    async def list_propline_evidence(
        self,
        *,
        family: str | None = None,
        official_sport: str | None = None,
        canonical_sport: str | None = None,
        event_id: str | None = None,
        player_name: str | None = None,
        market: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        filters = []
        for column, value in (
            (PropLineEvidence.family, family),
            (PropLineEvidence.official_sport, official_sport),
            (PropLineEvidence.canonical_sport, canonical_sport),
            (PropLineEvidence.event_id, event_id),
            (PropLineEvidence.player_name, player_name),
            (PropLineEvidence.market, market),
        ):
            if value is not None:
                filters.append(column == value)
        async with self.session() as s:
            result = await s.execute(
                select(PropLineEvidence)
                .where(*filters)
                .order_by(desc(PropLineEvidence.fetched_at))
                .limit(max(0, int(limit)))
            )
            return [self._propline_evidence_dict(row) for row in result.scalars().all()]

    async def ensure_telegram_commands(
        self,
        definitions: "Iterable[dict]",
    ) -> int:
        """Seed/update command metadata while preserving operator enablement."""
        rows_written = 0
        async with self.session() as s:
            for definition in definitions:
                command = str(definition.get("command") or "").strip().lstrip("/")
                if not command:
                    continue
                result = await s.execute(
                    select(TelegramCommand).where(
                        TelegramCommand.command == command
                    )
                )
                row = result.scalar_one_or_none()
                values = {
                    "description": str(
                        definition.get("description") or ""
                    )[:256],
                    "category": str(
                        definition.get("category") or "System"
                    )[:64],
                    "usage": (
                        str(definition.get("usage"))[:256]
                        if definition.get("usage")
                        else None
                    ),
                    "alias_of": (
                        str(definition.get("alias_of")).strip().lstrip("/")
                        if definition.get("alias_of")
                        else None
                    ),
                    "sort_order": int(definition.get("sort_order") or 0),
                }
                if row is None:
                    row = TelegramCommand(
                        command=command,
                        enabled=bool(definition.get("enabled", True)),
                        **values,
                    )
                    s.add(row)
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                    if "enabled" in definition:
                        row.enabled = bool(definition["enabled"])
                rows_written += 1
            await s.commit()
        return rows_written

    async def disable_telegram_commands(self, commands: "Iterable[str]") -> int:
        """Disable retired commands so stale catalog rows cannot be published."""
        normalized = {
            str(command or "").strip().lstrip("/").lower()
            for command in commands
            if str(command or "").strip()
        }
        if not normalized:
            return 0
        async with self.session() as s:
            result = await s.execute(
                update(TelegramCommand)
                .where(TelegramCommand.command.in_(normalized))
                .values(enabled=False)
            )
            await s.commit()
            return int(result.rowcount or 0)

    async def get_telegram_commands(
        self,
        *,
        enabled_only: bool = False,
    ) -> list["TelegramCommand"]:
        """Return the persisted command catalog in menu order."""
        async with self.session() as s:
            stmt = select(TelegramCommand).order_by(
                TelegramCommand.sort_order,
                TelegramCommand.command,
            )
            if enabled_only:
                stmt = stmt.where(TelegramCommand.enabled.is_(True))
            result = await s.execute(stmt)
            return list(result.scalars().all())

    async def is_telegram_command_enabled(self, command: str) -> bool:
        """Return the DB-controlled availability of one registered command."""
        normalized = str(command or "").strip().lstrip("/").lower()
        if not normalized:
            return False
        async with self.session() as s:
            result = await s.execute(
                select(TelegramCommand.enabled).where(
                    TelegramCommand.command == normalized
                )
            )
            value = result.scalar_one_or_none()
        # A missing row remains available for compatibility during startup or
        # with an older test double; the post-init seed creates all known rows.
        return True if value is None else bool(value)

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

    @_retry_sqlite_write
    async def save_odds(self, record: OddsRecord) -> OddsRecord:
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    # ── Steam ────────────────────────────────────────────────────────────────

    @_retry_sqlite_write
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

    @_retry_sqlite_write
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

    async def _migrate_telegram_delivery_audit_v1(self) -> None:
        """Add source event identity to existing Telegram audit tables."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        "ALTER TABLE telegram_delivery_audit "
                        "ADD COLUMN event_id VARCHAR(128)"
                    )
                )
            except Exception:
                pass  # Column already exists; migration is idempotent.

    async def _migrate_telegram_delivery_audit_v2(self) -> None:
        """Add an idempotent key without deleting ambiguous historical rows."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        "ALTER TABLE telegram_delivery_audit "
                        "ADD COLUMN delivery_key VARCHAR(256)"
                    )
                )
            except Exception:
                pass

        # Only complete, successful PLAY rows are eligible for confirmation.
        # Keep the first occurrence of a duplicate key and leave later rows
        # unkeyed; they remain available as historical UNKNOWN records.
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(TelegramDeliveryAudit).where(
                        TelegramDeliveryAudit.delivery_key.is_(None),
                        TelegramDeliveryAudit.alert_kind == "ACTIONABLE_PICK",
                        TelegramDeliveryAudit.outcome == "sent",
                    )
                )
            ).scalars().all()
            used: set[str] = set()
            for row in rows:
                key = _telegram_delivery_key(
                    provider=row.provider,
                    alert_kind=row.alert_kind,
                    delivery_path=row.delivery_path,
                    external_id=row.external_id,
                    event_id=row.event_id,
                    stat_type=row.stat_type,
                    line_value=row.line_value,
                )
                if key and key not in used:
                    row.delivery_key = key
                    used.add(key)
            await s.commit()

        async with self._engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "uq_telegram_delivery_audit_key "
                        "ON telegram_delivery_audit (delivery_key) "
                        "WHERE delivery_key IS NOT NULL"
                    )
                )
            except Exception as exc:
                # Existing duplicate/legacy rows must never make startup fail.
                logger.warning(
                    "Telegram delivery uniqueness index unavailable: %s",
                    type(exc).__name__,
                )

    async def _migrate_telegram_delivery_audit_v3(self) -> None:
        """Add immutable send-time evidence provenance (idempotent)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE telegram_delivery_audit "
                    "ADD COLUMN evidence_snapshot_json TEXT"
                ))
            except Exception:
                pass

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
        self,
        limit: Optional[int] = 10,
        since_hours: int = 24,
        sports: Optional[Iterable[str]] = None,
    ) -> "list[PropLineHistory]":
        """Return the most-recent Underdog prop snapshot for each (player, stat) within the window.

        Non-removed props only, ordered by fetched_at DESC so freshest data appears first.
        Used as the primary data source for /picks and /slip. When ``sports`` is
        provided, the allowlist is applied before overfetch and balancing so
        inactive high-volume sports cannot crowd out the live slate.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        normalized_sports = {
            str(sport).strip().upper()
            for sport in (sports or ())
            if str(sport).strip()
        }
        if limit is None:
            raise ValueError("/picks current-row retrieval requires a bounded limit")

        # Read one bounded newest-first slice, then balance sports in memory.
        # Deduplicate only after reading the latest row for each provider source
        # so a removal marker cannot resurrect an older directional row.
        # The newest slice can be dominated by one large sport feed (often a
        # football slate whose rows are intentionally non-deliverable).  Read
        # a larger but still bounded window so later direction refresh and
        # sport balancing can see current CS/TENNIS/other rows as well.
        raw_limit = max(limit * 100, 1000)
        async with self.session() as s:
            filters = [
                PropLineHistory.provider == "Underdog",
                PropLineHistory.fetched_at >= cutoff,
            ]
            if sports is not None:
                filters.append(PropLineHistory.sport.in_(normalized_sports))
            result = await s.execute(
                select(PropLineHistory)
                .where(*filters)
                .order_by(desc(PropLineHistory.id))
                .limit(raw_limit)
            )
            raw_rows = list(result.scalars().all())

        latest_by_source: dict[tuple[str, str, str, str], PropLineHistory] = {}
        for row in sorted(raw_rows, key=lambda item: item.id, reverse=True):
            source_id = str(row.external_id or row.game_id or "")
            identity = (row.player_name, row.sport, row.stat_type, source_id)
            latest_by_source.setdefault(identity, row)

        now = datetime.utcnow()
        rows = [
            row
            for row in latest_by_source.values()
            if row.removed is not True
            and (row.game_time is None or row.game_time > now)
        ]
        rows.sort(
            key=lambda row: (
                0 if row.bet_recommendation in ("OVER", "UNDER") else 1,
                -(row.id or 0),
            )
        )

        # ── Sport-balanced selection ───────────────────────────────────────────
        # A flat [:limit] on fetched_at DESC lets high-volume sports (MLB, DOTA)
        # dominate the entire pool.  Instead, distribute slots evenly: take the
        # top ceil(limit / n_sports) rows per sport, then fill to limit.
        # Within each sport, fetched_at DESC order is preserved from the query.
        from math import ceil
        from collections import defaultdict as _dd
        _buckets: dict = _dd(list)
        for _row in rows:
            _buckets[_row.sport.upper()].append(_row)
        _n_sports   = max(len(_buckets), 1)
        _per_sport  = max(3, ceil(limit / _n_sports))
        _balanced: list = []
        _selected_ids: set[int] = set()
        for _sport_rows in _buckets.values():
            for _row in _sport_rows[:_per_sport]:
                _balanced.append(_row)
                _selected_ids.add(_row.id)
        # Fill unused slots from the remaining direction-first population.
        for _row in rows:
            if len(_balanced) >= limit:
                break
            if _row.id not in _selected_ids:
                _balanced.append(_row)
                _selected_ids.add(_row.id)

        logger.info(
            "UD picks query: raw=%d  sports=%d  per_sport_cap=%d  balanced=%d  returning=%d",
            len(raw_rows), _n_sports, _per_sport, len(_balanced),
            min(len(_balanced), limit),
        )

        return _balanced[:limit]

    async def get_current_actionable_opportunities(
        self,
        *,
        limit: int = 100,
        since_hours: int = 72,
    ) -> list["PropOpportunityLog"]:
        """Return recently delivered, still-pregame Underdog opportunities.

        User-facing pick/slip reports must not lose an alert merely because a
        later feed observation marked its current ``PropLineHistory`` row as
        removed or PASS.  The delivered opportunity is the durable actionable
        record; current event timing and sport scope remain presentation gates
        in the command layer.
        """
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(since_hours)))
        now = datetime.utcnow()
        async with self.session() as s:
            result = await s.execute(
                select(PropOpportunityLog)
                .where(
                    PropOpportunityLog.provider == "Underdog",
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                    PropOpportunityLog.alert_sent == True,  # noqa: E712
                    PropOpportunityLog.alert_sent_at >= cutoff,
                    PropOpportunityLog.game_time.isnot(None),
                    PropOpportunityLog.game_time > now,
                )
                .order_by(
                    desc(PropOpportunityLog.alert_sent_at),
                    desc(PropOpportunityLog.id),
                )
                .limit(max(1, min(int(limit), 500)))
            )
            rows = list(result.scalars().all())

        # A line-change alert can create more than one opportunity for the same
        # event/stat. Keep the newest delivered version for command display.
        latest: dict[tuple[str, str], PropOpportunityLog] = {}
        for row in rows:
            key = (str(row.external_id or ""), str(row.stat_type or ""))
            latest.setdefault(key, row)
        return list(latest.values())

    async def get_ud_recommendations_bulk(
        self,
        player_stat_identities: "list[tuple[str, str, str, str]]",
        since_hours: int = 24,
    ) -> "dict[tuple[str, str, str, str], tuple[str | None, int | None]]":
        """
        Return the most-recent bet_recommendation + bet_confidence from
        UnderdogSnapshotRecord for each (player_name, sport, stat_type) triple.

        Used by /picks to display the OVER/UNDER pick direction without
        changing any scoring or pick-generation logic.

        Returns a dict keyed by (player_name, sport, stat_type) →
        (bet_recommendation, bet_confidence).  Missing props get (None, None).
        """
        if not player_stat_identities:
            return {}
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        wanted = {
            (
                str(player),
                str(sport),
                str(stat),
                str(source_id or ""),
            )
            for player, sport, stat, source_id in player_stat_identities
        }

        out: dict[tuple[str, str, str, str], tuple[str | None, int | None]] = {}
        source_ids = {identity[3] for identity in wanted if identity[3]}
        if not source_ids:
            return out
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord).where(
                    UnderdogSnapshotRecord.external_id.in_(source_ids),
                    UnderdogSnapshotRecord.fetched_at >= cutoff,
                )
            )
            rows = list(result.scalars().all())

        latest: dict[
            tuple[str, str, str, str], UnderdogSnapshotRecord
        ] = {}
        for row in rows:
            identity = (
                row.player_name,
                row.sport,
                row.stat_type,
                row.external_id,
            )
            if identity not in wanted:
                continue
            previous = latest.get(identity)
            if previous is None or row.id > previous.id:
                latest[identity] = row
        for identity, row in latest.items():
            if row.removed is not True:
                out[identity] = (row.bet_recommendation, row.bet_confidence)
        return out

    async def get_ud_evidence_provenance_bulk(
        self,
        player_stat_identities: "list[tuple[str, str, str, str]]",
        since_hours: int = 24,
    ) -> "dict[tuple[str, str, str, str], str]":
        """Return persisted decision evidence provenance for current UD props.

        The provenance is read from the same latest snapshot used for the
        current recommendation, so /picks cannot mistake available windows for
        the evidence that actually supplied the direction.
        """
        if not player_stat_identities:
            return {}
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        wanted = {
            (
                str(player),
                str(sport),
                str(stat),
                str(source_id or ""),
            )
            for player, sport, stat, source_id in player_stat_identities
        }
        source_ids = {identity[3] for identity in wanted if identity[3]}
        if not source_ids:
            return {}
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord).where(
                    UnderdogSnapshotRecord.external_id.in_(source_ids),
                    UnderdogSnapshotRecord.fetched_at >= cutoff,
                )
            )
            rows = list(result.scalars().all())

        latest: dict[
            tuple[str, str, str, str], UnderdogSnapshotRecord
        ] = {}
        for row in rows:
            identity = (
                row.player_name,
                row.sport,
                row.stat_type,
                row.external_id,
            )
            if identity not in wanted:
                continue
            previous = latest.get(identity)
            if previous is None or row.id > previous.id:
                latest[identity] = row

        out: dict[tuple[str, str, str, str], str] = {}
        for identity, row in latest.items():
            if row.removed is True or not row.bet_evidence_json:
                continue
            try:
                source = str(
                    json.loads(row.bet_evidence_json).get("evidence_source") or ""
                ).strip().lower()
            except (TypeError, ValueError, AttributeError):
                continue
            if source in {"settled_results", "historical_average"}:
                out[identity] = source
        return out

    async def get_latest_downstream_intelligence(
        self,
        player_stat_triples: "list[tuple]",
        since_hours: int = 72,
    ) -> "dict[tuple, dict]":
        """Return the newest persisted downstream payload for each prop.

        The legacy three-field ``(player, sport, stat)`` form remains
        supported.  Current callers may provide
        ``(provider, player, sport, stat, source_id)`` so an intelligence
        payload cannot cross-contaminate providers or event/line identities.
        """
        if not player_stat_triples:
            return {}
        import json as _json
        from datetime import timedelta

        legacy_requested: set[tuple[str, str, str]] = set()
        scoped_requested: set[tuple[str, str, str, str, str]] = set()
        for identity in player_stat_triples:
            if len(identity) == 3:
                player, sport, stat = identity
                legacy_requested.add((str(player), str(sport), str(stat)))
            elif len(identity) == 5:
                provider, player, sport, stat, source_id = identity
                scoped_requested.add((
                    str(provider).strip().lower(),
                    str(player).strip().lower(),
                    str(sport).strip().upper(),
                    str(stat).strip().lower(),
                    str(source_id or "").strip().lower(),
                ))
        # Preserve the old return-key contract for old callers while making
        # scoped callers explicitly identity-aware.
        requested_triples = {
            (player, sport, stat) for player, sport, stat in legacy_requested
        } | {
            (player, sport, stat)
            for _provider, player, sport, stat, _source in scoped_requested
        }
        if not requested_triples:
            return {}
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            result = await s.execute(
                select(PropOpportunityLog)
                .where(
                    PropOpportunityLog.detected_at >= cutoff,
                    PropOpportunityLog.downstream_intelligence_json.isnot(None),
                    tuple_(
                        PropOpportunityLog.player_name,
                        PropOpportunityLog.sport,
                        PropOpportunityLog.stat_type,
                    ).in_(requested_triples),
                )
                .order_by(desc(PropOpportunityLog.detected_at))
                .limit(max(len(requested_triples) * 20, 100))
            )
            rows = list(result.scalars().all())

        out: dict[tuple, dict] = {}
        for row in rows:
            triple_key = (
                str(row.player_name).lower(),
                str(row.sport).upper(),
                str(row.stat_type).lower(),
            )
            try:
                payload = _json.loads(row.downstream_intelligence_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue

            # A legacy lookup is intentionally broad for compatibility.
            if triple_key in {
                (str(player).lower(), str(sport).upper(), str(stat).lower())
                for player, sport, stat in legacy_requested
            } and triple_key not in out:
                out[triple_key] = payload

            provider_key = str(row.provider or "").strip().lower()
            row_sources = {
                str(row.external_id or "").strip().lower(),
                str(row.game_id or "").strip().lower(),
            } - {""}
            for scoped in scoped_requested:
                if (
                    scoped[0] == provider_key
                    and scoped[1:] in {
                        (
                            triple_key[0],
                            triple_key[1],
                            triple_key[2],
                            source,
                        )
                        for source in row_sources
                    }
                ):
                    if scoped not in out:
                        out[scoped] = payload
        return out

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
        Count PP edge records that were alerted today in the reporting timezone.

        Parameters
        ----------
        tier:      When provided, only count records with that exact tier.
        in_tiers:  When provided, only count records whose tier is in the list
                   (e.g. ``["A", "B"]`` to count only non-S alerts).
                   Takes precedence over ``tier`` when both are supplied.
        None/None → count all tiers.
        """
        from reporting import current_reporting_day_bounds
        today_start, _today_end = current_reporting_day_bounds()
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
        """Count Underdog snapshot records alerted today in reporting time."""
        from reporting import current_reporting_day_bounds
        today_start, _today_end = current_reporting_day_bounds()
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

    async def count_today_actionable_alerts(self) -> int:
        """
        Count confirmed Telegram PLAYs delivered today in reporting time.

        Uses the identity-complete delivery ledger joined to the exact
        directional opportunity.  An opportunity flag by itself is not Telegram
        confirmation.
        """
        from reporting import current_reporting_day_bounds
        today_start, _today_end = current_reporting_day_bounds()
        async with self.session() as s:
            audits = await self._confirmed_telegram_play_audits(
                s, since=today_start
            )
            return len(audits)

    async def get_directional_performance_summary(
        self,
        enabled_sports: Optional[Iterable[str]] = None,
    ) -> dict:
        """Return compact provider/sport pick performance aggregates.

        This is deliberately a grouped SQL read rather than the broad
        monitoring summary.  It keeps `/performance` and `/grade` responsive
        on large historical databases while preserving the V4 population
        boundary: only OVER/UNDER opportunities are included.

        CLV is joined separately through the exact Underdog snapshot that
        created a seed.  A CLVRecord has no opportunity/source identifier, so
        joining it directly to a graded opportunity would be ambiguous.
        """
        enabled_sport_keys = (
            {
                str(sport).strip().upper()
                for sport in enabled_sports
                if str(sport).strip()
            }
            if enabled_sports is not None
            else None
        )
        sport_filter = (
            [func.upper(PropOpportunityLog.sport).in_(enabled_sport_keys)]
            if enabled_sport_keys is not None
            else []
        )
        async with self.session() as s:
            result = await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    PropOpportunityLog.grade_result_source,
                    func.count(),
                )
                .where(
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                    *sport_filter,
                )
                .group_by(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    PropOpportunityLog.grade_result_source,
                )
            )
            clv_result = await s.execute(
                select(
                    PropOpportunityLog.id,
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    AlertCLVSeed.clv_pct,
                    AlertCLVSeed.alerted_at,
                    AlertCLVSeed.id,
                )
                .select_from(PropOpportunityLog)
                .join(
                    UnderdogSnapshotRecord,
                    and_(
                        UnderdogSnapshotRecord.external_id
                        == PropOpportunityLog.external_id,
                        UnderdogSnapshotRecord.stat_type
                        == PropOpportunityLog.stat_type,
                        UnderdogSnapshotRecord.sport
                        == PropOpportunityLog.sport,
                    ),
                )
                .join(
                    AlertCLVSeed,
                    and_(
                        AlertCLVSeed.source_table == "underdog_snapshots",
                        AlertCLVSeed.source_id == UnderdogSnapshotRecord.id,
                        AlertCLVSeed.clv_computed == True,  # noqa: E712
                        AlertCLVSeed.clv_pct.isnot(None),
                    ),
                )
                .where(
                    _trusted_directional_grade_filter(),
                    or_(
                        PropOpportunityLog.provider == "Underdog",
                        PropOpportunityLog.provider.is_(None),
                    ),
                    *sport_filter,
                )
            )

        by_sport: dict[str, dict] = {}
        by_provider: dict[str, dict] = {}
        by_sport_direction: dict[str, dict[str, dict]] = {}

        def _new_performance_row() -> dict:
            return {
                "directional": 0,
                "pending": 0,
                "resolved": 0,
                "graded": 0,
                "unverified": 0,
                "denominator": 0,
                "hit_rate": 0.0,
                "hit": 0,
                "miss": 0,
                "push": 0,
                "void": 0,
                "raw": {},
            }

        def _add_outcome(
            target: dict,
            key: str,
            provider: str,
            recommendation: str,
            outcome: str,
            grade_result_source: str,
            count: int,
        ) -> None:
            row = target.setdefault(key, _new_performance_row())
            row["directional"] += count
            row["raw"].setdefault(recommendation, {})[outcome] = (
                row["raw"].setdefault(recommendation, {}).get(outcome, 0) + count
            )
            if outcome == "PENDING":
                row["pending"] += count
            elif not is_trusted_directional_grade_source(
                provider, outcome, grade_result_source
            ):
                row["unverified"] += count
            elif outcome in _DIRECTIONAL_GRADE_RESULTS:
                row["resolved"] += count
                row["graded"] += count
                if outcome == "PUSH":
                    row["push"] += count
                elif outcome == "HIT":
                    row["hit"] += count
                elif outcome == "MISS":
                    row["miss"] += count
                else:
                    row["void"] += count
            else:
                row["void"] += count

        for (
            provider,
            sport,
            recommendation,
            outcome,
            grade_result_source,
            count,
        ) in result.all():
            if not _is_supported_tracking_sport(sport):
                continue
            provider_key = str(provider or "Underdog")
            sport_key = str(sport or "UNKNOWN")
            rec_key = str(recommendation or "").upper()
            outcome_key = str(outcome or "PENDING").upper()
            count = int(count or 0)
            _add_outcome(
                by_sport,
                sport_key,
                provider_key,
                rec_key,
                outcome_key,
                str(grade_result_source or ""),
                count,
            )
            _add_outcome(
                by_provider,
                provider_key,
                provider_key,
                rec_key,
                outcome_key,
                str(grade_result_source or ""),
                count,
            )
            _add_outcome(
                by_sport_direction.setdefault(sport_key, {}),
                rec_key,
                provider_key,
                rec_key,
                outcome_key,
                str(grade_result_source or ""),
                count,
            )

        # One opportunity can have several snapshots/seeds as a line moves.
        # Keep the latest computed seed for that opportunity so CLV samples
        # cannot be inflated by repeated observations of one pick.
        latest_clv: dict[
            int, tuple[datetime, int, str, str, str, str, float]
        ] = {}
        for (
            opportunity_id,
            provider,
            sport,
            recommendation,
            outcome,
            clv_pct,
            alerted_at,
            seed_id,
        ) in clv_result.all():
            if not _is_supported_tracking_sport(sport) or clv_pct is None:
                continue
            candidate = (
                alerted_at or datetime.min,
                int(seed_id or 0),
                str(provider or "Underdog"),
                str(sport or "UNKNOWN"),
                str(recommendation or "OVER").upper(),
                str(outcome or "PENDING").upper(),
                float(clv_pct),
            )
            previous = latest_clv.get(int(opportunity_id))
            if previous is None or candidate[:2] > previous[:2]:
                latest_clv[int(opportunity_id)] = candidate

        for (
            _alerted_at,
            _seed_id,
            provider,
            sport,
            recommendation,
            outcome,
            clv_pct,
        ) in latest_clv.values():
            # CLV coverage is intentionally based on completed directional
            # results only; pending picks do not make the denominator larger.
            if outcome not in _DIRECTIONAL_GRADE_RESULTS or not is_trusted_directional_grade_source(
                provider,
                outcome,
                "propline_history"
                if str(provider or "").casefold() in {"", "underdog"}
                else "external",
            ):
                continue
            # PropOpportunityLog outcomes already reflect the recorded
            # recommendation; do not invert UNDER results a second time.
            directional_outcome = outcome
            for target, key in (
                (by_sport, sport),
                (by_provider, provider),
                (
                    by_sport_direction.setdefault(str(sport), {}),
                    str(recommendation).upper(),
                ),
            ):
                row = target.setdefault(
                    key,
                    _new_performance_row(),
                )
                row.setdefault("_clv_values", {"HIT": [], "MISS": [], "PUSH": []})[
                    "PUSH" if outcome == "PUSH" else directional_outcome
                ].append(clv_pct)

        for target in (by_sport, by_provider):
            for row in target.values():
                clv_values = row.pop("_clv_values", {})
                for outcome in ("HIT", "MISS", "PUSH"):
                    values = clv_values.get(outcome, [])
                    row[f"clv_{outcome.lower()}_count"] = len(values)
                    row[f"clv_{outcome.lower()}_avg"] = (
                        sum(values) / len(values) if values else None
                    )
                clv_sample_count = sum(
                    row[f"clv_{outcome.lower()}_count"]
                    for outcome in ("HIT", "MISS", "PUSH")
                )
                row["clv_count"] = clv_sample_count
                row["clv_coverage_pct"] = (
                    clv_sample_count / (row["hit"] + row["miss"] + row["push"]) * 100
                    if (row["hit"] + row["miss"] + row["push"])
                    else None
                )
        for direction_rows in by_sport_direction.values():
            for row in direction_rows.values():
                clv_values = row.pop("_clv_values", {})
                for outcome in ("HIT", "MISS", "PUSH"):
                    values = clv_values.get(outcome, [])
                    row[f"clv_{outcome.lower()}_count"] = len(values)
                    row[f"clv_{outcome.lower()}_avg"] = (
                        sum(values) / len(values) if values else None
                    )
                clv_sample_count = sum(
                    row[f"clv_{outcome.lower()}_count"]
                    for outcome in ("HIT", "MISS", "PUSH")
                )
                row["clv_count"] = clv_sample_count
                row["clv_coverage_pct"] = (
                    clv_sample_count / (row["hit"] + row["miss"] + row["push"]) * 100
                    if (row["hit"] + row["miss"] + row["push"])
                    else None
                )
        # One performance contract for every consumer: HIT, MISS, and PUSH all
        # contribute to the outcome denominator.
        for target in (by_sport, by_provider):
            for row in target.values():
                row["denominator"] = row["hit"] + row["miss"] + row["push"]
                row["hit_rate"] = (
                    round(row["hit"] / row["denominator"] * 100, 1)
                    if row["denominator"] else 0.0
                )
        for direction_rows in by_sport_direction.values():
            for row in direction_rows.values():
                row["denominator"] = row["hit"] + row["miss"] + row["push"]
                row["hit_rate"] = (
                    round(row["hit"] / row["denominator"] * 100, 1)
                    if row["denominator"] else 0.0
                )
        return {
            "by_sport": by_sport,
            "by_provider": by_provider,
            "by_sport_direction": by_sport_direction,
        }

    async def count_actionable_pick_records(self) -> int:
        """Total 🎯 ACTIONABLE BET PICK records ever delivered to Telegram.

        Counts identity-complete successful Telegram PLAY audit rows joined to
        their exact directional opportunity.

        Note: one row per (external_id, stat_type) due to the upsert model.
        A row whose recommendation was later overwritten (e.g. OVER → PASS) after
        alerting would otherwise inflate this count.  The OVER/UNDER filter keeps
        both counts consistent.

        The legacy source population used ``recommendation.in_(["OVER",
        "UNDER"])`` and ``alert_sent == True``.  Those fields remain
        compatibility bookkeeping only; this method now counts the stricter
        identity-complete audit population.

        "sent" = Telegram Bot API accepted the send; no message-ID receipt stored.
        """
        async with self.session() as s:
            return len(await self._confirmed_telegram_play_audits(s))

    async def log_telegram_delivery(
        self,
        *,
        provider: str,
        alert_kind: str = "ACTIONABLE_PICK",
        delivery_path: str = "unknown",
        player_name: Optional[str] = None,
        sport: Optional[str] = None,
        stat_type: Optional[str] = None,
        external_id: Optional[str] = None,
        event_id: Optional[str] = None,
        line_value: Optional[float] = None,
        outcome: str,
        recipients_sent: int = 0,
        recipients_failed: int = 0,
        attempted_at: Optional[datetime] = None,
    ) -> int:
        """Persist one outbound Telegram attempt without affecting delivery.

        Confirmed PLAY rows use an immutable storage key and ``INSERT OR
        IGNORE`` so retries and concurrent workers cannot create duplicate
        confirmation rows. Other alert kinds remain append-only audit events.
        """
        provider = str(provider or "Unknown")
        alert_kind = str(alert_kind or "ACTIONABLE_PICK")
        delivery_path = str(delivery_path or "unknown")
        delivery_key = _telegram_delivery_key(
            provider=provider,
            alert_kind=alert_kind,
            delivery_path=delivery_path,
            external_id=external_id,
            event_id=event_id,
            stat_type=stat_type,
            line_value=line_value,
        ) if str(outcome or "").lower() == "sent" else None
        values = dict(
            provider=provider,
            alert_kind=alert_kind,
            delivery_path=delivery_path,
            player_name=player_name,
            sport=sport,
            stat_type=stat_type,
            external_id=external_id,
            event_id=event_id,
            delivery_key=delivery_key,
            line_value=line_value,
            outcome=str(outcome or "failed"),
            recipients_sent=int(recipients_sent or 0),
            recipients_failed=int(recipients_failed or 0),
            attempted_at=attempted_at or datetime.utcnow(),
        )
        async with self.session() as s:
            from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
            if delivery_key:
                stmt = _sqlite_insert(TelegramDeliveryAudit).values(**values)
                stmt = stmt.prefix_with("OR IGNORE")
                result = await s.execute(stmt)
            else:
                result = await s.execute(
                    _sqlite_insert(TelegramDeliveryAudit).values(**values)
                )
            await s.commit()
            return int(result.rowcount or 0)

    async def _confirmed_telegram_play_audits(
        self,
        s: AsyncSession,
        *,
        since: Optional[datetime] = None,
    ) -> list[TelegramDeliveryAudit]:
        """Return only identity-complete successful PLAY audits with a linked opportunity.

        ``alert_sent`` is bookkeeping on an opportunity, not proof that Telegram
        accepted a message.  Conversely, an audit row without an exact
        provider/source/stat opportunity cannot prove which PLAY it delivered.
        Keeping this join rule in one helper prevents individual reports from
        quietly using different definitions of "delivered".
        """
        audit_filters = [
            TelegramDeliveryAudit.delivery_key.isnot(None),
            TelegramDeliveryAudit.alert_kind == "ACTIONABLE_PICK",
            TelegramDeliveryAudit.outcome == "sent",
            TelegramDeliveryAudit.provider.isnot(None),
            TelegramDeliveryAudit.external_id.isnot(None),
            TelegramDeliveryAudit.external_id != "",
            TelegramDeliveryAudit.event_id.isnot(None),
            TelegramDeliveryAudit.event_id != "",
            TelegramDeliveryAudit.stat_type.isnot(None),
            TelegramDeliveryAudit.stat_type != "",
            TelegramDeliveryAudit.line_value.isnot(None),
        ]
        if since is not None:
            audit_filters.append(TelegramDeliveryAudit.attempted_at >= since)

        audits = (
            await s.execute(
                select(TelegramDeliveryAudit)
                .where(*audit_filters)
                .order_by(TelegramDeliveryAudit.attempted_at, TelegramDeliveryAudit.id)
            )
        ).scalars().all()
        if not audits:
            return []

        opportunity_rows = (
            await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.external_id,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.game_id,
                ).where(
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                )
            )
        ).all()
        linked = set()
        for provider, external_id, stat_type, sport, game_id in opportunity_rows:
            event_identity = str(game_id or "").strip()
            if not event_identity:
                continue
            linked.add(
                (
                    str(provider or "Underdog").strip().lower() or "underdog",
                    _stored_opportunity_external_id(
                        str(external_id or ""), provider
                    ),
                    str(stat_type or "").strip().casefold(),
                    str(sport or "").strip().upper(),
                    event_identity,
                )
            )
        return [
            audit
            for audit in audits
            if (
                str(audit.provider or "Underdog").strip().lower() or "underdog",
                _stored_opportunity_external_id(
                    str(audit.external_id or ""), audit.provider
                ),
                str(audit.stat_type or "").strip().casefold(),
                str(audit.sport or "").strip().upper(),
                str(audit.event_id or "").strip(),
            ) in linked
        ]

    @_retry_sqlite_write
    async def record_telegram_play_delivery(
        self,
        *,
        provider: str,
        delivery_path: str,
        player_name: Optional[str],
        sport: Optional[str],
        stat_type: str,
        external_id: str,
        event_id: str,
        line_value: float,
        recipients_sent: int,
        recipients_failed: int,
        attempted_at: Optional[datetime] = None,
        evidence_snapshot: Optional[dict] = None,
    ) -> bool:
        """Atomically persist a confirmed PLAY and mark its exact opportunity.

        A PLAY without complete stable identity cannot be confirmed. Existing
        rows are idempotent: a retry reuses the same audit row and only marks
        the provider-scoped opportunity matching that source identity.
        """
        delivery_key = _telegram_delivery_key(
            provider=provider,
            alert_kind="ACTIONABLE_PICK",
            delivery_path=delivery_path,
            external_id=external_id,
            event_id=event_id,
            stat_type=stat_type,
            line_value=line_value,
        )
        if delivery_key is None:
            logger.warning(
                "Cannot confirm Telegram PLAY without complete stable identity: "
                "provider=%s external_id=%s event_id=%s stat=%s",
                provider, external_id, event_id, stat_type,
            )
            return False

        async with self.session() as s:
            from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
            stmt = _sqlite_insert(TelegramDeliveryAudit).values(
                provider=str(provider or "Unknown"),
                alert_kind="ACTIONABLE_PICK",
                delivery_path=str(delivery_path or "unknown"),
                player_name=player_name,
                sport=sport,
                stat_type=stat_type,
                external_id=external_id,
                event_id=event_id,
                delivery_key=delivery_key,
                line_value=line_value,
                outcome="sent",
                recipients_sent=int(recipients_sent or 0),
                recipients_failed=int(recipients_failed or 0),
                attempted_at=attempted_at or datetime.utcnow(),
                evidence_snapshot_json=(
                    json.dumps(evidence_snapshot, sort_keys=True, separators=(",", ":"))
                    if evidence_snapshot is not None else None
                ),
            ).prefix_with("OR IGNORE")
            await s.execute(stmt)
            audit = (
                await s.execute(
                    select(TelegramDeliveryAudit).where(
                        TelegramDeliveryAudit.delivery_key == delivery_key,
                        TelegramDeliveryAudit.outcome == "sent",
                    )
                )
            ).scalar_one_or_none()
            if audit is None:
                await s.rollback()
                return False
            result = await s.execute(
                update(PropOpportunityLog)
                .where(
                    PropOpportunityLog.external_id == _stored_opportunity_external_id(
                        external_id, provider
                    ),
                    PropOpportunityLog.stat_type == stat_type,
                    PropOpportunityLog.provider == provider,
                )
                .values(alert_sent=True, alert_sent_at=audit.attempted_at)
            )
            await s.commit()
            if int(result.rowcount or 0) == 0:
                logger.warning(
                    "Confirmed Telegram PLAY has no exact opportunity row: "
                    "provider=%s external_id=%s stat=%s",
                    provider, external_id, stat_type,
                )
                return False
            return True

    async def get_telegram_delivery_state(
        self,
        *,
        provider: str,
        external_id: str,
        stat_type: str,
    ) -> dict[str, bool]:
        """Return durable tracking/opportunity state for one Telegram pick.

        Telegram is the delivery edge of the tracking and grading funnel. A
        source observation must already have a neutral tracking row and a
        directional opportunity row before it can be sent. The opportunity's
        ``alert_sent`` flag is the durable one-alert boundary across restarts
        and line re-evaluations.

        Tracking rows retain the observed threshold in a ``:line=`` suffix so
        moved thresholds remain independently gradeable. The source/stat
        prefix is stable across those observations, so this check accepts the
        current line while still requiring a tracked row.
        """
        provider_key = str(provider or "").strip()
        source_id = str(external_id or "").strip()
        market = str(stat_type or "").strip()
        if not provider_key or not source_id or not market:
            return {
                "tracked": False,
                "directional": False,
                "already_delivered": False,
            }

        stored_external_id = _stored_opportunity_external_id(
            source_id, provider_key
        )
        async with self.session() as s:
            tracked_result = await s.execute(
                select(PropTrackingLog.id)
                .where(
                    PropTrackingLog.provider == provider_key,
                    PropTrackingLog.stat_type == market,
                    or_(
                        PropTrackingLog.external_id == source_id,
                        PropTrackingLog.external_id.startswith(
                            f"{source_id}:line="
                        ),
                    ),
                )
                .limit(1)
            )
            tracking_id = tracked_result.scalar_one_or_none()

            opportunity = (
                await s.execute(
                    select(
                        PropOpportunityLog.alert_sent,
                        PropOpportunityLog.recommendation,
                    )
                    .where(
                        PropOpportunityLog.provider == provider_key,
                        PropOpportunityLog.external_id == stored_external_id,
                        PropOpportunityLog.stat_type == market,
                    )
                    .limit(1)
                )
            ).first()

        recommendation = str(opportunity[1] or "").upper() if opportunity else ""
        return {
            "tracked": tracking_id is not None,
            "directional": recommendation in {"OVER", "UNDER"},
            "already_delivered": bool(opportunity and opportunity[0]),
        }

    async def get_telegram_reconciliation(
        self,
        since_hours: int = 72,
        bucket_minutes: int = 60,
    ) -> dict:
        """Reconcile source volume with persisted and delivered alert volume.

        Legacy rows are intentionally reported separately from the durable
        audit ledger.  This prevents a candidate/evaluation count or a
        recipient fan-out count from being presented as Telegram messages.
        """
        from datetime import timedelta

        since_hours = max(1, int(since_hours))
        bucket_minutes = max(5, min(1440, int(bucket_minutes)))
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=since_hours)
        bucket_seconds = bucket_minutes * 60

        def _blank() -> dict:
            return {
                "observed": 0,
                "evaluated": 0,
                "tracked": 0,
                "directional": 0,
                "actionable_persisted": 0,
                "delivery_attempts": 0,
                "delivered_alerts": 0,
                "unverified_historical": 0,
                "delivered_recipients": 0,
                "audit_delivered_alerts": 0,
                "audit_recipients": 0,
                "failed_attempts": 0,
                "rate_limited": 0,
                "dedup_suppressed": 0,
                "pending": 0,
                "resolved": 0,
                "hit": 0,
                "miss": 0,
                "push": 0,
            }

        by_provider: dict[str, dict] = {}
        buckets: dict[tuple[str, str], dict] = {}
        legacy_deliveries = 0
        unverified_historical = 0
        audited_deliveries = 0
        legacy_opportunity_candidates: list[tuple[str, str, str]] = []

        def _provider(name: Optional[str]) -> str:
            return str(name or "Unknown").strip() or "Unknown"

        def _ensure(name: str) -> dict:
            return by_provider.setdefault(name, _blank())

        def _bucket(dt: Optional[datetime]) -> Optional[str]:
            if dt is None:
                return None
            try:
                epoch = int(dt.replace(tzinfo=None).timestamp())
                start = datetime.utcfromtimestamp(epoch - (epoch % bucket_seconds))
                return start.strftime("%Y-%m-%d %H:%M:%S")
            except (AttributeError, TypeError, ValueError, OSError):
                return None

        def _add_bucket(
            provider: str,
            dt: Optional[datetime],
            *,
            evaluated: int = 0,
            actionable_persisted: int = 0,
            delivery_attempts: int = 0,
            delivered_alerts: int = 0,
            delivered_recipients: int = 0,
            audit_delivered_alerts: int = 0,
            audit_recipients: int = 0,
        ) -> None:
            label = _bucket(dt)
            if label is None:
                return
            row = buckets.setdefault((label, provider), _blank())
            row["evaluated"] += evaluated
            row["actionable_persisted"] += actionable_persisted
            row["delivery_attempts"] += delivery_attempts
            row["delivered_alerts"] += delivered_alerts
            row["delivered_recipients"] += delivered_recipients
            row["audit_delivered_alerts"] += audit_delivered_alerts
            row["audit_recipients"] += audit_recipients

        async with self.session() as s:
            # PropLineHistory is the persistent tracking universe for both
            # primary providers. Count every normalized row as an observation;
            # this is deliberately independent of evaluation and delivery.
            # The legacy Underdog snapshot table is used only as a compatibility
            # fallback for installations that have no normalized UD history.
            try:
                observed_rows = await s.execute(
                    select(
                        PropLineHistory.provider,
                        PropLineHistory.sport,
                        func.count(PropLineHistory.id),
                    ).where(
                        PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS),
                        PropLineHistory.fetched_at >= cutoff,
                    ).group_by(
                        PropLineHistory.provider,
                        PropLineHistory.sport,
                    )
                )
                normalized_observed: dict[str, int] = {}
                normalized_provider_seen: set[str] = set()
                for provider_name, sport, count in observed_rows.all():
                    normalized_provider_seen.add(_provider(provider_name))
                    if not _is_supported_tracking_sport(sport):
                        continue
                    provider_key = _provider(provider_name)
                    normalized_observed[provider_key] = (
                        normalized_observed.get(provider_key, 0)
                        + int(count or 0)
                    )
                for provider_name, count in normalized_observed.items():
                    _ensure(provider_name)["observed"] += count
                if "Underdog" not in normalized_provider_seen:
                    legacy_ud_observed = await s.execute(
                        select(func.count()).select_from(UnderdogSnapshotRecord).where(
                            UnderdogSnapshotRecord.fetched_at >= cutoff
                        )
                    )
                    legacy_count = int(legacy_ud_observed.scalar() or 0)
                    if legacy_count:
                        _ensure("Underdog")["observed"] += legacy_count

                tracked_ids = (
                    select(func.max(PropLineHistory.id))
                    .where(
                        PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS),
                    )
                    .group_by(
                        PropLineHistory.provider,
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                        case(
                            (
                                and_(
                                    PropLineHistory.external_id.isnot(None),
                                    PropLineHistory.external_id != "",
                                ),
                                PropLineHistory.external_id,
                            ),
                            else_=PropLineHistory.game_id,
                        ),
                    )
                )
                tracked_rows = await s.execute(
                    select(
                        PropLineHistory.provider,
                        PropLineHistory.sport,
                        func.count(PropLineHistory.id),
                    )
                    .where(
                        PropLineHistory.id.in_(tracked_ids),
                        or_(
                            PropLineHistory.removed == False,  # noqa: E712
                            PropLineHistory.removed.is_(None),
                        ),
                        or_(
                            PropLineHistory.game_time.is_(None),
                            PropLineHistory.game_time > now,
                        ),
                    )
                    .group_by(
                        PropLineHistory.provider,
                        PropLineHistory.sport,
                    )
                )
                for provider_name, sport, count in tracked_rows.all():
                    if not _is_supported_tracking_sport(sport):
                        continue
                    provider = _provider(provider_name)
                    row = _ensure(provider)
                    row["tracked"] += int(count or 0)
            except Exception as exc:
                logger.debug("prop-history tracking query skipped: %s", exc)

            # Candidate rows are the broad evaluation stream.
            try:
                candidate_rows = await s.execute(
                select(
                    PropCandidateLog.provider,
                    PropCandidateLog.sport,
                    func.count(PropCandidateLog.id),
                )
                    .where(PropCandidateLog.scan_ts >= cutoff)
                .group_by(PropCandidateLog.provider, PropCandidateLog.sport)
                )
                candidate_providers: set[str] = set()
                for provider_name, sport, count in candidate_rows.all():
                    if not _is_supported_tracking_sport(sport):
                        continue
                    provider = _provider(provider_name)
                    candidate_providers.add(provider)
                    _ensure(provider)["evaluated"] += int(count or 0)
            except Exception as exc:
                candidate_providers = set()
                logger.debug("telegram reconciliation candidate query skipped: %s", exc)

            # Canonical Underdog/PrizePicks opportunity rows.
            pol_rows = await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.detected_at,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.external_id,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    PropOpportunityLog.alert_sent,
                    PropOpportunityLog.alert_sent_at,
                ).where(
                    or_(
                        PropOpportunityLog.detected_at >= cutoff,
                        PropOpportunityLog.alert_sent_at >= cutoff,
                    )
                )
            )
            for (
                provider_name, detected_at, sport, external_id, stat_type,
                recommendation, result,
                alert_sent, alert_sent_at,
            ) in pol_rows.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider = _provider(provider_name or "Underdog")
                row = _ensure(provider)
                if detected_at is not None and detected_at >= cutoff:
                    # PropCandidateLog is the broad evaluation stream.  POL is
                    # used as a fallback for providers without candidate rows,
                    # avoiding a double evaluation count for Underdog.
                    if provider not in candidate_providers:
                        row["evaluated"] += 1
                    if str(recommendation or "").upper() in {"OVER", "UNDER"}:
                        row["actionable_persisted"] += 1
                        row["directional"] += 1
                        result_key = str(result or "PENDING").upper()
                        if result_key == "PENDING":
                            row["pending"] += 1
                        elif result_key in {
                            "HIT", "MISS", "PUSH", "VOID", "CANCELLED",
                            "INJURY_VOID", "GAME_INTERRUPTED",
                        }:
                            row["resolved"] += 1
                        if result_key == "HIT":
                            row["hit"] += 1
                        elif result_key == "MISS":
                            row["miss"] += 1
                        elif result_key == "PUSH":
                            row["push"] += 1
                    _add_bucket(
                        provider,
                        detected_at,
                        evaluated=1 if provider not in candidate_providers else 0,
                        actionable_persisted=(
                            1 if str(recommendation or "").upper() in {"OVER", "UNDER"} else 0
                        ),
                    )
                if alert_sent and alert_sent_at is not None and alert_sent_at >= cutoff:
                    legacy_opportunity_candidates.append(
                        (provider, str(external_id or ""), str(stat_type or ""))
                    )

            # Legacy independent PrizePicks edge rows.
            pp_rows = await s.execute(
                select(
                    PPEdgeRecord.sport,
                    PPEdgeRecord.detected_at,
                    PPEdgeRecord.alert_sent,
                ).where(PPEdgeRecord.detected_at >= cutoff)
            )
            for sport, detected_at, alert_sent in pp_rows.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                row = _ensure("PrizePicks")
                row["actionable_persisted"] += 1
                row["directional"] += 1
                row["pending"] += 1
                if alert_sent:
                    row["unverified_historical"] += 1
                    unverified_historical += 1
                    legacy_deliveries += 1
                _add_bucket(
                    "PrizePicks",
                    detected_at,
                    actionable_persisted=1,
                    delivered_alerts=1 if alert_sent else 0,
                )

            # Legacy sportsbook paths are kept separate from Underdog and
            # PrizePicks.  They are normally scope-blocked, but historical
            # rows must remain visible if an older configuration delivered one.
            for model in (EVRecord, SteamRecord):
                legacy_rows = await s.execute(
                    select(model.sport, model.detected_at, model.alert_sent)
                    .where(model.detected_at >= cutoff)
                )
                for sport, detected_at, alert_sent in legacy_rows.all():
                    if not _is_supported_tracking_sport(sport):
                        continue
                    row = _ensure("Sportsbook")
                    row["evaluated"] += 1
                    row["actionable_persisted"] += 1
                    if alert_sent:
                        row["unverified_historical"] += 1
                        unverified_historical += 1
                        legacy_deliveries += 1
                    _add_bucket(
                        "Sportsbook",
                        detected_at,
                        evaluated=1,
                        actionable_persisted=1,
                        delivered_alerts=1 if alert_sent else 0,
                    )

            audit_rows = await s.execute(
                select(
                    TelegramDeliveryAudit.provider,
                    TelegramDeliveryAudit.id,
                    TelegramDeliveryAudit.alert_kind,
                    TelegramDeliveryAudit.delivery_path,
                    TelegramDeliveryAudit.player_name,
                    TelegramDeliveryAudit.sport,
                    TelegramDeliveryAudit.stat_type,
                    TelegramDeliveryAudit.external_id,
                    TelegramDeliveryAudit.event_id,
                    TelegramDeliveryAudit.line_value,
                    TelegramDeliveryAudit.outcome,
                    TelegramDeliveryAudit.recipients_sent,
                    TelegramDeliveryAudit.recipients_failed,
                    TelegramDeliveryAudit.attempted_at,
                ).where(TelegramDeliveryAudit.attempted_at >= cutoff)
            )
            audit_records = audit_rows.all()
            audit_identity_counts: dict[tuple, int] = {}
            audit_count = 0
            for (
                provider_name, audit_id, alert_kind, delivery_path, player_name, sport,
                stat_type, external_id, event_id, line_value, outcome,
                recipients_sent, recipients_failed, attempted_at,
            ) in audit_records:
                audit_count += 1
                identity = (
                    _provider(provider_name), alert_kind, delivery_path,
                    external_id, event_id, stat_type, line_value,
                )
                audit_identity_counts[identity] = audit_identity_counts.get(identity, 0) + 1
                is_play_delivery = (
                    str(alert_kind or "").strip().upper() == "ACTIONABLE_PICK"
                )
                outcome = str(outcome or "failed").lower()
                if not _is_supported_tracking_sport(sport):
                    # Preserve a provider row for delivery-ledger integrity
                    # reporting, while excluding football from current
                    # tracking/evaluation populations.
                    provider = _provider(provider_name)
                    row = _ensure(provider)
                    row["delivery_attempts"] += 1
                    if outcome == "rate_limited":
                        row["rate_limited"] += 1
                    elif outcome == "deduped":
                        row["dedup_suppressed"] += 1
                    else:
                        row["failed_attempts"] += 1
                    continue
                provider = _provider(provider_name)
                row = _ensure(provider)
                row["delivery_attempts"] += 1
                if outcome == "rate_limited":
                    row["rate_limited"] += 1
                elif outcome == "deduped":
                    row["dedup_suppressed"] += 1
                else:
                    row["failed_attempts"] += 1
                _add_bucket(
                    provider,
                    attempted_at,
                    delivery_attempts=1,
                    audit_delivered_alerts=(
                        1 if outcome == "sent" and is_play_delivery else 0
                    ),
                    audit_recipients=(
                        int(recipients_sent or 0) if is_play_delivery else 0
                    ),
                )

            confirmed_audits = await self._confirmed_telegram_play_audits(
                s, since=cutoff
            )
            confirmed_ids = {int(audit.id) for audit in confirmed_audits}
            confirmed_identities = {
                (
                    _provider(audit.provider),
                    _stored_opportunity_external_id(
                        str(audit.external_id or ""), audit.provider
                    ),
                    str(audit.stat_type or ""),
                )
                for audit in confirmed_audits
            }
            for provider, external_id, stat_type in legacy_opportunity_candidates:
                if (
                    provider,
                    _stored_opportunity_external_id(external_id, provider),
                    stat_type,
                ) not in confirmed_identities:
                    row = _ensure(provider)
                    row["unverified_historical"] += 1
                    unverified_historical += 1
                    legacy_deliveries += 1

            for (
                provider_name, audit_id, alert_kind, delivery_path, player_name,
                sport, stat_type, external_id, event_id, line_value, outcome,
                recipients_sent, recipients_failed, attempted_at,
            ) in audit_records:
                if (
                    str(outcome or "").lower() == "sent"
                    and str(alert_kind or "").strip().upper() == "ACTIONABLE_PICK"
                    and int(audit_id) not in confirmed_ids
                ):
                    row = _ensure(provider_name)
                    row["unverified_historical"] += 1
                    unverified_historical += 1

            for audit in confirmed_audits:
                provider = _provider(audit.provider)
                row = _ensure(provider)
                row["delivered_alerts"] += 1
                row["audit_delivered_alerts"] += 1
                row["audit_recipients"] += int(audit.recipients_sent or 0)
                audited_deliveries += 1
                _add_bucket(
                    provider,
                    audit.attempted_at,
                    delivered_alerts=1,
                    delivered_recipients=int(audit.recipients_sent or 0),
                    audit_delivered_alerts=1,
                    audit_recipients=int(audit.recipients_sent or 0),
                )

        duplicate_delivery_events = sum(
            max(0, count - 1) for count in audit_identity_counts.values()
        )
        pending_total = sum(row.get("pending", 0) for row in by_provider.values())
        resolved_total = sum(row.get("resolved", 0) for row in by_provider.values())
        directional_total = sum(
            row.get("directional", 0) for row in by_provider.values()
        )
        result_counts = {
            "HIT": sum(row.get("hit", 0) for row in by_provider.values()),
            "MISS": sum(row.get("miss", 0) for row in by_provider.values()),
            "PUSH": sum(row.get("push", 0) for row in by_provider.values()),
        }

        return {
            "since_hours": since_hours,
            "bucket_minutes": bucket_minutes,
            "window_start": cutoff,
            "by_provider": by_provider,
            "directional": directional_total,
            "actionable": directional_total,
            "pending": pending_total,
            "resolved": resolved_total,
            "result_counts": result_counts,
            "time_buckets": [
                {
                    "bucket": bucket,
                    "provider": provider,
                    **values,
                }
                for (bucket, provider), values in sorted(buckets.items())
            ],
            "audit_rows": audit_count,
            "audited_deliveries": audited_deliveries,
            "legacy_delivery_rows": legacy_deliveries,
            "unverified_historical": unverified_historical,
            "duplicate_delivery_events": duplicate_delivery_events,
            "exact_recipient_count_available": audit_count > 0,
            "accounting_note": (
                "Legacy alert_sent rows prove a successful send attempt but do not "
                "store Telegram message IDs, recipients, or retries. Exact outbound "
                "message and fan-out counts are available only for audit rows."
            ),
        }

    async def get_resolved_actionable_picks(self, limit: int = 200) -> list:
        """
        Directional tracked opportunities that have been graded.

        Delivery is deliberately not part of this query: undelivered
        observations remain tracked and gradeable.  The historical method name
        is retained for command/test compatibility.
        """
        async with self.session() as s:
            rows = await s.execute(
                select(PropOpportunityLog)
                .where(
                    _trusted_directional_grade_filter(),
                    PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]),
                )
                .order_by(PropOpportunityLog.detected_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def get_resolved_tracked_opportunities(self, limit: int = 200) -> list:
        """Return resolved directional opportunities from the tracked stream."""
        return await self.get_resolved_actionable_picks(limit=limit)

    async def count_directional_opportunity_records(self) -> int:
        """Count all persisted directional opportunities, delivered or not."""
        async with self.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(PropOpportunityLog)
                .where(
                    PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]),
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

    async def save_market_snapshots_bulk(
        self,
        records: "list[MarketSnapshotRecord]",
    ) -> int:
        """Persist market observations in one transaction."""
        if not records:
            return 0
        async with self.session() as s:
            s.add_all(records)
            await s.flush()
            await s.commit()
        return len(records)

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

    async def get_recent_market_snapshots(
        self,
        since_hours: int = 24,
        limit: int = 5000,
    ) -> list["MarketSnapshotRecord"]:
        """Return recent persisted non-pick'em market observations for reports."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(since_hours)))
        async with self.session() as s:
            result = await s.execute(
                select(MarketSnapshotRecord)
                .where(
                    MarketSnapshotRecord.recorded_at >= cutoff,
                    MarketSnapshotRecord.is_pickem == False,  # noqa: E712
                )
                .order_by(desc(MarketSnapshotRecord.recorded_at))
                .limit(max(1, min(int(limit), 10000)))
            )
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

    async def save_clv_records_bulk(
        self,
        records: "list[CLVRecord]",
    ) -> int:
        """Persist CLV results in bounded write transactions."""
        if not records:
            return 0
        written = 0
        for start in range(0, len(records), _CLV_BULK_BATCH_SIZE):
            batch = records[start:start + _CLV_BULK_BATCH_SIZE]
            async with self.session() as s:
                s.add_all(batch)
                await s.commit()
            written += len(batch)
        return written

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

    async def update_ud_props_mq_scores_bulk(
        self,
        mq_map: "dict[tuple[str, str, str], tuple[int, float]]",
        *,
        since_at: datetime | None = None,
    ) -> None:
        """
        Update market_quality_score and score_total on the most-recent PropLineHistory
        row for each (player_name, sport, stat_type) entry in mq_map.

        mq_map values are (mq_score: int, bq_score: float) tuples written by the
        scan loop at each compute_market_quality() call site.

        Called once per scan cycle.  Non-fatal — write failures are logged and skipped.
        """
        if not mq_map:
            return
        try:
            async with self.session() as s:
                # Find the latest active row for every key in one grouped
                # read. The old loop issued a SELECT and UPDATE per prop,
                # turning a 5k feed into roughly 10k SQLite statements.
                keys = list(mq_map)
                latest_filters = [
                    PropLineHistory.provider == "Underdog",
                    PropLineHistory.removed.isnot(True),
                    tuple_(
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                    ).in_(keys),
                ]
                if since_at is not None:
                    latest_filters.append(PropLineHistory.fetched_at >= since_at)
                latest_result = await s.execute(
                    select(
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                        func.max(PropLineHistory.id).label("row_id"),
                    )
                    .where(*latest_filters)
                    .group_by(
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                    )
                )
                latest_ids = {
                    (row.player_name, row.sport, row.stat_type): row.row_id
                    for row in latest_result
                }
                updates_with_bq: list[dict] = []
                updates_mq_only: list[dict] = []
                for key, scores in mq_map.items():
                    row_id = latest_ids.get(key)
                    if row_id is None:
                        continue
                    # Unpack — backward-compat: accept bare int from legacy callers.
                    if isinstance(scores, tuple):
                        mq_score, bq_score = scores
                    else:
                        mq_score, bq_score = int(scores), None
                    values: dict = {
                        "id": row_id,
                        "market_quality_score": int(mq_score),
                    }
                    if bq_score is not None:
                        values["score_total"] = float(bq_score)
                        updates_with_bq.append(values)
                    else:
                        updates_mq_only.append(values)
                for updates in (updates_with_bq, updates_mq_only):
                    for start in range(0, len(updates), 500):
                        await s.execute(
                            sa_update(PropLineHistory),
                            updates[start:start + 500],
                        )
                await s.commit()
        except Exception as _bulk_exc:
            logger.debug("update_ud_props_mq_scores_bulk: session error: %s", _bulk_exc)

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

    async def save_prop_line_observations_batch(
        self,
        provider: str,
        props: "list[object]",
        *,
        snapshot_id: Optional[str] = None,
    ) -> int:
        """Append one normalized provider snapshot without mutating history."""
        if not props:
            return 0
        if len(props) > _PROP_HISTORY_BATCH_SIZE:
            observed = 0
            for start in range(0, len(props), _PROP_HISTORY_BATCH_SIZE):
                observed += await self.save_prop_line_observations_batch(
                    provider,
                    props[start:start + _PROP_HISTORY_BATCH_SIZE],
                    snapshot_id=snapshot_id,
                )
                await asyncio.sleep(0)
            return observed

        snapshot = str(snapshot_id or "").strip() or datetime.utcnow().strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        rows: list[PropLineObservation] = []
        seen: set[tuple] = set()
        for prop in props:
            player = str(getattr(prop, "player_name", "") or "").strip()
            sport = str(getattr(prop, "sport", "") or "").upper().strip()
            stat = str(getattr(prop, "stat_type", "") or "").strip()
            if not player or not sport or not stat:
                continue
            try:
                line = float(getattr(prop, "line_value", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if line < 0:
                continue
            external_id = str(getattr(prop, "external_id", "") or "").strip()
            game_id = str(
                getattr(prop, "event_id", "")
                or getattr(prop, "game_id", "")
                or ""
            ).strip()
            line_variant = str(
                getattr(prop, "line_variant", "") or ""
            ).strip().lower()
            identity = (
                str(provider or "").strip(),
                player,
                sport,
                stat,
                external_id,
                game_id,
                line_variant,
                line,
            )
            if identity in seen:
                continue
            seen.add(identity)
            observation_key = hashlib.sha256(
                "\x1f".join(str(value) for value in identity).encode("utf-8")
            ).hexdigest()
            fetched_at = getattr(prop, "fetched_at", None)
            if not isinstance(fetched_at, datetime):
                fetched_at = datetime.utcnow()
            rows.append(
                PropLineObservation(
                    snapshot_id=snapshot,
                    observation_key=observation_key,
                    provider=str(provider or "").strip() or "Unknown",
                        product=PLAYER_PROPS_PRODUCT,
                    sport=sport,
                    player_name=player,
                    team=str(getattr(prop, "team", "") or ""),
                    stat_type=stat,
                    line_value=line,
                    game_time=getattr(prop, "game_time", None),
                    provider_status=(
                        getattr(prop, "provider_status", None)
                        or getattr(prop, "game_status", None)
                        or None
                    ),
                    external_id=external_id,
                    game_id=game_id,
                    line_variant=line_variant,
                    fetched_at=fetched_at,
                )
            )
        if not rows:
            return 0
        async with self.session() as s:
            s.add_all(rows)
            await s.commit()
        return len(rows)

    async def get_prop_line_observations(
        self,
        provider: str,
        *,
        since: Optional[datetime] = None,
        limit: int = 500,
    ) -> "list[PropLineObservation]":
        """Return immutable normalized observations newest first."""
        filters = [PropLineObservation.provider == provider]
        if since is not None:
            filters.append(PropLineObservation.fetched_at >= since)
        async with self.session() as s:
            result = await s.execute(
                select(PropLineObservation)
                .where(*filters)
                .order_by(
                    PropLineObservation.fetched_at.desc(),
                    PropLineObservation.id.desc(),
                )
                .limit(max(1, int(limit)))
            )
            return list(result.scalars().all())

    async def count_prop_line_observations(
        self, provider: Optional[str] = None
    ) -> int:
        async with self.session() as s:
            query = select(func.count(PropLineObservation.id))
            if provider:
                query = query.where(PropLineObservation.provider == provider)
            result = await s.execute(query)
            return int(result.scalar_one() or 0)

    async def upsert_prop_line_history_batch(
        self,
        provider: str,
        props: "list[object]",
        *,
        append_snapshot: bool = False,
        refresh_metadata: bool = False,
    ) -> int:
        """
        Persist a normalized provider batch in the shared prop-history table.

        This lets authorized PrizePicks observations share lifecycle/history
        storage with Underdog without adding a second database or pipeline.

        ``append_snapshot`` is used by a provider's monitoring ingestion path
        when every observation must remain historically available.  The
        default retains the existing current-state upsert behavior for
        corroboration and metadata refresh callers. ``refresh_metadata`` makes
        the current-row metadata mirror the supplied object exactly, including
        clearing a recommendation when the latest evaluation is non-directional.
        """
        if not props:
            return 0
        if len(props) > _PROP_HISTORY_BATCH_SIZE:
            # A full Underdog feed can contain thousands of rows. Do not hold
            # the shared SQLite writer gate across the complete feed: each
            # chunk gets its own read/write transaction and the event loop can
            # service independent grading, history, and command work between
            # commits. Recursive calls intentionally preserve the existing
            # identity/upsert semantics for every row.
            upserted = 0
            for start in range(0, len(props), _PROP_HISTORY_BATCH_SIZE):
                upserted += await self.upsert_prop_line_history_batch(
                    provider,
                    props[start:start + _PROP_HISTORY_BATCH_SIZE],
                    append_snapshot=append_snapshot,
                    refresh_metadata=refresh_metadata,
                )
                await asyncio.sleep(0)
            return upserted

        def _identity(
            player: str,
            sport: str,
            stat: str,
            event_id: str,
            external_id: str,
        ) -> tuple[str, str, str, str, str]:
            # PrizePicks source IDs identify separate active projections,
            # including alternate lines for the same event.  Keep the legacy
            # Underdog identity unchanged because its event/source lifecycle
            # is maintained by the provider-specific snapshot bridge.
            # A provider source ID is the stable identity through line
            # movement.  If it is unavailable, event identity is the safest
            # fallback; a later line update can still refresh that row.
            source_key = external_id or event_id
            return (player, sport, stat, event_id, source_key)

        keys = {
            _identity(
                getattr(prop, "player_name", "") or "",
                str(getattr(prop, "sport", "") or "").upper().strip(),
                getattr(prop, "stat_type", "") or "",
                (
                    getattr(prop, "event_id", "")
                    or getattr(prop, "game_id", "")
                    or ""
                ),
                getattr(prop, "external_id", "") or "",
            )
            for prop in props
        }
        base_keys = {(player, sport, stat) for player, sport, stat, _, _ in keys}
        async with self.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.provider == provider,
                    tuple_(
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                    ).in_(list(base_keys)),
                )
                .order_by(desc(PropLineHistory.fetched_at))
            )
            latest: dict[tuple[str, str, str, str, str], PropLineHistory] = {}
            for row in result.scalars().all():
                key = _identity(
                    row.player_name,
                    str(row.sport or "").upper().strip(),
                    row.stat_type,
                    row.game_id or "",
                    row.external_id or "",
                )
                latest.setdefault(key, row)

            for prop in props:
                key = _identity(
                    getattr(prop, "player_name", "") or "",
                    str(getattr(prop, "sport", "") or "").upper().strip(),
                    getattr(prop, "stat_type", "") or "",
                    (
                        getattr(prop, "event_id", "")
                        or getattr(prop, "game_id", "")
                        or ""
                    ),
                    getattr(prop, "external_id", "") or "",
                )
                line_value = float(getattr(prop, "line_value", 0.0) or 0.0)
                observed_at = (
                    getattr(prop, "fetched_at", None)
                    or getattr(prop, "source_timestamp", None)
                    or datetime.utcnow()
                )
                provider_status = getattr(prop, "provider_status", None) or None
                row = latest.get(key)
                if row is None:
                    row = PropLineHistory(
                        provider=provider,
                        sport=key[1],
                        player_name=key[0],
                        team=getattr(prop, "team", "") or "",
                        stat_type=key[2],
                        line_value=line_value,
                        game_time=getattr(prop, "game_time", None),
                        provider_status=provider_status,
                        external_id=getattr(prop, "external_id", "") or "",
                        line_variant=str(
                            getattr(prop, "line_variant", "") or ""
                        ).lower(),
                        game_id=(
                            getattr(prop, "event_id", "")
                            or getattr(prop, "game_id", "")
                            or ""
                        ),
                        fetched_at=observed_at,
                        first_seen=observed_at,
                        last_seen=observed_at,
                        change_count=0,
                        opening_line=line_value,
                        score_tier=None,
                        bet_recommendation=getattr(prop, "bet_recommendation", None),
                        bet_confidence=None,
                        market_quality_score=None,
                        score_total=None,
                    )
                    s.add(row)
                    latest[key] = row
                    continue

                old_line = float(row.line_value or 0.0)
                changed = abs(old_line - line_value) >= 0.01
                if append_snapshot:
                    row = PropLineHistory(
                        provider=provider,
                        sport=key[1],
                        player_name=key[0],
                        team=getattr(prop, "team", "") or "",
                        stat_type=key[2],
                        line_value=line_value,
                        game_time=getattr(prop, "game_time", None),
                        provider_status=provider_status,
                        external_id=getattr(prop, "external_id", "") or "",
                        line_variant=str(
                            getattr(prop, "line_variant", "") or ""
                        ).lower(),
                        game_id=(
                            getattr(prop, "event_id", "")
                            or getattr(prop, "game_id", "")
                            or ""
                        ),
                        fetched_at=observed_at,
                        first_seen=(
                            getattr(latest[key], "first_seen", None)
                            or observed_at
                        ),
                        last_seen=observed_at,
                        change_count=(
                            (getattr(latest[key], "change_count", 0) or 0)
                            + (1 if changed else 0)
                        ),
                        prev_line=old_line if changed else getattr(
                            latest[key], "prev_line", None
                        ),
                        removed=False,
                        lifecycle_state=(
                            getattr(latest[key], "lifecycle_state", None)
                            or "DISCOVERED"
                        ),
                        first_alert_sent_at=getattr(
                            latest[key], "first_alert_sent_at", None
                        ),
                        opening_line=(
                            getattr(latest[key], "opening_line", None)
                            if getattr(latest[key], "opening_line", None) is not None
                            else old_line
                        ),
                        score_tier=None,
                        bet_recommendation=getattr(prop, "bet_recommendation", None),
                        bet_confidence=None,
                        market_quality_score=None,
                        score_total=None,
                    )
                    s.add(row)
                    latest[key] = row
                    continue
                if changed:
                    row.prev_line = old_line
                    row.change_count = (row.change_count or 0) + 1
                row.line_value = line_value
                row.team = getattr(prop, "team", "") or row.team
                row.game_time = getattr(prop, "game_time", None) or row.game_time
                row.provider_status = (
                    provider_status
                    if refresh_metadata
                    else provider_status or row.provider_status
                )
                row.external_id = getattr(prop, "external_id", "") or row.external_id
                row.line_variant = str(
                    getattr(prop, "line_variant", "") or row.line_variant or ""
                ).lower()
                row.game_id = (
                    getattr(prop, "event_id", "")
                    or getattr(prop, "game_id", "")
                    or row.game_id
                )
                row.fetched_at = observed_at
                row.first_seen = row.first_seen or observed_at
                row.last_seen = observed_at
                row.removed = False
                row.lifecycle_state = "DISCOVERED"
                if refresh_metadata:
                    row.score_tier = None
                    row.bet_recommendation = getattr(
                        prop, "bet_recommendation", None
                    )
                    row.bet_confidence = None
                    row.market_quality_score = None
                    row.score_total = None
                else:
                    row.score_tier = None
                    row.bet_recommendation = (
                        getattr(prop, "bet_recommendation", None)
                        or row.bet_recommendation
                    )
                    row.bet_confidence = None
                    row.market_quality_score = None
                    row.score_total = None

            await s.commit()
        return len(props)

    async def get_prop_line_history(
        self,
        provider:    str,
        player_name: str,
        sport:       str,
        stat_type:   str,
        limit:       int = 30,
        *,
        external_id: str = "",
        game_id: str = "",
    ) -> "list[PropLineHistory]":
        """Return recent snapshots for one source prop, newest first.

        When a provider source ID or event ID is supplied, only that source's
        snapshots are returned.  Calls without identity arguments retain the
        legacy player/stat history behavior used by older callers.
        """
        source_id = str(external_id or "").strip()
        event_id = str(game_id or "").strip()
        async with self.session() as s:
            filters = [
                PropLineHistory.provider    == provider,
                PropLineHistory.player_name == player_name,
                PropLineHistory.sport       == sport,
                PropLineHistory.stat_type   == stat_type,
            ]
            if source_id:
                filters.append(PropLineHistory.external_id == source_id)
            elif event_id:
                filters.append(PropLineHistory.game_id == event_id)
            result = await s.execute(
                select(PropLineHistory)
                .where(*filters)
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def save_propline_catalog_audit(
        self,
        identities: Iterable[dict],
        *,
        observed_at: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Persist unmapped catalog identities and mapping transitions.

        The input is the observational output of
        ``audit_propline_discovery``.  Mapped identities that have never been
        tracked are ignored; mapped identities with an existing unmapped row
        resolve that row.  This keeps catalog history out of all player-prop
        tables and scoring/delivery queries.
        """
        timestamp = observed_at or datetime.utcnow()
        normalized: list[dict[str, object]] = []
        for identity in identities or ():
            if not isinstance(identity, dict):
                continue
            kind = str(identity.get("kind") or "").strip().lower()
            provider_key = str(identity.get("provider_key") or "").strip()
            if kind not in {"sport", "market"} or not provider_key:
                continue
            try:
                sample_count = max(0, int(identity.get("sample_count") or 0))
            except (TypeError, ValueError):
                sample_count = 0
            mapped = bool(
                identity.get("mapped")
                or str(identity.get("status") or "").strip().lower() == "mapped"
            )
            canonical_context = identity.get("canonical_context")
            if not isinstance(canonical_context, dict):
                canonical_context = {}
            canonical_sport = str(
                identity.get("canonical_sport")
                or canonical_context.get("sport")
                or ""
            ).strip()
            canonical_market = str(
                identity.get("canonical_market")
                or canonical_context.get("market")
                or ""
            ).strip()
            provider_label = str(identity.get("provider_label") or "").strip()
            identity_key = json.dumps(
                [
                    kind,
                    provider_key,
                    canonical_sport,
                    canonical_market,
                    provider_label,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            normalized.append(
                {
                    "identity_key": identity_key,
                    "kind": kind,
                    "provider_key": provider_key,
                    "canonical_sport": canonical_sport,
                    "canonical_market": canonical_market,
                    "provider_label": provider_label,
                    "sample_count": sample_count,
                    "mapped": mapped,
                }
            )

        result_counts = {
            "observed": 0,
            "new": 0,
            "mapped": 0,
            "unmapped": 0,
            "unchanged": 0,
        }
        if not normalized:
            return result_counts

        async with self.session() as s:
            for item in normalized:
                result_counts["observed"] += 1
                existing_result = await s.execute(
                    select(PropLineCatalogAudit).where(
                        PropLineCatalogAudit.identity_key == item["identity_key"]
                    )
                )
                row = existing_result.scalar_one_or_none()
                is_mapped = bool(item["mapped"])
                if row is None:
                    if is_mapped:
                        # Known identities are not catalog drift.  Do not
                        # create a row merely because they were in the feed.
                        continue
                    s.add(
                        PropLineCatalogAudit(
                            identity_key=str(item["identity_key"]),
                            kind=str(item["kind"]),
                            provider_key=str(item["provider_key"]),
                            canonical_sport=str(item["canonical_sport"]),
                            canonical_market=str(item["canonical_market"]),
                            provider_label=str(item["provider_label"]),
                            sample_count=int(item["sample_count"]),
                            status="unmapped",
                            first_seen_at=timestamp,
                            last_seen_at=timestamp,
                            last_changed_at=timestamp,
                        )
                    )
                    result_counts["new"] += 1
                    result_counts["unmapped"] += 1
                    continue

                row.last_seen_at = timestamp
                row.sample_count = int(item["sample_count"])
                next_status = "mapped" if is_mapped else "unmapped"
                if row.status == next_status:
                    result_counts["unchanged"] += 1
                    continue
                row.status = next_status
                row.last_changed_at = timestamp
                if is_mapped:
                    row.resolved_at = timestamp
                    result_counts["mapped"] += 1
                else:
                    # A mapping can regress; keep the original first-seen
                    # timestamp but make the new unmapped state visible.
                    row.resolved_at = None
                    result_counts["unmapped"] += 1
            await s.commit()
        return result_counts

    async def get_propline_catalog_audit_history(
        self,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Return the latest catalog identity changes for operational reports."""
        safe_limit = max(1, min(int(limit), 100))
        async with self.session() as s:
            result = await s.execute(
                select(PropLineCatalogAudit)
                .order_by(
                    PropLineCatalogAudit.last_changed_at.desc(),
                    PropLineCatalogAudit.id.desc(),
                )
                .limit(safe_limit)
            )
            rows = list(result.scalars().all())
        return [
            {
                "kind": row.kind,
                "provider_key": row.provider_key,
                "canonical_sport": row.canonical_sport,
                "canonical_market": row.canonical_market,
                "canonical_context": {
                    "sport": row.canonical_sport,
                    "market": row.canonical_market,
                },
                "provider_label": row.provider_label,
                "sample_count": int(row.sample_count or 0),
                "status": row.status,
                "first_seen_at": (
                    row.first_seen_at.isoformat()
                    if row.first_seen_at
                    else None
                ),
                "last_seen_at": (
                    row.last_seen_at.isoformat()
                    if row.last_seen_at
                    else None
                ),
                "last_changed_at": (
                    row.last_changed_at.isoformat()
                    if row.last_changed_at
                    else None
                ),
                "resolved_at": (
                    row.resolved_at.isoformat() if row.resolved_at else None
                ),
            }
            for row in rows
        ]

    async def get_propline_line_movement_history(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        limit: int = 30,
        *,
        provider: str = "PrizePicks",
        external_id: str = "",
        game_id: str = "",
    ) -> "list[PropLineHistory]":
        """Return chronological observations for one provider/source identity.

        A source ID or event ID is required so separate provider events or
        alternate projections cannot be combined into one movement timeline.
        The existing append-only ``PropLineHistory`` rows remain the sole data
        source; this method only changes selection order and applies the
        provider boundary.
        """
        source_id = str(external_id or "").strip()
        event_id = str(game_id or "").strip()
        if not source_id and not event_id:
            return []
        safe_limit = max(1, int(limit))
        async with self.session() as s:
            filters = [
                PropLineHistory.provider == provider,
                PropLineHistory.player_name == player_name,
                PropLineHistory.sport == sport,
                PropLineHistory.stat_type == stat_type,
            ]
            if source_id:
                filters.append(PropLineHistory.external_id == source_id)
            else:
                filters.append(PropLineHistory.game_id == event_id)
            result = await s.execute(
                select(PropLineHistory)
                .where(*filters)
                .order_by(PropLineHistory.fetched_at.asc(), PropLineHistory.id.asc())
                .limit(safe_limit)
            )
            return list(result.scalars().all())

    async def get_propline_opening_line(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        provider: str = "PrizePicks",
        external_id: str = "",
        game_id: str = "",
    ) -> Optional[PropLineHistory]:
        """Return the first persisted provider observation for one source."""
        rows = await self.get_propline_line_movement_history(
            player_name,
            sport,
            stat_type,
            limit=1,
            provider=provider,
            external_id=external_id,
            game_id=game_id,
        )
        return rows[0] if rows else None

    async def get_propline_terminal_line(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        provider: str = "PrizePicks",
        external_id: str = "",
        game_id: str = "",
    ) -> Optional[PropLineHistory]:
        """Return the latest persisted provider observation for one source."""
        source_id = str(external_id or "").strip()
        event_id = str(game_id or "").strip()
        if not source_id and not event_id:
            return None
        async with self.session() as s:
            filters = [
                PropLineHistory.provider == provider,
                PropLineHistory.player_name == player_name,
                PropLineHistory.sport == sport,
                PropLineHistory.stat_type == stat_type,
            ]
            if source_id:
                filters.append(PropLineHistory.external_id == source_id)
            else:
                filters.append(PropLineHistory.game_id == event_id)
            result = await s.execute(
                select(PropLineHistory)
                .where(*filters)
                .order_by(
                    PropLineHistory.fetched_at.desc(),
                    PropLineHistory.id.desc(),
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_latest_props_for_provider(
        self,
        provider: str,
        since_hours: int = 6,
        limit: Optional[int] = None,
        directional_first: bool = False,
    ) -> "list[PropLineHistory]":
        """Return the latest persistent snapshot for each provider prop.

        This is tracking data rather than delivery history, so it is not
        filtered by alert state, score, recommendation, or sport.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        now = datetime.utcnow()
        async with self.session() as s:
            if limit is not None:
                raw_limit = max(limit * 10, 200)
                result = await s.execute(
                    select(PropLineHistory)
                    .where(
                        PropLineHistory.provider == provider,
                        PropLineHistory.fetched_at >= cutoff,
                    )
                    .order_by(desc(PropLineHistory.id))
                    .limit(raw_limit)
                )
                raw_rows = list(result.scalars().all())
                latest_by_source: dict[
                    tuple[str, str, str, str], PropLineHistory
                ] = {}
                for row in raw_rows:
                    source_id = str(row.external_id or row.game_id or "")
                    identity = (
                        row.player_name,
                        row.sport,
                        row.stat_type,
                        source_id,
                    )
                    latest_by_source.setdefault(identity, row)
                rows = [
                    row
                    for row in latest_by_source.values()
                    if row.removed is not True
                ]
                rows.sort(
                    key=lambda row: (
                        (
                            0
                            if directional_first
                            and row.bet_recommendation in ("OVER", "UNDER")
                            else 1
                        ),
                        -(row.id or 0),
                    )
                )
                return rows[:limit]

            source_identity = case(
                (
                    PropLineHistory.external_id.isnot(None)
                    & (PropLineHistory.external_id != ""),
                    PropLineHistory.external_id,
                ),
                else_=PropLineHistory.game_id,
            )
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
                    source_identity,
                )
                .scalar_subquery()
            )
            query = (
                select(PropLineHistory)
                .where(PropLineHistory.id.in_(subq))
                .order_by(
                    *(
                        [
                            case(
                                (
                                    PropLineHistory.bet_recommendation.in_(
                                        ("OVER", "UNDER")
                                    ),
                                    0,
                                ),
                                else_=1,
                            )
                        ]
                        if directional_first
                        else []
                    ),
                    desc(PropLineHistory.fetched_at),
                )
            )
            result = await s.execute(query)
            return list(result.scalars().all())

    async def get_recent_tracked_props(
        self, provider: str = "Underdog", since_hours: int = 1, limit: int = 100
    ) -> "list[PropLineHistory]":
        """Return latest-per-prop persistent tracking rows across all sports."""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            source_identity = case(
                (
                    PropLineHistory.external_id.isnot(None)
                    & (PropLineHistory.external_id != ""),
                    PropLineHistory.external_id,
                ),
                else_=PropLineHistory.game_id,
            )
            subq = (
                select(func.max(PropLineHistory.id))
                .where(
                    PropLineHistory.provider == provider,
                    PropLineHistory.fetched_at >= cutoff,
                )
                .group_by(
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                    source_identity,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(PropLineHistory)
                .where(PropLineHistory.id.in_(subq))
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(limit)
            )
            return [
                row for row in result.scalars().all()
                if _is_supported_tracking_sport(row.sport)
            ]

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
            "ALTER TABLE prop_line_history ADD COLUMN provider_status TEXT",
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
        *,
        external_id: str = "",
        game_id: str = "",
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
            filters = [
                PropLineHistory.provider    == provider,
                PropLineHistory.player_name == player_name,
                PropLineHistory.sport       == sport,
                PropLineHistory.stat_type   == stat_type,
            ]
            if external_id:
                filters.append(PropLineHistory.external_id == str(external_id))
            elif game_id:
                filters.append(PropLineHistory.game_id == str(game_id))
            result = await s.execute(
                select(PropLineHistory)
                .where(*filters)
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

    async def update_prop_lifecycle_states_bulk(
        self,
        updates: "list[dict]",
    ) -> int:
        """Apply lifecycle transitions in one transaction."""
        if not updates:
            return 0
        from sqlalchemy import update as sa_update

        applied = 0
        async with self.session() as s:
            for item in updates:
                provider = str(item.get("provider") or "")
                player_name = str(item.get("player_name") or "")
                sport = str(item.get("sport") or "")
                stat_type = str(item.get("stat_type") or "")
                new_state = str(item.get("new_state") or "")
                if not provider or not player_name or not sport or not stat_type or not new_state:
                    continue
                filters = [
                    PropLineHistory.provider == provider,
                    PropLineHistory.player_name == player_name,
                    PropLineHistory.sport == sport,
                    PropLineHistory.stat_type == stat_type,
                ]
                external_id = str(item.get("external_id") or "")
                game_id = str(item.get("game_id") or "")
                if external_id:
                    filters.append(PropLineHistory.external_id == external_id)
                elif game_id:
                    filters.append(PropLineHistory.game_id == game_id)
                result = await s.execute(
                    select(PropLineHistory)
                    .where(*filters)
                    .order_by(desc(PropLineHistory.fetched_at))
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    continue
                values = {"lifecycle_state": new_state}
                first_alert = item.get("first_alert_sent_at")
                if first_alert is not None and row.first_alert_sent_at is None:
                    values["first_alert_sent_at"] = first_alert
                await s.execute(
                    sa_update(PropLineHistory)
                    .where(PropLineHistory.id == row.id)
                    .values(**values)
                )
                applied += 1
            await s.commit()
        return applied

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
        limit: int | None = None,
        since_hours: int = 48,
        since_at: datetime | None = None,
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

        cutoff = (
            since_at
            if since_at is not None
            else datetime.utcnow() - timedelta(hours=since_hours)
        )

        # Load recent Underdog snapshots (all, including removed — we track removals)
        async with self.session() as s:
            snapshot_query = (
                select(UnderdogSnapshotRecord)
                .where(UnderdogSnapshotRecord.fetched_at >= cutoff)
                .order_by(UnderdogSnapshotRecord.fetched_at.asc())
            )
            if limit is not None and int(limit) > 0:
                snapshot_query = snapshot_query.limit(int(limit))
            result = await s.execute(snapshot_query)
            snaps = list(result.scalars().all())

        if not snaps:
            return 0

        # Group the current scan before looking up history.  The live path
        # supplies an explicit boundary and therefore knows exactly which
        # provider identities can be relevant.  Do not run the maintenance
        # path's all-history GROUP BY against a growing append-only table on
        # every full feed.
        snap_by_key: dict[tuple, list] = {}
        for snap in snaps:
            key = (
                snap.player_name or "",
                str(snap.sport or "").upper().strip(),
                snap.stat_type or "",
                getattr(snap, "external_id", None)
                or getattr(snap, "game_id", None)
                or "",
            )
            snap_by_key.setdefault(key, []).append(snap)

        # Load latest PropLineHistory row per (player_name, sport, stat_type) for Underdog
        async with self.session() as s:
            history_filters = [PropLineHistory.provider == "Underdog"]
            if since_at is not None:
                # The canonical feed writer has already persisted this scan's
                # current rows before the bridge runs. Read only that
                # freshness slice instead of expanding thousands of source
                # identity predicates (many legacy Underdog rows have no
                # external/game ID). Maintenance callers keep the existing
                # all-history behavior.
                history_filters.append(PropLineHistory.fetched_at >= cutoff)
                history_query = select(PropLineHistory).where(*history_filters)
            else:
                source_identity = case(
                    (
                        and_(
                            PropLineHistory.external_id.isnot(None),
                            PropLineHistory.external_id != "",
                        ),
                        PropLineHistory.external_id,
                    ),
                    else_=PropLineHistory.game_id,
                )
                # Subquery: max id per provider source identity
                subq = (
                    select(func.max(PropLineHistory.id))
                    .where(PropLineHistory.provider == "Underdog")
                    .group_by(
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                        source_identity,
                    )
                    .scalar_subquery()
                )
                history_query = select(PropLineHistory).where(
                    PropLineHistory.id.in_(subq)
                )
            result = await s.execute(
                history_query
            )
            latest_rows = {
                (
                    r.player_name,
                    r.sport,
                    r.stat_type,
                    r.external_id or r.game_id or "",
                ): r
                for r in result.scalars().all()
            }

        # Track changes to accumulate within this batch
        # ── Build insert/update batches (no DB I/O yet) ──────────────────────
        new_rows:    list[PropLineHistory]          = []
        update_jobs: list[tuple[int, dict]]         = []   # (existing_id, update_vals)

        for key, key_snaps in snap_by_key.items():
            player_name, sport, stat_type, source_identity = key
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
                    provider_status = getattr(latest_snap, "provider_status", None),
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
                    row.score_total  = getattr(latest_snap, "score_total", None)
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

                # The live monitoring bridge receives a complete feed every
                # minute, but a stable prop does not need a write on every
                # observation.  The snapshot ledger is the authoritative
                # freshness record; PropLineHistory only needs a write here
                # when lifecycle, line, or diagnostic state changes.  Keep
                # the legacy full-history behavior for maintenance callers
                # that do not provide an explicit scan boundary.
                if since_at is not None:
                    _state_changed = (
                        line_changed
                        or bool(getattr(existing, "removed", False)) != is_removed
                        or bool(getattr(latest_snap, "provider_status", None))
                        and getattr(existing, "provider_status", None)
                        != getattr(latest_snap, "provider_status", None)
                    )
                    if not _state_changed:
                        continue

                uvals: dict = {
                    "line_value": new_line,
                    "fetched_at": now_ts,
                    "last_seen":  now_ts,
                    "change_count": change_delta,
                    "removed":    is_removed,
                }
                if latest_snap.game_time is not None:
                    uvals["game_time"] = latest_snap.game_time
                _snap_status = getattr(latest_snap, "provider_status", None)
                if _snap_status:
                    uvals["provider_status"] = _snap_status
                _snap_external_id = getattr(latest_snap, "external_id", None)
                if _snap_external_id:
                    uvals["external_id"] = _snap_external_id
                if latest_snap.game_id:
                    uvals["game_id"] = latest_snap.game_id
                if line_changed:
                    uvals["prev_line"] = old_line
                # Always propagate score_tier when the snapshot has a value
                if snap_tier is not None:
                    uvals["score_tier"] = snap_tier
                _snap_score_total = getattr(latest_snap, "score_total", None)
                if _snap_score_total is not None:
                    uvals["score_total"] = _snap_score_total
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
                # Core executemany avoids one ORM unit-of-work entry and flush
                # per row when the first full feed is bridged.  A large feed can
                # contain thousands of new rows; keeping bounded chunks preserves
                # the SQLite bind-parameter limit without holding the writer gate
                # for the ORM bookkeeping of every object.
                for start in range(0, len(new_rows), 500):
                    batch = new_rows[start:start + 500]
                    values = [
                        {
                            "provider": row.provider,
                            "sport": row.sport,
                            "player_name": row.player_name,
                            "team": row.team,
                            "stat_type": row.stat_type,
                            "line_value": row.line_value,
                            "game_time": row.game_time,
                            "provider_status": row.provider_status,
                            "external_id": row.external_id,
                            "game_id": row.game_id,
                            "fetched_at": row.fetched_at,
                            "first_seen": getattr(row, "first_seen", None),
                            "last_seen": getattr(row, "last_seen", None),
                            "change_count": getattr(row, "change_count", 0),
                            "prev_line": getattr(row, "prev_line", None),
                            "removed": getattr(row, "removed", False),
                            "score_tier": getattr(row, "score_tier", None),
                            "score_total": getattr(row, "score_total", None),
                            "bet_recommendation": getattr(
                                row, "bet_recommendation", None
                            ),
                            "bet_confidence": getattr(
                                row, "bet_confidence", None
                            ),
                        }
                        for row in batch
                    ]
                    await s.execute(sa_insert(PropLineHistory), values)
                await s.commit()
            upserted += len(new_rows)

        # ── Execute batched UPDATEs in a single session ───────────────────────
        if update_jobs:
            async with self.session() as s:
                # SQLAlchemy's ORM bulk-update-by-primary-key path turns this
                # into bounded executemany calls instead of one awaited
                # statement per prop.  Keep batches bounded so a large live
                # feed does not monopolize the shared SQLite writer gate.
                for start in range(0, len(update_jobs), 500):
                    batch = [
                        {"id": row_id, **uvals}
                        for row_id, uvals in update_jobs[start:start + 500]
                    ]
                    await s.execute(sa_update(PropLineHistory), batch)
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
        provider_status: str         = "",
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

        # Find most-recent existing row for this provider-owned source identity.
        # Legacy imports without either identity continue to use the old
        # player/stat grouping so their lifecycle behavior remains compatible.
        source_id = str(external_id or "").strip()
        event_id = str(game_id or "").strip()
        identity_filters = [
            PropLineHistory.provider    == provider,
            PropLineHistory.player_name == player_name,
            PropLineHistory.sport       == sport,
            PropLineHistory.stat_type   == stat_type,
        ]
        if source_id:
            identity_filters.append(PropLineHistory.external_id == source_id)
        elif event_id:
            identity_filters.append(PropLineHistory.game_id == event_id)
        else:
            identity_filters.extend((
                PropLineHistory.external_id == "",
                PropLineHistory.game_id == "",
            ))
        async with self.session() as s:
            result = await s.execute(
                select(PropLineHistory)
                .where(*identity_filters)
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
                provider_status = provider_status or None,
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
        if game_time is not None:
            update_vals["game_time"] = game_time
        if provider_status:
            update_vals["provider_status"] = provider_status
        if external_id:
            update_vals["external_id"] = external_id
        if game_id:
            update_vals["game_id"] = game_id
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

    async def mark_clv_seeds_computed_bulk(
        self,
        updates: "dict[int, float]",
    ) -> int:
        """Mark computed seeds and assign their CLV values in bulk batches."""
        if not updates:
            return 0
        from sqlalchemy import case as sa_case

        updated = 0
        items = list(updates.items())
        for start in range(0, len(items), _CLV_BULK_BATCH_SIZE):
            batch = dict(items[start:start + _CLV_BULK_BATCH_SIZE])
            async with self.session() as s:
                await s.execute(
                    update(AlertCLVSeed)
                    .where(AlertCLVSeed.id.in_(batch))
                    .values(
                        clv_pct=sa_case(
                            batch,
                            value=AlertCLVSeed.id,
                        ),
                        clv_computed=True,
                    )
                )
                await s.commit()
            updated += len(batch)
        return updated

    async def mark_clv_seeds_expired_bulk(
        self,
        seed_ids: "list[int]",
    ) -> int:
        """Expire unresolved seeds in bounded bulk updates."""
        if not seed_ids:
            return 0
        updated = 0
        for start in range(0, len(seed_ids), _CLV_BULK_BATCH_SIZE):
            batch = seed_ids[start:start + _CLV_BULK_BATCH_SIZE]
            async with self.session() as s:
                result = await s.execute(
                    update(AlertCLVSeed)
                    .where(AlertCLVSeed.id.in_(batch))
                    .values(clv_pct=None, clv_computed=True)
                )
                await s.commit()
            updated += int(result.rowcount or 0)
        return updated

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
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.session() as s:
            result = await s.execute(
                select(EVRecord)
                .where(
                    EVRecord.alert_sent == True,              # noqa: E712
                )
                .limit(max(limit * 10, limit))
            )
            candidates = list(result.scalars().all())
            candidate_ids = [row.id for row in candidates]
            seeded = set()
            if candidate_ids:
                seeded_result = await s.execute(
                    select(AlertCLVSeed.source_id).where(
                        AlertCLVSeed.source_table == "ev_records",
                        AlertCLVSeed.source_id.in_(candidate_ids),
                    )
                )
                seeded = set(seeded_result.scalars().all())
            ev_rows = [row for row in candidates if row.id not in seeded][:limit]

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
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.alert_sent == True,    # noqa: E712
                )
                .limit(max(limit * 10, limit))
            )
            candidates = list(result.scalars().all())
            candidate_ids = [row.id for row in candidates]
            seeded = set()
            if candidate_ids:
                seeded_result = await s.execute(
                    select(AlertCLVSeed.source_id).where(
                        AlertCLVSeed.source_table == "underdog_snapshots",
                        AlertCLVSeed.source_id.in_(candidate_ids),
                    )
                )
                seeded = set(seeded_result.scalars().all())
            ud_rows = [row for row in candidates if row.id not in seeded][:limit]

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

    async def seed_clv_from_ud_confirmation(
        self,
        *,
        source_id: int,
        sport: str,
        stat_type: str,
        player_name: str,
        line: float,
        game_time: "Optional[datetime]",
        tier: str,
        avg_odds: int,
        alerted_at: "Optional[datetime]" = None,
    ) -> bool:
        """
        Create or update an AlertCLVSeed for an Underdog S/A alert that has
        OddsAPI market confirmation with real sportsbook odds.

        Unlike ``seed_clv_from_ud_snapshots`` (which sets bet_odds=None and is
        immediately expired by the harvest job), this method sets bet_odds to
        ``avg_odds`` from the OddsAPI confirmation so the seed survives until a
        closing line is available.

        Uses ON CONFLICT DO UPDATE so that if a seed was already created by the
        periodic seed job (bet_odds=NULL), it is upgraded with the real market
        odds — ensuring at most one CLV seed per alert.

        Returns True if the seed was inserted or upgraded.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        selection = f"{player_name} {stat_type} {line:g}"
        now = alerted_at or datetime.utcnow()

        row = dict(
            source_table     = "underdog_snapshots",
            source_id        = source_id,
            alert_type       = "UNDERDOG",
            sport            = sport or "",
            market_type      = stat_type or "",
            event            = f"{player_name} {sport}",
            selection        = selection,
            bet_odds         = avg_odds,
            counterpart_odds = None,
            tier             = tier or "",
            game_time        = game_time,
            alerted_at       = now,
            clv_pct          = None,
            clv_computed     = False,
        )

        async with self.session() as s:
            stmt = (
                sqlite_insert(AlertCLVSeed)
                .values([row])
                .on_conflict_do_update(
                    index_elements=["source_table", "source_id"],
                    # Only overwrite bet_odds when the existing row has none —
                    # never overwrite a seed that already has real odds or is computed.
                    set_={"bet_odds": avg_odds},
                    where=AlertCLVSeed.bet_odds.is_(None),
                )
            )
            result = await s.execute(stmt)
            await s.commit()
            return result.rowcount > 0

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
            "ALTER TABLE underdog_snapshots ADD COLUMN provider_status TEXT",
        ]
        async with self._engine.begin() as conn:
            for sql in new_cols:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass  # column already exists — safe to ignore

    async def save_underdog_snapshot(self, record: "UnderdogSnapshotRecord") -> "UnderdogSnapshotRecord":
        # Scores are diagnostic metadata, not delivery gates, but retaining
        # them is required for audit, CLV seeding, and historical reports.
        record.score_total = _bounded_score(record.score_total, maximum=100)
        record.score_tier = _normalized_score_tier(record.score_tier)
        record.score_stars = _bounded_score(record.score_stars, maximum=5)
        record.bet_confidence = _bounded_score(record.bet_confidence, maximum=100)
        async with self.session() as s:
            s.add(record)
            await s.commit()
            await s.refresh(record)
        return record

    async def save_underdog_snapshots_bulk(
        self,
        records: "list[UnderdogSnapshotRecord]",
        *,
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """Insert a list of UnderdogSnapshotRecords in a single transaction.

        Used by the cold-start path to avoid opening one SQLite write
        transaction per prop (which causes lock contention with concurrent
        background jobs).  A lock or transaction error propagates immediately
        so the owning scan can preserve the batch and report incomplete
        persistence instead of silently retrying.
        """
        if not records:
            return {"records_written": 0, "retry_count": 0}

        chunk_size = max(1, int(batch_size or _UNDERDOG_BULK_BATCH_SIZE))
        # A full live feed can contain several thousand observations. Keep
        # each transaction bounded so one ORM flush does not monopolize SQLite
        # or create a multi-gigabyte WAL burst that blocks evidence writers.
        # Aggregate the per-batch results so callers still receive an exact
        # durable count and retry total for the complete feed.
        if len(records) > chunk_size:
            records_written = 0
            retry_count = 0
            for start in range(0, len(records), chunk_size):
                result = await self.save_underdog_snapshots_bulk(
                    records[start: start + chunk_size],
                    batch_size=chunk_size,
                )
                records_written += int(result.get("records_written", 0))
                retry_count += int(result.get("retry_count", 0))
            return {
                "records_written": records_written,
                "retry_count": retry_count,
            }

        retry_count = 0
        for attempt in range(_UNDERDOG_BULK_MAX_RETRIES + 1):
            session = None
            session_invalidated = False
            stage = "pool_checkout"
            try:
                session = self.session()
                async with session as s:
                    try:
                        if hasattr(s, "execute"):
                            # Core executemany avoids constructing/flushing one
                            # ORM unit-of-work entry per observation. The
                            # transaction remains bounded by the outer chunk,
                            # but the full feed is still awaited before the
                            # monitoring cycle can release its slot.
                            values = [
                                {
                                    "external_id": str(record.external_id or ""),
                                    "player_name": str(record.player_name or ""),
                                    "team": str(record.team or ""),
                                    "sport": str(record.sport or ""),
                                    "stat_type": str(record.stat_type or ""),
                                    "line_value": float(record.line_value or 0.0),
                                    "game_id": str(record.game_id or ""),
                                    "game_time": record.game_time,
                                    "provider_status": record.provider_status,
                                    "line_moved": bool(record.line_moved),
                                    "prev_line": record.prev_line,
                                    "line_delta": record.line_delta,
                                    "removed": bool(record.removed),
                                    "alert_sent": bool(record.alert_sent),
                                     "score_total": _bounded_score(record.score_total, maximum=100),
                                     "score_tier": _normalized_score_tier(record.score_tier),
                                     "score_stars": _bounded_score(record.score_stars, maximum=5),
                                    "alert_outcome": record.alert_outcome,
                                    "validation_json": record.validation_json,
                                    "bet_recommendation": record.bet_recommendation,
                                     "bet_confidence": _bounded_score(record.bet_confidence, maximum=100),
                                    "bet_reason": record.bet_reason,
                                    "bet_evidence_json": record.bet_evidence_json,
                                    "fetched_at": record.fetched_at or datetime.utcnow(),
                                }
                                for record in records
                            ]
                            await s.execute(sa_insert(UnderdogSnapshotRecord), values)
                        else:
                            # Lightweight test doubles and older adapters keep
                            # the original ORM contract.
                            s.add_all(records)
                        stage = "transaction_begin"
                        # Flush separately so failures while acquiring the
                        # transaction/connection are distinguishable from a
                        # failure in the final commit.
                        await s.flush()
                        stage = "commit"
                        await s.commit()
                    except Exception as exc:
                        if _is_sqlite_cantopen_error(exc):
                            await s.invalidate()
                            session_invalidated = True
                        raise
                if retry_count:
                    logger.info(
                        "save_underdog_snapshots_bulk: persisted %d records after %d retries",
                        len(records), retry_count,
                    )
                return {
                    "records_written": len(records),
                    "retry_count": retry_count,
                }
            except Exception as exc:
                _log_sqlite_failure(
                    operation="save_underdog_snapshots_bulk",
                    stage=stage,
                    exc=exc,
                )
                if (
                    _is_sqlite_cantopen_error(exc)
                    and session is not None
                    and not session_invalidated
                ):
                    # A failure before the session context acquired a
                    # connection still needs to invalidate the session object
                    # before it is discarded. The next loop creates a new
                    # session and cannot reuse this failed connection.
                    await session.invalidate()
                if _is_sqlite_cantopen_error(exc):
                    if attempt >= _UNDERDOG_BULK_MAX_RETRIES:
                        logger.error(
                            "save_underdog_snapshots_bulk: exhausted CANTOPEN retries "
                            "(%d retries, %d records): %s",
                            _UNDERDOG_BULK_MAX_RETRIES,
                            len(records),
                            exc,
                        )
                        raise
                    retry_count += 1
                    delay = _UNDERDOG_CANTOPEN_RETRY_DELAYS[
                        min(
                            retry_count - 1,
                            len(_UNDERDOG_CANTOPEN_RETRY_DELAYS) - 1,
                        )
                    ]
                    logger.warning(
                        "save_underdog_snapshots_bulk: SQLite CANTOPEN; "
                        "fresh-session retry %d/%d in %.2fs (%d records)",
                        retry_count,
                        _UNDERDOG_BULK_MAX_RETRIES,
                        delay,
                        len(records),
                    )
                    await asyncio.sleep(delay)
                    continue
                if not _is_sqlite_lock_error(exc):
                    logger.error(
                        "save_underdog_snapshots_bulk: non-retryable failure "
                        "(%d records): %s",
                        len(records), exc,
                    )
                    raise
                if attempt >= _UNDERDOG_BULK_MAX_RETRIES:
                    logger.error(
                        "save_underdog_snapshots_bulk: exhausted %d retries "
                        "(%d records): %s",
                        _UNDERDOG_BULK_MAX_RETRIES, len(records), exc,
                    )
                    raise
                retry_count += 1
                _record_sqlite_backpressure(self._url, "write_retries")
                delay = _UNDERDOG_BULK_RETRY_DELAYS[
                    min(retry_count - 1, len(_UNDERDOG_BULK_RETRY_DELAYS) - 1)
                ]
                logger.warning(
                    "save_underdog_snapshots_bulk: SQLite lock; retry %d/%d "
                    "in %.2fs (%d records)",
                    retry_count, _UNDERDOG_BULK_MAX_RETRIES, delay, len(records),
                )
                await asyncio.sleep(delay)

    async def get_recent_underdog_snapshots(self, limit: int = 50) -> list["UnderdogSnapshotRecord"]:
        async with self.session() as s:
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_known_underdog_prop_keys(
        self,
        since_days: int = 60,
    ) -> "set[tuple[str, str]]":
        """
        Return every (player_name, stat_type) pair seen in underdog_snapshots
        within the last *since_days* days, including removed rows.

        Used at the start of each underdog_job cycle to detect genuinely
        new props on their very first appearance.  Including removed rows
        ensures a re-listed prop is NOT re-flagged as new.

        The *since_days* cutoff (default 60 days) keeps the result set bounded
        as the DB grows.  Props absent for longer than this window are treated
        as new on re-appearance — acceptable because Underdog rarely re-lists
        props that have been gone for two months.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_days * 24)
        async with self.session() as s:
            result = await s.execute(
                select(
                    UnderdogSnapshotRecord.player_name,
                    UnderdogSnapshotRecord.stat_type,
                )
                .distinct()
                .where(UnderdogSnapshotRecord.fetched_at >= cutoff)
            )
            return {(row[0], row[1]) for row in result.all()}

    async def get_first_alert_times_ud(
        self,
        since_hours: int = 24,
    ) -> "dict[tuple[str, str], datetime]":
        """
        Return the earliest alert timestamp for each (player_name, stat_type)
        pair that had an Underdog alert sent within the last *since_hours*.

        PropLineHistory is the normalized lifecycle source for this value.
        Reading it avoids scanning the much larger raw Underdog snapshot table
        during cold-start recovery.

        Used at startup to restore _MARKET_FIRST_ALERT state so the market
        availability window is accurate after a restart.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            result = await s.execute(
                select(
                    PropLineHistory.player_name,
                    PropLineHistory.stat_type,
                    func.min(PropLineHistory.first_alert_sent_at),
                )
                .where(
                    PropLineHistory.provider == "Underdog",
                    PropLineHistory.first_alert_sent_at.isnot(None),
                    PropLineHistory.first_alert_sent_at >= cutoff,
                )
                .group_by(
                    PropLineHistory.player_name,
                    PropLineHistory.stat_type,
                )
            )
            return {(row[0], row[1]): row[2] for row in result.all()}

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
        # The table is append-only and can be very large.  Restrict the read
        # to a recent primary-key window before applying the player/stat
        # filters; an unbounded ORDER BY over the full archive can monopolize
        # SQLite long enough to stall the live scan cadence.
        _HISTORY_LOOKBACK_ROWS = 500_000
        async with self.session() as s:
            _latest_id = (
                await s.execute(
                    select(UnderdogSnapshotRecord.id)
                    .order_by(desc(UnderdogSnapshotRecord.id))
                    .limit(1)
                )
            ).scalar()
            if _latest_id is None:
                return []
            result = await s.execute(
                select(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.id
                    >= max(1, int(_latest_id) - _HISTORY_LOOKBACK_ROWS),
                    UnderdogSnapshotRecord.player_name == player_name,
                    UnderdogSnapshotRecord.stat_type == stat_type,
                    UnderdogSnapshotRecord.removed == False,  # noqa: E712
                )
                .order_by(desc(UnderdogSnapshotRecord.fetched_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_ud_prop_history_bulk(
        self,
        props: "list[tuple[str, str]]",
        limit: int = 20,
    ) -> "dict[tuple[str, str], list[UnderdogSnapshotRecord]]":
        """Load recent Underdog histories for many player/stat pairs.

        FPR evaluates thousands of alternate lines, often sharing the same
        player/stat history.  Read each pair once in bounded tuple-IN chunks,
        retaining the same newest-first, non-removed semantics as
        ``get_ud_prop_history``.
        """
        pairs = list(dict.fromkeys(
            (str(player), str(stat)) for player, stat in props
            if player and stat
        ))
        histories = {pair: [] for pair in pairs}
        if not pairs:
            return histories

        # The snapshot table is append-only and each live Underdog scan writes
        # one contiguous batch.  The most recent 500,000 rows cover many
        # complete scans while keeping this read bounded even when the raw
        # history has grown to billions of rows.  FPR only needs the newest
        # ``limit`` records for each pair, so older rows cannot affect the
        # result when the bounded window contains enough history.
        _HISTORY_LOOKBACK_ROWS = 500_000
        async with self.session() as s:
            _latest_id = (
                await s.execute(
                    select(UnderdogSnapshotRecord.id)
                    .order_by(desc(UnderdogSnapshotRecord.id))
                    .limit(1)
                )
            ).scalar()
            if _latest_id is not None:
                result = await s.execute(
                    select(UnderdogSnapshotRecord)
                    .where(
                        UnderdogSnapshotRecord.id
                        >= max(1, int(_latest_id) - _HISTORY_LOOKBACK_ROWS),
                        UnderdogSnapshotRecord.removed == False,  # noqa: E712
                        UnderdogSnapshotRecord.player_name.in_(
                            {pair[0] for pair in pairs}
                        ),
                        UnderdogSnapshotRecord.stat_type.in_(
                            {pair[1] for pair in pairs}
                        ),
                    )
                )
                for record in result.scalars().all():
                    pair = (record.player_name, record.stat_type)
                    if pair in histories:
                        histories[pair].append(record)

        for records in histories.values():
            records.sort(
                key=lambda record: (
                    record.fetched_at or datetime.min,
                    record.id or 0,
                ),
                reverse=True,
            )
        return histories

    async def get_prop_line_history_bulk(
        self,
        props: "list[tuple[str, str, str, str]]",
        limit: int = 30,
    ) -> "dict[tuple[str, str, str, str], list[PropLineHistory]]":
        """Load recent normalized histories for mixed providers in one read.

        The provider is part of the lookup key so matching Underdog and
        PrizePicks props never share history accidentally. A bounded ID window
        keeps this safe for the append-only table while covering recent feed
        batches.
        """
        keys = list(dict.fromkeys(
            (
                str(provider or ""),
                str(player or ""),
                str(sport or "").upper(),
                str(stat or ""),
            )
            for provider, player, sport, stat in props
            if provider and player and sport and stat
        ))
        histories: dict[tuple[str, str, str, str], list[PropLineHistory]] = {
            key: [] for key in keys
        }
        if not keys:
            return histories

        _HISTORY_LOOKBACK_ROWS = 500_000
        async with self.session() as s:
            latest_id = (
                await s.execute(
                    select(PropLineHistory.id)
                    .order_by(desc(PropLineHistory.id))
                    .limit(1)
                )
            ).scalar()
            if latest_id is None:
                return histories
            result = await s.execute(
                select(PropLineHistory)
                .where(
                    PropLineHistory.id >= max(1, int(latest_id) - _HISTORY_LOOKBACK_ROWS),
                    tuple_(
                        PropLineHistory.provider,
                        PropLineHistory.player_name,
                        PropLineHistory.sport,
                        PropLineHistory.stat_type,
                    ).in_(keys),
                    PropLineHistory.removed.isnot(True),
                )
                .order_by(desc(PropLineHistory.fetched_at), desc(PropLineHistory.id))
            )
            for record in result.scalars().all():
                key = (
                    record.provider,
                    record.player_name,
                    (record.sport or "").upper(),
                    record.stat_type,
                )
                if key in histories and len(histories[key]) < max(1, int(limit)):
                    histories[key].append(record)
        return histories

    async def get_recently_alerted_prop_keys(
        self,
        within_seconds: int = 86400,
    ) -> "frozenset[tuple[str, str]]":
        """
        Single-query bulk alternative to has_recent_ud_alert for batch scanning.

        Returns a frozenset of (player_name, stat_type) pairs for which
        alert_sent=True was recorded within the last *within_seconds* seconds.

        Used by _stable_refresh_job to pre-load the dedup set once per cycle
        rather than issuing one query per prop, which would open tens of
        thousands of SQLite sessions for a 10k-prop batch.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(seconds=within_seconds)
        async with self.session() as s:
            result = await s.execute(
                select(
                    UnderdogSnapshotRecord.player_name,
                    UnderdogSnapshotRecord.stat_type,
                )
                .where(
                    UnderdogSnapshotRecord.alert_sent == True,   # noqa: E712
                    UnderdogSnapshotRecord.fetched_at >= cutoff,
                )
                .distinct()
            )
            return frozenset((r[0], r[1]) for r in result.all())

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

    async def mark_ud_snapshot_alert_sent(
        self,
        player_name: str,
        stat_type: str,
    ) -> None:
        """Set alert_sent=True on recent UnderdogSnapshotRecord rows for player+stat.

        Called after every 95+ priority-override broadcast_alert so that
        has_recent_ud_alert() returns True in subsequent scan cycles, preventing
        the standing-opportunity path from sending a cross-cycle duplicate.
        """
        from sqlalchemy import update as sa_update
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=25)  # 25 h to cover one full day + slack
        async with self.session() as s:
            await s.execute(
                sa_update(UnderdogSnapshotRecord)
                .where(
                    UnderdogSnapshotRecord.player_name == player_name,
                    UnderdogSnapshotRecord.stat_type   == stat_type,
                    UnderdogSnapshotRecord.fetched_at  >= cutoff,
                )
                .values(alert_sent=True)
            )
            await s.commit()

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

    @_retry_sqlite_write
    async def upsert_player_result(self, raw: object) -> None:
        """
        Insert or update a PlayerGameResult row.

        *raw* must have attributes matching ``providers.player_stats.RawGameResult``:
        player_name, sport, stat_type, game_date, actual_value, opponent, source.

        If a row already exists for (player_name, sport, stat_type, game_date)
        the actual_value and opponent are refreshed; no duplicate is created.
        """
        from engine.identity import display_player_name
        player_name  = display_player_name(getattr(raw, "player_name", None))
        sport        = raw.sport
        stat_type    = raw.stat_type.lower().strip()
        game_date    = raw.game_date
        actual_value = raw.actual_value
        opponent     = raw.opponent
        source       = raw.source
        game_id      = str(getattr(raw, "game_id", "") or "")

        async with self.session() as s:
            result = await s.execute(
                select(PlayerGameResult).where(
                    PlayerGameResult.player_name == player_name,
                    PlayerGameResult.sport       == sport,
                    PlayerGameResult.stat_type   == stat_type,
                    PlayerGameResult.game_date   == game_date,
                    PlayerGameResult.game_id     == game_id,
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
                    game_id      = game_id,
                ))
            else:
                record.actual_value = actual_value
                if opponent:
                    record.opponent = opponent
                record.fetched_at = datetime.utcnow()
            await s.commit()

    @_retry_sqlite_write
    async def upsert_player_results_bulk(self, records: "list[object]") -> int:
        """Insert or update player-game results in one short transaction.

        Player history collection can return several games for one target.
        Keeping those rows in a single small transaction avoids repeatedly
        contending for SQLite's single writer slot while still committing
        progress before the collector continues through more targets.
        """
        if not records:
            return 0

        from engine.identity import display_player_name
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        prepared: dict[tuple[str, str, str, str, str], dict] = {}
        fetched_at = datetime.utcnow()
        for raw in records:
            player_name = display_player_name(getattr(raw, "player_name", None))
            sport = str(getattr(raw, "sport", "") or "").strip().upper()
            stat_type = str(getattr(raw, "stat_type", "") or "").lower().strip()
            game_date = str(getattr(raw, "game_date", "") or "").strip()
            actual_value = getattr(raw, "actual_value", None)
            opponent = getattr(raw, "opponent", None)
            source = str(getattr(raw, "source", "api") or "api")
            game_id = str(getattr(raw, "game_id", "") or "")
            key = (player_name, sport, stat_type, game_date, game_id)
            prepared[key] = {
                "player_name": player_name,
                "sport": sport,
                "stat_type": stat_type,
                "game_date": game_date,
                "opponent": opponent,
                "actual_value": actual_value,
                "source": source,
                "game_id": game_id,
                "fetched_at": fetched_at,
            }

        if not prepared:
            return 0

        insert_stmt = sqlite_insert(PlayerGameResult).values(list(prepared.values()))
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[
                "player_name",
                "sport",
                "stat_type",
                "game_date",
                "game_id",
            ],
            set_={
                "actual_value": insert_stmt.excluded.actual_value,
                # Preserve an already-known opponent when a later provider
                # refresh omits it.
                "opponent": func.coalesce(
                    insert_stmt.excluded.opponent,
                    PlayerGameResult.opponent,
                ),
                "fetched_at": fetched_at,
            },
        )
        async with self.session() as s:
            await s.execute(stmt)
            await s.commit()
        return len(prepared)

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

    async def get_player_results_bulk(
        self,
        props: "list[tuple[str, str, str]]",
        limit: int = 30,
    ) -> "dict[tuple[str, str, str], list[PlayerGameResult]]":
        """Load game-result evidence for many FPR props in one read session."""
        keys = list(dict.fromkeys(
            (str(player), str(sport), str(stat).lower().strip())
            for player, sport, stat in props
            if player and sport and stat
        ))
        results_by_key = {key: [] for key in keys}
        if not keys:
            return results_by_key

        players = {key[0] for key in keys}
        sports = {key[1] for key in keys}
        stats = {key[2] for key in keys}
        async with self.session() as s:
            result = await s.execute(
                select(PlayerGameResult).where(
                    PlayerGameResult.player_name.in_(players),
                    PlayerGameResult.sport.in_(sports),
                    PlayerGameResult.stat_type.in_(stats),
                )
            )
            for record in result.scalars().all():
                key = (
                    record.player_name,
                    record.sport,
                    (record.stat_type or "").lower().strip(),
                )
                if key in results_by_key:
                    results_by_key[key].append(record)

        for records in results_by_key.values():
            records.sort(
                key=lambda record: record.game_date or "",
                reverse=True,
            )
            del records[max(1, int(limit)):]
        return results_by_key

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

    async def get_player_result_coverage_by_sport(
        self,
        sports: Optional[Iterable[str]] = None,
    ) -> dict[str, dict[str, int]]:
        """Return stored player-result counts split by sport and source.

        The source split lets reports distinguish real historical evidence
        from shadow-only imports such as weekly aggregate feeds.
        """
        q = select(
            PlayerGameResult.sport,
            PlayerGameResult.source,
            func.count(),
        ).group_by(
            PlayerGameResult.sport,
            PlayerGameResult.source,
        )
        if sports:
            normalized_sports = {
                str(sport).strip().upper()
                for sport in sports
                if str(sport).strip()
            }
            if normalized_sports:
                q = q.where(PlayerGameResult.sport.in_(normalized_sports))
        coverage: dict[str, dict[str, int]] = {}
        async with self.session() as s:
            result = await s.execute(q)
            for sport, source, count in result.all():
                sport_key = str(sport or "").strip().upper()
                source_key = str(source or "unknown").strip().lower()
                if sport_key:
                    coverage.setdefault(sport_key, {})[source_key] = int(count or 0)
        return coverage

    async def get_directional_opportunity_counts_by_sport(
        self,
        since_hours: int = 24,
        sports: Optional[Iterable[str]] = None,
    ) -> dict[str, int]:
        """Count recently logged OVER/UNDER opportunities by sport."""
        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(since_hours)))
        q = (
            select(
                PropOpportunityLog.sport,
                func.count(),
            )
            .where(
                PropOpportunityLog.detected_at >= cutoff,
                PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
            )
            .group_by(PropOpportunityLog.sport)
        )
        if sports:
            normalized_sports = {
                str(sport).strip().upper()
                for sport in sports
                if str(sport).strip()
            }
            if normalized_sports:
                q = q.where(PropOpportunityLog.sport.in_(normalized_sports))
        counts: dict[str, int] = {}
        async with self.session() as s:
            result = await s.execute(q)
            for sport, count in result.all():
                sport_key = str(sport or "").strip().upper()
                if sport_key:
                    counts[sport_key] = int(count or 0)
        return counts

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

    async def _migrate_prop_line_history_v4(self) -> None:
        """Add market_quality_score column to prop_line_history (idempotent)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_line_history ADD COLUMN market_quality_score INTEGER"
                ))
                logger.info("_migrate_prop_line_history_v4: added market_quality_score column")
            except Exception:
                pass  # already exists

    async def _migrate_prop_line_history_v5(self) -> None:
        """Add score_total (BQ) column to prop_line_history (idempotent)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_line_history ADD COLUMN score_total REAL"
                ))
                logger.info("_migrate_prop_line_history_v5: added score_total column")
            except Exception:
                pass  # already exists

    async def _migrate_prop_line_history_v6(self) -> None:
        """Preserve provider event state on current prop rows (idempotent)."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_line_history ADD COLUMN provider_status TEXT"
                ))
                logger.info(
                    "_migrate_prop_line_history_v6: added provider_status column"
                )
            except Exception:
                pass  # already exists

    async def _migrate_prop_line_history_v7(self) -> None:
        """Index the bounded current-day monitoring slice (idempotent)."""
        async with self._engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_prop_line_history_provider_fetched_at "
                "ON prop_line_history (provider, fetched_at)"
            ))

    async def _migrate_prop_line_history_v8(self) -> None:
        """Index provider-owned source identities used by the live bridge."""
        async with self._engine.begin() as conn:
            for statement in (
                "CREATE INDEX IF NOT EXISTS "
                "ix_prop_line_history_provider_external_id "
                "ON prop_line_history (provider, external_id)",
                "CREATE INDEX IF NOT EXISTS "
                "ix_prop_line_history_provider_game_id "
                "ON prop_line_history (provider, game_id)",
            ):
                await conn.execute(text(statement))

    async def _migrate_prop_line_variants_v1(self) -> None:
        """Retain provider-owned standard/goblin/demon line identity."""
        async with self._engine.begin() as conn:
            for statement in (
                "ALTER TABLE prop_line_history "
                "ADD COLUMN line_variant VARCHAR(16) NOT NULL DEFAULT ''",
                "ALTER TABLE prop_tracking_log "
                "ADD COLUMN line_variant VARCHAR(16) NOT NULL DEFAULT ''",
            ):
                try:
                    await conn.execute(text(statement))
                except Exception:
                    pass

    async def _migrate_player_props_product_v1(self) -> None:
        """Classify shared prop records as Player Props while retaining source provenance."""
        async with self._engine.begin() as conn:
            for statement in (
                "ALTER TABLE prop_line_history "
                "ADD COLUMN product VARCHAR(32) NOT NULL DEFAULT 'Player Props'",
                "ALTER TABLE prop_line_observations "
                "ADD COLUMN product VARCHAR(32) NOT NULL DEFAULT 'Player Props'",
                "ALTER TABLE prop_tracking_log "
                "ADD COLUMN product VARCHAR(32) NOT NULL DEFAULT 'Player Props'",
                "ALTER TABLE prop_opportunity_log "
                "ADD COLUMN product VARCHAR(32) NOT NULL DEFAULT 'Player Props'",
            ):
                try:
                    await conn.execute(text(statement))
                except Exception:
                    pass

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

    async def _migrate_prop_opportunity_log_v4(self) -> None:
        """Add Telegram actionable pick tracking columns to prop_opportunity_log (idempotent)."""
        async with self._engine.begin() as conn:
            for col_sql in [
                "ALTER TABLE prop_opportunity_log ADD COLUMN alert_sent BOOLEAN NOT NULL DEFAULT 0",
                "ALTER TABLE prop_opportunity_log ADD COLUMN alert_sent_at DATETIME",
            ]:
                try:
                    await conn.execute(text(col_sql))
                except Exception:
                    pass  # column already exists
        logger.info("_migrate_prop_opportunity_log_v4: Telegram pick tracking columns ensured")

    async def _migrate_prop_opportunity_log_v5(self) -> None:
        """Add downstream intelligence summaries (idempotent)."""
        async with self._engine.begin() as conn:
            for col_sql in [
                "ALTER TABLE prop_opportunity_log ADD COLUMN downstream_intelligence_json TEXT",
                "ALTER TABLE prop_opportunity_log ADD COLUMN evidence_completeness INTEGER",
                "ALTER TABLE prop_opportunity_log ADD COLUMN sharp_confidence INTEGER",
                "ALTER TABLE prop_opportunity_log ADD COLUMN sharp_confidence_components TEXT",
            ]:
                try:
                    await conn.execute(text(col_sql))
                except Exception:
                    pass
        logger.info("_migrate_prop_opportunity_log_v5: downstream intelligence columns ensured")

    async def _migrate_player_game_results_v1(self) -> None:
        """Add game identity and replace the legacy date-only uniqueness key."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE player_game_results ADD COLUMN game_id "
                    "VARCHAR(128) NOT NULL DEFAULT ''"
                ))
            except Exception:
                pass

            # Fresh databases already have the five-column constraint. For
            # SQLite databases created before game identity existed, rebuild
            # this table so same-day events can coexist safely.
            if not self._url.startswith("sqlite"):
                return
            try:
                indexes = await conn.execute(
                    text("PRAGMA index_list('player_game_results')")
                )
                unique_old = False
                for row in indexes.mappings().all():
                    if not row.get("unique"):
                        continue
                    index_name = str(row.get("name", "")).replace("'", "''")
                    info = await conn.execute(
                        text(f"PRAGMA index_info('{index_name}')")
                    )
                    columns = [item.get("name") for item in info.mappings().all()]
                    if columns == ["player_name", "sport", "stat_type", "game_date"]:
                        unique_old = True
                        break
                if not unique_old:
                    return

                await conn.execute(text(
                    "ALTER TABLE player_game_results "
                    "RENAME TO player_game_results_legacy"
                ))
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text(
                    "INSERT INTO player_game_results "
                    "(id, player_name, sport, stat_type, game_date, opponent, "
                    "actual_value, source, game_id, fetched_at) "
                    "SELECT id, player_name, sport, stat_type, game_date, opponent, "
                    "actual_value, source, game_id, "
                    "COALESCE(fetched_at, CURRENT_TIMESTAMP) "
                    "FROM player_game_results_legacy"
                ))
                await conn.execute(text("DROP TABLE player_game_results_legacy"))
            except Exception as exc:
                logger.warning(
                    "_migrate_player_game_results_v1: preserving legacy schema: %s",
                    exc,
                )

    async def _migrate_prop_opportunity_log_v6(self) -> None:
        """Add provider event identity to opportunity rows."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_opportunity_log ADD COLUMN "
                    "game_id VARCHAR(128) NOT NULL DEFAULT ''"
                ))
            except Exception:
                pass

    async def _migrate_prop_opportunity_log_v7(self) -> None:
        """Add isolated, auditable AI analyst fields."""
        columns = (
            ("analyst_prediction", "VARCHAR(16)"),
            ("analyst_confidence", "INTEGER"),
            ("analyst_risk_level", "VARCHAR(16)"),
            ("analyst_model", "VARCHAR(64)"),
            ("analyst_model_version", "VARCHAR(32)"),
            ("analyst_payload_json", "TEXT"),
            ("analyst_grade", "VARCHAR(8)"),
            ("analyst_graded_at", "DATETIME"),
        )
        async with self._engine.begin() as conn:
            for name, definition in columns:
                try:
                    await conn.execute(text(
                        f"ALTER TABLE prop_opportunity_log ADD COLUMN "
                        f"{name} {definition}"
                    ))
                except Exception:
                    pass

    async def _migrate_prop_opportunity_log_v8(self) -> None:
        """Persist the exact result-row source behind each terminal grade."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE prop_opportunity_log ADD COLUMN "
                    "grade_result_source VARCHAR(64)"
                ))
            except Exception:
                pass

    async def repair_underdog_opportunity_game_ids(self) -> int:
        """Repair blank opportunity event IDs from retained UD snapshots.

        This is intentionally explicit maintenance, not startup work: the
        snapshot ledger is append-only and large, so a historical repair must
        never delay the monitoring process from becoming healthy.
        """
        repaired = 0
        async with self._engine.begin() as conn:
            try:
                result = await conn.execute(text(
                    """
                    UPDATE prop_opportunity_log
                    SET game_id = (
                        SELECT snapshot.game_id
                        FROM underdog_snapshots AS snapshot
                        WHERE snapshot.external_id =
                              prop_opportunity_log.external_id
                          AND trim(coalesce(snapshot.game_id, '')) <> ''
                        LIMIT 1
                    )
                    WHERE trim(coalesce(prop_opportunity_log.game_id, '')) = ''
                      AND EXISTS (
                        SELECT 1
                        FROM underdog_snapshots AS snapshot
                        WHERE snapshot.external_id =
                              prop_opportunity_log.external_id
                          AND trim(coalesce(snapshot.game_id, '')) <> ''
                    )
                    """
                ))
                if result.rowcount:
                    repaired = int(result.rowcount)
                    logger.info(
                        "repair_underdog_opportunity_game_ids: repaired %d "
                        "missing Underdog event IDs",
                        repaired,
                    )
            except Exception as exc:
                logger.warning(
                    "repair_underdog_opportunity_game_ids: backfill skipped: %s",
                    exc,
                )
        return repaired

    async def _migrate_slip_journal_leg_v1(self) -> None:
        """Add provider identity to journal legs without changing old entries."""
        async with self._engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TABLE slip_journal_legs ADD COLUMN provider VARCHAR(32)"
                ))
            except Exception:
                pass

    async def log_prop_opportunity(
        self,
        *,
        external_id: str,
        player_name: str,
        team: str,
        sport: str,
        stat_type: str,
        line_value: float,
        recommendation: str,            # Current writes: OVER | UNDER
        decision_tier: str,             # Legacy score metadata
        confidence: int,
        game_time: "Optional[datetime]",
        game_id: str = "",
        # Phase 2 enrichment — optional, captured at alert time
        stars:       "Optional[int]"   = None,   # 0–5 stars
        risk_level:  "Optional[str]"   = None,   # "LOW" | "MEDIUM" | "HIGH"
        explanation: "Optional[str]"   = None,   # reason / narrative excerpt
        # Phase 4 Evidence Infrastructure — optional
        provider:           "Optional[str]"  = None,   # "Underdog" | "PrizePicks"
        bet_quality_score:  "Optional[int]"  = None,   # 0-100 (== confidence for now)
        qualification_path: "Optional[list]" = None,   # gates passed, JSON-encoded
        reason_codes:       "Optional[list]" = None,   # structured codes, JSON-encoded
        watchlist_state:    "Optional[str]"  = None,   # Legacy compatibility; ignored on writes
        downstream_intelligence: "Optional[dict]" = None,
        analyst_result:      "Optional[dict]" = None,
    ) -> None:
        """
        Upsert a directional prop opportunity at evaluation time.

        Non-directional evaluations stay in the active scan universe and are
        intentionally not persisted as PASS/watchlist opportunities.

        On conflict with the same (external_id, stat_type) — i.e. the prop was
        re-evaluated in a later cycle — updates recommendation, tier, confidence,
        line_value, and enrichment to reflect the latest decision.
        Preserves any existing grading (result / actual_value / graded_at) so
        re-evaluation does not wipe a completed outcome.

        Extended result codes accepted by grade_opportunity:
          HIT | MISS | PUSH | PENDING | VOID | CANCELLED | INJURY_VOID | GAME_INTERRUPTED
        """
        await self.log_prop_opportunity_bulk([{
            "external_id": external_id,
            "player_name": player_name,
            "team": team,
            "sport": sport,
            "stat_type": stat_type,
            "line_value": line_value,
            "recommendation": recommendation,
            "decision_tier": decision_tier,
            "confidence": confidence,
            "game_time": game_time,
            "game_id": game_id,
            "stars": stars,
            "risk_level": risk_level,
            "explanation": explanation,
            "provider": provider,
            "bet_quality_score": bet_quality_score,
            "qualification_path": qualification_path,
            "reason_codes": reason_codes,
            "watchlist_state": watchlist_state,
            "downstream_intelligence": downstream_intelligence,
            "analyst_result": analyst_result,
            "product": PLAYER_PROPS_PRODUCT,
        }])
        return

    async def track_prop_records(
        self,
        records: "list[dict]",
    ) -> int:
        """
        Persist every valid ingested prop as a neutral grading record.

        This is intentionally separate from ``log_prop_opportunity``.  It
        records the provider/event/player/stat/line identity before any
        scoring or qualification work, while conflict updates only refresh
        observation metadata.  In particular, it never downgrades an existing
        OVER/UNDER row back to ``NONE`` and never overwrites a terminal result.
        """
        if not records:
            return 0
        if len(records) > _TRACKING_BATCH_SIZE:
            tracked = 0
            for start in range(0, len(records), _TRACKING_BATCH_SIZE):
                tracked += await self.track_prop_records(
                    records[start:start + _TRACKING_BATCH_SIZE]
                )
                await asyncio.sleep(0)
            return tracked

        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

        prepared_by_identity: dict[tuple[str, str], dict] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            provider = str(record.get("provider") or "").strip()
            player_name = str(record.get("player_name") or "").strip()
            sport = str(record.get("sport") or "").upper().strip()
            stat_type = str(record.get("stat_type") or "").strip()
            if not provider or not player_name or not sport or not stat_type:
                continue
            try:
                line_value = float(record.get("line_value"))
            except (TypeError, ValueError):
                continue
            if line_value < 0:
                continue

            external_id = str(record.get("external_id") or "").strip()
            game_id = str(record.get("game_id") or "").strip()
            line_variant = str(record.get("line_variant") or "").lower().strip()
            if not external_id:
                external_id = f"{player_name}:{sport}:{stat_type}:{game_id}"
            if line_variant and not external_id.endswith(f":{line_variant}"):
                external_id = f"{external_id}:{line_variant}"
            # The provider may retain one projection ID while moving its
            # threshold. Each distinct observed threshold must remain a
            # separately gradeable line rather than replacing the first one.
            line_identity = f"{line_value:g}"
            if not external_id.endswith(f":line={line_identity}"):
                external_id = f"{external_id}:line={line_identity}"
            prepared_by_identity[(provider, external_id, stat_type)] = {
                "provider": provider,
                "product": str(
                    record.get("product") or PLAYER_PROPS_PRODUCT
                ).strip() or PLAYER_PROPS_PRODUCT,
                "external_id": external_id,
                "player_name": player_name,
                "team": str(record.get("team") or ""),
                "sport": sport,
                "stat_type": stat_type,
                "line_value": line_value,
                "game_time": record.get("game_time"),
                "game_id": game_id,
                "line_variant": line_variant,
                "detected_at": datetime.utcnow(),
                "result": "PENDING",
            }

        prepared = list(prepared_by_identity.values())
        if not prepared:
            return 0

        async with self.session() as s:
            insert_stmt = _sqlite_insert(PropTrackingLog).values(prepared)
            # Every feed observation is retained in the canonical snapshot
            # history. This compact ledger keeps one gradeable row per
            # provider/line identity, while refreshing the latest observation
            # metadata when the same line is seen again. Settlement fields are
            # deliberately omitted from the conflict update so a later scan
            # can never erase a terminal HIT/MISS/PUSH.
            stmt = insert_stmt.on_conflict_do_update(
                index_elements=["provider", "external_id", "stat_type"],
                set_={
                    "player_name": insert_stmt.excluded.player_name,
                    "team": insert_stmt.excluded.team,
                    "sport": insert_stmt.excluded.sport,
                    "line_value": insert_stmt.excluded.line_value,
                    "game_time": insert_stmt.excluded.game_time,
                    "game_id": insert_stmt.excluded.game_id,
                    "line_variant": insert_stmt.excluded.line_variant,
                    "detected_at": insert_stmt.excluded.detected_at,
                },
            )
            await s.execute(stmt)
            await s.commit()
        return len(prepared)

    async def get_pending_tracked_props(
        self,
        cutoff_hours: float = 0.5,
        after_id: Optional[int] = None,
        limit: int = 5000,
        sport_cursors: Optional[dict[str, int]] = None,
    ) -> "list[PropTrackingLog]":
        """Return old, unresolved universal tracking rows for settlement."""
        import math
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=cutoff_hours)
        async with self.session() as s:
            _base_filters = [
                PropTrackingLog.result == "PENDING",
                or_(
                    and_(
                        PropTrackingLog.game_time.isnot(None),
                        PropTrackingLog.game_time < cutoff,
                    ),
                    and_(
                        PropTrackingLog.game_time.is_(None),
                        PropTrackingLog.detected_at < cutoff,
                        PropTrackingLog.game_id != "",
                    ),
                ),
            ]
            _limit = max(1, min(int(limit), 5000))

            if sport_cursors is not None:
                _sport_result = await s.execute(
                    select(func.upper(PropTrackingLog.sport))
                    .where(*_base_filters)
                    .distinct()
                )
                _sports = sorted(
                    {
                        str(row[0]).upper().strip()
                        for row in _sport_result.all()
                        if row[0]
                    }
                )
                if not _sports:
                    return []
                _per_sport_limit = max(
                    1,
                    math.ceil(_limit / len(_sports)),
                )
                _rows_by_sport: dict[str, list[PropTrackingLog]] = {}
                for _sport in _sports:
                    _sport_cursor = max(
                        0,
                        int(sport_cursors.get(_sport, 0) or 0),
                    )
                    _fresh_limit = (
                        _per_sport_limit
                        if _sport_cursor == 0
                        else max(1, _per_sport_limit // 2)
                    )
                    _sport_stmt = (
                        select(PropTrackingLog)
                        .where(
                            *_base_filters,
                            func.upper(PropTrackingLog.sport) == _sport,
                            PropTrackingLog.id > _sport_cursor,
                        )
                        .order_by(PropTrackingLog.id)
                        .limit(_fresh_limit)
                    )
                    _sport_rows = await s.execute(_sport_stmt)
                    _selected_rows = list(_sport_rows.scalars().all())
                    _retry_limit = _per_sport_limit - len(_selected_rows)
                    if _retry_limit > 0 and _sport_cursor > 0:
                        _retry_stmt = (
                            select(PropTrackingLog)
                            .where(
                                *_base_filters,
                                func.upper(PropTrackingLog.sport) == _sport,
                                PropTrackingLog.id <= _sport_cursor,
                            )
                            .order_by(PropTrackingLog.id)
                            .limit(_retry_limit)
                        )
                        _retry_rows = await s.execute(_retry_stmt)
                        _selected_rows.extend(_retry_rows.scalars().all())
                    _rows_by_sport[_sport] = _selected_rows

                _interleaved: list[PropTrackingLog] = []
                for _index in range(_per_sport_limit):
                    for _sport in _sports:
                        _sport_rows = _rows_by_sport[_sport]
                        if _index < len(_sport_rows):
                            _interleaved.append(_sport_rows[_index])
                            if len(_interleaved) >= _limit:
                                return _interleaved
                return _interleaved

            stmt = select(PropTrackingLog).where(*_base_filters)
            if after_id is not None:
                stmt = stmt.where(PropTrackingLog.id > int(after_id))
            _sport_rank = case(
                {
                    "MLB": 0,
                    "NBA": 1,
                    "WNBA": 2,
                    "NHL": 3,
                    "TENNIS": 4,
                    "DOTA": 5,
                    "MMA": 6,
                    "LIGUE1": 7,
                    "EPL": 8,
                    "LALIGA": 9,
                    "SERIEA": 10,
                    "BUNDESLIGA": 11,
                    "MLS": 12,
                    "LIGA_MX": 13,
                    "UFC": 14,
                    "NFL": 20,
                },
                value=func.upper(PropTrackingLog.sport),
                else_=30,
            )
            stmt = stmt.order_by(_sport_rank, PropTrackingLog.id)
            result = await s.execute(stmt.limit(_limit))
            return list(result.scalars().all())

    @_retry_sqlite_write
    async def grade_tracked_prop(
        self,
        tracking_id: int,
        result: str,
        actual_value: float,
        void_reason: Optional[str] = None,
    ) -> None:
        """Set a terminal settlement on a universal tracking row once."""
        from sqlalchemy import update as _sa_update

        values: dict = {
            "result": result,
            "actual_value": actual_value,
            "graded_at": datetime.utcnow(),
            "settlement_source": "auto_grade",
        }
        if void_reason is not None:
            values["void_reason"] = str(void_reason)[:64]
        async with self.session() as s:
            await s.execute(
                _sa_update(PropTrackingLog)
                .where(
                    PropTrackingLog.id == tracking_id,
                    or_(
                        PropTrackingLog.result == "PENDING",
                        PropTrackingLog.result.is_(None),
                        PropTrackingLog.result == "",
                    ),
                )
                .values(**values)
            )
            await s.commit()

    @_retry_sqlite_write
    async def grade_tracked_props_bulk(
        self,
        updates: "list[tuple[int, str, float]]",
    ) -> int:
        """Set multiple universal tracking settlements in one transaction."""
        if not updates:
            return 0
        from sqlalchemy import bindparam as _bindparam
        from sqlalchemy import update as _sa_update

        async with self.session() as s:
            stmt = (
                _sa_update(PropTrackingLog)
                .where(
                    PropTrackingLog.id == _bindparam("_tracking_id"),
                    or_(
                        PropTrackingLog.result == "PENDING",
                        PropTrackingLog.result.is_(None),
                        PropTrackingLog.result == "",
                    ),
                )
                .values(
                    result=_bindparam("_result"),
                    actual_value=_bindparam("_actual_value"),
                    graded_at=_bindparam("_graded_at"),
                    settlement_source="auto_grade",
                )
                .execution_options(
                    synchronize_session=False,
                    dml_strategy="core_only",
                )
            )
            params = [
                {
                    "_tracking_id": int(tracking_id),
                    "_result": str(result),
                    "_actual_value": float(actual_value),
                    "_graded_at": datetime.utcnow(),
                }
                for tracking_id, result, actual_value in updates
            ]
            response = await s.execute(stmt, params)
            await s.commit()
        return int(response.rowcount or 0)

    async def log_prop_opportunity_bulk(
        self,
        opportunities: "list[dict]",
    ) -> int:
        """Batch-upsert directional prop opportunities in bounded chunks.

        The universal ingestion ledger uses ``track_prop_records`` instead.
        Grading fields are deliberately omitted from conflict updates.
        """
        if not opportunities:
            return 0

        import hashlib
        import json as _json
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
        from engine.downstream_intelligence import build_downstream_payload

        # Keep the statement well below SQLite's default bind-parameter limit.
        chunk_size = 30
        now = datetime.utcnow()
        prepared_by_identity: dict[tuple[str, str], dict] = {}
        for opportunity in opportunities:
            if not isinstance(opportunity, dict):
                continue
            _recommendation = str(
                opportunity.get("recommendation") or ""
            ).upper()
            if _recommendation not in ("OVER", "UNDER"):
                continue

            _external_id = opportunity.get("external_id")
            _stat_type = opportunity.get("stat_type")
            if _external_id is None or _stat_type is None:
                continue
            _provider = opportunity.get("provider")
            _external_id = _stored_opportunity_external_id(_external_id, _provider)
            _stat_type = str(_stat_type)
            # These fields remain writable for older direct callers while the
            # active scan paths pass them as empty/None during migration.
            # Direction-only monitoring rows may intentionally omit the
            # retired confidence field, but the legacy table column remains
            # NOT NULL. Store neutral zero rather than letting one valid
            # directional row abort the entire bulk transaction.
            _confidence = opportunity.get("confidence")
            if _confidence is None:
                _confidence = 0
            _bet_quality = opportunity.get("bet_quality_score")
            _intelligence = opportunity.get("downstream_intelligence")
            _analyst = opportunity.get("analyst_result")
            if _intelligence is None:
                _intelligence = build_downstream_payload(
                    evidence={},
                    sample_size=0,
                )

            _rec_id = hashlib.sha256(
                f"{_external_id}:{_stat_type}".encode()
            ).hexdigest()[:16]
            _qpath = (
                _json.dumps(opportunity.get("qualification_path"))
                if opportunity.get("qualification_path") else None
            )
            _rcodes = (
                _json.dumps(opportunity.get("reason_codes"))
                if opportunity.get("reason_codes") else None
            )
            _intel_json = (
                _json.dumps(_intelligence, separators=(",", ":"))
                if _intelligence else None
            )
            _intel_completeness = (
                (_intelligence.get("evidence_completeness") or {}).get("score")
                if _intelligence else None
            )
            _sharp = (_intelligence or {}).get("sharp_confidence") or {}
            _sharp_confidence = _sharp.get("score") if _intelligence else None
            _sharp_components = (
                _json.dumps(_sharp.get("components") or {}, separators=(",", ":"))
                if _intelligence else None
            )
            _analyst_json = (
                _json.dumps(_analyst, separators=(",", ":"))
                if isinstance(_analyst, dict) else None
            )

            # The active monitoring universe has no secondary classification.
            # Keep the nullable column only for compatibility with old databases.
            prepared_by_identity[(_external_id, _stat_type)] = {
                "external_id": _external_id,
                "player_name": opportunity.get("player_name"),
                "team": opportunity.get("team"),
                "sport": opportunity.get("sport"),
                "stat_type": _stat_type,
                "line_value": opportunity.get("line_value"),
                "recommendation": _recommendation,
                "decision_tier": "",
                "confidence": _confidence,
                "game_time": opportunity.get("game_time"),
                "game_id": str(opportunity.get("game_id") or ""),
                "detected_at": now,
                "result": "PENDING",
                "actual_value": None,
                "graded_at": None,
                "stars": opportunity.get("stars"),
                "risk_level": opportunity.get("risk_level"),
                "explanation": opportunity.get("explanation"),
                "recommendation_id": _rec_id,
                "provider": _provider,
                "product": str(
                    opportunity.get("product") or PLAYER_PROPS_PRODUCT
                ).strip() or PLAYER_PROPS_PRODUCT,
                "bet_quality_score": _bet_quality,
                "qualification_path": _qpath,
                "reason_codes": _rcodes,
                "watchlist_state": None,
                "downstream_intelligence_json": _intel_json,
                "evidence_completeness": _intel_completeness,
                "sharp_confidence": _sharp_confidence,
                "sharp_confidence_components": _sharp_components,
                "analyst_prediction": (
                    _analyst.get("prediction") if isinstance(_analyst, dict) else None
                ),
                "analyst_confidence": (
                    _analyst.get("confidence") if isinstance(_analyst, dict) else None
                ),
                "analyst_risk_level": (
                    _analyst.get("risk_level") if isinstance(_analyst, dict) else None
                ),
                "analyst_model": (
                    _analyst.get("model") if isinstance(_analyst, dict) else None
                ),
                "analyst_model_version": (
                    _analyst.get("model_version")
                    if isinstance(_analyst, dict) else None
                ),
                "analyst_payload_json": _analyst_json,
            }

        prepared = list(prepared_by_identity.values())
        if not prepared:
            return 0

        for start in range(0, len(prepared), chunk_size):
            rows = prepared[start:start + chunk_size]
            # Open one short transaction per chunk. Keeping the session outside
            # this loop made a 10k-prop full-pool rotation monopolize SQLite's
            # writer gate even though the SQL statements were already chunked.
            async with self.session() as s:
                _insert_stmt = _sqlite_insert(PropOpportunityLog).values(rows)
                _update_columns = {
                    column: getattr(_insert_stmt.excluded, column)
                    for column in (
                        "recommendation",
                        "decision_tier",
                        "confidence",
                        "line_value",
                        "game_id",
                        "stars",
                        "risk_level",
                        "explanation",
                        "recommendation_id",
                        "provider",
                        "product",
                        "bet_quality_score",
                        "qualification_path",
                        "reason_codes",
                        "watchlist_state",
                        "downstream_intelligence_json",
                        "evidence_completeness",
                        "sharp_confidence",
                        "sharp_confidence_components",
                        "analyst_prediction",
                        "analyst_confidence",
                        "analyst_risk_level",
                        "analyst_model",
                        "analyst_model_version",
                        "analyst_payload_json",
                    )
                }
                # A compatibility caller may not carry event identity even
                # though the existing opportunity already has it. Never let
                # an empty later observation erase a known settlement key.
                _update_columns["game_id"] = case(
                    (
                        func.trim(_insert_stmt.excluded.game_id) != "",
                        _insert_stmt.excluded.game_id,
                    ),
                    else_=PropOpportunityLog.game_id,
                )
                stmt = _insert_stmt.on_conflict_do_update(
                    index_elements=["external_id", "stat_type"],
                    set_=_update_columns,
                )
                await s.execute(stmt)
                await s.commit()
            await asyncio.sleep(0)
        return len(prepared)

    async def log_prop_candidate_batch(
        self,
        candidates: "list[dict]",
        *,
        strict: bool = False,
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
        rows: list[PropCandidateLog] = []
        failed = 0
        for c in candidates:
            try:
                rows.append(PropCandidateLog(
                    scan_ts              = c.get("scan_ts") or datetime.utcnow(),
                    player_name          = c.get("player_name", ""),
                    team                 = c.get("team", ""),
                    sport                = c.get("sport", ""),
                    stat_type            = c.get("stat_type", ""),
                    line_value           = float(c.get("line_value") or 0.0),
                    provider             = c.get("provider", "Underdog"),
                    # Preserve diagnostic score metadata when the evaluator
                    # supplied it. Direction and evidence remain the delivery
                    # gates, but reports must distinguish an evaluated prop
                    # from one that was never scored.
                    score_total          = c.get("score_total"),
                    score_tier           = c.get("score_tier"),
                    confidence           = c.get("confidence"),
                    gate_decision        = c.get("gate_decision", "EVALUATED"),
                    rejection_reason     = c.get("rejection_reason"),
                    reason_codes         = c.get("reason_codes"),
                    snapshot_external_id = c.get("snapshot_external_id"),
                ))
            except Exception as _row_exc:
                logger.warning(
                    "log_prop_candidate_batch: skipping bad row (%s/%s): %s",
                    c.get("player_name", "?"), c.get("stat_type", "?"), _row_exc,
                )
                failed += 1

        def _values(row: PropCandidateLog) -> dict:
            return {
                "scan_ts": row.scan_ts,
                "player_name": row.player_name,
                "team": row.team,
                "sport": row.sport,
                "stat_type": row.stat_type,
                "line_value": row.line_value,
                "provider": row.provider,
                "score_total": row.score_total,
                "score_tier": row.score_tier,
                "confidence": row.confidence,
                "gate_decision": row.gate_decision,
                "rejection_reason": row.rejection_reason,
                "reason_codes": row.reason_codes,
                "snapshot_external_id": row.snapshot_external_id,
            }

        # Candidate telemetry can contain thousands of neutral evaluations.
        # Core executemany avoids ORM bookkeeping for every row while bounded
        # transactions still give other SQLite users opportunities to acquire
        # the writer between feed chunks.
        inserted = 0
        # Strict mode remains fail-fast (it never falls back to row isolation),
        # but it must not hold SQLite for the entire 5k+ candidate feed.
        chunk_size = 500
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            try:
                async with self.session() as s:
                    await s.execute(
                        sa_insert(PropCandidateLog),
                        [_values(row) for row in chunk],
                    )
                    await s.commit()
                inserted += len(chunk)
            except Exception as _batch_exc:
                logger.warning(
                    "log_prop_candidate_batch: batch insert failed; "
                    "falling back to row isolation (%d rows): %s",
                    len(chunk),
                    _batch_exc,
                )
                for row in chunk:
                    try:
                        async with self.session() as s:
                            s.add(row)
                            await s.commit()
                        inserted += 1
                    except Exception as _row_exc:
                        logger.warning(
                            "log_prop_candidate_batch: skipping bad row (%s/%s): %s",
                            row.player_name,
                            row.stat_type,
                            _row_exc,
                        )
                        failed += 1
        if failed:
            logger.warning(
                "log_prop_candidate_batch: %d/%d rows failed", failed, inserted + failed
            )
        return inserted

    async def get_active_underdog_snapshot_per_prop(
        self,
    ) -> "dict[tuple[str, str], UnderdogSnapshotRecord]":
        """
        Return the single most-recent snapshot for every (player_name, stat_type) pair
        **where the latest snapshot is not a removal**.

        Unlike :meth:`get_latest_underdog_snapshot_per_prop`, the MAX(id) subquery
        runs over *all* rows (removed and non-removed alike).  This correctly excludes
        props whose most-recent feed record is a removal marker — they are simply
        absent from the returned dict, so the stable-refresh job never rescores them
        and the watchlist-rescan job correctly transitions them to 'Removed'.

        The old method (max over non-removed rows only) can return a stale non-removed
        snapshot for a prop that was subsequently removed, causing false alerts.
        """
        async with self.session() as s:
            # Step 1 — latest row id per prop (all rows, including removals)
            subq = (
                select(func.max(UnderdogSnapshotRecord.id))
                .group_by(
                    UnderdogSnapshotRecord.player_name,
                    UnderdogSnapshotRecord.stat_type,
                )
                .scalar_subquery()
            )
            # Step 2 — keep only those latest rows where removed=False
            result = await s.execute(
                select(UnderdogSnapshotRecord).where(
                    UnderdogSnapshotRecord.id.in_(subq),
                    UnderdogSnapshotRecord.removed == False,  # noqa: E712
                )
            )
            rows = result.scalars().all()
        return {(r.player_name, r.stat_type): r for r in rows}

    async def get_all_active_underdog_snapshots_by_line(
        self,
    ) -> "dict[tuple[str, str, float], UnderdogSnapshotRecord]":
        """
        Return the current active Underdog prop universe keyed by
        ``(player_name, stat_type, line_value)``.

        A source ``external_id`` identifies one Underdog prop across line
        movements.  Therefore the latest row is selected by external_id first,
        rather than by line alone.  This keeps an old line as historical data
        without leaving it in the active rotation after that same source prop
        moves.  Distinct source IDs (the normal alternate-line representation)
        still produce distinct active entries.

        Rows with no source ID use the full player/stat/line triplet as a
        backwards-compatible fallback. Started events are no longer eligible
        for active rescanning, but their snapshots remain available to grading.
        """
        now = datetime.utcnow()
        async with self.session() as s:
            # Every serialized Underdog scan stamps its snapshot records with
            # one shared ``fetched_at`` value and inserts them in contiguous
            # primary-key order.  Prefer that newest complete feed batch over
            # grouping the entire raw-history table by external_id.  The
            # latter is exact but becomes prohibitively expensive once the
            # append-only snapshot table reaches billions of rows.
            #
            # Keep the bounded ID lookback deliberately larger than the
            # normal feed size.  If a legacy/small database does not have a
            # substantial batch, use the historical query below so tests and
            # older data retain their exact source-ID reconciliation semantics.
            _latest_row = (
                await s.execute(
                    select(
                        UnderdogSnapshotRecord.id,
                        UnderdogSnapshotRecord.fetched_at,
                    )
                    .order_by(desc(UnderdogSnapshotRecord.id))
                    .limit(1)
                )
            ).first()
            _recent_rows: list[UnderdogSnapshotRecord] = []
            if _latest_row is not None and _latest_row[1] is not None:
                _latest_id, _latest_fetched_at = _latest_row
                _recent_rows = list(
                    (
                        await s.execute(
                            select(UnderdogSnapshotRecord)
                            .where(
                                UnderdogSnapshotRecord.id >= max(
                                    1, int(_latest_id) - 50_000
                                ),
                                UnderdogSnapshotRecord.fetched_at
                                == _latest_fetched_at,
                            )
                            .order_by(UnderdogSnapshotRecord.id)
                        )
                    ).scalars().all()
                )

            if len(_recent_rows) >= 1_000:
                # This is the current complete feed, so each source ID has
                # exactly one authoritative row in this bounded result.
                rows = _recent_rows
                logger.info(
                    "get_all_active_underdog_snapshots_by_line: "
                    "using latest feed batch (%d rows)",
                    len(rows),
                )
            else:
                # Historical fallback: source IDs are stable through line
                # movements. Compute MAX(id) over all rows, including removal
                # markers, so a removal wins.
                rows = []
            # Source IDs are stable through line movements. Compute MAX(id)
            # over all rows, including removal markers, so a removal wins.
            if not rows:
                source_subq = (
                    select(func.max(UnderdogSnapshotRecord.id))
                    .where(
                        UnderdogSnapshotRecord.external_id.isnot(None),
                        UnderdogSnapshotRecord.external_id != "",
                    )
                    .group_by(
                        UnderdogSnapshotRecord.external_id,
                    )
                    .scalar_subquery()
                )
                # Rows without a source ID retain the historical by-line
                # fallback.
                legacy_subq = (
                    select(func.max(UnderdogSnapshotRecord.id))
                    .where(
                        or_(
                            UnderdogSnapshotRecord.external_id.is_(None),
                            UnderdogSnapshotRecord.external_id == "",
                        )
                    )
                    .group_by(
                        UnderdogSnapshotRecord.player_name,
                        UnderdogSnapshotRecord.stat_type,
                        UnderdogSnapshotRecord.line_value,
                    )
                    .scalar_subquery()
                )
                rows = list(
                    (
                        await s.execute(
                            select(UnderdogSnapshotRecord).where(
                                or_(
                                    UnderdogSnapshotRecord.id.in_(source_subq),
                                    UnderdogSnapshotRecord.id.in_(legacy_subq),
                                ),
                            ).order_by(UnderdogSnapshotRecord.id)
                        )
                    ).scalars().all()
                )

        # Older versions used ``player_stat`` as external_id.  Once a real
        # source ID exists for the same player/stat, that synthetic identity
        # is no longer authoritative—even when its latest row is still active
        # and the modern row is a removal marker.
        modern_props = {
            (r.player_name, r.stat_type)
            for r in rows
            if r.external_id
            and r.external_id != f"{r.player_name}_{r.stat_type}"[:64]
        }

        active: dict[tuple[str, str, float], UnderdogSnapshotRecord] = {}
        for row in rows:
            if row.removed:
                continue
            if (
                row.game_time is not None
                and row.game_time <= now
            ):
                continue
            is_legacy = row.external_id == f"{row.player_name}_{row.stat_type}"[:64]
            if is_legacy and (row.player_name, row.stat_type) in modern_props:
                continue
            active[(row.player_name, row.stat_type, row.line_value)] = row
        return active

    async def get_all_active_prop_line_history_by_line(
        self,
        providers: "Optional[Iterable[str]]" = None,
    ) -> "dict[tuple[str, str, str, str, float, str], PropLineHistory]":
        """Return the current normalized active prop universe by provider/line.

        PropLineHistory is the shared tracking stream for every configured
        player-prop provider. The latest row is selected per provider-owned
        event identity, so a removal marker cannot resurrect an older line.
        """
        provider_names = tuple(dict.fromkeys(
            str(provider).strip()
            for provider in (
                providers
                or ("Underdog", "PrizePicks", "DraftKings", "FanDuel")
            )
            if str(provider).strip()
        ))
        if not provider_names:
            return {}
        from reporting import current_reporting_day_bounds

        now = datetime.utcnow()
        # This method feeds current monitoring/FPR only.  Never group the
        # append-only history table across all time: old observations are
        # retained for grading and reports, but they cannot be today's active
        # feed.  Use the configured reporting calendar day rather than UTC
        # midnight so the monitoring universe matches user-facing reports.
        today_start, today_end = current_reporting_day_bounds()
        async with self.session() as s:
            source_identity = case(
                (
                    and_(
                        PropLineHistory.external_id.isnot(None),
                        PropLineHistory.external_id != "",
                    ),
                    PropLineHistory.external_id,
                ),
                else_=PropLineHistory.game_id,
            )
            subq = (
                select(func.max(PropLineHistory.id))
                .where(
                    PropLineHistory.provider.in_(provider_names),
                    PropLineHistory.fetched_at >= today_start,
                    PropLineHistory.fetched_at < today_end,
                )
                .group_by(
                    PropLineHistory.provider,
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                    source_identity,
                )
                .scalar_subquery()
            )
            result = await s.execute(
                select(PropLineHistory).where(
                    PropLineHistory.id.in_(subq),
                    PropLineHistory.fetched_at >= today_start,
                    PropLineHistory.fetched_at < today_end,
                    PropLineHistory.removed.isnot(True),
                    or_(
                        PropLineHistory.game_time.is_(None),
                        PropLineHistory.game_time > now,
                    ),
                )
            )
            rows = result.scalars().all()
        return {
            (
                row.provider,
                row.player_name,
                (row.sport or "").upper(),
                row.stat_type,
                float(row.line_value or 0.0),
                str(row.external_id or row.game_id or ""),
            ): row
            for row in rows
            if row.line_value is not None
        }

    async def get_active_watchlist_candidates(self) -> list:
        """Compatibility shim; all active props use the canonical pool."""
        return []

    async def log_scan_cycle(
        self,
        scan_ts: "datetime",
        fetched: int = 0,
        removed: int = 0,
        futures: int = 0,
        active: int = 0,
        unchanged: int = 0,
        new_props: int = 0,
        line_changed: int = 0,
        cold_start: int = 0,
        analyzed: int = 0,
        qualified: int = 0,
        alert_delivered: int = 0,
        actionable_by_sport: Optional[dict] = None,
        removed_by_sport: Optional[dict] = None,
        enrichment_status: str = "complete",
        enrichment_deferred: int = 0,
    ) -> None:
        """Write one scan-cycle summary row. Non-fatal on failure."""
        async with self.session() as s:
            s.add(ScanCycleLog(
                scan_ts         = scan_ts,
                fetched         = fetched,
                removed         = removed,
                futures         = futures,
                active          = active,
                unchanged       = unchanged,
                new_props       = new_props,
                line_changed    = line_changed,
                cold_start      = cold_start,
                analyzed        = analyzed,
                qualified       = qualified,
                alert_delivered = alert_delivered,
                actionable_by_sport = json.dumps(actionable_by_sport or {}),
                removed_by_sport    = json.dumps(removed_by_sport or {}),
                enrichment_status   = str(enrichment_status or "complete"),
                enrichment_deferred = max(0, int(enrichment_deferred or 0)),
            ))
            await s.commit()

    async def _migrate_scan_cycle_log_v1(self) -> None:
        """Add per-sport scan funnel counters to existing installations."""
        for statement in (
            "ALTER TABLE scan_cycle_log ADD COLUMN actionable_by_sport TEXT",
            "ALTER TABLE scan_cycle_log ADD COLUMN removed_by_sport TEXT",
        ):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(text(statement))
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    logger.debug("scan_cycle_log migration skipped: %s", exc)

    async def _migrate_scan_cycle_log_v2(self) -> None:
        """Persist optional-enrichment completion state for funnel reports."""
        for statement in (
            "ALTER TABLE scan_cycle_log ADD COLUMN enrichment_status "
            "VARCHAR(16) NOT NULL DEFAULT 'complete'",
            "ALTER TABLE scan_cycle_log ADD COLUMN enrichment_deferred "
            "INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(text(statement))
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    logger.debug("scan_cycle_log v2 migration skipped: %s", exc)

    async def get_scan_cycle_summary(
        self,
        since_hours: int = 24,
        session_started_at: Optional[object] = None,
    ) -> dict:
        """Aggregate ScanCycleLog for the full pipeline view in /funnel.

        Returns a dict with cumulative pipeline counts over the requested window,
        plus cycles (number of scan cycles recorded).  When a live monitoring
        session start is supplied, the scan population is also restricted to
        that session so a restart cannot make a fresh ``/funnel 1`` report
        include rows from the previous process.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        if session_started_at is not None:
            try:
                if isinstance(session_started_at, datetime):
                    session_cutoff = session_started_at
                else:
                    _session_text = str(session_started_at).strip()
                    if _session_text.endswith(" UTC"):
                        _session_text = _session_text[:-4] + "+00:00"
                    session_cutoff = datetime.fromisoformat(
                        _session_text.replace("Z", "+00:00")
                    )
                if session_cutoff.tzinfo is not None:
                    session_cutoff = session_cutoff.astimezone(timezone.utc).replace(
                        tzinfo=None
                    )
                cutoff = max(cutoff, session_cutoff)
            except (TypeError, ValueError):
                logger.debug(
                    "get_scan_cycle_summary: invalid session start %r",
                    session_started_at,
                )
        scan_filter = ScanCycleLog.scan_ts >= cutoff
        async with self.session() as s:
            result = await s.execute(
                select(
                    func.count(ScanCycleLog.id).label("cycles"),
                    func.min(ScanCycleLog.scan_ts).label("first_scan_ts"),
                    func.max(ScanCycleLog.scan_ts).label("last_scan_ts"),
                    func.sum(ScanCycleLog.fetched).label("fetched"),
                    func.sum(ScanCycleLog.removed).label("removed"),
                    func.sum(ScanCycleLog.futures).label("futures"),
                    func.sum(ScanCycleLog.active).label("active"),
                    func.sum(ScanCycleLog.unchanged).label("unchanged"),
                    func.sum(ScanCycleLog.new_props).label("new_props"),
                    func.sum(ScanCycleLog.line_changed).label("line_changed"),
                    func.sum(ScanCycleLog.cold_start).label("cold_start"),
                    func.sum(ScanCycleLog.analyzed).label("analyzed"),
                    func.sum(ScanCycleLog.qualified).label("qualified"),
                    func.sum(ScanCycleLog.alert_delivered).label("alert_delivered"),
                )
                .where(scan_filter)
            )
            row = result.one_or_none()
            if row is None or (row.cycles or 0) == 0:
                return {
                    "cycles": 0,
                    "first_scan_ts": None,
                    "last_scan_ts": None,
                    "session_scoped": session_started_at is not None,
                }
            actionable_by_sport: dict[str, int] = {}
            removed_by_sport: dict[str, int] = {}
            maps = await s.execute(select(
                ScanCycleLog.actionable_by_sport,
                ScanCycleLog.removed_by_sport,
            ).where(scan_filter))
            for actionable_json, removed_json in maps.all():
                for target, raw in (
                    (actionable_by_sport, actionable_json),
                    (removed_by_sport, removed_json),
                ):
                    try:
                        values = json.loads(raw or "{}")
                        if isinstance(values, dict):
                            for sport, count in values.items():
                                key = str(sport)
                                target[key] = target.get(key, 0) + int(count or 0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
            latest_cycle = await s.execute(
                select(
                    ScanCycleLog.enrichment_status,
                    ScanCycleLog.enrichment_deferred,
                )
                .where(scan_filter)
                .order_by(ScanCycleLog.scan_ts.desc(), ScanCycleLog.id.desc())
                .limit(1)
            )
            latest_enrichment = latest_cycle.first()
            return {
                "cycles":           row.cycles or 0,
                "first_scan_ts":    row.first_scan_ts,
                "last_scan_ts":     row.last_scan_ts,
                "fetched":          row.fetched or 0,
                "removed":          row.removed or 0,
                "futures":          row.futures or 0,
                "active":           row.active or 0,
                "unchanged":        row.unchanged or 0,
                "new_props":        row.new_props or 0,
                "line_changed":     row.line_changed or 0,
                "cold_start":       row.cold_start or 0,
                "analyzed":         row.analyzed or 0,
                "qualified":        row.qualified or 0,
                "alert_delivered":  row.alert_delivered or 0,
                "actionable_by_sport": actionable_by_sport,
                "removed_by_sport":    removed_by_sport,
                "enrichment_status": (
                    getattr(latest_enrichment, "enrichment_status", None)
                    or "complete"
                ),
                "enrichment_deferred": int(
                    getattr(latest_enrichment, "enrichment_deferred", 0) or 0
                ),
                "session_scoped": session_started_at is not None,
            }

    async def get_funnel_summary(self, since_hours: int = 24) -> dict:
        """
        Aggregate the unified current active-prop monitoring stream from
        PropLineHistory for /funnel.

        Every persisted provider observation is monitoring data.  Alert
        delivery is reported separately and never changes tracking status.
        The active population is limited to latest prop rows observed inside
        the requested report window, so every active row has an observation
        in the same funnel population.
        ``PropCandidateLog`` fallback exists only for old databases/tests that
        have no normalized history rows yet; the returned ``tracked`` field
        always represents the monitoring population, not Telegram delivery.
        """
        from datetime import timedelta
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=since_hours)

        async with self.session() as s:
            current_ids = (
                select(func.max(PropLineHistory.id))
                .where(
                    PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS),
                )
                .group_by(
                    PropLineHistory.provider,
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                    case(
                        (
                            and_(
                                PropLineHistory.external_id.isnot(None),
                                PropLineHistory.external_id != "",
                            ),
                            PropLineHistory.external_id,
                        ),
                        else_=PropLineHistory.game_id,
                    ),
                )
            )
            current_res = await s.execute(
                select(
                    PropLineHistory.provider,
                    PropLineHistory.sport,
                ).where(
                    PropLineHistory.id.in_(current_ids),
                    PropLineHistory.fetched_at >= cutoff,
                    or_(
                        PropLineHistory.removed == False,  # noqa: E712
                        PropLineHistory.removed.is_(None),
                    ),
                    or_(
                        PropLineHistory.game_time.is_(None),
                        PropLineHistory.game_time > now,
                    ),
                )
            )
            current_rows = current_res.all()
            observation_provider_res = await s.execute(
                select(
                    PropLineHistory.provider,
                    PropLineHistory.sport,
                    func.count(PropLineHistory.id),
                )
                .where(
                    PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS),
                    PropLineHistory.fetched_at >= cutoff,
                )
                .group_by(PropLineHistory.provider, PropLineHistory.sport)
            )
            tracked_observations_by_provider: dict[str, int] = {}
            observation_by_sport: dict[str, int] = {}
            for provider, sport, count in observation_provider_res.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider_key = str(provider or "Unknown")
                sport_key = str(sport or "UNKNOWN").upper().strip()
                tracked_observations_by_provider[provider_key] = (
                    tracked_observations_by_provider.get(provider_key, 0)
                    + int(count or 0)
                )
                observation_by_sport[sport_key] = (
                    observation_by_sport.get(sport_key, 0) + int(count or 0)
                )
            tracked_observations = sum(tracked_observations_by_provider.values())
            has_normalized_history = tracked_observations > 0
            tracked_ledger_count = (
                await s.execute(
                    select(func.count(PropTrackingLog.id)).where(
                        PropTrackingLog.detected_at >= cutoff
                    )
                )
            ).scalar() or 0

            # Compatibility fallback for installations predating normalized
            # history, where candidate telemetry is the only available stream.
            if (
                not current_rows
                and not has_normalized_history
                and not tracked_ledger_count
            ):
                sport_res = await s.execute(
                    select(
                        PropCandidateLog.sport,
                        func.count(PropCandidateLog.id),
                    )
                    .where(PropCandidateLog.scan_ts >= cutoff)
                    .group_by(PropCandidateLog.sport)
                    .order_by(func.count(PropCandidateLog.id).desc())
                )
                by_sport = [
                    {
                        "sport": sport or "UNKNOWN",
                        # Compatibility field: in this fallback only, this
                        # is candidate/evaluation volume, never active
                        # tracking. The explicit evaluated key is authoritative.
                        "tracked": int(count or 0),
                        "evaluated": int(count or 0),
                    }
                    for sport, count in sport_res.all()
                    if _is_supported_tracking_sport(sport)
                ]
                candidate_total = sum(row["evaluated"] for row in by_sport)
                return {
                    "since_hours": since_hours,
                    "tracked": 0,
                    "active_tracked": 0,
                    "tracked_observations": 0,
                    "historical_observations": 0,
                    "evaluated": candidate_total,
                    "tracked_total": 0,
                    "tracked_pending": 0,
                    "tracked_resolved": 0,
                    "tracked_by_provider": {},
                    "tracked_by_sport": {},
                    "tracked_result_counts": {},
                    "directional_total": 0,
                    "actionable_total": 0,
                    "delivered": 0,
                    "pending": 0,
                    "resolved": 0,
                    "active_by_provider": {},
                    "active_by_sport": {},
                    "observations_by_sport": {},
                    "provider_breakdown": {},
                    "by_sport": by_sport,
                    "source": "candidate_log_legacy_fallback",
                }

            by_sport_counts: dict[str, int] = {}
            by_provider_counts: dict[str, int] = {}
            active_by_provider: dict[str, int] = {}
            active_by_sport: dict[str, int] = {}
            for provider, sport in current_rows:
                if not _is_supported_tracking_sport(sport):
                    continue
                sport_key = str(sport or "UNKNOWN").upper().strip()
                provider_key = str(provider or "Unknown")
                by_sport_counts[sport_key] = by_sport_counts.get(sport_key, 0) + 1
                by_provider_counts[provider_key] = (
                    by_provider_counts.get(provider_key, 0) + 1
                )
                active_by_provider[provider_key] = (
                    active_by_provider.get(provider_key, 0) + 1
                )
                active_by_sport[sport_key] = active_by_sport.get(sport_key, 0) + 1
            by_sport = [
                {"sport": sport, "tracked": count}
                for sport, count in sorted(
                    by_sport_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ]
            tracked_count = sum(by_provider_counts.values())

            # Grading population is universal and intentionally independent of
            # directional recommendations.  Keep these counters separate from
            # the directional performance population below so neutral
            # observations do not appear as PLAYs.
            tracking_res = await s.execute(
                select(
                    PropTrackingLog.provider,
                    PropTrackingLog.sport,
                    PropTrackingLog.result,
                ).where(PropTrackingLog.detected_at >= cutoff)
            )
            tracked_total = 0
            tracked_pending = 0
            tracked_resolved = 0
            tracked_by_provider: dict[str, int] = {}
            tracked_by_sport: dict[str, int] = {}
            tracked_result_counts: dict[str, int] = {}
            tracked_pending_by_provider: dict[str, int] = {}
            tracked_resolved_by_provider: dict[str, int] = {}
            tracked_result_counts_by_provider: dict[str, dict[str, int]] = {}
            for provider, sport, result in tracking_res.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider_key = str(provider or "Unknown")
                sport_key = str(sport or "UNKNOWN").upper().strip()
                result_key = str(result or "PENDING").upper()
                tracked_total += 1
                tracked_by_provider[provider_key] = (
                    tracked_by_provider.get(provider_key, 0) + 1
                )
                tracked_by_sport[sport_key] = tracked_by_sport.get(sport_key, 0) + 1
                tracked_result_counts[result_key] = (
                    tracked_result_counts.get(result_key, 0) + 1
                )
                provider_results = tracked_result_counts_by_provider.setdefault(
                    provider_key, {}
                )
                provider_results[result_key] = (
                    provider_results.get(result_key, 0) + 1
                )
                if result_key == "PENDING":
                    tracked_pending += 1
                    tracked_pending_by_provider[provider_key] = (
                        tracked_pending_by_provider.get(provider_key, 0) + 1
                    )
                elif result_key in {
                    "HIT", "MISS", "PUSH", "VOID", "CANCELLED",
                    "INJURY_VOID", "GAME_INTERRUPTED",
                }:
                    tracked_resolved += 1
                    tracked_resolved_by_provider[provider_key] = (
                        tracked_resolved_by_provider.get(provider_key, 0) + 1
                    )

            # Directional opportunities and delivery are separate populations
            # from the active observation universe. All populations here are
            # scoped to the requested report window so the funnel remains
            # ordered: observed -> active -> evaluated -> directional ->
            # delivered.
            opportunity_rows = await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    PropOpportunityLog.alert_sent,
                    PropOpportunityLog.reason_codes,
                ).where(
                    PropOpportunityLog.detected_at >= cutoff,
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                )
            )
            directional_total = 0
            delivered_total = 0
            pending_total = 0
            resolved_total = 0
            directional_by_provider: dict[str, int] = {}
            delivered_by_provider: dict[str, int] = {}
            pending_by_provider: dict[str, int] = {}
            resolved_by_provider: dict[str, int] = {}
            directional_by_sport: dict[str, int] = {}
            delivered_by_sport: dict[str, int] = {}
            result_counts: dict[str, int] = {}
            result_counts_by_provider: dict[str, dict[str, int]] = {}
            result_counts_by_sport: dict[str, dict[str, int]] = {}
            evidence_by_source: dict[str, int] = {
                "settled_results": 0,
                "historical_average": 0,
                "unavailable": 0,
            }
            for (
                provider,
                sport,
                recommendation,
                result,
                alert_sent,
                reason_codes,
            ) in opportunity_rows.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider_key = str(provider or "Underdog")
                directional_total += 1
                directional_by_provider[provider_key] = (
                    directional_by_provider.get(provider_key, 0) + 1
                )
                sport_key = str(sport or "UNKNOWN").upper().strip()
                directional_by_sport[sport_key] = (
                    directional_by_sport.get(sport_key, 0) + 1
                )
                result_key = str(result or "PENDING").upper()
                result_counts[result_key] = result_counts.get(result_key, 0) + 1
                provider_results = result_counts_by_provider.setdefault(provider_key, {})
                provider_results[result_key] = provider_results.get(result_key, 0) + 1
                sport_results = result_counts_by_sport.setdefault(sport_key, {})
                sport_results[result_key] = sport_results.get(result_key, 0) + 1
                try:
                    parsed_codes = (
                        json.loads(reason_codes)
                        if isinstance(reason_codes, str)
                        else reason_codes
                    )
                except (TypeError, ValueError):
                    parsed_codes = []
                if isinstance(parsed_codes, list) and "EVIDENCE:SETTLED_RESULTS" in parsed_codes:
                    evidence_by_source["settled_results"] += 1
                elif (
                    isinstance(parsed_codes, list)
                    and "EVIDENCE:HISTORICAL_AVERAGE" in parsed_codes
                ):
                    evidence_by_source["historical_average"] += 1
                else:
                    evidence_by_source["unavailable"] += 1
                if result_key == "PENDING":
                    pending_total += 1
                    pending_by_provider[provider_key] = (
                        pending_by_provider.get(provider_key, 0) + 1
                    )
                elif result_key in {
                    "HIT", "MISS", "PUSH", "VOID", "CANCELLED",
                    "INJURY_VOID", "GAME_INTERRUPTED",
                }:
                    resolved_total += 1
                    resolved_by_provider[provider_key] = (
                        resolved_by_provider.get(provider_key, 0) + 1
                    )

            confirmed_audits = await self._confirmed_telegram_play_audits(
                s, since=cutoff
            )
            for audit in confirmed_audits:
                provider_key = str(audit.provider or "Underdog")
                delivered_total += 1
                delivered_by_provider[provider_key] = (
                    delivered_by_provider.get(provider_key, 0) + 1
                )
                sport_key = str(audit.sport or "UNKNOWN").upper().strip()
                delivered_by_sport[sport_key] = (
                    delivered_by_sport.get(sport_key, 0) + 1
                )

            evaluated_by_provider: dict[str, int] = {}
            evaluated_by_sport: dict[str, int] = {}
            evaluated_total = 0
            candidate_res = await s.execute(
                select(
                    PropCandidateLog.provider,
                    PropCandidateLog.sport,
                    func.count(PropCandidateLog.id),
                )
                .where(PropCandidateLog.scan_ts >= cutoff)
                .group_by(PropCandidateLog.provider, PropCandidateLog.sport)
            )
            for provider, sport, count in candidate_res.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider_key = str(provider or "Unknown")
                sport_key = str(sport or "UNKNOWN").upper().strip()
                count_value = int(count or 0)
                evaluated_total += count_value
                evaluated_by_provider[provider_key] = (
                    evaluated_by_provider.get(provider_key, 0) + count_value
                )
                evaluated_by_sport[sport_key] = (
                    evaluated_by_sport.get(sport_key, 0) + count_value
                )

            # A directional opportunity and a confirmed delivery are both
            # downstream evidence that evaluation occurred.  Candidate
            # telemetry is intentionally non-critical and may be deferred or
            # unavailable for a short report window, so it cannot make the
            # funnel claim ``directional > evaluated`` or ``delivered >
            # evaluated``.  Use those downstream populations as a conservative
            # lower bound while retaining all neutral candidate evaluations
            # whenever they are present.
            for sport_key, count_value in directional_by_sport.items():
                evaluated_by_sport[sport_key] = max(
                    evaluated_by_sport.get(sport_key, 0),
                    int(count_value or 0),
                )
            for sport_key, count_value in delivered_by_sport.items():
                evaluated_by_sport[sport_key] = max(
                    evaluated_by_sport.get(sport_key, 0),
                    int(count_value or 0),
                )
            evaluated_total = sum(evaluated_by_sport.values())

            provider_breakdown: dict[str, dict] = {}
            all_providers = (
                set(active_by_provider)
                | set(tracked_observations_by_provider)
                | set(evaluated_by_provider)
                | set(directional_by_provider)
                | set(delivered_by_provider)
                | set(tracked_by_provider)
            )
            for provider in sorted(all_providers):
                provider_breakdown[provider] = {
                    "active_tracked": active_by_provider.get(provider, 0),
                    "historical_observations": tracked_observations_by_provider.get(
                        provider, 0
                    ),
                    "evaluated": evaluated_by_provider.get(provider, 0),
                    "directional": directional_by_provider.get(provider, 0),
                    "actionable": directional_by_provider.get(provider, 0),
                    "delivered": delivered_by_provider.get(provider, 0),
                    "pending": tracked_pending_by_provider.get(provider, 0),
                    "resolved": tracked_resolved_by_provider.get(provider, 0),
                    "result_counts": tracked_result_counts_by_provider.get(
                        provider, {}
                    ),
                    "tracked": tracked_by_provider.get(provider, 0),
                }
        return {
            "since_hours": since_hours,
            "tracked": tracked_count,
            "active_tracked": tracked_count,
            "tracked_observations": tracked_observations,
            "historical_observations": tracked_observations,
            "tracked_observations_by_provider": tracked_observations_by_provider,
            "historical_observations_by_provider": tracked_observations_by_provider,
            "observations_by_sport": observation_by_sport,
            "active_by_provider": active_by_provider,
            "active_by_sport": active_by_sport,
            "evaluated": evaluated_total,
            "evaluated_by_provider": evaluated_by_provider,
            "evaluated_by_sport": evaluated_by_sport,
            "tracked_total": tracked_total,
            "tracked_pending": tracked_pending,
            "tracked_resolved": tracked_resolved,
            "tracked_by_provider": tracked_by_provider,
            "tracked_by_sport": tracked_by_sport,
            "tracked_result_counts": tracked_result_counts,
            "tracked_pending_by_provider": tracked_pending_by_provider,
            "tracked_resolved_by_provider": tracked_resolved_by_provider,
            "tracked_result_counts_by_provider": tracked_result_counts_by_provider,
            "directional_total": directional_total,
            "actionable_total": directional_total,
            "delivered": delivered_total,
            "pending": pending_total,
            "resolved": resolved_total,
            "directional_by_provider": directional_by_provider,
            "delivered_by_provider": delivered_by_provider,
            "pending_by_provider": pending_by_provider,
            "resolved_by_provider": resolved_by_provider,
            "directional_by_sport": directional_by_sport,
            "delivered_by_sport": delivered_by_sport,
            "result_counts": result_counts,
            "result_counts_by_provider": result_counts_by_provider,
            "result_counts_by_sport": result_counts_by_sport,
            "evidence_by_source": evidence_by_source,
            "provider_breakdown": provider_breakdown,
            "by_sport": by_sport,
            "by_provider": [
                {"provider": provider, "tracked": count}
                for provider, count in sorted(
                    by_provider_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "source": "prop_line_history",
        }

    async def get_funnel_observation_summary(self, since_hours: int = 24) -> dict:
        """Return the lightweight observed-stage population for ``/funnel``.

        The complete funnel also aggregates candidate, tracking, opportunity,
        and delivery tables.  Those reads can be slow while a large rotation
        is writing SQLite.  The observed stage must remain independently
        reportable so a partial funnel never renders missing observations as
        zero.
        """
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(since_hours)))
        async with self.session() as s:
            result = await s.execute(
                select(
                    PropLineHistory.sport,
                    func.count(PropLineHistory.id),
                )
                .where(
                    PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS),
                    PropLineHistory.fetched_at >= cutoff,
                )
                .group_by(PropLineHistory.sport)
            )
            observations_by_sport: dict[str, int] = {}
            for sport, count in result.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                sport_key = str(sport or "UNKNOWN").upper().strip()
                observations_by_sport[sport_key] = (
                    observations_by_sport.get(sport_key, 0) + int(count or 0)
                )
        return {
            "since_hours": max(1, int(since_hours)),
            "historical_observations": sum(observations_by_sport.values()),
            "tracked_observations": sum(observations_by_sport.values()),
            "observations_by_sport": observations_by_sport,
            "source": "prop_line_history_observations_only",
        }

    async def get_pending_opportunities(
        self,
        cutoff_hours: float = 0.5,
        after_id: Optional[int] = None,
        limit: int = 5000,
        sport_cursors: Optional[dict[str, int]] = None,
    ) -> "list[PropOpportunityLog]":
        """
        Return PENDING tracking rows old enough to attempt resolution.

        Used by the opportunity_grader job to find outcomes that can now be
        looked up in player_game_results.  Rows with a known game time use that
        timestamp; rows whose provider did not expose one use detected_at and
        are resolved only when an event identity is available.  ``after_id``
        lets the hourly job rotate through a large backlog without retrying the
        same oldest rows on every cycle.
        """
        import math
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=cutoff_hours)
        async with self.session() as s:
            _base_filters = [
                PropOpportunityLog.result == "PENDING",
                PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                or_(
                    and_(
                        PropOpportunityLog.game_time.isnot(None),
                        PropOpportunityLog.game_time < cutoff,
                    ),
                    and_(
                        PropOpportunityLog.game_time.is_(None),
                        PropOpportunityLog.detected_at < cutoff,
                        PropOpportunityLog.game_id != "",
                    ),
                ),
            ]
            _limit = max(1, min(int(limit), 5000))

            # Production grading uses independent sport cursors. Fetch an
            # even quota per sport and interleave the rows so a large MLB
            # backlog cannot consume the whole page before NBA/NFL/esports
            # receive a settlement attempt. The legacy single-ID path below
            # remains for older callers and compatibility adapters.
            if sport_cursors is not None:
                _sport_result = await s.execute(
                    select(func.upper(PropOpportunityLog.sport))
                    .where(*_base_filters)
                    .distinct()
                )
                _sports = sorted(
                    {
                        str(row[0]).upper().strip()
                        for row in _sport_result.all()
                        if row[0]
                    }
                )
                if not _sports:
                    return []
                _per_sport_limit = max(
                    1,
                    math.ceil(_limit / len(_sports)),
                )
                _rows_by_sport: dict[str, list[PropOpportunityLog]] = {}
                for _sport in _sports:
                    _sport_cursor = max(
                        0,
                        int(sport_cursors.get(_sport, 0) or 0),
                    )
                    _fresh_limit = (
                        _per_sport_limit
                        if _sport_cursor == 0
                        else max(1, _per_sport_limit // 2)
                    )
                    _sport_stmt = (
                        select(PropOpportunityLog)
                        .where(
                            *_base_filters,
                            func.upper(PropOpportunityLog.sport) == _sport,
                            PropOpportunityLog.id > _sport_cursor,
                        )
                        .order_by(PropOpportunityLog.id)
                        .limit(_fresh_limit)
                    )
                    _sport_rows = await s.execute(_sport_stmt)
                    _selected_rows = list(_sport_rows.scalars().all())
                    _retry_limit = _per_sport_limit - len(_selected_rows)
                    if _retry_limit > 0 and _sport_cursor > 0:
                        _retry_stmt = (
                            select(PropOpportunityLog)
                            .where(
                                *_base_filters,
                                func.upper(PropOpportunityLog.sport) == _sport,
                                PropOpportunityLog.id <= _sport_cursor,
                            )
                            .order_by(PropOpportunityLog.id)
                            .limit(_retry_limit)
                        )
                        _retry_rows = await s.execute(_retry_stmt)
                        _selected_rows.extend(_retry_rows.scalars().all())
                    _rows_by_sport[_sport] = _selected_rows

                _interleaved: list[PropOpportunityLog] = []
                for _index in range(_per_sport_limit):
                    for _sport in _sports:
                        _sport_rows = _rows_by_sport[_sport]
                        if _index < len(_sport_rows):
                            _interleaved.append(_sport_rows[_index])
                            if len(_interleaved) >= _limit:
                                return _interleaved
                return _interleaved

            stmt = select(PropOpportunityLog).where(*_base_filters)
            if after_id is not None:
                stmt = stmt.where(PropOpportunityLog.id > int(after_id))
            _sport_rank = case(
                {
                    "MLB": 0,
                    "NBA": 1,
                    "WNBA": 2,
                    "NHL": 3,
                    "TENNIS": 4,
                    "DOTA": 5,
                    "MMA": 6,
                    "LIGUE1": 7,
                    "EPL": 8,
                    "LALIGA": 9,
                    "SERIEA": 10,
                    "BUNDESLIGA": 11,
                    "MLS": 12,
                    "LIGA_MX": 13,
                    "UFC": 14,
                    "NFL": 20,
                },
                value=func.upper(PropOpportunityLog.sport),
                else_=30,
            )
            stmt = stmt.order_by(_sport_rank, PropOpportunityLog.id)
            result = await s.execute(stmt.limit(_limit))
            return list(result.scalars().all())

    @_retry_sqlite_write
    async def grade_opportunity(
        self,
        opp_id:       int,
        result:       str,
        actual_value: float,
        error_type:   Optional[str] = None,
        void_reason:  Optional[str] = None,
        grade_result_source: Optional[str] = None,
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
        from engine.identity import display_player_name
        values: dict = {
            "result":       result,
            "actual_value": actual_value,
            "graded_at":    datetime.utcnow(),
            "grade_result_source": (
                str(grade_result_source).strip()[:64]
                if grade_result_source
                else None
            ),
        }
        if error_type is not None:
            values["error_type"] = error_type
        if void_reason is not None:
            values["void_reason"] = str(void_reason)[:64]
        async with self.session() as s:
            # Preserve completed prop outcomes as the bot's own historical
            # evidence.  Provider rows remain authoritative when one already
            # exists for the same player × sport × stat × date.
            evidence_result = str(result or "").upper()
            # Outcomes are terminal. A retry, duplicate scheduler run, or
            # stale settlement response must not overwrite recorded history.
            opp_row = await s.get(PropOpportunityLog, opp_id)
            if opp_row is None:
                return
            existing_result = str(opp_row.result or "").upper()
            if existing_result not in {"", "PENDING"}:
                return
            _ai_grade = _analyst_grade(
                opp_row.analyst_prediction,
                evidence_result,
                opp_row.recommendation,
            )
            if _ai_grade is not None:
                values["analyst_grade"] = _ai_grade
                values["analyst_graded_at"] = datetime.utcnow()
            if evidence_result in {"HIT", "MISS", "PUSH"}:
                player_name = display_player_name(opp_row.player_name)
                sport = (opp_row.sport or "").upper().strip()
                stat_type = (opp_row.stat_type or "").lower().strip()
                evidence_date = (
                    opp_row.game_time.strftime("%Y-%m-%d")
                    if opp_row.game_time
                    else datetime.utcnow().strftime("%Y-%m-%d")
                )
                evidence_game_id = str(opp_row.game_id or "")
                existing = await s.execute(
                    select(PlayerGameResult).where(
                        PlayerGameResult.player_name == player_name,
                        PlayerGameResult.sport == sport,
                        PlayerGameResult.stat_type == stat_type,
                        PlayerGameResult.game_date == evidence_date,
                        PlayerGameResult.game_id == evidence_game_id,
                    )
                )
                if existing.scalar_one_or_none() is None:
                    s.add(PlayerGameResult(
                        player_name=player_name,
                        sport=sport,
                        stat_type=stat_type,
                        game_date=evidence_date,
                        opponent=opp_row.team or None,
                        actual_value=actual_value,
                        source=(
                            str(grade_result_source).strip()[:32]
                            if grade_result_source
                            else "graded_prop"
                        ),
                        game_id=evidence_game_id,
                    ))
            await s.execute(
                _sa_update(PropOpportunityLog)
                .where(
                    PropOpportunityLog.id == opp_id,
                    or_(
                        PropOpportunityLog.result == "PENDING",
                        PropOpportunityLog.result.is_(None),
                        PropOpportunityLog.result == "",
                    ),
                )
                .values(**values)
            )
            await s.commit()

    @_retry_sqlite_write
    async def grade_opportunities_bulk(
        self,
        updates: "list[dict]",
    ) -> int:
        """Grade directional opportunities with one bounded SQLite commit.

        The single-row method remains the compatibility API.  The scheduled
        grader uses this method so a large settlement pass does not acquire and
        release the process-wide writer gate for every pick.
        """
        if not updates:
            return 0
        from engine.identity import display_player_name
        from sqlalchemy import bindparam as _bindparam
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

        ids = [int(item["id"]) for item in updates if item.get("id") is not None]
        if not ids:
            return 0
        async with self.session() as s:
            rows_result = await s.execute(
                select(PropOpportunityLog).where(PropOpportunityLog.id.in_(ids))
            )
            rows_by_id = {
                int(row.id): row for row in rows_result.scalars().all()
            }
            evidence_rows = []
            update_params = []
            for item in updates:
                opp_row = rows_by_id.get(int(item["id"]))
                if opp_row is None:
                    continue
                if str(opp_row.result or "").upper() not in {"", "PENDING"}:
                    continue

                outcome = str(item.get("result") or "").upper()
                actual_value = float(item.get("actual_value", 0.0))
                if outcome in {"HIT", "MISS", "PUSH"}:
                    evidence_date = (
                        opp_row.game_time.strftime("%Y-%m-%d")
                        if opp_row.game_time
                        else datetime.utcnow().strftime("%Y-%m-%d")
                    )
                    evidence_game_id = str(opp_row.game_id or "")
                    evidence_rows.append(
                        {
                            "player_name": display_player_name(opp_row.player_name),
                            "sport": (opp_row.sport or "").upper().strip(),
                            "stat_type": (opp_row.stat_type or "").lower().strip(),
                            "game_date": evidence_date,
                            "opponent": opp_row.team or None,
                            "actual_value": actual_value,
                            "source": (
                                str(item.get("grade_result_source") or "").strip()[:32]
                                or "graded_prop"
                            ),
                            "game_id": evidence_game_id,
                            "fetched_at": datetime.utcnow(),
                        }
                    )

                _graded_at = datetime.utcnow()
                _ai_grade = _analyst_grade(
                    opp_row.analyst_prediction,
                    outcome,
                    opp_row.recommendation,
                )
                update_params.append(
                    {
                        "_opportunity_id": int(opp_row.id),
                        "_result": outcome,
                        "_actual_value": actual_value,
                        "_grade_result_source": (
                            str(item.get("grade_result_source") or "").strip()[:64]
                            or None
                        ),
                        "_graded_at": _graded_at,
                        "_analyst_grade": _ai_grade,
                        "_analyst_graded_at": (
                            _graded_at if _ai_grade is not None else None
                        ),
                        "_error_type": item.get("error_type"),
                        "_void_reason": (
                            str(item["void_reason"])[:64]
                            if item.get("void_reason") is not None
                            else None
                        ),
                    }
                )

            if evidence_rows:
                await s.execute(
                    _sqlite_insert(PlayerGameResult)
                    .values(evidence_rows)
                    .on_conflict_do_nothing(
                        index_elements=[
                            "player_name",
                            "sport",
                            "stat_type",
                            "game_date",
                            "game_id",
                        ]
                    )
                )

            if update_params:
                stmt = (
                    update(PropOpportunityLog)
                    .where(
                        PropOpportunityLog.id == _bindparam("_opportunity_id"),
                        or_(
                            PropOpportunityLog.result == "PENDING",
                            PropOpportunityLog.result.is_(None),
                            PropOpportunityLog.result == "",
                        ),
                    )
                    .values(
                        result=_bindparam("_result"),
                        actual_value=_bindparam("_actual_value"),
                        grade_result_source=_bindparam("_grade_result_source"),
                        graded_at=_bindparam("_graded_at"),
                        analyst_grade=_bindparam("_analyst_grade"),
                        analyst_graded_at=_bindparam("_analyst_graded_at"),
                        error_type=_bindparam("_error_type"),
                        void_reason=_bindparam("_void_reason"),
                    )
                    .execution_options(
                        synchronize_session=False,
                        dml_strategy="core_only",
                    )
                )
                response = await s.execute(stmt, update_params)
                applied = int(response.rowcount or 0)
            else:
                applied = 0
            await s.commit()
        return applied

    async def get_analyst_performance(
        self,
        *,
        group_by: str = "sport",
        sport: Optional[str] = None,
        stat_type: Optional[str] = None,
        direction: Optional[str] = None,
        confidence_min: Optional[int] = None,
        confidence_max: Optional[int] = None,
        risk_level: Optional[str] = None,
        player_name: Optional[str] = None,
        league: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> dict:
        """Return auditable analyst results grouped by a supported dimension."""
        dimensions = {
            "sport": lambda row, payload: row.sport or "UNAVAILABLE",
            "market": lambda row, payload: row.stat_type or "UNAVAILABLE",
            "direction": lambda row, payload: row.analyst_prediction or "UNAVAILABLE",
            "confidence": lambda row, payload: (
                str(row.analyst_confidence)
                if row.analyst_confidence is not None else "UNAVAILABLE"
            ),
            "risk": lambda row, payload: row.analyst_risk_level or "UNAVAILABLE",
            "player": lambda row, payload: row.player_name or "UNAVAILABLE",
            "league": lambda row, payload: payload.get("league", "UNAVAILABLE"),
            "model_version": lambda row, payload: (
                row.analyst_model_version or "UNAVAILABLE"
            ),
        }
        if group_by not in dimensions:
            raise ValueError(f"unsupported analyst dimension: {group_by}")

        async with self.session() as s:
            result = await s.execute(
                select(PropOpportunityLog).where(
                    PropOpportunityLog.analyst_prediction.in_(("OVER", "UNDER"))
                )
            )
            rows = list(result.scalars().all())

        buckets: dict[str, dict[str, int | float | None]] = {}
        for row in rows:
            if sport and (row.sport or "").upper() != sport.upper():
                continue
            if stat_type and (row.stat_type or "").lower() != stat_type.lower():
                continue
            if direction and (row.analyst_prediction or "").upper() != direction.upper():
                continue
            if confidence_min is not None and (
                row.analyst_confidence is None
                or row.analyst_confidence < confidence_min
            ):
                continue
            if confidence_max is not None and (
                row.analyst_confidence is None
                or row.analyst_confidence > confidence_max
            ):
                continue
            if risk_level and (row.analyst_risk_level or "").upper() != risk_level.upper():
                continue
            if player_name and (row.player_name or "").lower() != player_name.lower():
                continue
            if model_version and row.analyst_model_version != model_version:
                continue
            try:
                payload = json.loads(row.analyst_payload_json or "{}")
            except (TypeError, ValueError):
                payload = {}
            if league and payload.get("league") != league:
                continue

            key = str(dimensions[group_by](row, payload))
            bucket = buckets.setdefault(
                key, {
                    "total": 0,
                    "graded": 0,
                    "hits": 0,
                    "misses": 0,
                    "pushes": 0,
                    "unverified": 0,
                }
            )
            bucket["total"] = int(bucket["total"] or 0) + 1
            if row.analyst_grade in {"HIT", "MISS", "PUSH"}:
                if is_trusted_directional_grade_source(
                    row.provider,
                    row.result,
                    row.grade_result_source,
                ):
                    bucket["graded"] = int(bucket["graded"] or 0) + 1
                    name = {
                        "HIT": "hits",
                        "MISS": "misses",
                        "PUSH": "pushes",
                    }[row.analyst_grade]
                    bucket[name] = int(bucket[name] or 0) + 1
                else:
                    bucket["unverified"] = int(bucket["unverified"] or 0) + 1

        for bucket in buckets.values():
            denominator = int(bucket["graded"] or 0)
            bucket["denominator"] = denominator
            bucket["hit_rate"] = (
                round(int(bucket["hits"] or 0) / denominator * 100, 2)
                if denominator else None
            )
        return {"group_by": group_by, "buckets": buckets}

    @_retry_sqlite_write
    async def mark_opportunity_alert_sent(
        self,
        external_id: str,
        stat_type:   str,
        provider:    Optional[str] = None,
    ) -> int:
        """
        Mark an existing PropOpportunityLog row as a delivered Telegram actionable pick.

        Called immediately after ``deliver_underdog()`` returns ``sent=True`` for a
        🎯 ACTIONABLE BET PICK alert. Only those rows end up with
        alert_sent=True; monitoring observations and undelivered directional
        evaluations are never marked.

        The row must already exist (created by ``log_prop_opportunity``).  If it
        doesn't exist yet this is a no-op (which is safe — the try/except in the
        engine prevents it from blocking the alert flow).
        """
        from sqlalchemy import update as _sa_update
        async with self.session() as s:
            result = await s.execute(
                _sa_update(PropOpportunityLog)
                .where(
                    PropOpportunityLog.external_id == _stored_opportunity_external_id(
                        external_id, provider
                    ),
                    PropOpportunityLog.stat_type   == stat_type,
                    *(
                        [PropOpportunityLog.provider == provider]
                        if provider else []
                    ),
                )
                .values(
                    alert_sent    = True,
                    alert_sent_at = datetime.utcnow(),
                )
            )
            await s.commit()
            return int(result.rowcount or 0)

    async def get_alerted_opportunity_log(
        self,
        since_hours: int = 72,
        limit: int = 8,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
    ) -> "list[PropOpportunityLog]":
        """
        Return recently delivered 🎯 ACTIONABLE BET PICK alerts, newest first.

        Uses identity-complete successful Telegram PLAY audit rows as the
        canonical source of truth.  Legacy ``alert_sent`` rows without a
        matching audit remain preserved but are not presented as confirmed
        Telegram history.
        """
        from datetime import timedelta
        cutoff = start_at or (datetime.utcnow() - timedelta(hours=since_hours))
        async with self.session() as s:
            audit_rows = await self._confirmed_telegram_play_audits(
                s, since=cutoff
            )
            if end_at is not None:
                audit_rows = [
                    row for row in audit_rows
                    if row.attempted_at is not None and row.attempted_at < end_at
                ]
            if not audit_rows:
                return []

            opportunities = (
                await s.execute(
                    select(PropOpportunityLog).where(
                        PropOpportunityLog.alert_sent == True,  # noqa: E712
                        PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]),
                    )
                )
            ).scalars().all()
            by_identity = {
                (
                    str(row.provider or "Underdog"),
                    _stored_opportunity_external_id(
                        str(row.external_id or ""), row.provider
                    ),
                    str(row.stat_type or ""),
                ): row
                for row in opportunities
            }
            confirmed: list[PropOpportunityLog] = []
            # The audit helper returns chronological rows for aggregation, but
            # /alerts is a newest-first rolling delivery view.
            for audit in reversed(audit_rows):
                key = (
                    str(audit.provider or "Underdog"),
                    _stored_opportunity_external_id(
                        audit.external_id or "", audit.provider
                    ),
                    str(audit.stat_type or ""),
                )
                row = by_identity.get(key)
                if row is None:
                    continue
                # Detach the display object so a legitimate line-change
                # delivery retains that audit's line and acceptance time.
                display_row = SimpleNamespace(**{
                    column.name: getattr(row, column.name)
                    for column in PropOpportunityLog.__table__.columns
                })
                display_row.line_value = audit.line_value
                display_row.alert_sent_at = audit.attempted_at
                confirmed.append(display_row)
                if len(confirmed) >= max(1, int(limit)):
                    break
            return confirmed

    async def get_recent_alerted_props_for_dedup(
        self,
        since_hours: int = 24,
    ) -> "dict[tuple, tuple[float, float]]":
        """
        Return recently alerted props for restoring the in-memory dedup dict
        (_prop_market_alerted) after a bot restart.

        Extended keys are
        ``(player, sport, stat, provider, source_id, event_id, direction)``.
        A compact provider-scoped legacy alias is also returned for older
        recovery callers; active delivery uses the extended identity.

        Primary source: PropOpportunityLog rows where alert_sent=True.
        Fallback source: PropLineHistory rows where first_alert_sent_at is set,
          used when log_prop_opportunity failed silently (DB stress) so no
          PropOpportunityLog row was created — without this, a post-restart scan
          would re-alert the same prop because the dedup dict would be empty.

        Only OVER/UNDER recommendations are included from PropOpportunityLog.
        PropLineHistory entries are included unconditionally (no recommendation
        column) because they represent confirmed deliveries.

        Rows are ordered oldest-first so newer entries overwrite older ones,
        leaving the most-recent alert for each key in the dict.
        """
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            # The delivery ledger is the only restart source.  Legacy
            # alert_sent/lifecycle fields are intentionally not enough to
            # suppress a new PLAY after a restart.
            pol_result = await s.execute(
                select(
                    PropOpportunityLog.player_name,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.provider,
                    PropOpportunityLog.external_id,
                    PropOpportunityLog.recommendation,
                ).where(
                    PropOpportunityLog.alert_sent == True,  # noqa: E712
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                )
            )
            direction_by_identity = {
                (
                    str(provider or "Underdog"),
                    str(external_id or ""),
                    str(stat_type or ""),
                ): str(recommendation or "").upper()
                for (
                    player_name, sport, stat_type, provider, external_id,
                    recommendation,
                ) in pol_result.all()
            }
            audit_rows = await self._confirmed_telegram_play_audits(
                s, since=cutoff
            )
            out: dict = {}
            for audit in audit_rows:
                sent_at = audit.attempted_at
                if sent_at is None:
                    continue
                if hasattr(sent_at, "timestamp"):
                    ts_unix = sent_at.timestamp()
                else:
                    ts_unix = datetime.fromisoformat(
                        str(sent_at).replace("Z", "+00:00")
                    ).timestamp()
                line_f = float(audit.line_value or 0)
                provider_name = str(audit.provider or "Underdog").strip() or "Underdog"
                source_key = str(audit.external_id or "").strip()
                event_key = str(audit.event_id or "").strip()
                direction_key = direction_by_identity.get(
                    (
                        provider_name,
                        _stored_opportunity_external_id(source_key, provider_name),
                        str(audit.stat_type or ""),
                    ),
                    "",
                )
                out[(
                    str(audit.player_name or "").strip(),
                    audit.sport or "UNKNOWN",
                    audit.stat_type,
                    provider_name,
                    source_key,
                    event_key,
                    direction_key,
                )] = (ts_unix, line_f)
                # Compatibility with pre-identity recovery code.
                out[(
                    _dedup_player_key(audit.player_name, provider_name),
                    audit.sport or "UNKNOWN",
                    audit.stat_type,
                )] = (ts_unix, line_f)

            return out

    async def get_telegram_pick_performance(self) -> "dict":
        """
        Return HIT/MISS/PUSH/PENDING counts for Telegram actionable picks only
        (``alert_sent=True``, recommendation in OVER|UNDER).

        Used by /rollups to show 🎯 TELEGRAM PICKS performance separately from the
        broader 'all evaluated props' rollup.

        Returns
        -------
        dict with keys:
          total     — total actionable picks sent (OVER + UNDER)
          hit       — count of HIT results
          miss      — count of MISS results
          push      — count of PUSH results
          pending   — count still PENDING (or VOID/CANCELLED)
          graded    — hit + miss + push
          hit_rate  — float (0.0–100.0) based on graded picks only
        """
        async with self.session() as s:
            confirmed_audits = await self._confirmed_telegram_play_audits(s)
            opportunity_rows = await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.external_id,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.result,
                    PropOpportunityLog.grade_result_source,
                ).where(
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                )
            )
            result_by_identity = {
                (
                    str(provider or "Underdog").strip().lower() or "underdog",
                    _stored_opportunity_external_id(
                        str(external_id or ""), provider
                    ),
                    str(stat_type or "").strip(),
                ): (
                    str(result or "PENDING").upper(),
                    str(provider or "Underdog"),
                    str(grade_result_source or ""),
                )
                for provider, external_id, stat_type, result, grade_result_source
                in opportunity_rows.all()
            }
            buckets: dict = {}
            by_provider: dict = {}
            for audit in confirmed_audits:
                source = str(audit.provider or "Underdog")
                stored_result = result_by_identity.get(
                    (
                        source.strip().lower() or "underdog",
                        _stored_opportunity_external_id(
                            str(audit.external_id or ""), source
                        ),
                        str(audit.stat_type or "").strip(),
                    ),
                    None,
                )
                if stored_result is None:
                    result_val = "PENDING"
                else:
                    result_val, grade_provider, grade_source = stored_result
                    if (
                        result_val in _TERMINAL_GRADE_RESULTS
                        and not is_trusted_directional_grade_source(
                            grade_provider, result_val, grade_source
                        )
                    ):
                        result_val = "UNVERIFIED"
                buckets[result_val] = buckets.get(result_val, 0) + 1
                source_buckets = by_provider.setdefault(source, {})
                source_buckets[result_val] = source_buckets.get(result_val, 0) + 1

        hit     = buckets.get("HIT",  0)
        miss    = buckets.get("MISS", 0)
        push    = buckets.get("PUSH", 0)
        _void_codes = {"VOID", "CANCELLED", "INJURY_VOID", "GAME_INTERRUPTED",
                       "UNRESOLVED", "OUTLIER", "PLATFORM_DIFF"}
        void = sum(n for k, n in buckets.items() if k in _void_codes)
        unverified = buckets.get("UNVERIFIED", 0)
        pending = sum(n for k, n in buckets.items()
                      if k not in ("HIT", "MISS", "PUSH", "UNVERIFIED")
                      and k not in _void_codes)
        graded  = hit + miss + push
        denominator = graded
        total   = graded + pending + void + unverified
        hit_rate = round(hit / denominator * 100, 1) if denominator > 0 else 0.0
        def _provider_summary(source_buckets: dict) -> dict:
            source_hit = source_buckets.get("HIT", 0)
            source_miss = source_buckets.get("MISS", 0)
            source_push = source_buckets.get("PUSH", 0)
            source_pending = sum(
                n for k, n in source_buckets.items()
                if k not in ("HIT", "MISS", "PUSH", "UNVERIFIED")
                and k not in _void_codes
            )
            source_void = sum(n for k, n in source_buckets.items() if k in _void_codes)
            source_unverified = source_buckets.get("UNVERIFIED", 0)
            source_graded = source_hit + source_miss + source_push
            source_denominator = source_graded
            return {
                "total": (
                    source_graded + source_pending + source_void + source_unverified
                ),
                "hit": source_hit,
                "miss": source_miss,
                "push": source_push,
                "void": source_void,
                "pending": source_pending,
                "unverified": source_unverified,
                "graded": source_graded,
                "denominator": source_denominator,
                "hit_rate": round(source_hit / source_denominator * 100, 1)
                if source_denominator > 0 else 0.0,
            }

        return {
            "total":    total,
            "hit":      hit,
            "miss":     miss,
            "push":     push,
            "void":     void,
            "pending":  pending,
            "unverified": unverified,
            "graded":   graded,
            "denominator": denominator,
            "hit_rate": hit_rate,
            "by_provider": {
                source: _provider_summary(source_buckets)
                for source, source_buckets in by_provider.items()
            },
        }

    async def rebuild_adaptive_learning_weights(
        self,
        *,
        min_sample_size: Optional[int] = None,
        min_model_errors: Optional[int] = None,
        max_delta: Optional[float] = None,
        activate: Optional[bool] = None,
    ) -> dict:
        """Build and optionally activate one auditable adaptive-weight version.

        Only directional opportunity rows are considered.  ``HIT`` rows and
        ``MISS`` rows explicitly classified as ``Model`` form the eligible
        sample; pushes and all other miss classifications remain descriptive.
        """
        from engine.adaptive_learning import (
            DEFAULT_MAX_DELTA,
            DEFAULT_MIN_MODEL_ERRORS,
            DEFAULT_MIN_SAMPLE_SIZE,
            build_adaptive_weight,
            sample_from_outcomes,
        )

        try:
            from config import config as app_config
        except Exception:
            app_config = None
        min_sample_size = max(
            1,
            int(
                min_sample_size
                if min_sample_size is not None
                else getattr(
                    app_config,
                    "ADAPTIVE_LEARNING_MIN_SAMPLE_SIZE",
                    DEFAULT_MIN_SAMPLE_SIZE,
                )
            ),
        )
        min_model_errors = max(
            1,
            int(
                min_model_errors
                if min_model_errors is not None
                else getattr(
                    app_config,
                    "ADAPTIVE_LEARNING_MIN_MODEL_ERRORS",
                    DEFAULT_MIN_MODEL_ERRORS,
                )
            ),
        )
        max_delta = max(
            0.0,
            min(
                0.25,
                float(
                    max_delta
                    if max_delta is not None
                    else getattr(
                        app_config,
                        "ADAPTIVE_LEARNING_MAX_DELTA",
                        DEFAULT_MAX_DELTA,
                    )
                ),
            ),
        )
        if activate is None:
            activate = bool(
                getattr(app_config, "ENABLE_LEARNING_UPDATES", False)
            )

        version = "adaptive-v1-" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        grouped: dict[tuple[str, str, str], list[tuple[str, Optional[str]]]] = {}
        async with self.session() as s:
            rows = await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.result,
                    PropOpportunityLog.error_type,
                ).where(
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                    _trusted_directional_grade_filter(),
                )
            )
            for provider, sport, market_type, result, error_type in rows.all():
                key = (
                    str(provider or "Underdog"),
                    str(sport or "UNKNOWN").upper(),
                    str(market_type or "UNKNOWN"),
                )
                grouped.setdefault(key, []).append(
                    (str(result or ""), error_type)
                )

            weights = []
            for (provider, sport, market_type), outcomes in sorted(grouped.items()):
                sample = sample_from_outcomes(
                    provider=provider,
                    sport=sport,
                    market_type=market_type,
                    outcomes=outcomes,
                )
                weights.append(
                    build_adaptive_weight(
                        sample,
                        version=version,
                        min_sample_size=min_sample_size,
                        min_model_errors=min_model_errors,
                        max_delta=max_delta,
                        active=bool(activate),
                    )
                )

            now = datetime.utcnow()
            if activate:
                await s.execute(
                    update(AdaptiveLearningRun).values(active=False)
                )

            run = AdaptiveLearningRun(
                version=version,
                created_at=now,
                activated_at=now if activate else None,
                active=bool(activate),
                min_sample_size=min_sample_size,
                min_model_errors=min_model_errors,
                max_delta=max_delta,
                eligible_groups=sum(1 for item in weights if item.eligible),
                total_groups=len(weights),
                status="ACTIVE" if activate else "DESCRIPTIVE",
            )
            s.add(run)
            for item in weights:
                s.add(
                    AdaptiveLearningWeight(
                        version=item.version,
                        provider=item.provider,
                        sport=item.sport,
                        market_type=item.market_type,
                        sample_size=item.sample_size,
                        eligible_samples=item.eligible_samples,
                        model_errors=item.model_errors,
                        hit_rate=item.hit_rate,
                        multiplier=item.multiplier,
                        status=item.status,
                        created_at=now,
                    )
                )
            await s.commit()

        return {
            "version": version,
            "active": bool(activate),
            "status": "ACTIVE" if activate else "DESCRIPTIVE",
            "min_sample_size": min_sample_size,
            "min_model_errors": min_model_errors,
            "max_delta": max_delta,
            "eligible_groups": sum(1 for item in weights if item.eligible),
            "total_groups": len(weights),
        }

    async def get_adaptive_learning_status(self) -> dict:
        """Return the active version and its provider-aware audit summary."""
        async with self.session() as s:
            active_run = (
                await s.execute(
                    select(AdaptiveLearningRun)
                    .where(AdaptiveLearningRun.active.is_(True))
                    .order_by(AdaptiveLearningRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            latest_run = (
                await s.execute(
                    select(AdaptiveLearningRun)
                    .order_by(AdaptiveLearningRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            run = active_run or latest_run
            if run is None:
                return {
                    "version": None,
                    "active": False,
                    "status": "NOT_INITIALIZED",
                    "weights": [],
                }

            rows = await s.execute(
                select(AdaptiveLearningWeight)
                .where(AdaptiveLearningWeight.version == run.version)
                .order_by(
                    AdaptiveLearningWeight.provider,
                    AdaptiveLearningWeight.sport,
                    AdaptiveLearningWeight.market_type,
                )
            )
            weights = [
                {
                    "provider": row.provider,
                    "sport": row.sport,
                    "market_type": row.market_type,
                    "sample_size": row.sample_size,
                    "eligible_samples": row.eligible_samples,
                    "model_errors": row.model_errors,
                    "hit_rate": row.hit_rate,
                    "multiplier": row.multiplier,
                    "status": row.status,
                }
                for row in rows.scalars().all()
            ]
            return {
                "version": run.version,
                "active": bool(active_run),
                "status": run.status,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "activated_at": (
                    run.activated_at.isoformat() if run.activated_at else None
                ),
                "min_sample_size": run.min_sample_size,
                "min_model_errors": run.min_model_errors,
                "max_delta": run.max_delta,
                "eligible_groups": run.eligible_groups,
                "total_groups": run.total_groups,
                "weights": weights,
            }

    async def get_active_adaptive_weight(
        self,
        provider: str,
        sport: str,
        market_type: str,
    ) -> Optional[dict]:
        """Return one eligible active weight, or None for safe neutral behavior."""
        async with self.session() as s:
            run = (
                await s.execute(
                    select(AdaptiveLearningRun)
                    .where(AdaptiveLearningRun.active.is_(True))
                    .order_by(AdaptiveLearningRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if run is None:
                return None
            row = (
                await s.execute(
                    select(AdaptiveLearningWeight)
                    .where(
                        AdaptiveLearningWeight.version == run.version,
                        AdaptiveLearningWeight.provider == str(provider or "Underdog"),
                        AdaptiveLearningWeight.sport == str(sport or "UNKNOWN").upper(),
                        AdaptiveLearningWeight.market_type == str(market_type or "UNKNOWN"),
                        AdaptiveLearningWeight.status == "ACTIVE",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "version": row.version,
                "provider": row.provider,
                "sport": row.sport,
                "market_type": row.market_type,
                "multiplier": row.multiplier,
                "eligible_samples": row.eligible_samples,
                "model_errors": row.model_errors,
                "hit_rate": row.hit_rate,
            }

    async def get_learning_rollups(
        self,
        enabled_sports: Optional[Iterable[str]] = None,
    ) -> "dict":
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
                    if res == "HIT":   entry["W"] += n
                    elif res == "MISS": entry["L"] += n
                    else:              entry["P"] += n
            for v in acc.values():
                t = v["total"]
                v["denominator"] = t
                v["win_pct"] = round(v["W"] / t * 100, 1) if t else 0.0
            return acc

        graded_filter = _trusted_directional_grade_filter()
        play_filter   = PropOpportunityLog.recommendation.in_(["OVER", "UNDER"])
        enabled_sport_keys = (
            {
                str(sport).strip().upper()
                for sport in enabled_sports
                if str(sport).strip()
            }
            if enabled_sports is not None
            else None
        )
        sport_filter = (
            [func.upper(PropOpportunityLog.sport).in_(enabled_sport_keys)]
            if enabled_sport_keys is not None
            else []
        )

        async with self.session() as s:
            # ── by_tier ──────────────────────────────────────────────────────
            tier_rows = await s.execute(
                select(
                    PropOpportunityLog.decision_tier,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    func.count(PropOpportunityLog.id).label("n"),
                ).where(graded_filter, play_filter, *sport_filter).group_by(
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
                ).where(graded_filter, play_filter, *sport_filter).group_by(
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
                ).where(graded_filter, play_filter, *sport_filter).group_by(
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
                    graded_filter,
                    PropOpportunityLog.result      == "MISS",
                    PropOpportunityLog.error_type.isnot(None),
                    play_filter,
                    *sport_filter,
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
                ).where(graded_filter, play_filter, *sport_filter).group_by(
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
                    if res == "HIT":    entry["W"] += n
                    elif res == "MISS": entry["L"] += n
                    else:              entry["P"] += n
            player_trend = [
                {
                    "player": k[0], "sport": k[1], "stat_type": k[2],
                    "W": v["W"], "L": v["L"], "P": v["P"], "total": v["total"],
                    "win_pct": round(v["W"] / v["total"] * 100, 1)
                              if v["total"] > 0 else 0.0,
                }
                for k, v in sorted(_player_acc.items(), key=lambda kv: -kv[1]["total"])[:10]
            ]

            # ── population counts ─────────────────────────────────────────────
            total_row = await s.execute(
                select(func.count(PropOpportunityLog.id)).where(
                    graded_filter, play_filter, *sport_filter
                )
            )
            total_graded = total_row.scalar() or 0
            unverified_row = await s.execute(
                select(func.count(PropOpportunityLog.id)).where(
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                    PropOpportunityLog.result.in_(tuple(_TERMINAL_GRADE_RESULTS)),
                    ~_trusted_terminal_grade_filter(),
                    *sport_filter,
                )
            )
            unverified_graded = unverified_row.scalar() or 0
            confirmed_audits = await self._confirmed_telegram_play_audits(s)
            graded_identities = {
                (
                    str(provider or "Underdog"),
                    str(external_id or ""),
                    str(stat_type or ""),
                )
                for provider, external_id, stat_type, result
                in (
                    await s.execute(
                        select(
                            PropOpportunityLog.provider,
                            PropOpportunityLog.external_id,
                            PropOpportunityLog.stat_type,
                            PropOpportunityLog.result,
                        ).where(graded_filter, play_filter, *sport_filter)
                    )
                ).all()
            }
            telegram_graded = sum(
                1
                for audit in confirmed_audits
                if (
                    str(audit.provider or "Underdog"),
                    _stored_opportunity_external_id(
                        str(audit.external_id or ""), audit.provider
                    ),
                    str(audit.stat_type or ""),
                ) in graded_identities
            )

            intel_row = await s.execute(
                select(
                    func.count(PropOpportunityLog.id),
                    func.avg(PropOpportunityLog.sharp_confidence),
                    func.avg(PropOpportunityLog.evidence_completeness),
                    func.count(PropOpportunityLog.sharp_confidence),
                ).where(graded_filter, play_filter, *sport_filter)
            )
            _intel_n, _avg_sharp, _avg_evidence, _sharp_n = intel_row.one()

        adaptive_status = await self.get_adaptive_learning_status()
        return {
            "by_tier":      by_tier,
            "by_sport":     by_sport,
            "by_stat_type": by_stat_type,
            "by_error_type": by_error_type,
            "player_trend": player_trend,
            "total_graded": total_graded,
            "unverified_graded": unverified_graded,
            # ``total_graded`` is intentionally delivery-independent so
            # historical evaluation data remains available.  These explicit
            # counters prevent it from being mistaken for Telegram PLAY data.
            "telegram_graded": telegram_graded,
            "historical_legacy_graded": max(0, total_graded - telegram_graded),
            "intelligence": {
                "sample_size": int(_intel_n or 0),
                "sharp_confidence_avg": round(float(_avg_sharp), 1) if _avg_sharp is not None else None,
                "evidence_completeness_avg": round(float(_avg_evidence), 1) if _avg_evidence is not None else None,
                "sharp_confidence_samples": int(_sharp_n or 0),
                "calibration_minimum": 5,
                "calibrated": int(_intel_n or 0) >= 5,
            },
            "adaptive_learning": adaptive_status,
        }

    async def get_game_results_for_grading_bulk(
        self,
        opportunities: "list[object]",
    ) -> "dict[int, tuple[Optional[PlayerGameResult], Optional[str]]]":
        """Resolve a pending grading page with bounded bulk reads.

        The single-row resolver remains the compatibility API.  The scheduled
        grader uses this method for large pages so settlement does not acquire
        a SQLite session and issue a query for every opportunity.

        Matching stays strict: a dated opportunity requires that date (or the
        provider-local previous date already allowed by the grader), while an
        undated opportunity requires its event identity. Underdog accepts only
        explicit PropLine settlement rows and keeps supplied event IDs strict;
        other providers may use the existing dated fallback.
        """
        if not opportunities:
            return {}

        composite_components = {
            "hits + runs + rbis": ("hits", "runs", "rbis"),
            "hits+runs+rbis": ("hits", "runs", "rbis"),
            "points + rebounds + assists": ("points", "rebounds", "assists"),
            "pts+reb+ast": ("points", "rebounds", "assists"),
            "pts + reb + ast": ("points", "rebounds", "assists"),
            "points + rebounds": ("points", "rebounds"),
            "pts+reb": ("points", "rebounds"),
            "points + assists": ("points", "assists"),
            "pts+ast": ("points", "assists"),
            "rebounds + assists": ("rebounds", "assists"),
            "reb+ast": ("rebounds", "assists"),
            "goals + assists": ("goals", "assists"),
            "g+a": ("goals", "assists"),
        }

        def _metadata(opp):
            game_date = (
                opp.game_time.strftime("%Y-%m-%d")
                if getattr(opp, "game_time", None)
                else None
            )
            if (
                not game_date
                and getattr(opp, "game_id", "")
                and getattr(opp, "detected_at", None)
            ):
                game_date = opp.detected_at.strftime("%Y-%m-%d")
            dates = [game_date] if game_date else []
            if game_date and getattr(opp, "game_time", None):
                previous = (
                    opp.game_time - timedelta(days=1)
                ).strftime("%Y-%m-%d")
                if previous != game_date:
                    dates.append(previous)
            stat = str(getattr(opp, "stat_type", "") or "").lower().strip()
            stats = set(composite_components.get(stat, (stat,)))
            stats.add(stat)
            return (
                str(getattr(opp, "player_name", "") or ""),
                str(getattr(opp, "sport", "") or "").upper().strip(),
                stat,
                dates,
                str(getattr(opp, "game_id", "") or ""),
                str(getattr(opp, "provider", None) or "").strip().casefold(),
                stats,
            )

        metadata = {
            int(getattr(opp, "id", 0) or 0): (opp, _metadata(opp))
            for opp in opportunities
            if getattr(opp, "id", None) is not None
        }
        if not metadata:
            return {}

        # SQLite's bind limit is lower than the largest grading page. Chunk
        # player names and event IDs while keeping each query a bulk read.
        player_names = {
            info[1][0]
            for info in metadata.values()
            if info[1][0]
        }
        sports = {info[1][1] for info in metadata.values() if info[1][1]}
        dates = {
            date
            for info in metadata.values()
            for date in info[1][3]
            if date
        }
        stats = {
            stat
            for info in metadata.values()
            for stat in info[1][6]
            if stat
        }
        event_ids = {
            info[1][4]
            for info in metadata.values()
            if info[1][4]
        }

        rows: list[PlayerGameResult] = []
        async with self.session() as s:
            player_list = sorted(player_names)
            for offset in range(0, len(player_list), 350):
                player_chunk = player_list[offset : offset + 350]
                if not player_chunk or not dates:
                    continue
                result = await s.execute(
                    select(PlayerGameResult).where(
                        # Provider upserts already normalize these columns.
                        # Keep the SQL predicates index-friendly; the
                        # in-memory keys below retain case-insensitive
                        # provider reconciliation.
                        PlayerGameResult.player_name.in_(player_chunk),
                        PlayerGameResult.sport.in_(sports),
                        PlayerGameResult.stat_type.in_(stats),
                        PlayerGameResult.game_date.in_(dates),
                    ).order_by(PlayerGameResult.id.desc())
                )
                rows.extend(result.scalars().all())

            event_list = sorted(event_ids)
            for offset in range(0, len(event_list), 350):
                event_chunk = event_list[offset : offset + 350]
                if not event_chunk:
                    continue
                result = await s.execute(
                    select(PlayerGameResult).where(
                        PlayerGameResult.game_id.in_(event_chunk),
                    ).order_by(PlayerGameResult.id.desc())
                )
                rows.extend(result.scalars().all())

        by_date: dict[tuple[str, str, str, str], list[PlayerGameResult]] = {}
        by_event: dict[tuple[str, str, str, str], list[PlayerGameResult]] = {}
        for row in rows:
            player_key = str(row.player_name or "").lower().strip()
            sport_key = str(row.sport or "").upper().strip()
            stat_key = str(row.stat_type or "").lower().strip()
            date_key = str(row.game_date or "")
            by_date.setdefault(
                (player_key, sport_key, stat_key, date_key), []
            ).append(row)
            if row.game_id:
                by_event.setdefault(
                    (player_key, sport_key, stat_key, str(row.game_id)), []
                ).append(row)

        resolved: dict[int, tuple[Optional[PlayerGameResult], Optional[str]]] = {}
        for opp_id, (opp, info) in metadata.items():
            player_name, sport, stat, candidate_dates, game_id, provider, _ = info
            is_underdog = not provider or provider == "underdog"
            player_key = player_name.lower().strip()
            event_row = None
            if game_id:
                for row in by_event.get((player_key, sport, stat, game_id), []):
                    if not is_gradeable_player_result_source(provider, row.source):
                        continue
                    event_row = row
                    break
                if event_row is not None:
                    resolved[opp_id] = (event_row, event_row.game_date)
                    continue

            # A direct dated match is valid even when the source event ID is
            # absent. With a provider, this also covers cross-provider IDs.
            for candidate_date in candidate_dates:
                dated_rows = by_date.get(
                    (player_key, sport, stat, candidate_date), []
                )
                if is_underdog:
                    dated_rows = [
                        row
                        for row in dated_rows
                        if is_gradeable_player_result_source(provider, row.source)
                        and (
                            not game_id
                            or str(row.game_id or "") == game_id
                        )
                    ]
                else:
                    dated_rows = [
                        row
                        for row in dated_rows
                        if is_gradeable_player_result_source(provider, row.source)
                        and (
                            not game_id
                            or str(row.game_id or "") == game_id
                        )
                    ]
                if dated_rows:
                    resolved[opp_id] = (dated_rows[0], candidate_date)
                    break
            if opp_id in resolved:
                continue

            components = composite_components.get(stat)
            if not components or not provider or is_underdog:
                continue
            for candidate_date in candidate_dates:
                component_rows = {}
                for component in components:
                    eligible_components = [
                        row
                        for row in by_date.get(
                            (player_key, sport, component, candidate_date), []
                        )
                        if is_gradeable_player_result_source(provider, row.source)
                    ]
                    if eligible_components:
                        component_rows[component] = eligible_components[0]
                if len(component_rows) != len(components):
                    continue
                selected = [component_rows[component] for component in components]
                resolved[opp_id] = (
                    PlayerGameResult(
                        player_name=player_name,
                        sport=sport,
                        stat_type=stat,
                        game_date=candidate_date,
                        opponent=next(
                            (row.opponent for row in selected if row.opponent),
                            None,
                        ),
                        actual_value=sum(
                            float(row.actual_value) for row in selected
                        ),
                        source="derived_from_db",
                        game_id=next(
                            (str(row.game_id) for row in selected if row.game_id),
                            "",
                        ),
                    ),
                    candidate_date,
                )
                break
        return resolved

    async def get_game_result_for_grading(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        game_date: "Optional[str]",
        *,
        game_id: str = "",
        provider: Optional[str] = None,
    ) -> "Optional[PlayerGameResult]":
        """
        Return the PlayerGameResult for a given player / sport / stat / date and
        optional event identity, or None. Underdog opportunities accept only
        explicit PropLine settlement rows; other providers keep their existing
        dated-stat lookup behavior.

        stat_type is normalised to lowercase before lookup because upsert_player_result
        stores all stat types in lowercase.  This prevents silent mismatches between
        Underdog's mixed-case stat names ("Points") and the stored values ("points").

        If a source provider supplies an event ID that differs from the result
        provider's event ID, retry by player / sport / stat / date without the
        source ID. This is safe because the date is still required for the
        cross-provider fallback.
        """
        normalised_stat = stat_type.lower().strip()
        is_underdog = str(provider or "").strip().casefold() == "underdog"
        async with self.session() as s:
            filters = [
                PlayerGameResult.player_name == player_name,
                PlayerGameResult.sport       == sport,
                PlayerGameResult.stat_type   == normalised_stat,
            ]
            if is_underdog:
                filters.append(
                    func.lower(PlayerGameResult.source).in_(
                        tuple(_UNDERDOG_SETTLEMENT_RESULT_SOURCES)
                    )
                )
            else:
                filters.extend(
                    [
                        func.lower(PlayerGameResult.source) != "",
                        func.lower(PlayerGameResult.source).notin_(
                            tuple(_UNTRUSTED_PLAYER_RESULT_SOURCES)
                        ),
                    ]
                )
            if game_date:
                filters.append(PlayerGameResult.game_date == game_date)
            # Some stat providers do not expose an event ID. Requiring an
            # empty game_id in that case silently misses valid official stats.
            if game_id:
                filters.append(PlayerGameResult.game_id == str(game_id))
            if not game_date and not game_id:
                return None
            result = await s.execute(
                select(PlayerGameResult)
                .where(*filters)
                .order_by(PlayerGameResult.id.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            # Keep direct event-identity callers strict. The dated fallback is
            # for provider-scoped grading, where source and official result
            # providers are known to use different event-ID namespaces.
            if row is not None:
                return row

            if game_date and provider and not (is_underdog and game_id):
                # Other providers may reconcile distinct event-ID namespaces
                # by date. Underdog keeps a supplied event ID strict.
                fallback_filters = [
                    func.lower(PlayerGameResult.player_name)
                    == str(player_name).lower().strip(),
                    func.upper(PlayerGameResult.sport)
                    == str(sport).upper().strip(),
                    func.lower(PlayerGameResult.stat_type)
                    == normalised_stat,
                    PlayerGameResult.game_date == game_date,
                ]
                if is_underdog:
                    fallback_filters.append(
                        func.lower(PlayerGameResult.source).in_(
                            tuple(_UNDERDOG_SETTLEMENT_RESULT_SOURCES)
                        )
                    )
                fallback = await s.execute(
                    select(PlayerGameResult)
                    .where(*fallback_filters)
                    .order_by(PlayerGameResult.id.desc())
                    .limit(1)
                )
                row = fallback.scalar_one_or_none()
                if row is not None:
                    return row

            # Some providers persist the component stats but cannot return a
            # composite market label (for example MLB H+R+RBI). Derive that
            # market only when every named component exists for the same
            # player, sport, and completed game date. This is database-backed
            # evidence, not a player/stat-only guess.
            composite_components = {
                "hits + runs + rbis": ("hits", "runs", "rbis"),
                "hits+runs+rbis": ("hits", "runs", "rbis"),
                "points + rebounds + assists": ("points", "rebounds", "assists"),
                "pts+reb+ast": ("points", "rebounds", "assists"),
                "pts + reb + ast": ("points", "rebounds", "assists"),
                "points + rebounds": ("points", "rebounds"),
                "pts+reb": ("points", "rebounds"),
                "points + assists": ("points", "assists"),
                "pts+ast": ("points", "assists"),
                "rebounds + assists": ("rebounds", "assists"),
                "reb+ast": ("rebounds", "assists"),
                "goals + assists": ("goals", "assists"),
                "g+a": ("goals", "assists"),
            }
            components = composite_components.get(normalised_stat)
            if is_underdog or not components or not game_date or not provider:
                return None

            component_result = await s.execute(
                select(PlayerGameResult)
                .where(
                    func.lower(PlayerGameResult.player_name)
                    == str(player_name).lower().strip(),
                    func.upper(PlayerGameResult.sport)
                    == str(sport).upper().strip(),
                    func.lower(PlayerGameResult.stat_type).in_(components),
                    PlayerGameResult.game_date == game_date,
                )
                .order_by(PlayerGameResult.id.desc())
            )
            component_rows = component_result.scalars().all()
            by_stat: dict[str, PlayerGameResult] = {}
            for component_row in component_rows:
                key = str(component_row.stat_type or "").lower().strip()
                by_stat.setdefault(key, component_row)
            if any(component not in by_stat for component in components):
                return None

            selected = [by_stat[component] for component in components]
            return PlayerGameResult(
                player_name=player_name,
                sport=str(sport).upper().strip(),
                stat_type=normalised_stat,
                game_date=game_date,
                opponent=next(
                    (row.opponent for row in selected if row.opponent),
                    None,
                ),
                actual_value=sum(float(row.actual_value) for row in selected),
                source="derived_from_db",
                game_id=next(
                    (str(row.game_id) for row in selected if row.game_id),
                    "",
                ),
            )

    async def get_tracking_summary(self) -> "dict":
        """
        Return monitoring and directional performance statistics.

        Returns a dict with:
          tracked:  current active provider universe (latest source row only)
          counts:   directional { recommendation → { result → count } }
          by_sport/by_provider: directional result breakdowns
          total:     directional rows ever logged (compatibility field)
          actionable/delivered/resolved: separate directional populations

        Historical non-directional rows are represented by ``tracked`` when
        they are current provider observations; they are never inferred from
        delivery or from repeated candidate/evaluation rows.
        """
        now = datetime.utcnow()
        async with self.session() as s:
            directional = PropOpportunityLog.recommendation.in_(["OVER", "UNDER"])
            counts: dict = {}
            by_sport: dict = {}
            by_provider: dict = {}
            directional_rows = await s.execute(
                select(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    PropOpportunityLog.grade_result_source,
                    func.count(),
                ).where(directional)
                .group_by(
                    PropOpportunityLog.provider,
                    PropOpportunityLog.sport,
                    PropOpportunityLog.recommendation,
                    PropOpportunityLog.result,
                    PropOpportunityLog.grade_result_source,
                )
            )
            total = 0
            pending = 0
            resolved = 0
            unverified = 0
            delivered = 0
            provider_delivered: dict[str, int] = {}
            directional_by_provider: dict[str, int] = {}
            directional_by_sport: dict[str, int] = {}
            pending_by_provider: dict[str, int] = {}
            resolved_by_provider: dict[str, int] = {}
            unverified_by_provider: dict[str, int] = {}
            result_counts: dict[str, int] = {}
            result_counts_by_provider: dict[str, dict[str, int]] = {}
            result_counts_by_sport: dict[str, dict[str, int]] = {}
            resolved_results = {
                "HIT", "MISS", "PUSH", "VOID", "CANCELLED",
                "INJURY_VOID", "GAME_INTERRUPTED",
            }
            for provider, sport, rec, res, grade_source, row_count in directional_rows.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                row_count = int(row_count or 0)
                provider_key = str(provider or "Underdog")
                recommendation_key = str(rec or "").upper()
                result_key = str(res or "PENDING").upper()
                if (
                    result_key in _TERMINAL_GRADE_RESULTS
                    and not is_trusted_directional_grade_source(
                        provider_key, result_key, grade_source
                    )
                ):
                    result_key = "UNVERIFIED"
                    unverified += row_count
                    unverified_by_provider[provider_key] = (
                        unverified_by_provider.get(provider_key, 0) + row_count
                    )
                counts.setdefault(recommendation_key, {})[result_key] = (
                    counts.setdefault(recommendation_key, {}).get(result_key, 0) + row_count
                )
                by_sport.setdefault(sport, {}).setdefault(
                    recommendation_key, {}
                )[result_key] = (
                    by_sport.setdefault(sport, {}).setdefault(
                        recommendation_key, {}
                    ).get(result_key, 0) + row_count
                )
                by_provider.setdefault(provider_key, {}).setdefault(
                    recommendation_key, {}
                )[result_key] = (
                    by_provider.setdefault(provider_key, {}).setdefault(
                        recommendation_key, {}
                    ).get(result_key, 0) + row_count
                )
                directional_by_provider[provider_key] = (
                    directional_by_provider.get(provider_key, 0) + row_count
                )
                sport_key = str(sport or "UNKNOWN")
                directional_by_sport[sport_key] = (
                    directional_by_sport.get(sport_key, 0) + row_count
                )
                result_counts[result_key] = result_counts.get(result_key, 0) + row_count
                provider_results = result_counts_by_provider.setdefault(provider_key, {})
                provider_results[result_key] = provider_results.get(result_key, 0) + row_count
                sport_results = result_counts_by_sport.setdefault(sport_key, {})
                sport_results[result_key] = sport_results.get(result_key, 0) + row_count
                total += row_count
                if result_key == "PENDING":
                    pending += row_count
                    pending_by_provider[provider_key] = (
                        pending_by_provider.get(provider_key, 0) + row_count
                    )
                elif result_key in resolved_results:
                    resolved += row_count
                    resolved_by_provider[provider_key] = (
                        resolved_by_provider.get(provider_key, 0) + row_count
                    )

            # Delivery is queried separately because the tracking query above
            # intentionally keeps only the fields needed for outcome counts.
            # A provider/opportunity flag alone is not Telegram confirmation.
            delivered_by_sport: dict[str, int] = {}
            from reporting import current_reporting_day_bounds
            today_cutoff, _today_end = current_reporting_day_bounds()
            delivered_today_by_provider: dict[str, int] = {}
            for audit in await self._confirmed_telegram_play_audits(s):
                if not _is_supported_tracking_sport(audit.sport):
                    continue
                provider_key = str(audit.provider or "Underdog")
                sport_key = str(audit.sport or "UNKNOWN")
                delivered += 1
                provider_delivered[provider_key] = (
                    provider_delivered.get(provider_key, 0) + 1
                )
                delivered_by_sport[sport_key] = (
                    delivered_by_sport.get(sport_key, 0) + 1
                )
                if audit.attempted_at is not None and audit.attempted_at >= today_cutoff:
                    delivered_today_by_provider[provider_key] = (
                        delivered_today_by_provider.get(provider_key, 0) + 1
                    )

            # Persistent observations are a separate population from
            # directional opportunities and from Telegram deliveries. Count
            # them independently so a reporting command cannot imply that
            # only evaluated or sent props were monitored.
            observation_rows = await s.execute(
                select(
                    PropLineHistory.provider,
                    PropLineHistory.sport,
                    func.count(PropLineHistory.id),
                )
                .where(PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS))
                .group_by(PropLineHistory.provider, PropLineHistory.sport)
            )
            tracked_observations_by_provider: dict[str, int] = {}
            for provider, sport, count in observation_rows.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider_key = str(provider or "Unknown")
                tracked_observations_by_provider[provider_key] = (
                    tracked_observations_by_provider.get(provider_key, 0)
                    + int(count or 0)
                )

            # Current tracking is latest-row-per-provider-source, not the
            # append-only history row count. A latest removal marker removes
            # that source from the active universe without resurrecting older
            # lines.
            current_ids = (
                select(func.max(PropLineHistory.id))
                .where(PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS))
                .group_by(
                    PropLineHistory.provider,
                    PropLineHistory.player_name,
                    PropLineHistory.sport,
                    PropLineHistory.stat_type,
                    case(
                        (
                            and_(
                                PropLineHistory.external_id.isnot(None),
                                PropLineHistory.external_id != "",
                            ),
                            PropLineHistory.external_id,
                        ),
                        else_=PropLineHistory.game_id,
                    ),
                )
            )
            current_rows = await s.execute(
                select(
                    PropLineHistory.provider,
                    PropLineHistory.sport,
                    func.count(PropLineHistory.id),
                ).where(
                    PropLineHistory.id.in_(current_ids),
                    or_(
                        PropLineHistory.removed == False,  # noqa: E712
                        PropLineHistory.removed.is_(None),
                    ),
                    or_(
                        PropLineHistory.game_time.is_(None),
                        PropLineHistory.game_time > now,
                    ),
                ).group_by(
                    PropLineHistory.provider,
                    PropLineHistory.sport,
                )
            )
            active_by_provider: dict[str, int] = {}
            active_by_sport: dict[str, int] = {}
            for provider, sport, row_count in current_rows.all():
                if not _is_supported_tracking_sport(sport):
                    continue
                provider_key = str(provider or "Unknown")
                sport_key = str(sport or "UNKNOWN")
                row_count = int(row_count or 0)
                active_by_provider[provider_key] = (
                    active_by_provider.get(provider_key, 0) + row_count
                )
                active_by_sport[sport_key] = (
                    active_by_sport.get(sport_key, 0) + row_count
                )
            tracked = sum(active_by_provider.values())
            observations_by_sport = {
                str(sport or "UNKNOWN"): int(count or 0)
                for sport, count in (
                    await s.execute(
                        select(
                            PropLineHistory.sport,
                            func.count(PropLineHistory.id),
                        )
                        .where(
                            PropLineHistory.provider.in_(_PLAYER_PROP_PROVIDERS)
                        )
                        .group_by(PropLineHistory.sport)
                    )
                ).all()
                if _is_supported_tracking_sport(sport)
            }

        return {
            "counts":   counts,
            "by_sport": by_sport,
            "by_provider": by_provider,
            "active_by_provider": active_by_provider,
            "active_by_sport": active_by_sport,
            "tracked": tracked,
            "active_tracked": tracked,
            "tracked_observations": sum(tracked_observations_by_provider.values()),
            "historical_observations": sum(tracked_observations_by_provider.values()),
            "tracked_observations_by_provider": tracked_observations_by_provider,
            "historical_observations_by_provider": tracked_observations_by_provider,
            "observations_by_sport": observations_by_sport,
            "total":    total,
            "unverified": unverified,
            "unverified_by_provider": unverified_by_provider,
            "directional_total": total,
            "directional_by_provider": directional_by_provider,
            "directional_by_sport": directional_by_sport,
            "actionable": total,
            "actionable_total": total,
            "delivered": delivered,
            "provider_delivered": provider_delivered,
            "delivered_by_provider": provider_delivered,
            "delivered_by_sport": delivered_by_sport,
            "delivered_today_by_provider": delivered_today_by_provider,
            "resolved": resolved,
            "pending":  pending,
            "pending_by_provider": pending_by_provider,
            "resolved_by_provider": resolved_by_provider,
            "result_counts": result_counts,
            "result_counts_by_provider": result_counts_by_provider,
            "result_counts_by_sport": result_counts_by_sport,
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

    # ── Slip Journal CRUD (P9/P10) ───────────────────────────────────────────

    async def claim_auto_slip_notification(
        self,
        signature: str,
        slip_size: int,
        *,
        combination_signature: str = "",
        legs: Optional[list[dict]] = None,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> bool:
        """Claim a send and persist its exact legs, or reclaim a stale send."""
        now = datetime.utcnow()
        cutoff = now - stale_after
        async with self.session() as s:
            result = await s.execute(
                select(AutoSlipNotification).where(
                    AutoSlipNotification.signature == signature
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                if existing.status != "SENDING":
                    return False
                reclaimed = await s.execute(
                    update(AutoSlipNotification)
                    .where(
                        AutoSlipNotification.signature == signature,
                        AutoSlipNotification.status == "SENDING",
                        AutoSlipNotification.created_at < cutoff,
                    )
                    .values(created_at=now, slip_size=slip_size, sent_at=None)
                )
                if reclaimed.rowcount != 1:
                    await s.rollback()
                    return False
            else:
                s.add(
                    AutoSlipNotification(
                        signature=signature,
                        slip_size=slip_size,
                        status="SENDING",
                        created_at=now,
                    )
                )

            if legs and combination_signature:
                existing_legs = await s.execute(
                    select(func.count())
                    .select_from(AutoSlipNotificationLeg)
                    .where(
                        AutoSlipNotificationLeg.notification_signature
                        == signature
                    )
                )
                if not (existing_legs.scalar() or 0):
                    for index, leg in enumerate(legs):
                        s.add(
                            AutoSlipNotificationLeg(
                                notification_signature=signature,
                                combination_signature=combination_signature,
                                leg_index=index,
                                provider=str(leg.get("provider") or "Underdog"),
                                external_id=str(leg.get("external_id") or ""),
                                game_id=str(leg.get("game_id") or ""),
                                player_name=str(leg.get("player_name") or ""),
                                sport=str(leg.get("sport") or ""),
                                stat_type=str(leg.get("stat_type") or ""),
                                line_value=(
                                    float(leg["line_value"])
                                    if leg.get("line_value") is not None
                                    else None
                                ),
                                direction=str(leg.get("direction") or "").upper(),
                                game_time=leg.get("game_time"),
                            )
                        )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return False
            return True

    async def mark_auto_slip_notification_sent(self, signature: str) -> bool:
        """Mark a claimed notification delivered after Telegram accepts it."""
        async with self.session() as s:
            result = await s.execute(
                update(AutoSlipNotification)
                .where(
                    AutoSlipNotification.signature == signature,
                    AutoSlipNotification.status == "SENDING",
                )
                .values(status="SENT", sent_at=datetime.utcnow())
            )
            await s.commit()
            return result.rowcount == 1

    async def release_auto_slip_notification(self, signature: str) -> None:
        """Release an unsent claim so the next completed scan can retry it."""
        async with self.session() as s:
            await s.execute(
                delete(AutoSlipNotificationLeg).where(
                    AutoSlipNotificationLeg.notification_signature == signature
                )
            )
            await s.execute(
                delete(AutoSlipNotification).where(
                    AutoSlipNotification.signature == signature,
                    AutoSlipNotification.status == "SENDING",
                )
            )
            await s.commit()

    async def backfill_auto_slip_notification_legs(
        self,
        signature: str,
        combination_signature: str,
        legs: list[dict],
    ) -> bool:
        """Attach current exact leg data to a matching legacy sent notification."""
        if not legs or not combination_signature:
            return False
        async with self.session() as s:
            result = await s.execute(
                select(AutoSlipNotification).where(
                    AutoSlipNotification.signature == signature,
                    AutoSlipNotification.status == "SENT",
                )
            )
            if result.scalar_one_or_none() is None:
                return False
            count = await s.execute(
                select(func.count())
                .select_from(AutoSlipNotificationLeg)
                .where(
                    AutoSlipNotificationLeg.notification_signature == signature
                )
            )
            if count.scalar():
                return False
            for index, leg in enumerate(legs):
                s.add(
                    AutoSlipNotificationLeg(
                        notification_signature=signature,
                        combination_signature=combination_signature,
                        leg_index=index,
                        provider=str(leg.get("provider") or "Underdog"),
                        external_id=str(leg.get("external_id") or ""),
                        game_id=str(leg.get("game_id") or ""),
                        player_name=str(leg.get("player_name") or ""),
                        sport=str(leg.get("sport") or ""),
                        stat_type=str(leg.get("stat_type") or ""),
                        line_value=(
                            float(leg["line_value"])
                            if leg.get("line_value") is not None
                            else None
                        ),
                        direction=str(leg.get("direction") or "").upper(),
                        game_time=leg.get("game_time"),
                    )
                )
            await s.commit()
            return True

    async def grade_auto_slip_notification_legs(self) -> int:
        """Grade pending automatic-slip legs from already persisted settlements."""
        now = datetime.utcnow()
        async with self.session() as s:
            pending_result = await s.execute(
                select(AutoSlipNotificationLeg)
                .join(
                    AutoSlipNotification,
                    AutoSlipNotification.signature
                    == AutoSlipNotificationLeg.notification_signature,
                )
                .where(
                    AutoSlipNotification.status == "SENT",
                    AutoSlipNotificationLeg.result == "PENDING",
                )
            )
            pending = list(pending_result.scalars().all())
            if not pending:
                return 0

            keys = {
                (
                    str(leg.player_name or "").strip().casefold(),
                    str(leg.sport or "").strip().upper(),
                    str(leg.stat_type or "").strip().casefold(),
                )
                for leg in pending
            }
            result_rows_by_key: dict[tuple[str, str, str], list[PlayerGameResult]] = {}
            key_list = list(keys)
            trusted_sources = tuple(
                _UNDERDOG_SETTLEMENT_RESULT_SOURCES | _UNDERDOG_VOID_RESULT_SOURCES
            )
            for offset in range(0, len(key_list), 200):
                batch = key_list[offset : offset + 200]
                result = await s.execute(
                    select(PlayerGameResult).where(
                        tuple_(
                            func.lower(func.trim(PlayerGameResult.player_name)),
                            func.upper(func.trim(PlayerGameResult.sport)),
                            func.lower(func.trim(PlayerGameResult.stat_type)),
                        ).in_(batch),
                        func.lower(func.trim(PlayerGameResult.source)).in_(
                            trusted_sources
                        ),
                    )
                )
                for row in result.scalars().all():
                    key = (
                        str(row.player_name or "").strip().casefold(),
                        str(row.sport or "").strip().upper(),
                        str(row.stat_type or "").strip().casefold(),
                    )
                    result_rows_by_key.setdefault(key, []).append(row)

            voids_by_identity: dict[tuple[str, str, str], list[PropOpportunityLog]] = {}
            external_ids = {
                str(leg.external_id or "").strip()
                for leg in pending
                if str(leg.external_id or "").strip()
            }
            if external_ids:
                external_id_list = list(external_ids)
                for offset in range(0, len(external_id_list), 200):
                    void_result = await s.execute(
                        select(PropOpportunityLog).where(
                            PropOpportunityLog.external_id.in_(
                                external_id_list[offset : offset + 200]
                            ),
                            func.lower(
                                func.trim(
                                    func.coalesce(PropOpportunityLog.provider, "")
                                )
                            )
                            == "underdog",
                            func.upper(func.trim(PropOpportunityLog.result)).in_(
                                tuple(_VOID_GRADE_RESULTS)
                            ),
                            func.lower(
                                func.trim(
                                    func.coalesce(
                                        PropOpportunityLog.grade_result_source, ""
                                    )
                                )
                            ).in_(tuple(_UNDERDOG_VOID_RESULT_SOURCES)),
                        )
                    )
                    for row in void_result.scalars().all():
                        key = (
                            str(row.external_id or "").strip(),
                            str(row.stat_type or "").strip().casefold(),
                            str(row.game_id or "").strip(),
                        )
                        voids_by_identity.setdefault(key, []).append(row)

            graded = 0
            for leg in pending:
                if str(leg.provider or "").strip().casefold() != "underdog":
                    continue
                event_id = str(leg.game_id or "").strip()
                if event_id and leg.external_id:
                    void_rows = voids_by_identity.get(
                        (
                            str(leg.external_id).strip(),
                            str(leg.stat_type or "").strip().casefold(),
                            event_id,
                        ),
                        [],
                    )
                    if void_rows:
                        leg.result = "VOID"
                        leg.actual_value = None
                        leg.grade_result_source = str(
                            void_rows[0].grade_result_source or ""
                        )
                        leg.graded_at = now
                        graded += 1
                        continue

                key = (
                    str(leg.player_name or "").strip().casefold(),
                    str(leg.sport or "").strip().upper(),
                    str(leg.stat_type or "").strip().casefold(),
                )
                candidate_rows = result_rows_by_key.get(key, [])
                if event_id:
                    matching = [
                        row
                        for row in candidate_rows
                        if str(row.game_id or "").strip() == event_id
                    ]
                else:
                    game_time = leg.game_time
                    valid_dates = (
                        {
                            game_time.strftime("%Y-%m-%d"),
                            (game_time - timedelta(days=1)).strftime("%Y-%m-%d"),
                        }
                        if game_time is not None
                        else set()
                    )
                    matching = [
                        row
                        for row in candidate_rows
                        if str(row.game_date or "") in valid_dates
                    ]
                if not matching:
                    continue
                row = max(
                    matching,
                    key=lambda candidate: (
                        (
                            candidate.fetched_at.isoformat()
                            if candidate.fetched_at
                            else ""
                        ),
                        candidate.id or 0,
                    ),
                )
                source = str(row.source or "").strip().casefold()
                if source in _UNDERDOG_VOID_RESULT_SOURCES:
                    leg.result = "VOID"
                    leg.actual_value = None
                else:
                    if leg.line_value is None:
                        continue
                    actual = float(row.actual_value)
                    if abs(actual - float(leg.line_value)) < 0.01:
                        outcome = "PUSH"
                    elif str(leg.direction or "").upper() == "UNDER":
                        outcome = "HIT" if actual < float(leg.line_value) else "MISS"
                    elif str(leg.direction or "").upper() == "OVER":
                        outcome = "HIT" if actual > float(leg.line_value) else "MISS"
                    else:
                        continue
                    leg.result = outcome
                    leg.actual_value = actual
                leg.grade_result_source = source
                leg.graded_at = now
                graded += 1

            if graded:
                await s.commit()
            return graded

    @staticmethod
    def _auto_slip_combination_result(legs: list[dict]) -> str:
        results = {str(leg.get("result") or "PENDING").upper() for leg in legs}
        if "MISS" in results:
            return "MISS"
        if "PENDING" in results or not results:
            return "PENDING"
        if "VOID" in results:
            return "VOID"
        if "PUSH" in results:
            return "PUSH"
        if results == {"HIT"}:
            return "HIT"
        return "PENDING"

    async def _get_auto_slip_notification_combinations(self) -> list[dict]:
        async with self.session() as s:
            result = await s.execute(
                select(AutoSlipNotification, AutoSlipNotificationLeg)
                .join(
                    AutoSlipNotificationLeg,
                    AutoSlipNotificationLeg.notification_signature
                    == AutoSlipNotification.signature,
                )
                .where(AutoSlipNotification.status == "SENT")
                .order_by(
                    desc(AutoSlipNotification.sent_at),
                    AutoSlipNotificationLeg.leg_index,
                )
            )
            rows = result.all()

        grouped: dict[str, dict] = {}
        result_priority = {"PENDING": 0, "VOID": 1, "HIT": 2, "MISS": 2, "PUSH": 2}
        for notification, leg in rows:
            combination = str(leg.combination_signature or "")
            if not combination:
                continue
            group = grouped.setdefault(
                combination,
                {
                    "combination_signature": combination,
                    "slip_size": notification.slip_size,
                    "created_at": notification.created_at,
                    "sent_at": notification.sent_at,
                    "_legs": {},
                },
            )
            if notification.sent_at and (
                group["sent_at"] is None or notification.sent_at < group["sent_at"]
            ):
                group["sent_at"] = notification.sent_at
            existing_leg = group["_legs"].get(leg.leg_index)
            leg_record = {
                "leg_index": leg.leg_index,
                "provider": leg.provider,
                "external_id": leg.external_id,
                "game_id": leg.game_id,
                "player_name": leg.player_name,
                "sport": leg.sport,
                "stat_type": leg.stat_type,
                "line_value": leg.line_value,
                "direction": leg.direction,
                "game_time": leg.game_time,
                "result": leg.result,
                "actual_value": leg.actual_value,
                "grade_result_source": leg.grade_result_source,
            }
            if existing_leg is None or result_priority.get(
                str(leg.result or "PENDING").upper(), 0
            ) > result_priority.get(str(existing_leg["result"] or "PENDING").upper(), 0):
                group["_legs"][leg.leg_index] = leg_record

        records = []
        for group in grouped.values():
            legs = [
                group["_legs"][index]
                for index in sorted(group["_legs"])
            ]
            records.append(
                {
                    "combination_signature": group["combination_signature"],
                    "slip_size": group["slip_size"],
                    "created_at": group["created_at"],
                    "sent_at": group["sent_at"],
                    "result": self._auto_slip_combination_result(legs),
                    "legs": legs,
                }
            )
        records.sort(
            key=lambda record: record.get("sent_at") or datetime.min,
            reverse=True,
        )
        return records

    async def get_auto_slip_notification_history(self, limit: int = 10) -> list[dict]:
        """Return recent distinct auto-sent combinations with their leg grades."""
        records = await self._get_auto_slip_notification_combinations()
        return records[: max(1, int(limit))]

    async def get_auto_slip_notification_stats(self) -> dict:
        """Aggregate distinct automatic slips by size and HIT/MISS/PUSH outcome."""
        records = await self._get_auto_slip_notification_combinations()
        by_size: dict[int, dict[str, int]] = {}
        for record in records:
            size = int(record["slip_size"])
            stats = by_size.setdefault(
                size,
                {
                    "hit": 0,
                    "miss": 0,
                    "push": 0,
                    "pending": 0,
                    "void": 0,
                    "total": 0,
                },
            )
            outcome = str(record["result"]).lower()
            if outcome not in stats:
                outcome = "pending"
            stats[outcome] += 1
            stats["total"] += 1

        async with self.session() as s:
            sent_result = await s.execute(
                select(AutoSlipNotification.signature).where(
                    AutoSlipNotification.status == "SENT"
                )
            )
            sent_signatures = set(sent_result.scalars().all())
            tracked_result = await s.execute(
                select(AutoSlipNotificationLeg.notification_signature).distinct()
            )
            tracked_signatures = set(tracked_result.scalars().all())

        return {
            "by_size": by_size,
            "total_slips": len(records),
            "untracked_deliveries": len(sent_signatures - tracked_signatures),
        }

    async def create_slip_journal(
        self,
        stake: Optional[float] = None,
        notes: Optional[str]   = None,
    ) -> str:
        """Create a new OPEN slip journal entry.  Returns the slip_code (e.g. 'SLP-007')."""
        async with self.session() as s:
            # Auto-increment slip number from existing rows
            count_result = await s.execute(select(func.count()).select_from(SlipJournal))
            n = (count_result.scalar() or 0) + 1
            code = f"SLP-{n:03d}"
            row  = SlipJournal(slip_code=code, stake=stake, notes=notes, status="OPEN")
            s.add(row)
            await s.commit()
        return code

    async def get_open_slip_journal(self) -> Optional[SlipJournal]:
        """Return the most recently created OPEN slip, or None."""
        async with self.session() as s:
            result = await s.execute(
                select(SlipJournal)
                .where(SlipJournal.status == "OPEN")
                .order_by(desc(SlipJournal.created_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def add_slip_journal_leg(
        self,
        slip_code:   str,
        player_name: str,
        stat_type:   str,
        *,
        opp_id:      Optional[int]      = None,
        team:        str                = "",
        sport:       str                = "",
        line_value:  Optional[float]    = None,
        direction:   Optional[str]      = None,
        tier:        Optional[str]      = None,
        confidence:  Optional[int]      = None,
        game_time:   Optional[datetime] = None,
        provider:    Optional[str]      = None,
    ) -> SlipJournalLeg:
        """Append a leg to an existing slip journal entry."""
        async with self.session() as s:
            leg = SlipJournalLeg(
                slip_code   = slip_code,
                opp_id      = opp_id,
                provider    = provider or "Underdog",
                player_name = player_name,
                team        = team,
                sport       = sport,
                stat_type   = stat_type,
                line_value  = line_value,
                direction   = direction,
                tier        = tier,
                confidence  = confidence,
                game_time   = game_time,
                result      = "PENDING",
            )
            s.add(leg)
            await s.commit()
            await s.refresh(leg)
            return leg

    async def get_slip_journal_legs(self, slip_code: str) -> list[SlipJournalLeg]:
        """Return all legs for a slip_code, ordered by id."""
        async with self.session() as s:
            result = await s.execute(
                select(SlipJournalLeg)
                .where(SlipJournalLeg.slip_code == slip_code)
                .order_by(SlipJournalLeg.id)
            )
            return list(result.scalars().all())

    async def grade_slip_journal(
        self,
        slip_code: str,
        payout:    Optional[float] = None,
    ) -> dict:
        """
        Auto-grade slip legs from prop_opportunity_log results.

        For each PENDING leg with an opp_id, fetch the graded result from
        PropOpportunityLog and copy it to the leg.  Legs without opp_id remain
        PENDING (user must manually update them).

        Returns a summary dict:
          { total, graded, hit, miss, push, pending, all_hit, payout, roi_pct }
        """
        now = datetime.utcnow()
        async with self.session() as s:
            legs_result = await s.execute(
                select(SlipJournalLeg)
                .where(SlipJournalLeg.slip_code == slip_code)
                .order_by(SlipJournalLeg.id)
            )
            legs: list[SlipJournalLeg] = list(legs_result.scalars().all())

        counts = {"HIT": 0, "MISS": 0, "PUSH": 0, "PENDING": 0}
        for leg in legs:
            if leg.result != "PENDING":
                counts[leg.result if leg.result in counts else "PENDING"] += 1
                continue
            if leg.opp_id is None:
                counts["PENDING"] += 1
                continue
            # Look up result from PropOpportunityLog
            async with self.session() as s:
                opp_result = await s.execute(
                    select(PropOpportunityLog)
                    .where(PropOpportunityLog.id == leg.opp_id)
                )
                opp = opp_result.scalar_one_or_none()
            if opp is None or opp.result in (None, "PENDING"):
                counts["PENDING"] += 1
                continue
            # Map graded result — only HIT/MISS/PUSH matter for slips
            mapped = opp.result if opp.result in ("HIT", "MISS", "PUSH") else "PENDING"
            async with self.session() as s:
                await s.execute(
                    __import__("sqlalchemy", fromlist=["update"]).update(SlipJournalLeg)
                    .where(SlipJournalLeg.id == leg.id)
                    .values(
                        result       = mapped,
                        actual_value = opp.actual_value,
                        graded_at    = now,
                    )
                )
                await s.commit()
            counts[mapped if mapped in counts else "PENDING"] += 1

        total    = len(legs)
        graded   = counts["HIT"] + counts["MISS"] + counts["PUSH"]
        all_hit  = (counts["HIT"] == total and total > 0)

        # Compute payout/ROI and close the slip if fully graded
        slip_roi: Optional[float] = None
        if payout is not None:
            async with self.session() as s:
                slip_row_res = await s.execute(
                    select(SlipJournal).where(SlipJournal.slip_code == slip_code)
                )
                slip_row = slip_row_res.scalar_one_or_none()
            stake = slip_row.stake if slip_row else None
            slip_roi = ((payout / stake) - 1) * 100 if (stake and stake > 0) else None

        new_status = "GRADED" if graded == total else "OPEN"
        async with self.session() as s:
            update_vals: dict = {"status": new_status}
            if new_status == "GRADED":
                update_vals["graded_at"] = now
                update_vals["slip_type"] = f"{total}-man"
            if payout is not None:
                update_vals["payout"]  = payout
                update_vals["roi_pct"] = slip_roi
            await s.execute(
                __import__("sqlalchemy", fromlist=["update"]).update(SlipJournal)
                .where(SlipJournal.slip_code == slip_code)
                .values(**update_vals)
            )
            await s.commit()

        return {
            "total":   total,
            "graded":  graded,
            "hit":     counts["HIT"],
            "miss":    counts["MISS"],
            "push":    counts["PUSH"],
            "pending": counts["PENDING"],
            "all_hit": all_hit,
            "payout":  payout,
            "roi_pct": slip_roi,
        }

    async def get_slip_journal_history(self, limit: int = 10) -> list[SlipJournal]:
        """Return recent slip journal entries (most recent first)."""
        async with self.session() as s:
            result = await s.execute(
                select(SlipJournal)
                .order_by(desc(SlipJournal.created_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_slip_journal_stats(self) -> dict:
        """
        Aggregate slip journal performance for /slipstats.

        Returns per-size (2-man / 3-man / 6-man) record, plus overall totals.
        """
        async with self.session() as s:
            graded_result = await s.execute(
                select(SlipJournal)
                .where(SlipJournal.status == "GRADED")
                .order_by(desc(SlipJournal.created_at))
            )
            graded: list[SlipJournal] = list(graded_result.scalars().all())

        by_size: dict = {}
        total_staked  = 0.0
        total_payout  = 0.0
        for slip in graded:
            size = slip.slip_type or "?"
            if size not in by_size:
                by_size[size] = {"win": 0, "loss": 0, "staked": 0.0, "payout": 0.0}
            # A slip is a WIN if payout > stake (or payout is set and > 0)
            is_win = (slip.payout is not None and slip.stake is not None and slip.payout > slip.stake)
            if is_win:
                by_size[size]["win"] += 1
            else:
                by_size[size]["loss"] += 1
            if slip.stake:
                by_size[size]["staked"] += slip.stake
                total_staked += slip.stake
            if slip.payout:
                by_size[size]["payout"] += slip.payout
                total_payout += slip.payout

        overall_roi = ((total_payout / total_staked) - 1) * 100 if total_staked > 0 else None
        return {
            "by_size":      by_size,
            "total_staked": total_staked,
            "total_payout": total_payout,
            "overall_roi":  overall_roi,
            "total_slips":  len(graded),
        }

    async def find_opportunity_for_slip(
        self,
        query: str,
        since_hours: int = 48,
    ) -> Optional[PropOpportunityLog]:
        """
        Find a PropOpportunityLog row to add to a slip.

        `query` is either:
          - A numeric string → lookup by id
          - A player-name substring → most recent matching directional row
        """
        from datetime import timedelta
        if query.isdigit():
            async with self.session() as s:
                result = await s.execute(
                    select(PropOpportunityLog).where(
                        PropOpportunityLog.id == int(query),
                        PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                    )
                )
                return result.scalar_one_or_none()
        # Fuzzy name match
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        pattern = f"%{query.strip()}%"
        async with self.session() as s:
            result = await s.execute(
                select(PropOpportunityLog)
                .where(
                    PropOpportunityLog.player_name.ilike(pattern),
                    PropOpportunityLog.detected_at >= cutoff,
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                )
                .order_by(desc(PropOpportunityLog.detected_at))
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_player_prop_history(
        self,
        player_name: str,
        limit: int = 20,
    ) -> list:
        """
        Return the recent provider-neutral tracking history for a player.

        The result intentionally combines persistent PropLineHistory
        observations with directional PropOpportunityLog rows. Tracking is not
        contingent on a direction or on Telegram delivery.  Historical callers
        can continue to use the shared fields (player_name, line_value, sport,
        result, and so on); observation-only rows expose recommendation=None and
        result=PENDING.
        """
        from types import SimpleNamespace
        pattern = f"%{player_name.strip()}%"
        async with self.session() as s:
            opportunity_result = await s.execute(
                select(PropOpportunityLog)
                .where(PropOpportunityLog.player_name.ilike(pattern))
                .order_by(desc(PropOpportunityLog.detected_at))
                .limit(max(1, min(int(limit) * 4, 500)))
            )
            opportunities = list(opportunity_result.scalars().all())
            history_result = await s.execute(
                select(PropLineHistory)
                .where(PropLineHistory.player_name.ilike(pattern))
                .order_by(desc(PropLineHistory.fetched_at))
                .limit(max(1, min(int(limit) * 4, 500)))
            )
            observations = list(history_result.scalars().all())

        rows = list(opportunities)
        for observation in observations:
            rows.append(SimpleNamespace(
                id=None,
                external_id=observation.external_id or "",
                player_name=observation.player_name,
                team=observation.team or "",
                sport=observation.sport,
                stat_type=observation.stat_type,
                line_value=observation.line_value,
                recommendation=getattr(observation, "bet_recommendation", None),
                provider=observation.provider,
                result="PENDING",
                actual_value=None,
                detected_at=observation.fetched_at,
                game_time=observation.game_time,
                alert_sent=False,
                tracked_observation=True,
            ))
        rows.sort(
            key=lambda row: (
                getattr(row, "detected_at", None)
                or getattr(row, "fetched_at", None)
                or datetime.min
            ),
            reverse=True,
        )
        return rows[:max(1, int(limit))]

    async def get_pick_accuracy_by_sport(
        self,
        limit_sports: int = 10,
        enabled_sports: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """
        Aggregate HIT/MISS counts per provider and sport from PropOpportunityLog.

        Provider is included so source performance cannot be mistaken for a
        single blended market.  Returns rows sorted by total picks desc:
          [{"provider": str, "sport": str, "hits": int, "misses": int,
            "pushes": int, "total": int}]
        """
        enabled_sport_keys = (
            {
                str(sport).strip().upper()
                for sport in enabled_sports
                if str(sport).strip()
            }
            if enabled_sports is not None
            else None
        )
        sport_filter = (
            [func.upper(PropOpportunityLog.sport).in_(enabled_sport_keys)]
            if enabled_sport_keys is not None
            else []
        )
        async with self.session() as s:
            confirmed_audits = await self._confirmed_telegram_play_audits(s)
            opportunity_rows = await s.execute(
                select(
                    PropOpportunityLog.sport,
                    PropOpportunityLog.provider,
                    PropOpportunityLog.external_id,
                    PropOpportunityLog.stat_type,
                    PropOpportunityLog.result,
                ).where(
                    _trusted_directional_grade_filter(),
                    PropOpportunityLog.recommendation.in_(("OVER", "UNDER")),
                    PropOpportunityLog.alert_sent == True,  # noqa: E712
                    *sport_filter,
                )
            )
            result_by_identity = {
                (
                    str(provider or "Underdog"),
                    str(external_id or ""),
                    str(stat_type or ""),
                ): (sport or "?", str(result or "").upper())
                for sport, provider, external_id, stat_type, result
                in opportunity_rows.all()
            }
            raw = []
            for audit in confirmed_audits:
                identity = (
                    str(audit.provider or "Underdog"),
                    _stored_opportunity_external_id(
                        str(audit.external_id or ""), audit.provider
                    ),
                    str(audit.stat_type or ""),
                )
                sport, result = result_by_identity.get(identity, ("?", "PENDING"))
                if result in {"HIT", "MISS", "PUSH"}:
                    raw.append(SimpleNamespace(
                        sport=sport,
                        provider=audit.provider,
                        result=result,
                        n=1,
                    ))

        # Aggregate into per-sport dicts
        by_sport: dict[str, dict] = {}
        for row in raw:
            sp = row.sport or "?"
            provider = row.provider or "Underdog"
            key = (provider, sp)
            if key not in by_sport:
                by_sport[key] = {
                    "provider": provider,
                    "sport": sp,
                    "hits": 0,
                    "misses": 0,
                    "pushes": 0,
                }
            if row.result == "HIT":
                by_sport[key]["hits"] += row.n
            elif row.result == "MISS":
                by_sport[key]["misses"] += row.n
            elif row.result == "PUSH":
                by_sport[key]["pushes"] += row.n

        result_list = sorted(
            by_sport.values(),
            key=lambda d: d["hits"] + d["misses"] + d["pushes"],
            reverse=True,
        )
        return result_list[:limit_sports]

    async def close(self) -> None:
        engine = self._engine
        self._engine = None
        self._session_factory = None
        if engine:
            await engine.dispose()
            logger.info("Database connection closed.")
