"""
market_engine.py — Multi-platform market engine background jobs.

Owns the polling jobs for:
  connector_poll_job      — fetch snapshots from all connectors, store, run consensus
  consensus_check_job     — run consensus engine, flag inefficiencies + multi-book steam
  clv_check_job           — detect CLV opportunities (current price > projected close)
  underdog_job            — fetch Underdog pick'em projections, alert on line changes

CLV design:
  - CLV *opportunities* (current price ahead of projected close) are alerted in
    clv_check_job.  They use MarketSnapshotRecord (alert_sent=True) for dedup.
    They are NOT stored as CLVRecord — there are no closing odds yet.
  - CLVRecord is reserved for *post-close* CLV results: compute_clv() called with
    real closing odds once the event starts.  /clv shows this history.

Steam dedup:
  After sending a multi-book steam alert a SteamRecord is persisted so that
  has_recent_steam_alert() correctly suppresses duplicates in the next cycle.

Underdog prop identity:
  The stat_type stored on UnderdogSnapshotRecord is the true stat category
  (e.g. "Fantasy Points"), extracted from the selection string produced by
  UnderdogConnector.  Change detection compares by player_name + stat_type,
  not by the raw selection string that includes the line value.

Pick'em isolation:
  Underdog snapshots (is_pickem=True) are never passed to sportsbook analysis.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import logging
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace
from typing import Optional

from config import config
from engine.health import get_health_tracker
from engine.prop_intelligence import compute_prop_intelligence as _compute_intel
from engine.downstream_intelligence import build_downstream_payload as _build_downstream_intel
from engine.player_prop_market import (
    _is_prop_deduped,
    _record_prop_alerted,
    _release_prop_delivery_claim,
    normalize_stat as _normalize_prop_stat,
)
from engine.score_validation import clamp_score
from engine.sport_policy import is_football_related_sport
from engine.telegram_rate_limiter import get_rate_limiter
from engine.strongest_pick import select_strongest_actionable_picks
from providers import FailureType, get_health_monitor
from engine.delivery_gates import (
    tier_delivery_gate as _canonical_tier_delivery_gate,
)
from database import (
    Database,
    MarketSnapshotRecord,
    CLVRecord,
    UnderdogSnapshotRecord,
    SteamRecord,
)
# AlertCLVSeed rows are seeded by the existing Underdog delivery/harvest
# pathway; keep the name visible here as an intentional compatibility marker.
from connectors import ConnectorRegistry, MarketSnapshot
from engine.consensus import compute_consensus, find_inefficiencies, build_multi_book_steam_inputs
from engine.clv import build_clv_opportunity
from engine.steam import compute_steam_simple
from engine.provider_confirmation import (
    ConfirmationTarget,
    confirm_market,
    observation_from_record,
    normalize_market,
    normalize_name,
)
from alerts import AlertDelivery, broadcast_alert, identify_sharp_books
from alerts_multiplatform import (
    format_steam_multibook_alert,
    format_inefficiency_alert,
    format_clv_opportunity_alert,
    format_underdog_change_alert,
    format_market_move_detected,  # noqa: F401 — imported for availability check
)


async def _broadcast_consensus_alert(
    *,
    db: Database,
    bot,
    chat_ids: list,
    message: str,
    sport: str,
    alert_kind: str,
    player_name: Optional[str] = None,
    stat_type: Optional[str] = None,
    line_value: Optional[float] = None,
) -> dict:
    """Rate-limit and audit the legacy consensus broadcast path."""
    if not chat_ids:
        return {"sent": 0, "failed": 0, "rate_limited": False}

    limiter = get_rate_limiter()
    limiter.record_qualified()
    rate = limiter.check("A", 80, 80, is_meaningful_change=True)
    if not rate.allowed:
        outcome = "rate_limited"
        counts = {"sent": 0, "failed": 0, "rate_limited": True}
    else:
        counts = await broadcast_alert(bot, chat_ids, message, sport=sport)
        sent = int(counts.get("sent", 0) or 0)
        if sent:
            limiter.record_sent()
        outcome = "sent" if sent else "failed"
        counts = {
            **counts,
            "sent": sent,
            "failed": int(counts.get("failed", 0) or 0),
            "rate_limited": False,
        }

    audit = getattr(db, "log_telegram_delivery", None)
    if audit is not None:
        try:
            result = audit(
                provider="Sportsbook",
                alert_kind=alert_kind,
                delivery_path="consensus",
                player_name=player_name,
                sport=sport,
                stat_type=stat_type,
                line_value=line_value,
                outcome=outcome,
                recipients_sent=counts["sent"],
                recipients_failed=counts["failed"],
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("Consensus Telegram audit write failed: %s", exc)
    return counts


def _prizepicks_line_from_confirmation(
    confirmation: Optional[dict],
) -> Optional[float]:
    """Return only a verified PrizePicks line from a confirmation result."""
    if not isinstance(confirmation, dict):
        return None
    if str(confirmation.get("source", "")).strip().lower() != "prizepicks":
        return None
    if not confirmation.get("confirmed"):
        return None
    value = confirmation.get("avg_line")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _active_prizepicks_lines(
    active_pool: dict,
) -> dict[tuple[str, str, str], float]:
    """Index current, non-removed PrizePicks observations for FPR display."""
    indexed: dict[tuple[str, str, str], tuple[datetime, float]] = {}
    for row in active_pool.values():
        if str(getattr(row, "provider", "") or "").strip().lower() != "prizepicks":
            continue
        if bool(getattr(row, "removed", False)):
            continue
        try:
            line = float(getattr(row, "line_value", None))
        except (TypeError, ValueError):
            continue
        key = (
            normalize_name(getattr(row, "player_name", "")),
            str(getattr(row, "sport", "") or "").upper(),
            _normalize_prop_stat(getattr(row, "stat_type", "")),
        )
        if not key[0] or not key[1] or not key[2]:
            continue
        observed_at = getattr(row, "fetched_at", None) or datetime.min
        previous = indexed.get(key)
        if previous is None or observed_at >= previous[0]:
            indexed[key] = (observed_at, line)
    return {key: value for key, (_observed_at, value) in indexed.items()}


logger = logging.getLogger(__name__)


class _CycleTiming:
    """In-memory aggregated timing for one monitoring cycle."""

    def __init__(self, cycle_id: int) -> None:
        self.cycle_id = cycle_id
        self.started_at = datetime.utcnow()
        self.started_mono = time.monotonic()
        self._open: dict[str, tuple[datetime, float]] = {}
        self.stages: dict[str, dict] = {}
        self.metrics: dict = {
            "counts": defaultdict(int),
            "timers": defaultdict(float),
            "providers": {},
            "database": {},
        }
        self._overlap_start: set[str] = set()
        self._overlap_during: set[str] = set()

    def start(self, name: str) -> None:
        self._open[name] = (datetime.utcnow(), time.monotonic())
        if name == "scoring_evidence":
            self._overlap_start = _active_job_names() - {"underdog"}
            self._overlap_during.update(self._overlap_start)

    def finish(self, name: str) -> None:
        opened = self._open.pop(name, None)
        if opened is None:
            return
        start_at, start_mono = opened
        end_at = datetime.utcnow()
        self.stages[name] = {
            "start": start_at.isoformat(timespec="milliseconds") + "Z",
            "end": end_at.isoformat(timespec="milliseconds") + "Z",
            "seconds": round(max(0.0, time.monotonic() - start_mono), 3),
        }
        if name == "scoring_evidence":
            self._overlap_during.update(_active_job_names() - {"underdog"})

    def count(self, name: str, amount: int = 1) -> None:
        self.metrics["counts"][name] += int(amount)

    def duration(self, name: str, seconds: float) -> None:
        self.metrics["timers"][name] += max(0.0, float(seconds))

    def provider(self, name: str, seconds: float) -> None:
        entry = self.metrics["providers"].setdefault(
            name, {"calls": 0, "seconds": 0.0}
        )
        entry["calls"] += 1
        entry["seconds"] += max(0.0, float(seconds))

    def database(self, kind: str, operation: str, seconds: float) -> None:
        entry = self.metrics["database"].setdefault(
            operation, {"kind": kind, "calls": 0, "seconds": 0.0}
        )
        entry["calls"] += 1
        entry["seconds"] += max(0.0, float(seconds))
        self.count(f"database_{kind}_operations")
        self.duration(f"database_{kind}", seconds)
        if kind == "write":
            operation_lower = operation.lower()
            if "snapshot" in operation_lower:
                self.count("persistence_snapshot_writes")
                self.duration("persistence_snapshot", seconds)
            elif "opportunity" in operation_lower or "candidate" in operation_lower:
                self.count("persistence_opportunity_writes")
                self.duration("persistence_opportunity", seconds)
            elif any(token in operation_lower for token in ("grade", "tracking", "lifecycle", "mq_score")):
                self.count("persistence_grading_tracking_writes")
                self.duration("persistence_grading_tracking", seconds)
            elif any(token in operation_lower for token in ("scan_cycle", "funnel", "current_run", "run_log")):
                self.count("persistence_funnel_run_writes")
                self.duration("persistence_funnel_run", seconds)

    async def await_operation(self, category: str, operation: str, awaitable):
        started = time.monotonic()
        self.count(f"{category}_operations")
        try:
            return await awaitable
        finally:
            elapsed = time.monotonic() - started
            self.duration(category, elapsed)

    async def await_db(self, kind: str, operation: str, awaitable):
        started = time.monotonic()
        try:
            return await awaitable
        finally:
            self.database(kind, operation, time.monotonic() - started)

    def sync_operation(self, category: str, operation: str, func, *args, **kwargs):
        started = time.monotonic()
        self.count(f"{category}_operations")
        try:
            return func(*args, **kwargs)
        finally:
            self.duration(category, time.monotonic() - started)

    def finish_open(self) -> None:
        for name in list(self._open):
            self.finish(name)

    def summary(self, props: int, batch_size: int) -> dict:
        self.finish_open()
        ended_at = datetime.utcnow()
        total = max(0.0, time.monotonic() - self.started_mono)
        return {
            "cycle_id": self.cycle_id,
            "start": self.started_at.isoformat(timespec="milliseconds") + "Z",
            "end": ended_at.isoformat(timespec="milliseconds") + "Z",
            "seconds": round(total, 3),
            "props": int(props),
            "props_per_sec": round(props / total, 3) if total else 0.0,
            "batch_size": int(batch_size),
            "stages": self.stages,
            "active_jobs": sorted(_active_job_names()),
            "diagnostics": {
                "counts": dict(self.metrics["counts"]),
                "timers": {
                    key: round(value, 3)
                    for key, value in self.metrics["timers"].items()
                },
                "providers": {
                    key: {
                        "calls": value["calls"],
                        "seconds": round(value["seconds"], 3),
                    }
                    for key, value in self.metrics["providers"].items()
                },
                "database": {
                    key: {
                        "kind": value["kind"],
                        "calls": value["calls"],
                        "seconds": round(value["seconds"], 3),
                    }
                    for key, value in self.metrics["database"].items()
                },
                "scoring_overlap": sorted(self._overlap_during),
            },
        }


_cycle_sequence = 0
_active_jobs: dict[str, float] = {}


def _job_activity_start(name: str) -> None:
    _active_jobs[name] = time.monotonic()


def _job_activity_end(name: str) -> None:
    _active_jobs.pop(name, None)


def _active_job_names() -> set[str]:
    return set(_active_jobs)

# Module-level registry — set by init_market_engine()
_registry: Optional[ConnectorRegistry] = None

# ── OddsAPI confirmation engine ───────────────────────────────────────────────
# Set once at startup via init_odds_confirmation(); used by
# _get_odds_api_confirmation() to read already-persisted provider data.
_analysis_engine: Optional[object] = None
_confirmation_db: Optional[Database] = None
_propline_provider: Optional[object] = None
_SPORTSBOOK_CONFIRMATION_BOOKS = frozenset({"draftkings", "fanduel"})


def _sportsbook_key(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _sportsbook_confirmation_enabled() -> bool:
    """Return the temporary gate for DK/FanDuel confirmation."""
    return bool(getattr(config, "SPORTSBOOK_CONFIRMATION_ENABLED", False))


def init_odds_confirmation(engine: object, db: Optional[Database] = None) -> None:
    """Store the existing analysis/DB services used for confirmation.

    The optional DB reference is used for already-polled h2h/totals rows and
    persisted PrizePicks snapshots; it never creates another polling cycle.
    One-argument callers remain supported for the existing sportsbook tests
    and integrations.
    """
    global _analysis_engine, _confirmation_db, _propline_provider
    _analysis_engine = engine
    _confirmation_db = db
    try:
        from providers.propline import PropLinePrizePicksProvider
        _propline_provider = PropLinePrizePicksProvider()
    except Exception:
        # Provider setup is optional; the existing OddsAPI confirmation path
        # must remain available even if PropLine dependencies/configuration
        # are unavailable.
        _propline_provider = None

# ── Player results integration ────────────────────────────────────────────────
# Singleton provider and per-day fetch dedup cache.
# Cache key: (player_name, sport, stat_type_lower, date_iso)
# The date component means stale entries are automatically bypassed next day.
_player_stats_provider = None
_player_result_fetch_cache: set = set()
# Maximum entries before the cache is wiped.  Each key is (player,sport,stat,date_iso);
# the date component means old entries are already bypassed by logic, but they
# accumulate in memory.  Clear the entire set once it grows past this ceiling so
# the next cycle re-fetches fresh data — a safe, cheap reset at ~300 s cadence.
_PLAYER_RESULT_CACHE_MAX = 5_000
_prizepicks_cycle_task: Optional[asyncio.Task] = None
_prizepicks_processing_tasks: set[asyncio.Task] = set()
_prizepicks_processing_lock: Optional[asyncio.Lock] = None
_PRIZEPICKS_DEDUP_PREFIX = "PrizePicks::"
_prizepicks_sport_cursor = 0
# Current PropLine observations are also available in-process while the
# companion branch is running.  This is supplemental evidence only; it never
# replaces an Underdog snapshot or creates a second alert path.
_PROPLINE_EVIDENCE_TTL_SECONDS = 20 * 60
_propline_current_observations: dict[tuple[str, str, str, str], object] = {}


async def _get_prizepicks_scan_sports(provider) -> tuple[str, ...]:
    """Return the provider's active canonical sports, with a safe config fallback.

    PropLine's sports catalog is authoritative when available.  The configured
    list remains a compatibility fallback for test/custom providers and for a
    temporary catalog outage; it is never used to turn an empty successful
    catalog into a fabricated sport.
    """
    configured = tuple(
        str(sport).strip()
        for sport in getattr(config, "prizepicks_leagues", [])
        if str(sport).strip()
    )
    discover = getattr(provider, "discover_sports", None)
    if not callable(discover):
        return tuple(
            sport for sport in configured
            if not is_football_related_sport(sport)
        )
    try:
        discovered = await discover()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "prizepicks_cycle: active-sport discovery failed; using configured fallback: %s",
            type(exc).__name__,
        )
        return tuple(
            sport for sport in configured
            if not is_football_related_sport(sport)
        )

    sports: list[str] = []
    seen: set[str] = set()
    for item in discovered or []:
        canonical = str(
            getattr(item, "canonical_key", None)
            or getattr(item, "sport", None)
            or item
        ).strip()
        if (
            canonical
            and not is_football_related_sport(canonical)
            and canonical.upper() not in seen
        ):
            seen.add(canonical.upper())
            sports.append(canonical)
    if discovered is None:
        return tuple(
            sport for sport in configured
            if not is_football_related_sport(sport)
        )
    # A successful but empty catalog means there is no currently advertised
    # PP slate.  Do not issue requests against guessed static sports.
    return tuple(sports)


def _player_provider_label(sport: str) -> str:
    """Best-effort provider attribution for the existing player-stats routes."""
    labels = {
        "MLB": "MLB Stats API",
        "NHL": "NHL",
        "CS": "PandaScore",
        "DOTA": "OpenDota",
        "TENNIS": "JeffSackmann",
        "SOCCER": "Soccer provider",
    }
    return labels.get((sport or "").upper(), "ESPN")

# Set to True after the first complete Underdog prop scan.  The first cycle
# uses the controlled cold-start target; subsequent cycles use incremental
# scoring (new props and line-change events only).
_cold_start_done: bool = False
# Controlled cold-start experiment: persisted active props are restored and remain
# available to Stable Refresh/FPR, but are not re-scored in the one-time startup
# pass. New props and actual line changes continue through their normal paths.
_COLD_START_RESCAN_TARGET: int = 0

# ── Market availability tracking ─────────────────────────────────────────────
# Maps "player__stat_type" → datetime of first alert (bet pick).
# Used to compute how long a market was available before removal.
# Internal only — no Telegram alert is sent on removal (doc #4).
_MARKET_FIRST_ALERT: dict = {}
# Evict entries older than this many hours each scan cycle (prevents unbounded growth).
_MARKET_FIRST_ALERT_TTL_H: int = 24

# ── Legacy priority-display compatibility state ───────────────────────────────
# Retained for compatibility with older state and tests.  It does not qualify,
# rank, or independently deliver a directional prop.
_priority_override_sent: set = set()

# ── Underdog full-scan concurrency guard ──────────────────────────────────────
# Set to True while underdog_job is executing a full scan (fetch + score + deliver).
# A second instance (via max_instances=2) that finds this flag set will run only the
# fast new-prop detection path and return immediately, keeping the 2-minute polling
# cadence alive without duplicating the heavy scoring work.
_ud_full_scan_running: bool = False

# FPR is scheduled independently, but it must not lose its only execution window
# when a long Underdog scan occupies the shared full-scan slot.  A deferred trigger
# records one retry request; the active Underdog job hands that request off after
# releasing the slot.  These are process-local coordination flags, not a second
# rotation or another scheduler.
_fpr_deferred_pending: bool = False
_fpr_deferred_context: object | None = None
_fpr_handoff_task: asyncio.Task | None = None
_fpr_execution_running: bool = False


async def reset_monitoring_runtime_state() -> None:
    """Destroy disposable market-monitoring state before a new session starts.

    SQLite remains the source of historical knowledge and delivery dedup
    records are restored later by ``_init_state_from_db``.  This function only
    clears process-local scans/cursors and cancels child tasks that could
    otherwise survive an in-process application restart.
    """
    global _cycle_sequence, _active_jobs, _registry, _player_stats_provider
    global _player_result_fetch_cache, _prizepicks_cycle_task
    global _prizepicks_processing_tasks, _prizepicks_processing_lock
    global _prizepicks_sport_cursor
    global _cold_start_done, _MARKET_FIRST_ALERT, _priority_override_sent
    global _ud_full_scan_running, _fpr_deferred_pending, _fpr_deferred_context
    global _fpr_handoff_task, _fpr_execution_running, _prop_market_alerted
    global _ud_scan_state, _ud_known_keys, _ud_scan_db_id, _prop_dedup_lock
    global _snapshot_cache, _analysis_engine, _confirmation_db, _propline_provider
    global _propline_current_observations

    current = asyncio.current_task()
    tasks = {
        task
        for task in (
            [_prizepicks_cycle_task, _fpr_handoff_task]
            + list(_prizepicks_processing_tasks)
        )
        if task is not None and task is not current and not task.done()
    }
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    _cycle_sequence = 0
    _active_jobs.clear()
    _registry = None
    _player_stats_provider = None
    _player_result_fetch_cache.clear()
    _prizepicks_cycle_task = None
    _prizepicks_processing_tasks.clear()
    _prizepicks_processing_lock = None
    _prizepicks_sport_cursor = 0
    _cold_start_done = False
    _MARKET_FIRST_ALERT.clear()
    _priority_override_sent.clear()
    _ud_full_scan_running = False
    _fpr_deferred_pending = False
    _fpr_deferred_context = None
    _fpr_handoff_task = None
    _fpr_execution_running = False
    _prop_market_alerted.clear()
    _ud_scan_state.clear()
    _ud_known_keys.clear()
    _ud_scan_db_id = None
    _prop_dedup_lock = asyncio.Lock()
    _snapshot_cache.clear()
    _analysis_engine = None
    _confirmation_db = None
    _propline_provider = None
    _propline_current_observations.clear()


def _cache_propline_evidence(props: list[object]) -> None:
    """Publish fresh PropLine observations to the read-only evidence boundary."""
    now = datetime.utcnow()
    for prop in props:
        player = str(getattr(prop, "player_name", "") or "").strip()
        sport = str(getattr(prop, "sport", "") or "").upper().strip()
        stat = str(getattr(prop, "stat_type", "") or "").strip().lower()
        event_id = str(
            getattr(prop, "event_id", "")
            or getattr(prop, "game_id", "")
            or ""
        ).strip()
        if not player or not sport or not stat:
            continue
        observed_at = getattr(prop, "fetched_at", None)
        if isinstance(observed_at, datetime) and (
            now - observed_at
        ).total_seconds() > _PROPLINE_EVIDENCE_TTL_SECONDS:
            continue
        # Exclude football before it can become supplemental evidence.
        if is_football_related_sport(sport):
            continue
        _propline_current_observations[(player, sport, stat, event_id)] = prop

    cutoff = now - timedelta(seconds=_PROPLINE_EVIDENCE_TTL_SECONDS)
    stale = [
        key
        for key, prop in _propline_current_observations.items()
        if isinstance(getattr(prop, "fetched_at", None), datetime)
        and getattr(prop, "fetched_at") < cutoff
    ]
    for key in stale:
        _propline_current_observations.pop(key, None)


def _queue_deferred_fpr(context: object) -> None:
    """Remember one FPR trigger to run after the current full scan."""
    global _fpr_deferred_pending, _fpr_deferred_context
    _fpr_deferred_pending = True
    _fpr_deferred_context = context


def _schedule_deferred_fpr() -> None:
    """Start the one queued FPR pass once the shared scan slot is free."""
    global _fpr_deferred_pending, _fpr_deferred_context, _fpr_handoff_task

    if not _fpr_deferred_pending:
        return
    if _fpr_handoff_task is not None and not _fpr_handoff_task.done():
        return

    context = _fpr_deferred_context
    _fpr_deferred_pending = False
    _fpr_deferred_context = None

    async def _run_deferred_fpr() -> None:
        global _fpr_handoff_task
        try:
            logger.info("full_pool_rescan: running deferred batch after Underdog scan")
            await _full_pool_rescan_job(context)
        except Exception:
            # The FPR job records its own failures.  This guard prevents an
            # unexpected task-level exception from becoming an unhandled-task
            # warning and hiding the next scheduled opportunity.
            logger.exception("full_pool_rescan: deferred handoff failed")
        finally:
            _fpr_handoff_task = None

    _fpr_handoff_task = asyncio.create_task(
        _run_deferred_fpr(),
        name="deferred_full_pool_rescan",
    )


def _release_underdog_scan_slot() -> None:
    """Release the shared slot and hand one deferred FPR trigger to the loop."""
    global _ud_full_scan_running, _prizepicks_cycle_task
    _ud_full_scan_running = False
    _schedule_deferred_fpr()


def _bq_stars(bq: int) -> str:
    """V3.4 star display string computed from Bet Quality (decision.confidence).

    Mapping:
      100     → ★★★★★
      80–99   → ★★★★☆
      70–79   → ★★★☆☆
      40–69   → ★★☆☆☆
      0–39    → ★☆☆☆☆
    """
    if bq >= 100: n = 5
    elif bq >= 80: n = 4
    elif bq >= 70: n = 3
    elif bq >= 40: n = 2
    else: n = 1
    return "★" * n + "☆" * (5 - n)


def _format_95_priority_alert(
    player: str,
    snap: object,
    stat_type: str,
    score: object,
    decision: "Optional[object]",
    line_val: float,
) -> str:
    """Format the legacy high-confidence priority-display message.

    Both OVER and UNDER are valid for all sports.  Current delivery uses the
    shared direction-only gate rather than this legacy formatter.
    """
    sport  = getattr(snap, "sport", None) or "UNKNOWN"
    rec    = getattr(decision, "recommendation", None) if decision is not None else None
    conf   = getattr(decision, "confidence", 0)      if decision is not None else 0
    stars  = _bq_stars(int(conf))  # V3.4: stars from Bet Quality, not raw score.total

    dir_line = ""
    if rec in ("OVER", "UNDER"):
        dir_line = f"\n📈 <b>{rec}</b>   Confidence: {conf}"

    gt_line = ""
    gt = getattr(snap, "game_time", None)
    if gt is not None:
        try:
            gt_line = f"\n🕐 {gt.strftime('%-I:%M %p ET')}"
        except Exception:
            pass

    return (
        f"🔥🚨 <b>S-TIER PRIORITY OVERRIDE — {conf}/100</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{player}</b>  ·  {stat_type}\n"
        f"🏆 S-TIER  {stars}  {sport}"
        f"{gt_line}"
        f"\nLine: {line_val:.1f}"
        f"{dir_line}\n"
        f"\n<i>🔥 Bet Quality {conf}/100 — Priority</i>"
        f"\n<i>Line movement and decision context are shown for research.</i>"
    )


def _is_game_live_or_past(snap: object, now: datetime) -> bool:
    """Return True when actionable alerts for this snap must be suppressed.

    Checks two signals in order:
    1. ``game_status`` attribute on *snap* — blocks when Underdog reports
       LIVE / IN_PROGRESS / FINAL / COMPLETED / CLOSED.  This is
       forward-compatible: if the field is absent it is silently skipped.
    2. ``game_time`` past check — blocks when the stored kick-off time has
       already elapsed (game_time < now).  If game_time is None the prop
       is allowed through (many valid props lack a scheduled time).

    Internal scanning may continue regardless; this gate only controls
    Telegram delivery of 🎯 ACTIONABLE BET PICK alerts.
    """
    _BLOCKED_STATUSES = {
        "started", "live", "in_progress", "in-progress",
        "final", "completed", "closed", "ended",
    }
    _status = (
        getattr(snap, "game_status", None)
        or getattr(snap, "provider_status", None)
    )
    if isinstance(_status, str) and _status.lower().replace(" ", "_") in _BLOCKED_STATUSES:
        return True
    _gt = getattr(snap, "game_time", None)
    if _gt is not None:
        try:
            if _gt.replace(tzinfo=None) < now:
                return True
        except Exception:
            pass
    return False


def _ud_line_fresh(
    candidate_line: float,
    player: str,
    stat_type: str,
    scan_line_map: "dict[tuple[str, str], float]",
) -> bool:
    """Return True when the candidate alert line matches the latest scan snapshot line.

    Within a single underdog_job cycle the candidate line is always sourced from the
    same snap object used in scoring, so this always returns True under normal operation.
    The guard makes the invariant explicit: any future refactor that decouples scoring
    from delivery will trigger a warning log and suppress the alert rather than
    silently delivering a stale line.

    Lines are discrete 0.5-step increments; a tolerance of 0.01 distinguishes
    floating-point noise from a genuine line divergence.
    """
    latest = scan_line_map.get((player, stat_type))
    if latest is None:
        return True   # prop not in this scan's map — allow through (no stale evidence)
    return abs(candidate_line - latest) < 0.01


def _rss_mb() -> Optional[float]:
    """Return current process RSS in MB.

    Reads VmRSS from /proc/self/status (Linux) which reflects *actual current*
    RSS — unlike resource.ru_maxrss which is the all-time high-water mark and
    never decreases.  Falls back to ru_maxrss on non-Linux systems.
    """
    try:
        with open("/proc/self/status") as _f:
            for _line in _f:
                if _line.startswith("VmRSS:"):
                    return float(_line.split()[1]) / 1024  # kB → MB
    except Exception:
        pass
    try:
        import resource as _res
        return _res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return None


async def _init_state_from_db(db: "Database") -> None:
    """Restore module-level state from the database after a restart.

    Called once per process on the first (cold-start) underdog_job cycle.
    This prevents the bot from losing context it had before it was stopped:

    * _MARKET_FIRST_ALERT    — re-populated from recently alerted props so the
      market availability window (detection → removal) is accurate.

    * _prop_market_alerted   — rebuilt from PropOpportunityLog.alert_sent=True
      records within the dedup window so the bot does not re-alert the same
      prop shortly after a restart.

    All restorations are non-fatal: any individual failure is logged at DEBUG
    and skipped so a DB error never prevents the bot from starting.
    """
    global _MARKET_FIRST_ALERT, _prop_market_alerted

    # ── Restore _MARKET_FIRST_ALERT ──────────────────────────────────────────
    try:
        first_alerts = await db.get_first_alert_times_ud(since_hours=_MARKET_FIRST_ALERT_TTL_H)
        restored = 0
        for (player, stat), ts in first_alerts.items():
            key = f"{player}__{stat}"
            if key not in _MARKET_FIRST_ALERT:
                _MARKET_FIRST_ALERT[key] = ts
                restored += 1
        if restored:
            logger.info(
                "State recovery: restored %d market first-alert entries from DB "
                "(window=%dh)",
                restored, _MARKET_FIRST_ALERT_TTL_H,
            )
        else:
            logger.debug("State recovery: no recent market first-alert entries to restore")
    except Exception as _exc:
        logger.debug("State recovery: _MARKET_FIRST_ALERT restore skipped — %s", _exc)

    # ── Restore _prop_market_alerted (alert dedup dict) ───────────────────────
    # Use 2× the dedup window so we cover the full suppression period even
    # when the bot was down for a while (capped at 24 h to match FIRST_ALERT_TTL).
    try:
        _dedup_restore_hours = min(
            24,
            max(2, int(config.UD_ALERT_DEDUP_WINDOW / 1800)),  # 2× half-hours → hours
        )
        recent_alerted = await db.get_recent_alerted_props_for_dedup(
            since_hours=_dedup_restore_hours,
        )
        restored_dedup = 0
        for raw_key, (ts_unix, line_f) in recent_alerted.items():
            # New recovery rows retain provider/source/event/direction.  Keep
            # accepting the compact three-part shape from older databases and
            # lightweight test doubles.
            if isinstance(raw_key, tuple) and len(raw_key) >= 7:
                key = tuple(raw_key)
            else:
                key = tuple(raw_key)
            if key not in _prop_market_alerted:
                _prop_market_alerted[key] = (ts_unix, line_f)
                restored_dedup += 1
        if restored_dedup:
            logger.info(
                "State recovery: restored %d prop-dedup entries from DB (window=%dh)",
                restored_dedup, _dedup_restore_hours,
            )
        else:
            logger.debug("State recovery: no recent prop-dedup entries to restore")
    except Exception as _exc:
        logger.debug("State recovery: _prop_market_alerted restore skipped — %s", _exc)


_STATE_RESTORE_TIMEOUT_SECONDS = 10.0
_SCAN_LOOKUP_TIMEOUT_SECONDS = 15.0


# ── Player Prop Market alert dedup ────────────────────────────────────────────
# Dict[(player, sport, stat_type)] → (last_alert_timestamp_float, last_alerted_line)
#
# An alert is suppressed when BOTH conditions hold:
#   1. time since last alert < config.UD_ALERT_DEDUP_WINDOW (default 3600 s)
#   2. line moved < config.MIN_UNDERDOG_LINE_CHANGE (default 0.5 units)
#
# A significant line movement always fires, even within the window.
# Intentionally module-level: persists across cycles, resets on bot restart.
_prop_market_alerted: dict = {}

# Previous fetched Underdog state used for in-process line-change detection.
# The database remains the durable source of record; this avoids a full-table
# grouped-history scan on every two-minute polling cycle.
_ud_scan_state: dict[tuple[str, str], object] = {}
_ud_known_keys: set[tuple[str, str]] = set()
_ud_scan_db_id: int | None = None

# ── Delivery dedup concurrency lock ───────────────────────────────────────────
# asyncio.Lock that makes the dedup check+record pair atomic across concurrent
# jobs (underdog_job delivery loop, stable_refresh_job, watchlist_job, fpr_job).
# Without this lock, two jobs can both pass _is_prop_deduped before either
# calls _record_prop_alerted, resulting in duplicate Telegram deliveries.
_prop_dedup_lock: asyncio.Lock = asyncio.Lock()

# ── Futures / season-long market filter ───────────────────────────────────────
# These stat types are season-aggregate or award markets, NOT single-game props.
# They cannot be resolved by game-result data and must not enter the alert pipeline.
_FUTURES_STAT_KEYWORDS: frozenset = frozenset({
    "season win",
    "season loss",
    "season era",
    "season hr",
    "season home run",
    "season strikeout",
    "season save",
    "season rbi",
    "season point",
    "season rebound",
    "season assist",
    "season steal",
    "season block",
    "season goal",
    "season kill",
    "season ace",
    "award",
    "cy young",
    " mvp",
    "hall of fame",
    "hof",
    "career",
    "world series",
    "championship",
    "pennant",
    "title",
})


def _is_futures_stat(stat_type: str) -> bool:
    """Return True if the stat_type matches a season-long / futures market keyword."""
    low = stat_type.lower()
    return any(kw in low for kw in _FUTURES_STAT_KEYWORDS)


# ── OddsAPI stat-type → market-key mapping ────────────────────────────────────
# Maps Underdog stat_type (lowercase) → OddsAPI player-prop market key.
# Only sports with entries in _SPORT_PLAYER_PROP_MARKETS (NBA + MLB) are queried.
_UD_TO_ODDS_API_MARKET: dict[str, str] = {
    "points":                  "player_points",
    "rebounds":                "player_rebounds",
    "assists":                 "player_assists",
    "3-pointers made":         "player_threes",
    "three-pointers made":     "player_threes",
    "3pt made":                "player_threes",
    "3-pt made":               "player_threes",
    "steals":                  "player_steals",
    "blocks":                  "player_blocks",
    "hits":                    "player_hits",
    "pitcher strikeouts":      "player_pitcher_strikeouts",
    "strikeouts":              "player_pitcher_strikeouts",
    "total bases":             "player_total_bases",
}


async def _get_odds_api_confirmation(
    sport: str,
    player_name: str = "",
    stat_type: str = "",
    direction: str = "",
    line: float = 0.0,
    *,
    market_type: str = "player_prop",
    selection: str = "",
    team: str = "",
    event: str = "",
    game_time: Optional[datetime] = None,
    db: Optional[Database] = None,
    player_id: str = "",
    event_id: str = "",
) -> Optional[dict]:
    """
    Query optional DraftKings/FanDuel observations as a non-blocking
    confirmation signal.  The sportsbook path is currently disabled by an
    explicit safety gate; PropLine PrizePicks corroboration remains available.

    Player props use the existing cached player-prop fetch.  Moneyline and
    totals use h2h/totals OddsRecord rows written by the existing OddsAPI
    poller only when the future sportsbook opt-in is enabled, so this function
    never starts a second polling cycle.

    Returns None when:
      • the source is disabled/unavailable or no safe match exists
      • any provider/database exception or timeout
    """
    normalized_market = normalize_market(market_type)
    sportsbook_confirmation_enabled = _sportsbook_confirmation_enabled()
    if not sportsbook_confirmation_enabled and normalized_market in {"moneyline", "total"}:
        return None
    source_db = db or _confirmation_db
    pp_history_reader = getattr(source_db, "get_prop_line_history", None)
    pp_pool_reader = getattr(source_db, "get_all_active_prop_line_history_by_line", None)
    pp_available = bool(
        normalized_market == "player_prop"
        and source_db is not None
        and (callable(pp_history_reader) or callable(pp_pool_reader))
    )
    if normalized_market == "player_prop" and is_football_related_sport(sport):
        return None
    if not getattr(config, "ODDS_API_ENABLED", True) and not pp_available:
        return None
    if normalized_market == "player_prop" and _analysis_engine is None and not pp_available:
        return None

    allowed_books = set()
    if sportsbook_confirmation_enabled:
        if getattr(config, "DRAFTKINGS_ENABLED", False):
            allowed_books.add("draftkings")
        if getattr(config, "FANDUEL_ENABLED", False):
            allowed_books.add("fanduel")
    if not allowed_books and not pp_available:
        return None

    target = ConfirmationTarget(
        sport=sport,
        market_type=normalized_market,
        selection=selection,
        player=player_name,
        team=team,
        stat_type=stat_type,
        direction=direction,
        line=line,
        event=event,
        game_time=game_time,
        player_id=player_id,
        event_id=event_id,
    )
    try:
        import asyncio as _asyncio
        if normalized_market == "player_prop":
            # PropLine is an additional, read-only PrizePicks corroboration
            # source. Confirmation must consume the snapshots written by the
            # companion scan; it must never start a second provider request.
            if pp_available:
                try:
                    if callable(pp_pool_reader):
                        # The shared current pool is already source-aware,
                        # freshness-bounded, and removal-safe. Filter its rows
                        # by normalized identity because provider-normalized
                        # stat labels do not preserve UD capitalization or
                        # composite-stat spacing.
                        try:
                            current_pool = await _asyncio.wait_for(
                                pp_pool_reader(providers=("PrizePicks",)),
                                timeout=5.0,
                            )
                        except TypeError:
                            current_pool = await _asyncio.wait_for(
                                pp_pool_reader(),
                                timeout=5.0,
                            )
                        wanted_player = normalize_name(player_name)
                        wanted_sport = str(sport or "").strip().upper()
                        wanted_stat = _normalize_prop_stat(stat_type)
                        pp_history = [
                            prop
                            for prop in (current_pool or {}).values()
                            if normalize_name(getattr(prop, "player_name", ""))
                            == wanted_player
                            and str(getattr(prop, "sport", "") or "").strip().upper()
                            == wanted_sport
                            and _normalize_prop_stat(getattr(prop, "stat_type", ""))
                            == wanted_stat
                        ]
                        pp_history.extend(
                            prop
                            for prop in _propline_current_observations.values()
                            if normalize_name(getattr(prop, "player_name", ""))
                            == wanted_player
                            and str(getattr(prop, "sport", "") or "").strip().upper()
                            == wanted_sport
                            and _normalize_prop_stat(getattr(prop, "stat_type", ""))
                            == wanted_stat
                        )
                    elif callable(pp_history_reader):
                        try:
                            pp_history = await _asyncio.wait_for(
                                pp_history_reader(
                                    "PrizePicks",
                                    player_name,
                                    sport,
                                    stat_type,
                                    limit=30,
                                ),
                                timeout=5.0,
                            )
                        except TypeError:
                            # Preserve compatibility with lightweight test
                            # doubles and older database adapters.
                            pp_history = await _asyncio.wait_for(
                                pp_history_reader(
                                    "PrizePicks",
                                    player_name,
                                    sport,
                                    stat_type,
                                ),
                                timeout=5.0,
                            )
                        pp_history = list(pp_history or [])
                        pp_history.extend(
                            prop
                            for prop in _propline_current_observations.values()
                            if normalize_name(getattr(prop, "player_name", ""))
                            == normalize_name(player_name)
                            and str(getattr(prop, "sport", "") or "").strip().upper()
                            == str(sport or "").strip().upper()
                            and _normalize_prop_stat(getattr(prop, "stat_type", ""))
                            == _normalize_prop_stat(stat_type)
                        )
                    else:
                        pp_history = []
                    pp_observations = []
                    # History is newest-first. Keep only the latest state for
                    # each source projection so an older active row cannot
                    # resurrect a projection whose newest row was removed.
                    latest_by_source: dict[str, object] = {}
                    for row in pp_history or []:
                        source_key = str(
                            getattr(row, "external_id", None)
                            or getattr(row, "game_id", None)
                            or ""
                        )
                        latest_by_source.setdefault(
                            source_key or f"row:{len(latest_by_source)}",
                            row,
                        )
                    for prop in latest_by_source.values():
                        if getattr(prop, "removed", False) is True:
                            continue
                        prop_line = getattr(prop, "line_value", None)
                        if prop_line is None:
                            continue
                        pp_observations.append(
                            observation_from_record(
                                prop,
                                provider="PrizePicks",
                                sport=getattr(prop, "sport", sport),
                                market_type="player_prop",
                                player=getattr(prop, "player_name", player_name),
                                stat_type=getattr(prop, "stat_type", stat_type),
                                # PropLine stores a projection line, not a
                                # sportsbook side. The requested side is the
                                # only safe direction to attach here.
                                direction=direction,
                                line=prop_line,
                                supplemental_source="PropLine",
                            )
                        )
                    pp_result = confirm_market(
                        target,
                        pp_observations,
                        allowed_providers={"PrizePicks"},
                        max_age_seconds=20 * 60,
                    )
                    if pp_result is not None:
                        return pp_result
                except Exception as _pp_exc:
                    logger.debug("PropLine PrizePicks confirmation skipped: %s", type(_pp_exc).__name__)

            if not sportsbook_confirmation_enabled:
                return None
            if not getattr(config, "ODDS_API_ENABLED", True):
                return None
            from engine.analysis import Sport as _Sport
            try:
                sport_enum = _Sport(sport)
            except ValueError:
                return None
            market_key = _UD_TO_ODDS_API_MARKET.get(stat_type.lower().strip())
            if not market_key:
                return None
            lines = await _asyncio.wait_for(
                _analysis_engine.fetch_player_prop_lines(sport_enum),
                timeout=5.0,
            )
            observations = [
                observation_from_record(
                    row,
                    provider=getattr(row, "sportsbook", ""),
                    sport=sport,
                    market_type=getattr(row, "market_key", market_key),
                    player=getattr(row, "player_name", ""),
                    stat_type=stat_type,
                    direction=getattr(row, "description", ""),
                    line=getattr(row, "line", None),
                    odds=getattr(row, "american_odds", None),
                )
                for row in lines
            ]
        else:
            source_db = db or _confirmation_db
            if source_db is None:
                return None
            # The existing h2h/totals poller records both books.  A short
            # freshness window prevents an old event from being presented as
            # current confirmation while tolerating normal poll gaps.
            rows = await _asyncio.wait_for(
                source_db.get_odds_window(
                    sport,
                    datetime.utcnow() - timedelta(hours=4),
                ),
                timeout=5.0,
            )
            observations = [
                observation_from_record(row)
                for row in rows
                if normalize_market(getattr(row, "market_type", "")) == normalized_market
            ]
    except Exception:
        return None

    if normalized_market == "player_prop":
        # Preserve the established display semantics: num_books counts books
        # that carry the same player/stat market, while avg_line/confirmed
        # only use the requested side.
        from engine.provider_confirmation import _matches_identity as _matches
        identity_target = replace(target, direction="")
        identity_rows = [
            row for row in observations
            if row.provider_key in {b.replace(" ", "") for b in allowed_books}
            and _matches(identity_target, row, candidate_names={
                normalize_name(getattr(candidate, "player", ""))
                for candidate in observations
            })
        ]
        directional = [
            row for row in identity_rows
            if row.direction == direction.strip().upper()
        ]
        if not identity_rows:
            return None
        by_provider = {row.provider_key: row for row in identity_rows}
        book_lines = [row.line for row in directional if row.line is not None]
        book_odds = [row.odds for row in directional if row.odds not in (None, 0)]
        num_books = len(by_provider)
        avg_line = sum(book_lines) / len(book_lines) if book_lines else None
        avg_odds = round(sum(book_odds) / len(book_odds)) if book_odds else None
        line_delta = abs(avg_line - line) if avg_line is not None else None
        diff = avg_line - line if avg_line is not None else None
        notes = f"{num_books} book{'s' if num_books != 1 else ''}"
        if avg_line is None:
            notes += " · no direct line match"
        elif abs(diff or 0) < 0.05:
            notes += f" · avg {avg_line:.1f} ✅"
        else:
            notes += f" · avg {avg_line:.1f} ({diff:+.1f} vs UD)"
        return {
            "market_type": normalized_market,
            "num_books": num_books,
            "directional_books": len(book_lines),
            "avg_line": avg_line,
            "avg_odds": avg_odds,
            "line_delta": line_delta,
            "notes": notes,
            "confirmed": avg_line is not None,
            "strong_confirmation": len(book_lines) >= 2 and line_delta is not None and line_delta <= 0.5,
        }

    result = confirm_market(
        target,
        observations,
        allowed_providers=allowed_books,
        max_age_seconds=4 * 60 * 60,
    )
    if result is None:
        return None
    return result


def _get_player_stats_provider():
    global _player_stats_provider
    if _player_stats_provider is None:
        from providers.player_stats import PlayerStatsProvider
        _player_stats_provider = PlayerStatsProvider()
    return _player_stats_provider


async def _fetch_and_compute_hit_rates(
    db: Database,
    player_name: str,
    sport: str,
    stat_type: str,
    current_line: float,
    allow_remote: bool = True,
    cycle_timing: Optional[_CycleTiming] = None,
    preloaded_results: Optional[list] = None,
    source_provider: str = "Underdog",
    supplemental_results: Optional[list] = None,
) -> "Optional[object]":
    """
    Fetch fresh game results (at most once per calendar day per player/stat),
    upsert to DB, then compute and return PlayerHitRates.

    Returns None on any failure so callers can always fall back to PASS.
    """
    from engine.player_results import compute_hit_rates

    today     = datetime.utcnow().date().isoformat()
    cache_key = (player_name, sport, stat_type.lower().strip(), today)

    try:
        if allow_remote and cache_key not in _player_result_fetch_cache:
            provider = _get_player_stats_provider()
            # Guard against unbounded growth: clear the whole set when it exceeds the
            # ceiling.  The date component in each key means old entries are already
            # bypassed by logic; this just frees the memory they occupy.
            if len(_player_result_fetch_cache) >= _PLAYER_RESULT_CACHE_MAX:
                logger.info(
                    "_fetch_and_compute_hit_rates: result cache hit %d entries — clearing",
                    len(_player_result_fetch_cache),
                )
                _player_result_fetch_cache.clear()
            _provider_started = time.monotonic()
            if cycle_timing is not None:
                raw_results = await provider.fetch_results(
                    player_name,
                    sport,
                    stat_type,
                    instrumentation=cycle_timing,
                )
            else:
                raw_results = await provider.fetch_results(
                    player_name, sport, stat_type
                )
            if cycle_timing is not None:
                cycle_timing.provider(
                    _player_provider_label(sport),
                    time.monotonic() - _provider_started,
                )
                cycle_timing.count("provider_enrichment_props")
            for r in raw_results:
                if cycle_timing is not None:
                    await cycle_timing.await_db(
                        "write", "upsert_player_result", db.upsert_player_result(r)
                    )
                else:
                    await db.upsert_player_result(r)
            _player_result_fetch_cache.add(cache_key)
            if raw_results:
                logger.debug(
                    "player_results: fetched %d results for %s / %s",
                    len(raw_results), player_name, stat_type,
                )

        if preloaded_results is not None:
            db_results = list(preloaded_results)
        elif cycle_timing is not None:
            db_results = await cycle_timing.await_db(
                "read",
                "get_player_results",
                db.get_player_results(player_name, sport, stat_type, limit=30),
            )
        else:
            db_results = await db.get_player_results(player_name, sport, stat_type, limit=30)
        if cycle_timing is not None:
            cycle_timing.count("evidence_lookups")
        primary_result_count = len(db_results)
        supplemental_count = 0
        if supplemental_results is None and len(db_results) < config.UD_VALIDATION_MIN_SAMPLES:
            try:
                from providers.player_history import get_player_history_provider
                supplemental_results = await get_player_history_provider().supplement_missing_results(
                    db,
                    player_name,
                    sport,
                    stat_type,
                    min_samples=config.UD_VALIDATION_MIN_SAMPLES,
                    source_provider=source_provider,
                    instrumentation=cycle_timing,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "_fetch_and_compute_hit_rates: supplemental history unavailable "
                    "for %s/%s: %s",
                    player_name,
                    stat_type,
                    exc,
                )
        if supplemental_results:
            db_results = _merge_hit_rate_results(db_results, supplemental_results)
            supplemental_count = max(0, len(db_results) - primary_result_count)
        if not db_results:
            if cycle_timing is not None:
                cycle_timing.count("props_without_evidence")
            return None
        _evidence_processing_started = time.monotonic()
        result = compute_hit_rates(db_results, current_line)
        if cycle_timing is not None:
            cycle_timing.duration(
                "evidence_processing",
                time.monotonic() - _evidence_processing_started,
            )
            if getattr(result, "has_real_data", False) or getattr(
                result, "has_proxy_data", False
            ):
                cycle_timing.count("props_with_evidence")
            else:
                cycle_timing.count("props_without_evidence")
        # Defensive guard: compute_hit_rates must return PlayerHitRates, never a list.
        # If it somehow does, log a warning and return None so the caller falls back to PASS.
        if not hasattr(result, "has_real_data"):
            logger.warning(
                "_fetch_and_compute_hit_rates: unexpected type %s for %s/%s — returning None",
                type(result).__name__, player_name, stat_type,
            )
            return None
        if supplemental_count:
            result = replace(
                result,
                data_source=(
                    "propline"
                    if primary_result_count == 0
                    else "primary+propline"
                ),
                n_supplemental_games=supplemental_count,
                supplemental_sources=("PropLine",),
            )
        return result

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_fetch_and_compute_hit_rates: %s / %s: %s", player_name, stat_type, exc
        )
        return None


def _merge_hit_rate_results(primary: list, supplemental: list) -> list:
    """Merge primary and supplemental game rows without counting one game twice."""
    merged: list = []
    seen_ids: set[tuple[str, str]] = set()
    seen_dates: set[tuple[str, str]] = set()
    seen_idless_dates: set[tuple[str, str]] = set()

    def _parts(row) -> tuple[str, str, str]:
        event_id = str(
            getattr(row, "game_id", "")
            or getattr(row, "event_id", "")
            or ""
        ).strip()
        game_date = str(
            getattr(row, "game_date", "")
            or getattr(row, "date", "")
            or ""
        ).strip()
        stat = str(
            getattr(row, "stat_type", "")
            or getattr(row, "stat", "")
            or ""
        ).lower().strip()
        return event_id, game_date, stat

    for row in [*primary, *supplemental]:
        event_id, game_date, stat = _parts(row)
        duplicate = (
            (event_id, stat) in seen_ids
            if event_id
            else (game_date, stat) in seen_dates
        )
        if event_id and (game_date, stat) in seen_idless_dates:
            duplicate = True
        if duplicate:
            continue
        merged.append(row)
        if event_id:
            seen_ids.add((event_id, stat))
        if game_date:
            seen_dates.add((game_date, stat))
            if not event_id:
                seen_idless_dates.add((game_date, stat))
    merged.sort(
        key=lambda row: str(
            getattr(row, "game_date", "")
            or getattr(row, "date", "")
            or ""
        ),
        reverse=True,
    )
    return merged


def _prizepicks_dedup_player(player_name: str) -> str:
    """Keep the legacy three-part dedup map provider-scoped."""
    return f"{_PRIZEPICKS_DEDUP_PREFIX}{player_name}"


def _track_prizepicks_processing_task(task: asyncio.Task) -> None:
    """Keep deferred evaluations observable and consume task exceptions."""
    _prizepicks_processing_tasks.add(task)

    def _finished(completed: asyncio.Task) -> None:
        _prizepicks_processing_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:  # noqa: BLE001
            logger.exception("prizepicks_cycle: deferred evaluation failed")

    task.add_done_callback(_finished)


async def _run_prizepicks_independent_cycle(
    context,
    *,
    _fetched_props: Optional[list] = None,
    _fetched_sport: Optional[str] = None,
    _processing_locked: bool = False,
    background_processing: bool = False,
) -> None:
    """Evaluate the authorized PrizePicks feed through the shared prop pipeline.

    This is deliberately a companion coroutine of ``underdog_job`` rather than
    another scheduled job.  It owns no scoring, result, or delivery rules:
    those are the same functions used by Underdog.  A provider failure is
    contained here so it can never abort the Underdog scan.
    """
    global _prizepicks_processing_lock
    if _fetched_props is not None and not _processing_locked:
        if _prizepicks_processing_lock is None:
            _prizepicks_processing_lock = asyncio.Lock()
        async with _prizepicks_processing_lock:
            await _run_prizepicks_independent_cycle(
                context,
                _fetched_props=_fetched_props,
                _fetched_sport=_fetched_sport,
                _processing_locked=True,
            )
        return

    db = context.bot_data.get("db")
    bot = getattr(context, "bot", None)
    provider_health = get_health_monitor()
    if db is None or bot is None or _propline_provider is None:
        return
    if not getattr(_propline_provider, "is_available", lambda: False)():
        return

    # Discover the current PropLine catalog, then fetch as many sports as the
    # existing provider pacing and request budget permit.  The cursor only
    # advances by attempted sports, so a budget-limited cycle resumes where it
    # stopped instead of repeatedly favoring the first catalog entries.
    if _fetched_props is None:
        configured_sports = await _get_prizepicks_scan_sports(_propline_provider)
        if not configured_sports:
            logger.info("prizepicks_cycle: no active PrizePicks sports; skipping")
            return
        global _prizepicks_sport_cursor
        start_cursor = _prizepicks_sport_cursor % len(configured_sports)
        fetch_result_for_sport = getattr(
            _propline_provider, "fetch_props_for_sport_result", None
        )
        fetch_for_sport = getattr(_propline_provider, "fetch_props_for_sport", None)
        props = []
        fetched_sports: list[str] = []
        failed_sports: list[str] = []
        fetch_error = ""
        fetch_failure_kind = ""
        fetch_succeeded = False

        # A shared scheduler trigger owns exactly one PropLine sport.  The
        # cursor advances after this attempt so the next shared cycle resumes
        # round-robin coverage without turning the companion into an
        # all-sports polling loop.  Result metadata is still preferred for the
        # production adapter; legacy adapters retain the same one-sport scope.
        sports_to_fetch = 1
        for offset in range(sports_to_fetch):
            sport = configured_sports[(start_cursor + offset) % len(configured_sports)]
            if is_football_related_sport(sport):
                continue
            try:
                if callable(fetch_result_for_sport):
                    fetch_result = await fetch_result_for_sport(
                        sport, bookmaker="prizepicks"
                    )
                    sport_props = list(getattr(fetch_result, "props", []) or [])
                    current_succeeded = bool(getattr(fetch_result, "succeeded", False))
                    current_error = str(getattr(fetch_result, "error", "") or "")
                    current_failure_kind = str(
                        getattr(fetch_result, "failure_kind", "") or ""
                    )
                elif callable(fetch_for_sport):
                    sport_props = list(await fetch_for_sport(sport) or [])
                    current_succeeded = True
                    current_error = ""
                    current_failure_kind = ""
                else:
                    sport_props = list(await _propline_provider.fetch_props() or [])
                    current_succeeded = True
                    current_error = ""
                    current_failure_kind = ""
            except Exception as exc:  # noqa: BLE001
                current_succeeded = False
                sport_props = []
                current_error = str(exc)
                current_failure_kind = (
                    "timeout"
                    if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    else "http"
                )

            attempted = offset + 1
            if current_succeeded:
                fetch_succeeded = True
                fetched_sports.append(sport)
                props.extend(sport_props)
            else:
                failed_sports.append(sport)
                fetch_error = current_error or fetch_error
                fetch_failure_kind = current_failure_kind or fetch_failure_kind
                # A quota/budget/rate-limit response cannot be improved by
                # spending more requests in this cycle. Leave later sports for
                # the next cursor position.
                failure_text = f"{current_failure_kind} {current_error}".lower()
                if any(
                    marker in failure_text
                    for marker in ("quota", "budget", "rate", "429", "backoff")
                ):
                    _prizepicks_sport_cursor = (
                        start_cursor + attempted
                    ) % len(configured_sports)
                    break

            _prizepicks_sport_cursor = (
                start_cursor + attempted
            ) % len(configured_sports)

        sport = ",".join(fetched_sports or failed_sports) or configured_sports[start_cursor]
        logger.info(
            "prizepicks_cycle_started: provider=PrizePicks sport=%s sports=%s discovered=%d",
            fetched_sports[0] if fetched_sports else (
                failed_sports[0] if failed_sports else sport
            ),
            sport,
            len(configured_sports),
        )
        if fetch_succeeded and provider_health is not None:
            provider_health.record_success("PrizePicks")
        if fetch_succeeded:
            _health = get_health_tracker()
            if _health:
                _health.record_provider_fetch("PrizePicks")
        if not fetch_succeeded:
            if provider_health is not None:
                _failure_types = {
                    "timeout": FailureType.TIMEOUT,
                    "quota": FailureType.QUOTA,
                    "http": FailureType.HTTP_ERROR,
                    "configuration": FailureType.UNKNOWN,
                    "disabled": FailureType.UNKNOWN,
                }
                provider_health.record_failure(
                    "PrizePicks",
                    fetch_error or "PropLine fetch failed",
                    _failure_types.get(fetch_failure_kind, FailureType.UNKNOWN),
                )
            _health = get_health_tracker()
            if _health:
                _health.record_provider_error(
                    "PrizePicks", fetch_error or "PropLine fetch failed"
                )
            logger.info(
                "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=false "
                "sports=%s observations=0 persisted=0 evaluated=0 "
                "actionable=0 delivered=0",
                fetched_sports[0] if fetched_sports else (
                    failed_sports[0] if failed_sports else sport
                ),
                sport,
            )
            return
    else:
        sport = _fetched_sport or ""
        props = _fetched_props

    # Compatibility providers may return a mixed-sport batch.  Keep the
    # provider-wide custom endpoint usable while ensuring football never
    # reaches persistence, scoring, evidence, or delivery.
    props = [
        prop for prop in props
        if not is_football_related_sport(getattr(prop, "sport", ""))
    ]
    if is_football_related_sport(sport):
        props = []

    if not props:
        logger.info(
            "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
            "observations=0 persisted=0 evaluated=0 actionable=0 delivered=0",
            sport,
        )
        return

    if background_processing:
        processing_task = asyncio.create_task(
            _run_prizepicks_independent_cycle(
                context,
                _fetched_props=list(props),
                _fetched_sport=sport,
            ),
            name="prizepicks_deferred_evaluation",
        )
        _track_prizepicks_processing_task(processing_task)
        return

    # One observation per provider-owned identity per cycle.  The provider's
    # external/event IDs are retained; the fallback key still keeps provider
    # identity separate from Underdog.
    unique_props: dict[tuple[str, str, str, str], object] = {}
    for prop in props:
        player = str(getattr(prop, "player_name", "") or "").strip()
        sport = str(getattr(prop, "sport", "") or "").upper().strip()
        stat = str(getattr(prop, "stat_type", "") or "").strip()
        try:
            line = float(getattr(prop, "line_value", 0.0))
        except (TypeError, ValueError):
            continue
        if not player or not sport or not stat or line < 0:
            continue
        identity = (
            player,
            sport,
            stat,
            str(
                getattr(prop, "external_id", "")
                or getattr(prop, "event_id", "")
                or getattr(prop, "game_id", "")
                or ""
            ),
        )
        unique_props[identity] = prop

    if not unique_props:
        logger.info(
            "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
            "observations=0 persisted=0 evaluated=0 actionable=0 delivered=0",
            sport,
        )
        return

    # Make this cycle's PropLine observations available to the Underdog
    # confirmation boundary immediately.  The DB write below remains the
    # durable history path; this cache avoids a same-cycle race without
    # issuing another PropLine request.
    _cache_propline_evidence(list(unique_props.values()))

    from engine.ud_scoring import (
        score_ud_prop,
        compute_market_quality,
        detect_market_pressure,
    )
    from engine.player_validator import validate_player_prop
    from engine.ud_bet_decision import make_ud_bet_decision

    def _source_identity(prop: object) -> tuple[str, str]:
        """Return the provider source identity used by normalized history."""
        external_id = str(getattr(prop, "external_id", "") or "").strip()
        event_id = str(
            getattr(prop, "event_id", "")
            or getattr(prop, "game_id", "")
            or ""
        ).strip()
        return external_id, event_id

    # Read the prior source-specific history before inserting this cycle's
    # observation.  The raw observation must be durable even if scoring,
    # enrichment, or delivery later fails, but the new row must not become its
    # own "previous line" for scoring.
    history_by_identity: dict[tuple[str, str, str, str], list] = {}
    for prop in unique_props.values():
        player = str(getattr(prop, "player_name", "") or "").strip()
        prop_sport = str(getattr(prop, "sport", "") or "").upper().strip()
        stat = str(getattr(prop, "stat_type", "") or "").strip()
        external_id, event_id = _source_identity(prop)
        history_rows = []
        try:
            try:
                history_rows = await db.get_prop_line_history(
                    "PrizePicks",
                    player,
                    prop_sport,
                    stat,
                    limit=30,
                    external_id=external_id,
                    game_id=event_id,
                )
            except TypeError:
                # Keep lightweight legacy test doubles and older integrations
                # compatible while the real Database uses source-aware lookup.
                history_rows = await db.get_prop_line_history(
                    "PrizePicks", player, prop_sport, stat, limit=30
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "prizepicks_cycle: history lookup failed for %s/%s: %s",
                player, stat, type(exc).__name__,
            )
        history_by_identity[(
            player,
            prop_sport,
            stat,
            external_id or event_id,
        )] = history_rows or []

    try:
        await db.upsert_prop_line_history_batch(
            "PrizePicks",
            list(unique_props.values()),
            append_snapshot=True,
        )
    except TypeError:
        # Compatibility for old provider test doubles.  Production Database
        # supports append_snapshot and therefore always retains the observation.
        try:
            await db.upsert_prop_line_history_batch(
                "PrizePicks", list(unique_props.values())
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prizepicks_cycle: history persistence failed: %s", type(exc).__name__
            )
            logger.info(
                "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
                "observations=%d persisted=0 evaluated=0 actionable=0 delivered=0",
                sport, len(unique_props),
            )
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("prizepicks_cycle: history persistence failed: %s", type(exc).__name__)
        logger.info(
            "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
            "observations=%d persisted=0 evaluated=0 actionable=0 delivered=0",
            sport, len(unique_props),
        )
        return

    # Universal grading ledger: record every valid PrizePicks observation before
    # scoring/qualification.  This is deliberately independent of the
    # directional opportunity path below.
    _tracking_records = []
    for _tracked_prop in unique_props.values():
        _tracked_player = str(getattr(_tracked_prop, "player_name", "") or "").strip()
        _tracked_sport = str(getattr(_tracked_prop, "sport", "") or "").upper().strip()
        _tracked_stat = str(getattr(_tracked_prop, "stat_type", "") or "").strip()
        _tracked_external, _tracked_event = _source_identity(_tracked_prop)
        if not _tracked_external:
            _tracked_external = (
                f"{_tracked_player}:{_tracked_sport}:{_tracked_stat}:{_tracked_event}"
            )
        _tracking_records.append({
            "external_id": _tracked_external,
            "player_name": _tracked_player,
            "team": str(getattr(_tracked_prop, "team", "") or ""),
            "sport": _tracked_sport,
            "stat_type": _tracked_stat,
            "line_value": getattr(_tracked_prop, "line_value", None),
            "game_time": getattr(_tracked_prop, "game_time", None),
            "game_id": _tracked_event,
            "provider": "PrizePicks",
        })
    _track_props = getattr(db, "track_prop_records", None)
    if callable(_track_props):
        try:
            await _track_props(_tracking_records)
        except Exception as _tracking_exc:  # noqa: BLE001
            logger.warning(
                "prizepicks_cycle: universal tracking write failed: %s",
                type(_tracking_exc).__name__,
            )

    try:
        from engine.health import get_health_tracker as _get_pp_health_tracker
        _pp_health = _get_pp_health_tracker()
        if _pp_health is not None:
            _pp_health.record_provider_database_write(
                "PrizePicks", "prop_line_history"
            )
    except Exception:
        logger.debug("prizepicks_cycle: provider write telemetry unavailable")

    actionable: list[dict] = []
    _pp_candidate_rows: list[dict] = []
    enrichment_budget = 250
    enrichment_used = 0
    evaluated = 0
    directional = 0

    for prop in unique_props.values():
        player = str(getattr(prop, "player_name", "") or "").strip()
        sport = str(getattr(prop, "sport", "") or "").upper().strip()
        stat = str(getattr(prop, "stat_type", "") or "").strip()
        line = float(getattr(prop, "line_value", 0.0) or 0.0)
        external_id_raw, event_id = _source_identity(prop)
        history_rows = history_by_identity.get(
            (player, sport, stat, external_id_raw or event_id),
            [],
        )

        # PropLineHistory stores the current source line.  Adapt its movement
        # fields to the scoring/validation contract without importing an
        # Underdog model or attributing the row to Underdog.
        history = [
            SimpleNamespace(
                line_value=float(getattr(row, "line_value", 0.0) or 0.0),
                prev_line=getattr(row, "prev_line", None),
                removed=bool(getattr(row, "removed", False)),
                line_moved=(
                    getattr(row, "prev_line", None) is not None
                    or bool(getattr(row, "change_count", 0))
                ),
                fetched_at=getattr(row, "fetched_at", None),
            )
            for row in history_rows
        ]
        previous = (
            float(getattr(history_rows[0], "line_value"))
            if history_rows and getattr(history_rows[0], "line_value", None) is not None
            else None
        )
        score = score_ud_prop(
            player_name=player,
            stat_type=stat,
            sport=sport,
            current_line=line,
            prev_line=(previous if previous is not None and abs(line - previous) >= 0.01 else None),
            history=history,
        )
        market_quality = compute_market_quality(stat, line, score)
        market_pressure = detect_market_pressure(previous, history)

        history_snap = None
        try:
            from providers.player_history import get_player_history_provider
            history_snap = await get_player_history_provider().get_snapshot(
                db, player, sport, stat, line, ud_history=history,
                source_provider="PrizePicks",
            )
        except Exception:
            pass
        validation = validate_player_prop(
            player_name=player,
            stat_type=stat,
            current_line=line,
            history=history,
            min_samples=config.UD_VALIDATION_MIN_SAMPLES,
            history_snap=history_snap,
        )

        # Keep the existing enrichment safety budget.  This makes a full
        # multi-sport PropLine response harmless to the public stats providers.
        hit_rates = None
        if enrichment_used < enrichment_budget:
            hit_rates = await _fetch_and_compute_hit_rates(
                db, player, sport, stat, line,
                source_provider="PrizePicks",
            )
            enrichment_used += 1
        decision = None
        if hit_rates is not None:
            decision = make_ud_bet_decision(
                score=score,
                validation=validation,
                current_line=line,
                prev_line=(previous if previous is not None and abs(line - previous) >= 0.01 else None),
                hit_rates=hit_rates,
                sport=sport,
            )
        evaluated += 1

        external_id = str(
            getattr(prop, "external_id", "")
            or f"{player}:{sport}:{stat}:"
            f"{getattr(prop, 'event_id', '') or getattr(prop, 'game_id', '') or ''}"
        )
        _pp_candidate_rows.append({
            "scan_ts": datetime.utcnow(),
            "player_name": player,
            "team": str(getattr(prop, "team", "") or ""),
            "sport": sport,
            "stat_type": stat,
            "line_value": line,
            "provider": "PrizePicks",
            "score_total": getattr(score, "total", None),
            "score_tier": getattr(decision, "decision_tier", None),
            "confidence": getattr(decision, "confidence", None),
            "gate_decision": "EVALUATED",
            "rejection_reason": None,
            "reason_codes": json.dumps(_compute_reason_codes(score, decision)),
            "snapshot_external_id": external_id[:64],
        })

        if decision is None or decision.recommendation not in ("OVER", "UNDER"):
            continue
        directional += 1
        setattr(prop, "score_tier", getattr(decision, "decision_tier", None))
        setattr(prop, "bet_recommendation", decision.recommendation)
        setattr(prop, "bet_confidence", decision.confidence)
        setattr(prop, "market_quality_score", getattr(market_quality, "score", None))
        setattr(prop, "score_total", getattr(score, "total", None))
        try:
            await db.log_prop_opportunity(
                external_id=external_id,
                player_name=player,
                team=str(getattr(prop, "team", "") or ""),
                sport=sport,
                stat_type=stat,
                line_value=line,
                recommendation=decision.recommendation,
                decision_tier=decision.decision_tier,
                confidence=decision.confidence,
                game_time=getattr(prop, "game_time", None),
                game_id=str(
                    getattr(prop, "game_id", "")
                    or getattr(prop, "event_id", "")
                    or ""
                ),
                provider="PrizePicks",
                bet_quality_score=decision.confidence,
                reason_codes=_compute_reason_codes(score, decision),
                downstream_intelligence=_build_downstream_intel(
                    line_delta=(line - previous) if previous is not None else None,
                    evidence={
                        "historical": getattr(validation, "to_dict", lambda: {})(),
                        "market": getattr(decision, "evidence", None)
                        or getattr(decision, "bet_evidence", None),
                    },
                    line=line,
                    bet_quality=getattr(score, "total", None),
                    bet_confidence=decision.confidence,
                    market_quality=getattr(market_quality, "score", None),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prizepicks_cycle: opportunity write failed for %s/%s: %s",
                player, stat, type(exc).__name__,
            )
            continue

        if _tier_delivery_gate(
            sport,
            decision.recommendation,
            float(getattr(score, "total", 0) or 0),
            float(getattr(market_quality, "score", 0) or 0),
            _has_strong_delivery_evidence(
                score=score,
                validation=validation,
                decision=decision,
                intelligence_trace=None,
            ),
            score_tier=getattr(decision, "decision_tier", None),
            confidence=getattr(decision, "confidence", None),
        ):
            actionable.append({
                "provider": "PrizePicks",
                "prop": prop,
                "player": player,
                "sport": sport,
                "stat": stat,
                "line": line,
                "previous": previous or line,
                "external_id": external_id,
                "score": score,
                "validation": validation,
                "decision": decision,
                "market_quality": market_quality,
                "market_pressure": market_pressure,
            })

    try:
        # Refresh score/recommendation metadata on the current row without
        # inserting another snapshot.  The append-only observation was already
        # committed before evaluation above.
        try:
            await db.upsert_prop_line_history_batch(
                "PrizePicks",
                list(unique_props.values()),
                refresh_metadata=True,
            )
        except TypeError:
            # Older test doubles/integrations do not expose the optional
            # metadata-refresh flag; their current-state upsert is still safe.
            await db.upsert_prop_line_history_batch(
                "PrizePicks", list(unique_props.values())
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("prizepicks_cycle: history persistence failed: %s", type(exc).__name__)
        logger.info(
            "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
            "observations=%d persisted=0 evaluated=%d actionable=%d delivered=0",
            sport, len(unique_props), evaluated, len(actionable),
        )
        return

    if _pp_candidate_rows:
        try:
            candidate_logger = getattr(db, "log_prop_candidate_batch", None)
            if asyncio.iscoroutinefunction(candidate_logger):
                candidate_count = await candidate_logger(_pp_candidate_rows)
                logger.debug(
                    "prizepicks_cycle: logged %d evaluated candidates to PropCandidateLog",
                    candidate_count,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "prizepicks_cycle: candidate telemetry write failed: %s",
                type(exc).__name__,
            )

    chat_ids = list(config.allowed_user_ids)
    if not chat_ids:
        logger.info(
            "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
            "observations=%d persisted=%d evaluated=%d directional=%d actionable=%d "
            "delivered=0 (no recipients)",
            sport, len(unique_props), len(unique_props), evaluated, directional, len(actionable),
        )
        return

    actionable = select_strongest_actionable_picks(actionable)
    delivered = 0
    delivery = AlertDelivery(db, bot, chat_ids)
    for candidate in actionable:
        player = candidate["player"]
        sport = candidate["sport"]
        stat = candidate["stat"]
        line = candidate["line"]
        dedup_player = _prizepicks_dedup_player(player)
        if not await _try_claim_delivery_slot(
            player, sport, stat, line, provider="PrizePicks",
            source_id=str(getattr(candidate["prop"], "external_id", "") or ""),
            event_id=str(
                getattr(candidate["prop"], "event_id", "")
                or getattr(candidate["prop"], "game_id", "")
                or ""
            ),
            direction=str(getattr(candidate["decision"], "recommendation", "") or ""),
        ):
            continue
        try:
            result = await delivery.deliver_underdog(
                player_name=player,
                team=str(getattr(candidate["prop"], "team", "") or ""),
                sport=sport,
                stat_type=stat,
                old_line=candidate["previous"],
                new_line=line,
                game_time=getattr(candidate["prop"], "game_time", None),
                game_status=getattr(candidate["prop"], "provider_status", None),
                score=candidate["score"],
                validation=candidate["validation"],
                decision=candidate["decision"],
                market_quality=candidate["market_quality"],
                market_pressure=candidate["market_pressure"],
                pp_line=line,
                standing=True,
                strict_pregame=True,
                source="PrizePicks",
                source_id=str(getattr(candidate["prop"], "external_id", "") or ""),
                event_id=str(
                    getattr(candidate["prop"], "event_id", "")
                    or getattr(candidate["prop"], "game_id", "")
                    or ""
                ),
                delivery_path="prizepicks",
            )
        except Exception as exc:  # noqa: BLE001
            _release_prop_delivery_claim(
                _prop_market_alerted, player, sport, stat, line,
                provider="PrizePicks",
                source_id=str(getattr(candidate["prop"], "external_id", "") or ""),
                event_id=str(
                    getattr(candidate["prop"], "event_id", "")
                    or getattr(candidate["prop"], "game_id", "")
                    or ""
                ),
                direction=str(getattr(candidate["decision"], "recommendation", "") or ""),
            )
            logger.warning(
                "prizepicks_cycle: delivery failed; claim released for %s/%s: %s",
                player, stat, type(exc).__name__,
            )
            continue
        if result.sent:
            delivered += 1
            try:
                if getattr(result, "authoritative_recorded", True):
                    await db.mark_opportunity_alert_sent(
                        candidate["external_id"], stat, provider="PrizePicks"
                    )
                await db.update_prop_lifecycle_state(
                    "PrizePicks", player, sport, stat, "ACTIVE_ALERTED",
                    first_alert_sent_at=datetime.utcnow(),
                    external_id=str(getattr(candidate["prop"], "external_id", "") or ""),
                    game_id=str(
                        getattr(candidate["prop"], "event_id", "")
                        or getattr(candidate["prop"], "game_id", "")
                        or ""
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "prizepicks_cycle: delivery bookkeeping failed for %s/%s: %s",
                    player, stat, type(exc).__name__,
                )
        else:
            _release_prop_delivery_claim(
                _prop_market_alerted, player, sport, stat, line,
                provider="PrizePicks",
                source_id=str(getattr(candidate["prop"], "external_id", "") or ""),
                event_id=str(
                    getattr(candidate["prop"], "event_id", "")
                    or getattr(candidate["prop"], "game_id", "")
                    or ""
                ),
                direction=str(getattr(candidate["decision"], "recommendation", "") or ""),
            )

    logger.info(
        "prizepicks_cycle: provider=PrizePicks sport=%s fetch_succeeded=true "
        "observations=%d persisted=%d evaluated=%d directional=%d actionable=%d delivered=%d",
        sport, len(unique_props), len(unique_props), evaluated, directional, len(actionable), delivered,
    )


# In-memory snapshot cache: market_key -> list[MarketSnapshot]
_snapshot_cache: dict[tuple, list[MarketSnapshot]] = {}


def init_market_engine(registry: ConnectorRegistry) -> None:
    """Call once at startup to register the connector registry."""
    global _registry
    _registry = registry
    logger.info("Market engine initialized with %d connectors", len(registry.connectors))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_reason_codes(
    score:    "Optional[object]",
    decision: "Optional[object]" = None,
) -> list[str]:
    """
    Derive structured reason codes from a UDPropScore (+ optional UDBetDecision).

    Returns a list of string codes like ["STRONG_L5", "LINE_MOVEMENT", "S_TIER"].
    Safe to call with None arguments — returns ["NO_SCORE"].

    Confidence codes (sample depth):
      LOW_SAMPLE    n < 5
      STRONG_L5     5 ≤ n < 10
      STRONG_L10    10 ≤ n < 30
      STRONG_L30    n ≥ 30

    Historical performance:
      HISTORICAL_EDGE  avg_vs_line ≥ 16
      WEAK_HISTORY     avg_vs_line ≤ 4 (and n ≥ 5)

    Market signals:
      LINE_MOVEMENT    move_velocity ≥ 15
      STABLE_LINE      stability ≥ 12
      VOLATILE_LINE    stability ≤ 3

    Tier:
      S_TIER / A_TIER / B_TIER

    Gate outcome (appended when decision is provided):
      GATE:OVER / GATE:UNDER / GATE:DECISION_PASS
    """
    if score is None:
        return ["NO_SCORE"]

    codes: list[str] = []
    n    = getattr(score, "n_history",       0) or 0
    avg  = getattr(score, "avg_vs_line",     0) or 0
    sta  = getattr(score, "stability",       0) or 0
    vel  = getattr(score, "move_velocity",   0) or 0
    tier = getattr(score, "tier",       "PASS")

    # Sample-depth codes
    if n < 5:
        codes.append("LOW_SAMPLE")
    elif n >= 30:
        codes.append("STRONG_L30")
    elif n >= 10:
        codes.append("STRONG_L10")
    else:
        codes.append("STRONG_L5")

    # Historical performance
    if avg >= 16:
        codes.append("HISTORICAL_EDGE")
    elif avg <= 4 and n >= 5:
        codes.append("WEAK_HISTORY")

    # Market signals
    if vel and vel >= 15:
        codes.append("LINE_MOVEMENT")
    if sta >= 12:
        codes.append("STABLE_LINE")
    elif sta <= 3 and n >= 5:
        codes.append("VOLATILE_LINE")

    # Tier code
    _tier_code = {"S": "S_TIER", "A": "A_TIER", "B": "B_TIER"}.get(tier)
    if _tier_code:
        codes.append(_tier_code)

    # Direction is evidence metadata, not a monitoring classification.
    if decision is not None:
        _rec = getattr(decision, "recommendation", None)
        if _rec in ("OVER", "UNDER"):
            codes.append(f"GATE:{_rec}")

    return codes


def _compute_reason_codes_from_scored_dict(p: dict) -> list[str]:
    """
    Derive structured reason codes from a scored_prop dict (built during the main loop).

    Used for PropCandidateLog batch write.  Mirrors _compute_reason_codes() but reads
    the flat dict instead of UDPropScore attributes.
    """
    codes: list[str] = []
    n    = p.get("n", 0) or 0
    avg  = p.get("avg", 0) or 0
    sta  = p.get("sta", 0) or 0
    vel  = p.get("vel", 0) or 0
    tier = p.get("score_tier")

    if n < 5:
        codes.append("LOW_SAMPLE")
    elif n >= 30:
        codes.append("STRONG_L30")
    elif n >= 10:
        codes.append("STRONG_L10")
    else:
        codes.append("STRONG_L5")

    if avg >= 16:
        codes.append("HISTORICAL_EDGE")
    elif avg <= 4 and n >= 5:
        codes.append("WEAK_HISTORY")

    if vel and vel >= 15:
        codes.append("LINE_MOVEMENT")
    if sta >= 12:
        codes.append("STABLE_LINE")
    elif sta <= 3 and n >= 5:
        codes.append("VOLATILE_LINE")

    _tc = {"S": "S_TIER", "A": "A_TIER", "B": "B_TIER"}.get(tier)
    if _tc:
        codes.append(_tc)
    return codes


def _extract_ud_stat_type(selection: str, player: str | None, line: float | None) -> str:
    """
    Extract the stat-type label from an UnderdogConnector selection string.

    UnderdogConnector builds: ``f"{player_name} {stat_type} {line_value}"``
    e.g. ``"Patrick Mahomes Fantasy Points 27.5"``

    We strip the leading player name and the trailing numeric line value to
    arrive at the stable stat category ("Fantasy Points").  This is what we
    store and compare for identity; it does NOT change when the line moves.
    """
    s = selection.replace("[REMOVED]", "").strip()
    # Remove leading player name (may have spaces)
    if player:
        if s.startswith(player):
            s = s[len(player):].strip()
    # Remove trailing numeric token (the line value)
    if line is not None:
        parts = s.split()
        if parts:
            try:
                float(parts[-1])
                parts = parts[:-1]
            except ValueError:
                pass
            s = " ".join(parts)
    return s.strip() or "Unknown"


# ── Connector polling job ──────────────────────────────────────────────────────

async def connector_poll_job(context) -> None:
    """
    Fetch snapshots from all sportsbook connectors, persist them to the DB,
    and refresh the in-memory snapshot cache for consensus analysis.
    """
    if _registry is None:
        logger.debug("connector_poll_job: registry not set, skipping")
        return

    db: Database = context.bot_data.get("db")
    if db is None:
        return

    now = datetime.utcnow()
    snapshots = await _registry.fetch_sportsbook()
    if not snapshots:
        logger.debug("connector_poll_job: no snapshots returned")
        return

    for snap in snapshots:
        record = MarketSnapshotRecord(
            sportsbook   = snap.sportsbook,
            sport        = snap.sport,
            league       = snap.league,
            event        = snap.event,
            market_type  = snap.market_type,
            selection    = snap.selection,
            player       = snap.player,
            team         = snap.team,
            line         = snap.line,
            odds         = snap.odds,
            opening_odds = snap.opening_odds,
            is_pickem    = snap.is_pickem,
            game_time    = snap.game_time,
            recorded_at  = now,
        )
        await db.save_market_snapshot(record)

    # Rebuild in-memory cache
    global _snapshot_cache
    new_cache: dict[tuple, list[MarketSnapshot]] = {}
    for snap in snapshots:
        new_cache.setdefault(snap.market_key, []).append(snap)
    _snapshot_cache = new_cache

    logger.info(
        "connector_poll_job: stored %d snapshots, %d markets cached",
        len(snapshots), len(_snapshot_cache),
    )


# ── Consensus + inefficiency + multi-book steam job ───────────────────────────

async def consensus_check_job(context) -> None:
    """
    Run the consensus engine on cached snapshots.

    1. Detect market inefficiencies (book offering outlier value).
       Dedup via MarketSnapshotRecord(alert_sent=True).
    2. Run multi-book steam detection across DK/FD/other books.
       Dedup via SteamRecord — a SteamRecord IS persisted after every send so
       has_recent_steam_alert() correctly suppresses the next cycle.
    """
    if not _snapshot_cache:
        logger.debug("consensus_check_job: no cached snapshots, skipping")
        return

    db:  Database = context.bot_data.get("db")
    bot           = context.bot
    if db is None:
        return

    chat_ids  = list(config.allowed_user_ids)
    all_snaps = [s for snaps in _snapshot_cache.values() for s in snaps]
    now       = datetime.utcnow()

    # ── 1. Market inefficiency detection ─────────────────────────────────────
    inefficiencies = find_inefficiencies(
        all_snaps,
        outlier_threshold = config.INEFFICIENCY_THRESHOLD,
        min_books         = config.CONSENSUS_MIN_BOOKS,
        value_only        = True,
    )
    consensus_results = compute_consensus(
        all_snaps,
        outlier_threshold = config.INEFFICIENCY_THRESHOLD,
        min_books         = config.CONSENSUS_MIN_BOOKS,
    )
    consensus_by_key: dict[tuple, object] = {
        (cr.sport, cr.event, cr.market_type, cr.selection): cr
        for cr in consensus_results
    }

    from alert_normalizer import normalize_inefficiency, normalize_multibook_steam
    from alert_scope_filter import check as scope_check

    for ineff in inefficiencies:
        if ineff.abs_deviation < config.MIN_INEFFICIENCY_DEVIATION:
            continue

        # ── Early scope check — before any DB query or message formatting ──────
        scope = scope_check(normalize_inefficiency(ineff))
        if not scope.allowed:
            logger.debug("consensus_check_job: inefficiency out of scope — %s", scope.reason)
            continue

        already = await db.has_recent_inefficiency_alert(
            ineff.event, ineff.selection, ineff.sportsbook,
            within_seconds = config.INEFFICIENCY_DEDUP_WINDOW,
        )
        if already:
            continue

        cr = consensus_by_key.get((ineff.sport, ineff.event, ineff.market_type, ineff.selection))
        if cr is None:
            continue

        message = format_inefficiency_alert(ineff, cr)
        counts = await _broadcast_consensus_alert(
            db=db,
            bot=bot,
            chat_ids=chat_ids,
            message=message,
            sport=ineff.sport,
            alert_kind="INEFFICIENCY",
            stat_type=ineff.market_type,
        )
        alert_sent = counts["sent"] > 0
        logger.info(
            "Inefficiency alert: %s | %s | %s | dev=%+d → sent=%s",
            ineff.event, ineff.selection, ineff.sportsbook, ineff.deviation, alert_sent,
        )
        # Persist dedup marker only when alert was actually sent
        if alert_sent:
            await db.save_market_snapshot(MarketSnapshotRecord(
                sportsbook  = ineff.sportsbook,
                sport        = ineff.sport,
                league      = ineff.sport,
                event       = ineff.event,
                market_type = ineff.market_type,
                selection    = ineff.selection,
                odds        = ineff.offered_odds,
                recorded_at = now,
                alert_sent  = True,
            ))

    # ── 2. Multi-book steam detection ────────────────────────────────────────
    steam_inputs = build_multi_book_steam_inputs(all_snaps)
    for (sport, event, market_type, selection), book_snapshots in steam_inputs.items():
        if len(book_snapshots) < 2:
            continue

        # ── Early scope check — before steam computation and DB queries ────────
        scope = scope_check(normalize_multibook_steam(str(sport), str(market_type), event, selection))
        if not scope.allowed:
            logger.debug("consensus_check_job: multi-book steam out of scope — %s", scope.reason)
            continue

        try:
            result = compute_steam_simple(
                market         = event,
                sport          = sport,
                market_type    = market_type,
                selection      = selection,
                book_snapshots = book_snapshots,
            )
        except Exception as exc:
            logger.debug("Multi-book steam compute error: %s", exc)
            continue

        if not result.tier.should_alert:
            continue

        # Dedup: query SteamRecord (written below on send)
        already = await db.has_recent_steam_alert(event, selection)
        if already:
            continue

        books_moved  = [b["sportsbook"] for b in book_snapshots]
        sharp_books  = identify_sharp_books(books_moved)
        first_snap   = book_snapshots[0]
        open_odds    = first_snap.get("open_odds", 0)
        current_odds = first_snap.get("current_odds", 0)

        message = format_steam_multibook_alert(
            event           = event,
            selection       = selection,
            sport           = sport,
            market_type     = market_type,
            steam_score     = result.steam_score,
            steam_direction = result.direction.value,
            books_moved     = books_moved,
            opening_odds    = open_odds,
            current_odds    = current_odds,
            sharp_books     = sharp_books,
        )
        counts = await _broadcast_consensus_alert(
            db=db,
            bot=bot,
            chat_ids=chat_ids,
            message=message,
            sport=str(sport),
            alert_kind="STEAM",
            stat_type=market_type,
        )
        alert_sent = counts["sent"] > 0
        logger.info(
            "Multi-book steam alert: %s | %s | score=%d → sent=%s",
            event, selection, result.steam_score, alert_sent,
        )
        # Persist SteamRecord only when alert was actually sent
        if alert_sent:
            await db.save_steam(SteamRecord(
                alert_type      = "MULTI_BOOK_STEAM",
                sport           = sport,
                market_type     = market_type,
                event           = event,
                selection       = selection,
                opening_odds    = open_odds,
                current_odds    = current_odds,
                steam_score     = result.steam_score,
                steam_direction = result.direction.value,
                books_moved     = ", ".join(books_moved),
                notes           = f"Multi-book steam: {len(books_moved)} books moved",
                detected_at     = now,
                alert_sent      = True,
            ))


# ── Delivery priority helpers (used by underdog_job ranked-delivery phase) ────


def _has_strong_delivery_evidence(
    *,
    score: object = None,
    validation: object = None,
    decision: object = None,
    intelligence_trace: object = None,
) -> bool:
    """Return whether existing scoring/validation data supports delivery.

    This is deliberately a delivery-only check.  It does not change scoring,
    persistence, grading, or whether a prop is evaluated.
    """
    if bool(getattr(validation, "has_supporting_data", False)):
        source = getattr(validation, "data_source", None)
        # Production validation objects carry an explicit source.  Keep
        # source-less legacy fixtures compatible, but never let a declared
        # proxy source qualify a Telegram delivery.
        if not isinstance(source, str):
            return True
        if source.lower() == "real" and int(
            getattr(validation, "n_real_games", 0) or 0
        ) >= 5:
            return True
    for obj in (decision, intelligence_trace):
        if isinstance(obj, dict):
            for key in ("evidence", "bet_evidence", "intelligence_trace"):
                evidence = obj.get(key)
                if isinstance(evidence, dict):
                    source = str(
                        evidence.get("data_source")
                        or evidence.get("source")
                        or ""
                    ).lower()
                    if source in {"real", "game_results", "player_game_results"}:
                        return True
            continue
        if obj is not None:
            for attr in ("evidence", "bet_evidence", "intelligence_trace"):
                value = getattr(obj, attr, None)
                if isinstance(value, dict):
                    source = str(
                        value.get("data_source")
                        or value.get("source")
                        or ""
                    ).lower()
                    if source in {"real", "game_results", "player_game_results"}:
                        return True
    return False


def _tier_delivery_gate(
    sport: str,
    direction: str,
    bq_score: float,
    mq_score: float,
    evidence_available: bool = False,
    score_tier: str | None = None,
    confidence: float | None = None,
) -> bool:
    """
    Canonical directional qualification used by all Underdog paths.

    Extra score/evidence parameters remain for compatibility with existing
    call sites and diagnostics.  They are informational and never gate a
    valid directional pick.
    """
    return _canonical_tier_delivery_gate(
        sport=sport,
        direction=direction,
        bq_score=bq_score,
        mq_score=mq_score,
        evidence_available=evidence_available,
        confidence=confidence,
    )


async def _try_claim_delivery_slot(
    player: str,
    sport: str,
    stat: str,
    line: float,
    provider: str = "Underdog",
    source_id: str = "",
    event_id: str = "",
    direction: str = "",
) -> bool:
    """Atomically check the dedup dict and claim the delivery slot if available.

    Must be called under the asyncio event loop (i.e. with ``await``).

    Returns True  → slot claimed; caller should proceed with delivery.
    Returns False → another path already claimed this candidate; skip delivery.

    The claim is recorded in ``_prop_market_alerted`` BEFORE the actual
    ``deliver_underdog`` call so that a concurrent job (SR / WL / FPR / delivery
    queue) checking the same candidate during network I/O cannot also pass the
    dedup check and cause a duplicate Telegram alert.

    If delivery subsequently fails (rate-limited, Telegram error, filtered,
    etc.), the caller must release the claim.  A claim is a concurrency lock,
    not proof that an alert was delivered.
    """
    async with _prop_dedup_lock:
        if _is_prop_deduped(
            _prop_market_alerted, player, sport, stat, line,
            dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
            min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
            provider=provider,
            source_id=source_id,
            event_id=event_id,
            direction=direction,
        ):
            return False
        # Pre-claim: record immediately so concurrent paths see it as alerted.
        _record_prop_alerted(
            _prop_market_alerted, player, sport, stat, line,
            provider=provider,
            source_id=source_id,
            event_id=event_id,
            direction=direction,
        )
        return True


async def _deliver_full_pool_candidate(
    candidate: dict,
    *,
    db,
    bot,
    chat_ids: list,
) -> tuple[object, bool]:
    """Deliver one already-selected FPR candidate using existing protections."""
    from alerts import DeliveryResult

    player = candidate["player"]
    sport = candidate["sport"]
    stat = candidate["stat_type"]
    line = candidate["line_val"]
    snap = candidate["snap"]
    decision = candidate["decision"]
    source_id = str(
        getattr(snap, "external_id", None)
        or getattr(snap, "id", None)
        or ""
    )
    event_id = str(candidate.get("event_id", "") or "")
    direction = str(getattr(decision, "recommendation", "") or "")

    claimed = await _try_claim_delivery_slot(
        player,
        sport,
        stat,
        line,
        provider=candidate["provider"],
        source_id=source_id,
        event_id=event_id,
        direction=direction,
    )
    if not claimed:
        return DeliveryResult(sent=False), False

    try:
        result = await AlertDelivery(db, bot, chat_ids).deliver_underdog(
            player_name=player,
            team=getattr(snap, "team", "") or "",
            sport=sport,
            stat_type=stat,
            old_line=line,
            new_line=line,
            game_time=getattr(snap, "game_time", None),
            game_status=(
                getattr(snap, "game_status", None)
                or getattr(snap, "provider_status", None)
            ),
            fetched_at=getattr(snap, "fetched_at", None),
            score=candidate["score"],
            validation=candidate["validation"],
            decision=decision,
            market_quality=candidate["market_quality"],
            market_pressure=candidate["market_pressure"],
            pp_line=candidate.get("pp_line"),
            standing=True,
            strict_pregame=True,
            source=candidate["provider"],
            source_id=source_id,
            event_id=event_id,
            delivery_path="full_pool",
        )
        return result, True
    except Exception as exc:  # noqa: BLE001
        _release_prop_delivery_claim(
            _prop_market_alerted,
            player,
            sport,
            stat,
            line,
            provider=candidate["provider"],
            source_id=source_id,
            event_id=event_id,
            direction=direction,
        )
        logger.warning(
            "full_pool_rescan: delivery failed; claim released for "
            "%s/%s/%s: %s",
            candidate["provider"],
            player,
            stat,
            type(exc).__name__,
        )
        return DeliveryResult(sent=False), False


def _cand_priority(c: dict) -> float:
    """Neutral delivery ordering; quality labels never select winners."""
    mc   = 200.0 if c.get("is_meaningful_change") else 0.0
    return mc


def _apply_delivery_diversification(queue: list) -> list:
    """
    Rank all candidates by change priority with a soft diversification penalty.

    The delivery loop applies the total-window cap (10 per 5 minutes) via the
    rate limiter; this function only determines the within-group ordering.

    Soft group penalty: −300 for the 2nd candidate in the same (sport, stat_type)
    pair, −600 for the 3rd+.  High-BQ duplicates can still outrank weaker unique
    picks — the penalty is not a hard cap.
    """
    def _rank_group(group: list) -> list:
        """Sort a single tier group by raw priority, then re-sort with soft penalty."""
        group.sort(key=_cand_priority, reverse=True)
        group_hits: dict = {}
        for c in group:
            key = (c.get("sport", ""), c.get("stat_type", ""))
            n = group_hits.get(key, 0)
            c["_group_rank"] = n
            group_hits[key] = n + 1
        group.sort(
            key=lambda c: _cand_priority(c) - 300.0 * min(c.get("_group_rank", 0), 2),
            reverse=True,
        )
        return group

    result = _rank_group(list(queue))
    queue.clear()
    queue.extend(result)
    return queue


# ── CLV opportunity check job ──────────────────────────────────────────────────

async def clv_check_job(context) -> None:
    """
    Alert when a book's current price leads the projected closing line by
    MIN_CLV_LEAD cents — act-now signal before the market closes.

    This is a *forward-looking* alert: no closing odds exist yet, so we
    do NOT write CLVRecord (that is reserved for post-close results computed
    with real closing odds via compute_clv()).

    Dedup: we write a MarketSnapshotRecord(alert_sent=True) for the specific
    book/market/selection so has_recent_inefficiency_alert() suppresses
    re-alerts within CLV_DEDUP_WINDOW seconds.
    """
    if not _snapshot_cache:
        logger.debug("clv_check_job: no cached snapshots, skipping")
        return

    db:  Database = context.bot_data.get("db")
    bot           = context.bot
    if db is None:
        return

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()

    from alert_normalizer import normalize_clv
    from alert_scope_filter import check as scope_check

    for snap_group in _snapshot_cache.values():
        if len(snap_group) < 2:
            continue

        for snap in snap_group:
            if snap.odds == 0 or snap.is_pickem:
                continue

            opp = build_clv_opportunity(
                snap,
                snap_group,
                min_books = config.CONSENSUS_MIN_BOOKS,
                min_lead  = config.MIN_CLV_LEAD,
            )
            if opp is None or not opp.is_actionable:
                continue

            # ── Early scope check — before dedup query and message formatting ──
            scope = scope_check(normalize_clv(opp))
            if not scope.allowed:
                logger.debug("clv_check_job: opportunity out of scope — %s", scope.reason)
                continue

            # Dedup via MarketSnapshotRecord (same table used for inefficiency dedup)
            already = await db.has_recent_inefficiency_alert(
                snap.event,
                snap.selection,
                snap.sportsbook,
                within_seconds = config.CLV_DEDUP_WINDOW,
            )
            if already:
                continue

            message = format_clv_opportunity_alert(opp)
            counts = await _broadcast_consensus_alert(
                db=db,
                bot=bot,
                chat_ids=chat_ids,
                message=message,
                sport=str(snap.sport),
                alert_kind="CLV",
                stat_type=snap.market_type,
            )
            alert_sent = counts.get("sent", 0) > 0
            logger.info(
                "CLV opportunity: %s | %s | %s | lead=%+d → sent=%s",
                opp.event, opp.selection, opp.sportsbook, opp.clv_lead, alert_sent,
            )
            # Persist dedup marker only when alert was actually sent
            if alert_sent:
                await db.save_market_snapshot(MarketSnapshotRecord(
                    sportsbook  = snap.sportsbook,
                    sport        = snap.sport,
                    league       = snap.league,
                    event        = snap.event,
                    market_type = snap.market_type,
                    selection    = snap.selection,
                    odds         = snap.odds,
                    recorded_at = now,
                    alert_sent  = True,
                ))

            break  # one alert per market key per cycle


# ── Shared Underdog + PropLine monitoring cycle ────────────────────────────────

def _shared_monitoring_cycle(cycle_branch):
    """Run Underdog and PropLine from one trigger and await both branches."""

    @wraps(cycle_branch)
    async def _run_shared_cycle(context, *args, **kwargs):
        global _prizepicks_cycle_task

        # The PropLine branch is created at the exact same scheduler trigger as
        # the Underdog branch.  It is awaited below, so it cannot become an
        # independent cadence or outlive the shared cycle.
        _prizepicks_cycle_task = asyncio.create_task(
            _run_prizepicks_independent_cycle(context),
            name="shared_monitoring_propline",
        )
        cycle_completed = False
        try:
            result = await cycle_branch(context, *args, **kwargs)
        except asyncio.CancelledError:
            _prizepicks_cycle_task.cancel()
            await asyncio.gather(_prizepicks_cycle_task, return_exceptions=True)
            raise
        except BaseException:
            # The provider branch still belongs to this cycle. Wait for it
            # before allowing the scheduler to consider the cycle finished.
            pp_result = await asyncio.gather(
                _prizepicks_cycle_task, return_exceptions=True
            )
            if pp_result and isinstance(pp_result[0], BaseException):
                logger.error(
                    "shared_monitoring_cycle: PropLine branch failed while "
                    "Underdog branch raised: %s",
                    pp_result[0],
                )
            raise
        else:
            pp_result = await asyncio.gather(
                _prizepicks_cycle_task, return_exceptions=True
            )
            if pp_result and isinstance(pp_result[0], BaseException):
                logger.error(
                    "shared_monitoring_cycle: PropLine branch failed: %s",
                    pp_result[0],
                )
            cycle_completed = True
            return result
        finally:
            if _prizepicks_cycle_task is not None:
                _prizepicks_cycle_task = None
            if cycle_completed:
                health = get_health_tracker()
                if health is not None:
                    health.record_monitoring_cycle_complete()
                logger.info("shared_monitoring_cycle: completed")

    return _run_shared_cycle


# ── Underdog pick'em branch ────────────────────────────────────────────────────

@_shared_monitoring_cycle
async def underdog_job(context) -> None:
    """
    Fetch Underdog Fantasy pick'em projections and alert on prop changes.

    Prop identity uses (player_name, stat_type) where stat_type is the true
    stat category extracted from the selection string — NOT snap.market_type
    which is always "Pick'em".  This ensures "Patrick Mahomes Fantasy Points"
    is a stable identity even when the line moves from 27.5 → 29.5.

    Alert triggers:
      - line_changed: abs(new_line - prev_line) >= MIN_UNDERDOG_LINE_CHANGE
      - is_removed:   selection contains "[REMOVED]"
    """
    if _registry is None:
        return

    db:  Database = context.bot_data.get("db")
    bot           = context.bot
    if db is None:
        return

    global _cold_start_done, _ud_scan_state, _ud_known_keys, _ud_scan_db_id
    is_cold_start = not _cold_start_done   # True only on the very first successful fetch
    _new_db_context = _ud_scan_db_id != id(db)
    if _new_db_context:
        # Isolated callers/tests may provide a new database double for each
        # invocation.  Never leak the previous database's in-memory state.
        _ud_scan_state = {}
        _ud_known_keys = set()
        _ud_scan_db_id = id(db)

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()
    global _cycle_sequence
    _cycle_sequence += 1
    _cycle_timing = _CycleTiming(_cycle_sequence)
    _job_activity_start("underdog")
    _cycle_timing.start("state_restore")
    logger.info(
        "underdog_cycle_started: cycle_id=%d cold_start=%s active_jobs=%s",
        _cycle_sequence, is_cold_start, sorted(_active_job_names()),
    )

    _health = get_health_tracker()
    if _health:
        _health.record_job_started("underdog_job")

    # Claim the full-scan slot before cold-start state restoration.  Restoring
    # dedup/lifecycle state can itself be expensive on a large SQLite history;
    # FPR and Stable Refresh must not start competing full-pool reads during
    # that window.
    global _ud_full_scan_running
    if _ud_full_scan_running:
        logger.info(
            "underdog_job: full scan in progress — deferring overlapping trigger"
        )
        if _health:
            _health.record_job_run("underdog_job")
        _job_activity_end("underdog")
        return
    _ud_full_scan_running = True

    # ── Evict stale dedup entries ─────────────────────────────────────────────
    # _prop_market_alerted keys are (player, sport, stat_type); values are
    # (timestamp_float, line).  Remove any entry whose timestamp is older than
    # 2× UD_ALERT_DEDUP_WINDOW so the dict never grows without bound across days.
    _dedup_evict_cutoff = now.timestamp() - (config.UD_ALERT_DEDUP_WINDOW * 2)
    _stale_keys = [
        _k for _k, (_ts, _) in _prop_market_alerted.items()
        if _ts < _dedup_evict_cutoff
    ]
    for _k in _stale_keys:
        del _prop_market_alerted[_k]
    if _stale_keys:
        logger.debug(
            "underdog_job: evicted %d stale dedup entries (window×2=%ds, remaining=%d)",
            len(_stale_keys), config.UD_ALERT_DEDUP_WINDOW * 2, len(_prop_market_alerted),
        )

    # ── Evict stale _MARKET_FIRST_ALERT entries ───────────────────────────────
    # Keys are "player__stat_type" → datetime.  Entries older than
    # _MARKET_FIRST_ALERT_TTL_H hours are removed so the dict never grows
    # without bound.  The removal path deletes entries inline (line 1639), but
    # props that are never removed would otherwise accumulate indefinitely.
    _mfa_cutoff = now - timedelta(hours=_MARKET_FIRST_ALERT_TTL_H)
    _mfa_stale  = [_k for _k, _v in _MARKET_FIRST_ALERT.items() if _v < _mfa_cutoff]
    for _k in _mfa_stale:
        del _MARKET_FIRST_ALERT[_k]
    if _mfa_stale:
        logger.debug(
            "underdog_job: evicted %d stale _MARKET_FIRST_ALERT entries "
            "(ttl=%dh, remaining=%d)",
            len(_mfa_stale), _MARKET_FIRST_ALERT_TTL_H, len(_MARKET_FIRST_ALERT),
        )

    # On first run: restore module-level state from DB (market availability tracking)
    if is_cold_start:
        try:
            await asyncio.wait_for(
                _init_state_from_db(db),
                timeout=_STATE_RESTORE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # Recovery is a best-effort restart aid.  It must not prevent the
            # live Underdog feed from starting when a large SQLite history is
            # slow to read.
            logger.warning(
                "underdog_job: state recovery exceeded %.1fs; "
                "continuing with live feed",
                _STATE_RESTORE_TIMEOUT_SECONDS,
            )
    _cycle_timing.finish("state_restore")
    _cycle_timing.start("fetch_parse")

    # ── Concurrency guard — fast new-prop path when full scan is already running ──
    # max_instances=2 allows a second underdog_job to start while the first is still
    # scoring.  If the primary scan is active, the secondary instance only fetches +
    # detects new props (fast, ~2–5 s) and returns immediately so the 2-min cadence
    # is preserved without duplicating heavy scoring work.
    # Memory baseline — log current RSS before the heavy scan to diagnose OOM kills.
    # Uses VmRSS (/proc/self/status) which reflects actual live RSS rather than the
    # all-time high-water mark returned by resource.ru_maxrss.
    _mem_before = _rss_mb()

    try:
        snapshots = await _registry.fetch_pickem()
    except Exception as _fetch_exc:
        provider_health = get_health_monitor()
        if provider_health is not None:
            failure_type = (
                FailureType.TIMEOUT
                if isinstance(_fetch_exc, (asyncio.TimeoutError, TimeoutError))
                else FailureType.HTTP_ERROR
            )
            provider_health.record_failure("Underdog", str(_fetch_exc), failure_type)
        if _health:
            _health.record_provider_error("Underdog", str(_fetch_exc))
            _health.record_job_fail("underdog_job", str(_fetch_exc))
        logger.exception("underdog_job: fetch_pickem failed: %s", _fetch_exc)
        _cycle_timing.finish("fetch_parse")
        logger.info("underdog_cycle_timing: %s", _cycle_timing.summary(0, 0))
        _job_activity_end("underdog")
        _release_underdog_scan_slot()
        return

    # ConnectorRegistry deliberately contains provider exceptions so one feed
    # cannot abort other connectors.  Read the Underdog connector's explicit
    # outcome to distinguish a legitimate empty feed from a swallowed failure.
    _underdog_fetch_error = ""
    try:
        _underdog_connector = next(
            (
                connector
                for connector in _registry.pickem_connectors
                if getattr(connector, "name", "") == "Underdog"
            ),
            None,
        )
        if _underdog_connector is not None:
            _underdog_fetch_error = str(
                getattr(_underdog_connector, "last_fetch_error", "") or ""
            )
    except Exception:
        _underdog_connector = None

    provider_health = get_health_monitor()
    if _underdog_fetch_error:
        if provider_health is not None:
            _error_lower = _underdog_fetch_error.lower()
            _failure_type = (
                FailureType.BLOCKED
                if "403" in _error_lower or "blocked" in _error_lower
                else (
                    FailureType.TIMEOUT
                    if "timeout" in _error_lower or "timed out" in _error_lower
                    else FailureType.HTTP_ERROR
                )
            )
            provider_health.record_failure(
                "Underdog", _underdog_fetch_error, _failure_type
            )
        if _health:
            _health.record_provider_error("Underdog", _underdog_fetch_error)
            _health.record_job_fail("underdog_job", _underdog_fetch_error)
        logger.warning(
            "underdog_job: connector fetch failed: %s",
            _underdog_fetch_error,
        )
        _cycle_timing.finish("fetch_parse")
        logger.info("underdog_cycle_timing: %s", _cycle_timing.summary(0, 0))
        _job_activity_end("underdog")
        _release_underdog_scan_slot()
        return
    if provider_health is not None and _underdog_connector is not None:
        provider_health.record_success("Underdog")

    _cycle_timing.finish("fetch_parse")
    _cycle_timing.start("active_pool")

    if not snapshots:
        logger.debug("underdog_job: no pick'em snapshots")
        _cycle_timing.finish("active_pool")
        logger.info("underdog_cycle_timing: %s", _cycle_timing.summary(0, 0))
        _job_activity_end("underdog")
        if _health:
            _health.record_provider_fetch("Underdog")
            _health.record_job_run("underdog_job")   # empty response = successful run
        _release_underdog_scan_slot()
        return

    if _health:
        _health.record_provider_fetch("Underdog")

    ud_snaps = [
        s for s in snapshots
        if s.sportsbook == "Underdog"
        and not is_football_related_sport(getattr(s, "sport", ""))
    ]
    # Game markets share the pick'em feed but not the player-prop scoring
    # model. Persist them in the existing canonical snapshot history so
    # rotation/removal remains observable, then keep them out of prop scoring.
    ud_game_markets = [
        s for s in ud_snaps
        if getattr(s, "is_moneyline", False) is True
        or getattr(s, "is_game_market", False) is True
    ]
    if ud_game_markets:
        for _gm in ud_game_markets:
            await _cycle_timing.await_db(
                "write",
                "save_market_snapshot_underdog_game_market",
                db.save_market_snapshot(MarketSnapshotRecord(
                    sportsbook=_gm.sportsbook, sport=_gm.sport, league=_gm.league,
                    event=_gm.event,
                    market_type=(
                        "Moneyline" if getattr(_gm, "is_moneyline", False)
                        else (_gm.market_type or "Total (O/U)")
                    ),
                    selection=_gm.selection,
                    player=None, team=_gm.team, line=_gm.line, odds=_gm.odds,
                    opening_odds=_gm.opening_odds, is_pickem=True,
                    game_time=_gm.game_time, recorded_at=datetime.utcnow(),
                )),
            )
        ud_snaps = [s for s in ud_snaps if s not in ud_game_markets]
    _cycle_timing.count("active_pool_fetched", len(snapshots))
    _cycle_timing.count("active_pool_sportsbook_filtered", len(snapshots) - len(ud_snaps))
    # Capture count before the list is cleared later for memory recovery.
    # record_underdog_scan() at end-of-cycle reads this variable so it
    # always reflects the actual number of snapshots processed this cycle.
    _n_ud_snaps_this_cycle: int = len(ud_snaps)

    

    # Pre-delivery freshness map — (player, stat_type) → latest known line from this fetch.
    # Built once per scan from the same ud_snaps that scoring uses.  Every alert path
    # confirms its candidate line against this map before delivery, making the freshness
    # invariant explicit and guarding against future refactors that might decouple scoring
    # from delivery.  Within a single scan these always match (same snap object).
    _current_scan_line_map: dict[tuple[str, str], float] = {}
    for _fsnap in ud_snaps:
        if not _fsnap.player or "[REMOVED]" in (_fsnap.selection or ""):
            continue
        _fstat = _extract_ud_stat_type(_fsnap.selection, _fsnap.player, _fsnap.line)
        _current_scan_line_map[(_fsnap.player, _fstat)] = _fsnap.line or 0.0

    if not ud_snaps:
        logger.debug("underdog_job: no Underdog pick'em snapshots in response")
        _cycle_timing.finish("active_pool")
        logger.info("underdog_cycle_timing: %s", _cycle_timing.summary(0, 0))
        _release_underdog_scan_slot()
        _job_activity_end("underdog")
        if _health:
            _health.record_job_run("underdog_job")
        return

    _cycle_timing.count("active_pool_props", len(ud_snaps))

    # Summary counters — emitted as a single INFO line after the loop.
    # Monitoring reports counts and neutral evaluation metrics; score tiers are
    # scoring metadata, not monitoring states.
    _n_scored:        int       = 0
    _n_qualified:     int       = 0   # line-change props that passed the scoring gate
    _n_removed:       int       = 0   # props with [REMOVED] marker
    _n_new_prop:      int       = 0   # first-appearance props detected this cycle
    _n_new_prop_sent:      int       = 0   # immediate new-prop alerts delivered
    _n_cold_start_scored: int       = 0   # props scored during the one-time cold-start pass
    _n_standing_sent:      int       = 0   # standing opportunity alerts delivered (4A)
    _n_unchanged_skipped: int       = 0   # props with unchanged lines — stable, no re-score needed
    _n_lc_sent:            int       = 0   # line-change alerts delivered this cycle
    _cold_start_records:  list       = []  # records buffered for bulk save at end of cold-start
    _incremental_records: list       = []  # incremental records batched for end-of-loop bulk save
    _scored_props:        list[dict] = []  # all scored props this cycle — for end-of-cycle debug log
    _next_ud_scan_state:  dict[tuple[str, str], object] = {}
    _processed_keys:      set        = set()   # (player, stat_type) pairs handled in main loop
    # Batch for the end-of-cycle new-prop digest sent after the loop.
    # Each entry: {player, stat_type, sport, team, line, score, immediate, game_time}
    _new_props_batch: list[dict] = []
    # Lifecycle state queues — populated during loop, applied AFTER bridge so
    # PropLineHistory rows exist before we try to update them.
    _lifecycle_alerted:  list[tuple[str, str, str]] = []   # (player, sport, stat_type) → ACTIVE_ALERTED
    _lifecycle_removed:  list[tuple[str, str, str]] = []   # (player, sport, stat_type) → REMOVED
    # Delivery queue — candidates from all three alert paths (new-prop, line-change,
    # standing) collected during the scan loops and delivered together in ranked
    # priority order after the standing scan finishes.  Ranked delivery ensures the
    # best picks get rate-limit slots first when more qualify than the window allows.
    _delivery_queue:     list[dict]                 = []
    _snapshot_intentionally_skipped = 0
    _snapshot_intended = 0
    _snapshot_persisted = 0
    _snapshot_failed_batches = 0
    _snapshot_failed_records = 0
    _snapshot_retry_count = 0
    _persistence_ok = True
    # Scoring data for every scored prop — bulk-written to PropLineHistory at end of
    # scan cycle so /picks and /slip can apply tier-aware delivery gates.
    # Key: (player, sport, stat_type) → (mq_score: int, bq_score: float).
    _plh_mq_scores:      dict                       = {}
    # Per-sport pipeline stage counters — emitted in debug summary (#7 diagnostics)
    _sport_raw:           dict[str, int] = {}  # daily props: non-removed, non-futures
    _sport_futures:       dict[str, int] = {}  # season-long/futures props (tracked, not scored)
    _sport_move_detected: dict[str, int] = {}  # significant line moves (≥MIN_LINE_CHANGE)
    _sport_gated:         dict[str, int] = {}  # passed full scoring gate (actionable)
    _sport_actionable:    dict[str, int] = {}  # scan-qualified props by sport
    _sport_removed:       dict[str, int] = {}  # [REMOVED] feed entries by sport
    _cycle_timing.finish("active_pool")
    _cycle_timing.start("scoring_evidence")

    # Universal grading ledger: persist every valid active daily prop before
    # any scoring, qualification, strongest-pick, or delivery gate runs.
    # Futures/season-long markets have no single player-game settlement event
    # and remain represented by the monitoring history instead.
    _tracking_records = []
    for _tracked_snap in ud_snaps:
        if "[REMOVED]" in (_tracked_snap.selection or ""):
            continue
        _tracked_player = str(_tracked_snap.player or "").strip()
        _tracked_sport = str(_tracked_snap.sport or "").upper().strip()
        _tracked_stat = _extract_ud_stat_type(
            _tracked_snap.selection,
            _tracked_snap.player,
            _tracked_snap.line,
        )
        if (
            not _tracked_player
            or _tracked_player == "Unknown"
            or not _tracked_sport
            or not _tracked_stat
            or _is_futures_stat(_tracked_stat)
        ):
            continue
        try:
            _tracked_line = float(_tracked_snap.line or 0.0)
        except (TypeError, ValueError):
            continue
        if _tracked_line < 0:
            continue
        _tracked_game_id = str(getattr(_tracked_snap, "game_id", "") or "").strip()
        _tracked_external = str(
            getattr(_tracked_snap, "external_id", "") or ""
        ).strip()
        if not _tracked_external:
            _tracked_external = (
                f"{_tracked_player}:{_tracked_sport}:{_tracked_stat}:{_tracked_game_id}"
            )
        _tracking_records.append({
            "external_id": _tracked_external,
            "player_name": _tracked_player,
            "team": str(_tracked_snap.team or ""),
            "sport": _tracked_sport,
            "stat_type": _tracked_stat,
            "line_value": _tracked_line,
            "game_time": getattr(_tracked_snap, "game_time", None),
            "game_id": _tracked_game_id,
            "provider": "Underdog",
        })
    _track_props = getattr(db, "track_prop_records", None)
    if callable(_track_props) and _tracking_records:
        try:
            await _cycle_timing.await_db(
                "write",
                "track_prop_records",
                _track_props(_tracking_records),
            )
        except Exception as _tracking_exc:  # noqa: BLE001
            logger.warning(
                "underdog_job: universal tracking write failed: %s",
                type(_tracking_exc).__name__,
            )

    try:
        # Use the preceding fetched feed for normal-cycle comparisons.  On a
        # restart there is intentionally no movement classification on the
        # first fresh feed, which prevents false alerts while the durable
        # recovery queries are best-effort and may be slow on a large SQLite DB.
        if is_cold_start:
            known_keys = {
                (snap.player or "Unknown", _extract_ud_stat_type(
                    snap.selection, snap.player, snap.line,
                ))
                for snap in ud_snaps
            }
            recent_by_key = {}
            _cycle_timing.count("history_lookups_bypassed")
        elif _new_db_context:
            # Compatibility path for direct callers that start with an already
            # warm bot flag but have no process-local scan state.
            recent_by_key = await _cycle_timing.await_db(
                "read",
                "get_latest_underdog_snapshot_per_prop",
                db.get_latest_underdog_snapshot_per_prop(),
            )
            known_keys = await _cycle_timing.await_db(
                "read",
                "get_known_underdog_prop_keys",
                db.get_known_underdog_prop_keys(),
            )
            _cycle_timing.count("latest_snapshot_props_returned", len(recent_by_key))
            _cycle_timing.count("known_prop_keys_returned", len(known_keys))
        else:
            recent_by_key = _ud_scan_state
            known_keys = _ud_known_keys
            _cycle_timing.count("latest_snapshot_props_returned", len(recent_by_key))
            _cycle_timing.count("known_prop_keys_returned", len(known_keys))
    
        for snap in ud_snaps:
            is_removed = "[REMOVED]" in snap.selection
            if is_removed:
                _n_removed += 1
                _removed_sport = (snap.sport or "UNKNOWN").upper()
                _sport_removed[_removed_sport] = _sport_removed.get(_removed_sport, 0) + 1
            player = snap.player or "Unknown"

            # Extract stat-type before any counters so futures are correctly separated.
            stat_type = _extract_ud_stat_type(snap.selection, snap.player, snap.line)

            # Separate season-long futures / award markets from daily props.
            # Count them in _sport_futures so diagnostics show the split clearly.
            if not is_removed and _is_futures_stat(stat_type):
                _sp_fut = snap.sport or "UNKNOWN"
                _sport_futures[_sp_fut] = _sport_futures.get(_sp_fut, 0) + 1
                _snapshot_intentionally_skipped += 1
                continue

            # Stage 1: daily prop count per sport (non-removed, non-futures)
            if not is_removed:
                _sp_raw = snap.sport or "UNKNOWN"
                _sport_raw[_sp_raw] = _sport_raw.get(_sp_raw, 0) + 1
    
            # Detect first-ever appearance: (player, stat) not in DB at all yet
            is_new_prop = not is_removed and (player, stat_type) not in known_keys
    
            # Look up the previous record for this player + stat combo
            prev_record  = recent_by_key.get((player, stat_type))
            line_changed = False
            prev_line: Optional[float] = None
    
            if prev_record is not None and snap.line is not None:
                # Production cycles use compact in-memory state; compatibility
                # callers/tests may still provide ORM records.
                _previous_line = getattr(prev_record, "line_value", None)
                if _previous_line is not None and _previous_line != snap.line:
                    line_changed = True
                    prev_line    = _previous_line

            _path_kind = (
                "new_prop" if is_new_prop
                else "line_movement" if not is_removed and line_changed
                else None
            )
            _path_started = time.monotonic() if _path_kind else None

            # Stage 3 — movement detected: significant line move, non-cold-start
            if (
                not is_removed and not is_cold_start
                and line_changed and prev_line is not None
                and abs((snap.line or 0.0) - prev_line) >= config.MIN_UNDERDOG_LINE_CHANGE
            ):
                _sp_move = snap.sport or "UNKNOWN"
                _sport_move_detected[_sp_move] = _sport_move_detected.get(_sp_move, 0) + 1
    
            should_alert = is_removed or (
                line_changed
                and prev_line is not None
                and abs(snap.line - prev_line) >= config.MIN_UNDERDOG_LINE_CHANGE
            )
    
            # ── Scoring gate ─────────────────────────────────────────────────────
            from engine.ud_scoring import (
                score_ud_prop, UDPropScore,
                _score_historical_activity_legacy,  # for cold-start before/after comparison
                compute_market_quality, detect_market_pressure,
                _HIGH_FLOOR_STATS,
            )
            from engine.player_validator import validate_player_prop
            from engine.ud_bet_decision import make_ud_bet_decision
            from alerts import DeliveryResult
            score:              Optional[UDPropScore] = None
            ud_result:          DeliveryResult        = DeliveryResult(sent=False)
            _lc_odds_confirm:   Optional[dict]        = None    # initialised here so `if ud_result.sent` always sees it
            np_immediate:       bool                  = False   # set inside is_new_prop branch
            _market_move_sent:  bool                  = False   # lightweight market-move alert
            validation:         Optional[object]      = None    # PlayerPropValidation or None
            decision:           Optional[object]      = None    # UDBetDecision or None
            hit_rates:          Optional[object]      = None    # PlayerHitRates or None
            _hist_snap:         Optional[object]      = None    # evidence snapshot
            market_quality:     Optional[object]      = None    # MarketQuality — display context
            market_pressure:    Optional[object]      = None    # MarketPressureFlag — warning only
            _dq_collected:      bool                  = False   # True when this snap is queued for ranked delivery
    
            if is_new_prop:
                # ── New-prop path ────────────────────────────────────────────────
                _n_new_prop += 1
                line_val = snap.line or 0.0
                # Load history even for new props — needed for validation gate.
                # Truly first-ever props return empty list → validation blocks alert.
                np_history = await _cycle_timing.await_db(
                    "read",
                    "get_ud_prop_history",
                    db.get_ud_prop_history(player, stat_type, limit=30),
                )
                score = _cycle_timing.sync_operation("core_scoring", "score_ud_prop", score_ud_prop,
                    player_name  = player,
                    stat_type    = stat_type,
                    sport        = snap.sport or "UNKNOWN",
                    current_line = line_val,
                    prev_line    = None,
                    history      = np_history,
                )
                market_quality  = compute_market_quality(stat_type, line_val, score)
                market_pressure = detect_market_pressure(None, np_history)
                _plh_mq_scores[(player, snap.sport or "UNKNOWN", stat_type)] = (
                    int(getattr(market_quality, "score", 0)),
                    float(score.total if score else 0),
                )
                _processed_keys.add((player, stat_type))
                # Validation is evidence context for evaluation and delivery.
                # First-seen props must still be evaluated and tracked; the
                # canonical Telegram gate below decides whether they can be sent.
                _hist_snap = None
                try:
                    from providers.player_history import get_player_history_provider
                    _hist_snap = await get_player_history_provider().get_snapshot(
                        db,
                        player,
                        snap.sport or "UNKNOWN",
                        stat_type,
                        line_val,
                        ud_history=np_history,
                        source_provider="Underdog",
                        instrumentation=_cycle_timing,
                    )
                except Exception:
                    _hist_snap = None
                validation = validate_player_prop(
                    player_name  = player,
                    stat_type    = stat_type,
                    current_line = line_val,
                    history      = np_history,
                    min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                    history_snap = _hist_snap,
                )
                # Immediate criteria controls whether a new prop gets remote
                # history fetched in this pass.  It is not a Telegram
                # qualification gate; the shared delivery gate below owns that.
                np_immediate = (
                    (line_val <= config.UD_NEW_PROP_IMMEDIATE_LINE_THRESHOLD
                     and stat_type in config.UD_PRIORITY_STAT_CATEGORIES)
                    or score.stars >= config.UD_NON_STRICT_MIN_STARS
                )
                # Do not block evaluation when evidence is missing.  The
                # validation result remains attached to the opportunity and
                # direction is resolved for every active prop.
                if np_immediate and not validation.has_supporting_data:
                    logger.debug(
                        "UD new-prop has no supporting evidence yet: %s | %s | %s",
                        player, stat_type, validation.reason,
                    )
                # Fetch real game results — required before any directional pick.
                # Also fetch for np_immediate props (e.g. 0.5-line priority stats)
                # even when they score PASS tier (no prev_line → low movement score).
                hit_rates = await _fetch_and_compute_hit_rates(
                    db, player, snap.sport or "UNKNOWN", stat_type, line_val,
                    cycle_timing=_cycle_timing,
                )
                decision = make_ud_bet_decision(
                    score        = score,
                    validation   = validation,
                    current_line = line_val,
                    prev_line    = None,
                    hit_rates    = hit_rates,
                    sport        = snap.sport or "",
                )
                # Persist only directional opportunities. A prop without a
                # direction remains in the canonical active pool and is
                # evaluated again by normal scans, Stable Refresh, and FPR.
                if (
                    decision is not None
                    and decision.recommendation in ("OVER", "UNDER")
                ):
                    try:
                        await db.log_prop_opportunity(
                                external_id = getattr(snap, "external_id", None) or getattr(snap, "id", None) or "",
                                player_name       = player,
                                team              = snap.team or "",
                                sport             = snap.sport or "UNKNOWN",
                                stat_type         = stat_type,
                                line_value        = line_val,
                                recommendation    = decision.recommendation,
                                decision_tier     = decision.decision_tier,
                                confidence        = decision.confidence,
                                game_time         = snap.game_time,
                                game_id           = getattr(snap, "game_id", "") or "",
                                provider          = "Underdog",
                                bet_quality_score = decision.confidence,
                                reason_codes      = _compute_reason_codes(score, decision),
                                watchlist_state   = None,
                                downstream_intelligence = _build_downstream_intel(
                                    line_delta=getattr(snap, "line_delta", None),
                                    evidence={
                                        "historical": getattr(validation, "to_dict", lambda: {})(),
                                        "market": getattr(decision, "evidence", None)
                                        or getattr(decision, "bet_evidence", None),
                                    },
                                    line=line_val,
                                    bet_quality=getattr(score, "total", None),
                                    bet_confidence=decision.confidence,
                                    market_quality=getattr(score, "market_quality", None),
                                ),
                        )
                    except Exception as _pol_exc:
                        logger.warning(
                            "underdog_job: log_prop_opportunity [new-prop] failed: %s", _pol_exc
                        )

                # Always add to the cycle batch — even blocked props appear in digest
                _new_props_batch.append({
                    "player":     player,
                    "stat_type":  stat_type,
                    "sport":      snap.sport or "UNKNOWN",
                    "team":       snap.team or "",
                    "line":       line_val,
                    "score":      score,
                    "immediate":  np_immediate,
                    "game_time":  snap.game_time,
                    "validation": validation,
                    "decision":   decision,
                })
                # Any directional decision may enter the shared delivery gate.
                # Score tier, stars, and confidence labels remain metadata.
                _np_bet_ready = (
                    decision is not None
                    and decision.recommendation in ("OVER", "UNDER")
                    and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                )
                _np_direction = decision.recommendation if decision else ""
                # Game-live hard gate — block actionable alerts when game has already started (#5).
                # Uses _is_game_live_or_past which checks status field + game_time elapsed.
                # Internal market intelligence continues regardless.
                if _np_bet_ready:
                    if _is_game_live_or_past(snap, now):
                        _np_bet_ready = False
                        logger.debug(
                            "UD live_gate [new]: %s | %s | game started/live, alert blocked",
                            player, stat_type,
                        )

                # Dedup gate (#118) — suppress if same player/sport/stat/line was alerted
                # recently (within UD_ALERT_DEDUP_WINDOW) with no significant line move.
                # Prevents the same new-prop from firing every scan cycle.
                if _np_bet_ready:
                    if _is_prop_deduped(
                        _prop_market_alerted,
                        player,
                        snap.sport or "UNKNOWN",
                        stat_type,
                        line_val,
                        dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
                        min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
                        provider=getattr(snap, "provider", None) or "Underdog",
                        source_id=str(getattr(snap, "external_id", None) or getattr(snap, "id", None) or ""),
                        event_id=str(getattr(snap, "event", "") or getattr(snap, "game_id", "") or ""),
                        direction=str(_np_direction or ""),
                    ):
                        _np_bet_ready = False
                        logger.debug(
                            "UD dedup_gate [new]: %s | %s | already alerted recently at line=%.1f",
                            player, stat_type, line_val,
                        )

                # Pre-delivery freshness guard — candidate line must match latest scan line.
                if _np_bet_ready and not _ud_line_fresh(line_val, player, stat_type, _current_scan_line_map):
                    logger.warning(
                        "UD freshness_guard [np]: %s | %s | alert_line=%.1f"
                        " scan line diverged — BLOCKED",
                        player, stat_type, line_val,
                    )
                    _np_bet_ready = False

                if _np_bet_ready and chat_ids:
                    # Prop Intelligence trace for richer alert context
                    _np_intel_trace: Optional[dict] = None
                    if np_history:
                        try:
                            _np_intel = _compute_intel(
                                player, snap.sport or "UNKNOWN", stat_type, line_val, np_history
                            )
                            _np_intel_trace = _np_intel.intelligence_trace
                        except Exception:
                            pass
                    # OddsAPI market confirmation — optional, non-blocking
                    _np_odds_confirm: Optional[dict] = None
                    if (decision is not None
                            and decision.recommendation in ("OVER", "UNDER")):
                        try:
                            _np_odds_confirm = await _get_odds_api_confirmation(
                                snap.sport or "UNKNOWN",
                                player,
                                stat_type,
                                decision.recommendation,
                                line_val,
                                team=snap.team or "",
                                event=snap.event or "",
                                game_time=snap.game_time,
                                db=db,
                                event_id=str(getattr(snap, "game_id", "") or ""),
                            )
                        except Exception:
                            pass
                    # ── Direction delivery gate [new-prop] ───────────────────
                    # Scoring, evidence, and quality values are informational.
                    _np_mq_score  = float(getattr(market_quality, "score", 0) if market_quality else 0)
                    _np_bq_score  = float(score.total if score else 0)
                    _np_direction = decision.recommendation if decision else ""
                    _np_gate_ok   = _tier_delivery_gate(
                        snap.sport or "UNKNOWN", _np_direction, _np_bq_score, _np_mq_score,
                        _has_strong_delivery_evidence(
                            score=score, validation=validation, decision=decision,
                            intelligence_trace=_np_intel_trace,
                        ),
                        score_tier=getattr(decision, "decision_tier", None),
                        confidence=getattr(decision, "confidence", None),
                    )
                    if not _np_gate_ok:
                        logger.debug(
                            "UD tier_gate [new]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                            player, stat_type, snap.sport, _np_bq_score, _np_mq_score, _np_direction,
                        )
                    else:
                        # Collect into ranked delivery queue — all candidates compete for
                        # rate-limit slots after the standing scan.  This ensures the
                        # best picks across all three paths get first access to slots.
                        _np_ext_id = getattr(snap, "external_id", None) or getattr(snap, "id", None) or ""
                        _delivery_queue.append({
                            "path":               "new",
                            "provider":            "Underdog",
                            "player":             player,
                            "snap":               snap,
                            "stat_type":          stat_type,
                            "line_val":           line_val,
                            "prev_line":          None,
                            "score":              score,
                            "decision":           decision,
                            "validation":         validation,
                            "market_quality":     market_quality,
                            "market_pressure":    market_pressure,
                            "intel_trace":        _np_intel_trace,
                            "odds_confirm":       _np_odds_confirm,
                            "pp_line":            _prizepicks_line_from_confirmation(_np_odds_confirm),
                            "is_reentry":         False,
                            "ext_id":             _np_ext_id,
                            "record":             None,   # linked after record construction
                            "sport":              snap.sport or "UNKNOWN",
                            "tier":               decision.decision_tier if decision else "B",
                            "conf":               float(decision.confidence if decision else 0),
                            "bq":                 float(score.total if score else 0),
                            "mq":                 float(getattr(market_quality, "score", 0) if market_quality else 0),
                            "is_meaningful_change": False,  # new props have no prev line
                            "_sent":              False,
                        })
                        _dq_collected = True
                        # ud_result stays DeliveryResult(sent=False) — delivery happens below.
                # ── Neutral evaluation metrics (new-prop) ─────────────────────────
                if score is not None:
                    _scored_props.append({
                        "player":          player,
                        "stat_type":       stat_type,
                        "sport":           snap.sport or "UNKNOWN",
                        "total":           score.total,
                        "path":            "new",
                        "score_tier":      score.tier,
                        "direction":       (
                            decision.recommendation
                            if decision is not None
                            and decision.recommendation in ("OVER", "UNDER")
                            else None
                        ),
                        "decision_conf":   (decision.confidence if decision is not None else None),
                        "evidence_available": bool(validation.has_supporting_data),
                        # ── component breakdown ──────────────────────────────
                        "vel":       score.move_velocity,
                        "act":       score.historical_activity,
                        "avg":       score.avg_vs_line,
                        "con":       score.consistency,
                        "sta":       score.stability,
                        "n":         score.n_history,
                        "line":      score.current_line,
                        "prev_line": None,  # new props have no previous line
                    })
    
            else:
                # ── Line-change / removal path (existing logic) ──────────────────
                # Score line-change props that pass the raw magnitude pre-filter.
                # Removal notices bypass scoring and always qualify.
                if not is_removed and line_changed and prev_line is not None:
                    # Load limit=30 to cover L30 validation window as well as scoring
                    ud_history = await _cycle_timing.await_db(
                        "read",
                        "get_ud_prop_history",
                        db.get_ud_prop_history(player, stat_type, limit=30),
                    )

                    _hist_snap = None
                    try:
                        from providers.player_history import get_player_history_provider
                        _hist_snap = await get_player_history_provider().get_snapshot(
                            db,
                            player,
                            snap.sport or "UNKNOWN",
                            stat_type,
                            snap.line or 0.0,
                            ud_history=ud_history,
                            source_provider="Underdog",
                            instrumentation=_cycle_timing,
                        )
                    except Exception:
                        _hist_snap = None
                    _lc_magnitude   = abs(snap.line - prev_line) if (snap.line is not None and prev_line is not None) else None
                    score = _cycle_timing.sync_operation("core_scoring", "score_ud_prop", score_ud_prop,
                        player_name  = player,
                        stat_type    = stat_type,
                        sport        = snap.sport or "UNKNOWN",
                        current_line = snap.line or 0.0,
                        prev_line    = prev_line,
                        history      = ud_history,
                    )
                    market_quality  = compute_market_quality(stat_type, snap.line or 0.0, score)
                    market_pressure = detect_market_pressure(_lc_magnitude, ud_history)
                    _plh_mq_scores[(player, snap.sport or "UNKNOWN", stat_type)] = (
                        int(getattr(market_quality, "score", 0)),
                        float(score.total if score else 0),
                    )
                    # NOTE: _processed_keys is NOT set here for every line-change.
                    # It is only set when should_alert=True (actual alert eligible),
                    # so that qualified props with sub-threshold line movement
                    # (is_qualified=True, should_alert=False) remain available for
                    # the standing path to evaluate as stable high-quality props.
                    validation = validate_player_prop(
                        player_name  = player,
                        stat_type    = stat_type,
                        current_line = snap.line or 0.0,
                        history      = ud_history,
                        min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                        history_snap = _hist_snap,
                    )
                    # Fetch real game results — required before any directional pick
                    hit_rates = None
                    if not is_removed:
                        hit_rates = await _fetch_and_compute_hit_rates(
                            db, player, snap.sport or "UNKNOWN", stat_type, snap.line or 0.0,
                            cycle_timing=_cycle_timing,
                        )
                        decision = make_ud_bet_decision(
                            score        = score,
                            validation   = validation,
                            current_line = snap.line or 0.0,
                            prev_line    = prev_line,
                            hit_rates    = hit_rates,
                            sport        = snap.sport or "",
                        )
                        if decision.recommendation in ("OVER", "UNDER"):
                            try:
                                await _cycle_timing.await_db(
                                    "write",
                                    "log_prop_opportunity",
                                    db.log_prop_opportunity(
                                    external_id       = getattr(snap, "external_id", None) or getattr(snap, "id", None) or "",
                                    player_name       = player,
                                    team              = snap.team or "",
                                    sport             = snap.sport or "UNKNOWN",
                                    stat_type         = stat_type,
                                    line_value        = snap.line or 0.0,
                                    recommendation    = decision.recommendation,
                                    decision_tier     = decision.decision_tier,
                                    confidence        = decision.confidence,
                                    game_time         = snap.game_time,
                                    game_id           = getattr(snap, "game_id", "") or "",
                                    provider          = "Underdog",
                                    bet_quality_score = decision.confidence,
                                    reason_codes      = _compute_reason_codes(score, decision),
                                    watchlist_state   = None,
                                    ),
                                )
                            except Exception as _pol_exc:
                                logger.warning(
                                    "underdog_job: log_prop_opportunity [lc] failed: %s", _pol_exc
                                )
                    _n_scored += 1

                    logger.debug(
                        "UD score: %s | %s | %s (tier=%s stars=%d n=%d has_data=%s)",
                        player, stat_type, score.total, score.tier, score.stars,
                        score.n_history, validation.has_supporting_data,
                    )
    
                elif not is_removed and is_cold_start and _COLD_START_RESCAN_TARGET > 0:
                    # ── Cold-start path ───────────────────────────────────────────
                    # First cycle only: score the configured cold-start target so the
                    # DB has fresh tier / stars / validation data from startup.
                    # hit_rates are
                    # intentionally skipped — fetching them for ~1000 props on boot
                    # would hammer the stats API; they are populated lazily on the
                    # first qualifying incremental event.  No alerts are sent.
                    ud_history = await _cycle_timing.await_db(
                        "read",
                        "get_ud_prop_history",
                        db.get_ud_prop_history(player, stat_type, limit=30),
                    )
                    _hist_snap = None  # cold-start: no per-prop PlayerStatsProvider fetch

                    score = _cycle_timing.sync_operation("core_scoring", "score_ud_prop", score_ud_prop,
                        player_name        = player,
                        stat_type          = stat_type,
                        sport              = snap.sport or "UNKNOWN",
                        current_line       = snap.line or 0.0,
                        prev_line          = None,   # no change event — baseline score
                        history            = ud_history,
                        use_drift_velocity = True,   # cumulative drift from earliest known line
                    )
                    validation = validate_player_prop(
                        player_name  = player,
                        stat_type    = stat_type,
                        current_line = snap.line or 0.0,
                        history      = ud_history,
                        min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                    )
                    # Compute bet direction during cold-start (no hit_rates at boot).
                    # This populates bet_recommendation on the snapshot so /picks and
                    # /slip can display OVER/UNDER immediately after the first cycle.
                    decision = make_ud_bet_decision(
                        score        = score,
                        validation   = validation,
                        current_line = snap.line or 0.0,
                        prev_line    = None,
                        hit_rates    = None,  # no game history at cold-start
                        sport        = snap.sport or "",
                    )
                    _n_cold_start_scored += 1
    
                # ── Re-entry detection ────────────────────────────────────────────
                # A prop returns after removal: known_keys has it (via
                # get_known_underdog_prop_keys which includes removed rows) but
                # prev_record is None (get_latest_underdog_snapshot_per_prop
                # returns only non-removed rows, so a removed prop yields None).
                # Treat the re-appearing prop like a new-prop alert so it:
                #   - bypasses the timing filter (new_prop=True)
                #   - fires regardless of prev_line comparison
                #   - gets a fresh lifecycle DISCOVERED → ACTIVE_ALERTED
                is_reentry = not is_removed and not is_cold_start and prev_record is None
                is_reentry_qualified = False
                if is_reentry:
                    logger.info(
                        "underdog_job: prop re-entry — %s / %s / %s  line=%.1f",
                        player, stat_type, snap.sport or "UNKNOWN", snap.line or 0.0,
                    )
                    ud_history = await db.get_ud_prop_history(player, stat_type, limit=30)
                    _hist_snap = None
                    try:
                        from providers.player_history import get_player_history_provider
                        _hist_snap = await get_player_history_provider().get_snapshot(
                            db,
                            player,
                            snap.sport or "UNKNOWN",
                            stat_type,
                            snap.line or 0.0,
                            ud_history=ud_history,
                            source_provider="Underdog",
                            instrumentation=_cycle_timing,
                        )
                    except Exception:
                        _hist_snap = None
                    score = _cycle_timing.sync_operation("core_scoring", "score_ud_prop", score_ud_prop,
                        player_name  = player,
                        stat_type    = stat_type,
                        sport        = snap.sport or "UNKNOWN",
                        current_line = snap.line or 0.0,
                        prev_line    = None,
                        history      = ud_history,
                    )
                    market_quality  = compute_market_quality(stat_type, snap.line or 0.0, score)
                    market_pressure = detect_market_pressure(None, ud_history)
                    _plh_mq_scores[(player, snap.sport or "UNKNOWN", stat_type)] = (
                        int(getattr(market_quality, "score", 0)),
                        float(score.total if score else 0),
                    )
                    _processed_keys.add((player, stat_type))
                    validation = validate_player_prop(
                        player_name  = player,
                        stat_type    = stat_type,
                        current_line = snap.line or 0.0,
                        history      = ud_history,
                        min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                        history_snap = _hist_snap,
                    )
                    is_reentry_qualified = (
                        score is not None
                        and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                    )
                    if is_reentry_qualified:
                        _n_qualified += 1
                        # Re-entry props must reach Telegram as new-prop alerts.
                        # should_alert defaults to False for non-lc paths; setting
                        # it True here causes the main delivery block to fire with
                        # new_prop=True (timing filter bypassed, full scoring applied).
                        should_alert = True

                # Qualify for alert delivery.
                # Removal notices: only Telegram-alert for three conditions.
                # All removals are still saved to the DB regardless.
                if is_removed:
                    # Removal alerts suppressed from Telegram (doc #2/#3).
                    # Lifecycle tracking (REMOVED state) still applied via _lifecycle_removed.
                    is_qualified = False
                else:
                    # Line-change props need a real directional pick and an
                    # active provider scope. Cold-start, lifecycle, timing,
                    # deduplication, and freshness checks remain independent
                    # safety/integrity protections.
                    is_qualified = (
                        not is_cold_start
                        and score is not None
                        and decision is not None
                        and decision.recommendation in ("OVER", "UNDER")
                        and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                    )
                    if is_qualified and not is_reentry_qualified:
                        _n_qualified += 1
                    if is_qualified:
                        _actionable_sport = (snap.sport or "UNKNOWN").upper()
                        _sport_actionable[_actionable_sport] = (
                            _sport_actionable.get(_actionable_sport, 0) + 1
                        )
                    # ── Neutral evaluation metrics (line-change / cold-start) ─────
                    if score is not None:
                        _scored_props.append({
                            "player":          player,
                            "stat_type":       stat_type,
                            "sport":           snap.sport or "UNKNOWN",
                            "team":            snap.team or "",
                            "total":           score.total,
                            "score_tier":      score.tier,
                            "path":            "cs" if is_cold_start else "lc",
                            "direction":       (
                                decision.recommendation
                                if decision is not None
                                and decision.recommendation in ("OVER", "UNDER")
                                else None
                            ),
                            "evidence_available": bool(validation.has_supporting_data),
                            # ── component breakdown ──────────────────────────────
                            "vel":       score.move_velocity,
                            "act":       score.historical_activity,
                            "avg":       score.avg_vs_line,
                            "con":       score.consistency,
                            "sta":       score.stability,
                            "n":         score.n_history,
                            "line":      score.current_line,
                            "prev_line": prev_line,  # previous line for movement tracking
                            # ── Phase 4 Evidence Infrastructure ─────────────────
                            "external_id":   getattr(snap, "external_id", None) or getattr(snap, "id", None) or "",
                            "game_time":     snap.game_time,
                            "decision_conf": (decision.confidence if decision is not None else None),
                        })
    
                should_alert = is_qualified and (is_reentry or (
                    line_changed
                    and prev_line is not None
                    and abs(snap.line - prev_line) >= config.MIN_UNDERDOG_LINE_CHANGE
                ))
                # Stage 4: gated count — full betting gate passed
                if should_alert and not is_removed:
                    _sp_gated = snap.sport or "UNKNOWN"
                    _sport_gated[_sp_gated] = _sport_gated.get(_sp_gated, 0) + 1
                    # Mark as handled: props that are actually alert-eligible (should_alert=True)
                    # are excluded from the standing path to prevent double-evaluation.
                    # Props with sub-threshold line movement (should_alert=False) are intentionally
                    # left out of _processed_keys so the standing path can evaluate them.
                    _processed_keys.add((player, stat_type))

                # Market movement data is stored in UnderdogSnapshotRecord + PropCandidateLog.
                # No Telegram delivery for market moves — only 🎯 ACTIONABLE BET PICK alerts
                # reach users (doc #1/#8).  format_market_move_detected() kept for future use.

                # Game time validation: suppress alerts when game has already started/passed (#3)
                # Catches offseason props that slipped through with a past game_time.
                # Does NOT block when game_time is None — many valid props lack a scheduled time.
                # Game-live hard gate — strengthened: checks game_status field (if available)
                # AND game_time elapsed.  Alex Bregman regression: game already live for 30 min
                # → alert must be suppressed.  Internal scanning continues regardless.
                if should_alert and not is_removed:
                    if _is_game_live_or_past(snap, now):
                        should_alert = False
                        logger.debug(
                            "UD live_gate [lc]: %s | %s | game started/live, alert blocked",
                            player, stat_type,
                        )

                # Flip/reversal cooldown: prevent rapid back-and-forth alerts (#4)
                # Uses the most-recent DB snapshot: if it had alert_sent=True and was
                # stored within UD_FLIP_COOLDOWN seconds, suppress the new alert.
                # This avoids module-level state and works correctly across test runs.
                if should_alert and not is_removed and config.UD_FLIP_COOLDOWN > 0:
                    _pr_fetched = getattr(prev_record, "fetched_at", None)
                    if (
                        prev_record is not None
                        and prev_record.alert_sent
                        and isinstance(_pr_fetched, datetime)
                        and (now - _pr_fetched.replace(tzinfo=None)).total_seconds() < config.UD_FLIP_COOLDOWN
                    ):
                        should_alert = False
                        logger.debug(
                            "UD flip_cooldown: %s | %s — last alert %.0fs ago (cooldown=%ds)",
                            player, stat_type,
                            (now - _pr_fetched.replace(tzinfo=None)).total_seconds(),
                            config.UD_FLIP_COOLDOWN,
                        )

                # Dedup gate [lc] — prevents re-delivery when the same candidate
                # was recently alerted (e.g. by a concurrent SR/FPR job or the
                # previous scan cycle) without a meaningful line movement.
                # Mirrors the identical check in the new-prop path.
                if should_alert and not is_removed:
                    if _is_prop_deduped(
                        _prop_market_alerted,
                        player,
                        snap.sport or "UNKNOWN",
                        stat_type,
                        snap.line or 0.0,
                        dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
                        min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
                        provider=getattr(snap, "provider", None) or "Underdog",
                        source_id=str(getattr(snap, "external_id", None) or getattr(snap, "id", None) or ""),
                        event_id=str(getattr(snap, "event", "") or getattr(snap, "game_id", "") or ""),
                        direction=str(getattr(decision, "recommendation", "") or ""),
                    ):
                        should_alert = False
                        logger.debug(
                            "UD dedup_gate [lc]: %s | %s | already alerted recently at line=%.1f",
                            player, stat_type, snap.line or 0.0,
                        )

                # The canonical directional qualification is established above;
                # retain the timing, cooldown, and identity-aware dedup results.
                if should_alert and chat_ids:
                    # Prop Intelligence trace for richer alert context
                    # ud_history may be unbound in removal-only paths (no scoring was run)
                    _lc_intel_trace: Optional[dict] = None
                    try:
                        _lc_hist = ud_history  # NameError if not yet assigned in this path
                        if _lc_hist:
                            _lc_intel = _compute_intel(
                                player, snap.sport or "UNKNOWN", stat_type,
                                snap.line or 0.0, _lc_hist,
                            )
                            _lc_intel_trace = _lc_intel.intelligence_trace
                    except (NameError, Exception):
                        pass
                    # OddsAPI market confirmation — non-blocking, directional, non-removal
                    if (not is_removed
                            and decision is not None
                            and decision.recommendation in ("OVER", "UNDER")):
                        try:
                            _lc_odds_confirm = await _get_odds_api_confirmation(
                                snap.sport or "UNKNOWN",
                                player,
                                stat_type,
                                decision.recommendation,
                                snap.line or 0.0,
                                team=snap.team or "",
                                event=snap.event or "",
                                game_time=snap.game_time,
                                db=db,
                                event_id=str(getattr(snap, "game_id", "") or ""),
                            )
                        except Exception:
                            pass
                    # Non-removals: collect into ranked delivery queue.
                    # Removals are not Telegram-alerted (suppressed per doc #2/#3) —
                    # lifecycle tracking is applied via _lifecycle_removed separately.
                    # ── Tier delivery gate [lc] ─────────────────────────────────
                    _lc_mq_score  = float(getattr(market_quality, "score", 0) if market_quality else 0)
                    _lc_bq_score  = float(score.total if score else 0)
                    _lc_direction = decision.recommendation if decision else ""
                    if not is_removed and not _tier_delivery_gate(
                        snap.sport or "UNKNOWN", _lc_direction, _lc_bq_score, _lc_mq_score,
                        _has_strong_delivery_evidence(
                            score=score, validation=validation, decision=decision,
                            intelligence_trace=_lc_intel_trace,
                        ),
                        score_tier=getattr(decision, "decision_tier", None),
                        confidence=getattr(decision, "confidence", None),
                    ):
                        should_alert = False
                        logger.debug(
                            "UD tier_gate [lc]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                            player, stat_type, snap.sport, _lc_bq_score, _lc_mq_score, _lc_direction,
                        )
                    if not is_removed and should_alert:
                        _lc_mag_abs  = (
                            abs((snap.line or 0.0) - (prev_line or 0.0))
                            if prev_line is not None else 0.0
                        )
                        _lc_is_mc    = (
                            _lc_mag_abs >= 2 * config.MIN_UNDERDOG_LINE_CHANGE
                            or bool(is_reentry_qualified)
                        )
                        _lc_ext_id_q = getattr(snap, "external_id", None) or getattr(snap, "id", None) or ""
                        _delivery_queue.append({
                            "path":               "lc",
                            "provider":            "Underdog",
                            "player":             player,
                            "snap":               snap,
                            "stat_type":          stat_type,
                            "line_val":           snap.line or 0.0,
                            "prev_line":          prev_line,
                            "score":              score,
                            "decision":           decision,
                            "validation":         validation,
                            "market_quality":     market_quality,
                            "market_pressure":    market_pressure,
                            "intel_trace":        _lc_intel_trace,
                            "odds_confirm":       _lc_odds_confirm,
                            "pp_line":            _prizepicks_line_from_confirmation(_lc_odds_confirm),
                            "is_reentry":         bool(is_reentry_qualified),
                            "ext_id":             _lc_ext_id_q,
                            "record":             None,   # linked after record construction
                            "sport":              snap.sport or "UNKNOWN",
                            "tier":               decision.decision_tier if decision else "B",
                            "conf":               float(decision.confidence if decision else 0),
                            "bq":                 float(score.total if score else 0),
                            "mq":                 float(getattr(market_quality, "score", 0) if market_quality else 0),
                            "is_meaningful_change": _lc_is_mc,
                            "_sent":              False,
                        })
                        _dq_collected = True
                    # ud_result stays DeliveryResult(sent=False) — delivery happens below.
    
            # Post-send callbacks (dedup, lifecycle, CLV, marks) for non-removal props
            # are handled in the ranked delivery phase below.
            # Removals: track lifecycle independently (no Telegram alert).
            if _path_kind and _path_started is not None:
                _cycle_timing.count(f"{_path_kind}_props")
                _cycle_timing.duration(
                    f"{_path_kind}_processing",
                    time.monotonic() - _path_started,
                )
            if is_removed and prev_record is not None:
                _lifecycle_removed.append((player, snap.sport or "UNKNOWN", stat_type))
                # Log market availability window (detection → removal) for model improvement
                _mfa_key = f"{player}__{stat_type}"
                if _mfa_key in _MARKET_FIRST_ALERT:
                    _win_mins = (now - _MARKET_FIRST_ALERT[_mfa_key]).total_seconds() / 60
                    logger.info(
                        "Market window: %s | %s — available %.0f min before removal",
                        player, stat_type, _win_mins,
                    )
                    del _MARKET_FIRST_ALERT[_mfa_key]
            # Market first-alert time for delivered (non-removal) props is recorded
            # in the ranked delivery phase below after successful broadcast.

            # Resolve alert_outcome for historical analysis.
            # Queued candidates: "new_prop_queued"/"queued" initially — the delivery
            # phase updates the record to "new_prop_sent"/"sent" if delivered.
            if is_new_prop:
                if _dq_collected:
                    _alert_outcome: Optional[str] = "new_prop_queued"
                elif ud_result.sent:
                    _alert_outcome = "new_prop_sent"
                elif ud_result.filtered:
                    _alert_outcome = f"new_prop_filtered:{ud_result.filtered_reason}"[:64]
                elif np_immediate:
                    _alert_outcome = "new_prop_failed"      # tried but delivery failed
                else:
                    _alert_outcome = "new_prop_summary"     # in cycle digest, no individual alert
            elif not should_alert:
                if is_removed:
                    _alert_outcome = "removal_skipped"
                elif is_cold_start and score is not None:
                    _alert_outcome = "cold_start_scored"
                else:
                    _alert_outcome = "skipped"
                    _n_unchanged_skipped += 1  # stable line — no re-score needed this cycle
            elif _dq_collected:
                _alert_outcome = "queued"
            elif ud_result.sent:
                _alert_outcome = "removal_sent" if is_removed else "sent"
            elif ud_result.filtered:
                _alert_outcome = f"filtered:{ud_result.filtered_reason}"[:64]
            else:
                _alert_outcome = "failed"
    
            # Persist snapshot — includes scoring and delivery outcome for analysis
            _source_external_id = getattr(snap, "external_id", None)
            if not isinstance(_source_external_id, str) or not _source_external_id:
                _source_external_id = f"{player}_{stat_type}"
            record = UnderdogSnapshotRecord(
                # Preserve the connector's source ID.  It is stable across a
                # line movement and lets the active-by-line query retire the
                # old line without deleting its historical snapshot.
                external_id   = _source_external_id[:64],
                player_name   = player,
                team          = snap.team or "",
                sport         = snap.sport,
                stat_type     = stat_type,              # actual stat, not "Pick'em"
                line_value    = snap.line or 0.0,
                game_id       = snap.event,
                game_time     = snap.game_time,
                provider_status = (
                    getattr(snap, "game_status", None)
                    or getattr(snap, "provider_status", None)
                    or None
                ),
                line_moved    = line_changed,
                prev_line     = prev_line,
                line_delta    = (
                    (snap.line - prev_line)
                    if prev_line is not None and snap.line is not None
                    else None
                ),
                removed       = is_removed,
                alert_sent    = ud_result.sent,
                score_total   = clamp_score(score.total,  "ud_score.total",  0, 100) if score is not None else None,
                score_tier    = score.tier   if score is not None else None,
                score_stars   = clamp_score(score.stars,  "ud_score.stars",  0, 5)   if score is not None else None,
                alert_outcome      = _alert_outcome,
                validation_json    = validation.to_json() if validation is not None else None,
                bet_recommendation = decision.recommendation if decision is not None else None,
                bet_confidence     = clamp_score(decision.confidence, "ud_bet_confidence", 0, 100) if decision is not None else None,
                bet_reason         = decision.reason         if decision is not None else None,
                bet_evidence_json  = decision.to_json()      if decision is not None else None,
                fetched_at         = now,
            )
            _next_ud_scan_state[(player, stat_type)] = SimpleNamespace(
                line_value=float(snap.line or 0.0),
                score_tier=score.tier if score is not None else getattr(prev_record, "score_tier", None),
                score_total=(
                    clamp_score(score.total, "ud_score.total", 0, 100)
                    if score is not None
                    else getattr(prev_record, "score_total", None)
                ),
                alert_sent=bool(ud_result.sent),
                fetched_at=now,
            )
            # Cold-start: buffer for a single bulk transaction after the loop.
            # Incremental: also buffer — bulk-saved once after the loop completes
            # (same pattern as cold-start).  Avoids ~5 000 individual async
            # SQLite transactions per scan which dominated scan wall-clock time.
            if is_cold_start:
                _cold_start_records.append(record)
            else:
                _incremental_records.append(record)
                # Link this snapshot record into the delivery queue candidate so the
                # ranked delivery phase can update alert_sent=True before bulk-save.
                if _dq_collected and _delivery_queue:
                    for _dq_ref in reversed(_delivery_queue):
                        if (_dq_ref.get("player") == player
                                and _dq_ref.get("stat_type") == stat_type
                                and _dq_ref.get("record") is None):
                            _dq_ref["record"] = record
                            break
        _cycle_timing.finish("scoring_evidence")
        _cycle_timing.start("priority_selection")
        # (Incremental bulk-save is performed AFTER the ranked delivery phase so that
        #  alert_sent=True is reflected in the saved records for delivered candidates.)

        # ── 4A: Standing opportunity scan ─────────────────────────────────────────
        # After cold-start, re-evaluate stable HIGH_FLOOR props that had no line
        # change and are not new this cycle.  Allows evidence-driven (hit-rate-
        # based) alerts without requiring a line-change event.
        #
        # Constraints to prevent alert spam:
        #   • Only HIGH_FLOOR stat types in the betting-alert whitelist
        #   • Must have previously scored A or S tier (from cold-start or prior cycles)
        #   • No Underdog alert sent for this player/stat in the last 24 h
        #   • Top 5 candidates per cycle (avoids hammering the stats API)
        #   • Full scoring + validation + decision gate — same standard as live alerts
        #
        # Standing scan runs on scan 2+ only (after the cold-start cycle completes).
        # Cold-start cycle scores all props without sending alerts; standing picks
        # up high-confidence candidates from the next scan onward.
        if not is_cold_start and chat_ids:
            from engine.ud_scoring import (  # already imported above but local scope
                compute_market_quality as _cmq,
                detect_market_pressure as _dmp,
                _HIGH_FLOOR_STATS as _HFS,
            )
            _standing_candidates: list = []
            for _snap in ud_snaps:
                if "[REMOVED]" in _snap.selection:
                    continue
                _sp      = _snap.player or "Unknown"
                _st      = _extract_ud_stat_type(_snap.selection, _snap.player, _snap.line)
                _sport   = _snap.sport or "UNKNOWN"

                # Futures gate — same as the main loop
                if _is_futures_stat(_st):
                    continue

                if _st not in _HFS:
                    continue
                if _sport not in config.ud_alert_sports:
                    continue
                if (_sp, _st) in _processed_keys:
                    continue  # already handled in the main loop this cycle
    
                _prev = recent_by_key.get((_sp, _st))
                if _prev is None:
                    continue
                # Keep every scored/previously observed prop eligible for
                # re-evaluation.  score_tier is reporting metadata and must
                # not create a hidden S/A/B allowlist for the standing path.
                _standing_candidates.append((_snap, _sp, _st, _sport, _prev))
    
            # Sort within each sport (top 3 per sport) — never compare across sports
            _by_sport: dict = {}
            for _sc in _standing_candidates:
                _by_sport.setdefault(_sc[3], []).append(_sc)
            _standing_ordered: list = []
            for _sp_grp in _by_sport.values():
                _sp_grp.sort(key=lambda x: x[4].score_total or 0, reverse=True)
                _standing_ordered.extend(_sp_grp[:3])
    
            for (_ssnap, _sp, _st, _ssport, _prev) in _standing_ordered:
                _line_val = _ssnap.line or 0.0
    
                # 24 h dedup — skip if already alerted today (DB: UnderdogSnapshotRecord.alert_sent)
                _recent_alert_check = db.has_recent_ud_alert(
                    _sp, _st, within_seconds=86400
                )
                # Test doubles and compatibility callers may not expose the
                # async DB method.  Do not let that abort the whole scan.
                if not hasattr(_recent_alert_check, "__await__"):
                    continue
                if await _recent_alert_check:
                    continue

                # In-memory dedup — catches same-session props sent via 95+ broadcast_alert.
                # broadcast_alert does NOT set UnderdogSnapshotRecord.alert_sent, so
                # has_recent_ud_alert misses them.  _prop_market_alerted IS set by all 95+
                # paths, so this check closes the gap within the current bot session.
                if _is_prop_deduped(
                    _prop_market_alerted, _sp, _ssport, _st, _line_val,
                    dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
                    min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
                    provider=getattr(_ssnap, "provider", None) or "Underdog",
                    source_id=str(getattr(_ssnap, "external_id", None) or getattr(_ssnap, "id", None) or ""),
                    event_id=str(getattr(_ssnap, "event", "") or getattr(_ssnap, "game_id", "") or ""),
                    direction=str(getattr(_prev, "bet_recommendation", "") or ""),
                ):
                    logger.debug(
                        "standing dedup [in-memory]: %s | %s | %s — skipped (already alerted this session)",
                        _sp, _ssport, _st,
                    )
                    continue

                _shist = await _cycle_timing.await_db(
                    "read",
                    "get_ud_prop_history",
                    db.get_ud_prop_history(_sp, _st, limit=30),
                )
                _shistory_snap = None
                try:
                    from providers.player_history import get_player_history_provider
                    _shistory_snap = await get_player_history_provider().get_snapshot(
                        db,
                        _sp,
                        _ssport,
                        _st,
                        _line_val,
                        ud_history=_shist,
                        source_provider="Underdog",
                        instrumentation=_cycle_timing,
                    )
                except Exception:
                    _shistory_snap = None
                _sscore = _cycle_timing.sync_operation("core_scoring", "score_ud_prop", score_ud_prop,
                    player_name  = _sp,
                    stat_type    = _st,
                    sport        = _ssport,
                    current_line = _line_val,
                    prev_line    = None,
                    history      = _shist,
                )
                _sval = validate_player_prop(
                    player_name  = _sp,
                    stat_type    = _st,
                    current_line = _line_val,
                    history      = _shist,
                    min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                    history_snap = _shistory_snap,
                )
                if not _sval.has_supporting_data:
                    logger.debug(
                        "UD standing_gate [no_data]: %s | %s | %s — validation has no supporting data (n=%d)",
                        _sp, _st, _ssport, getattr(_sval, "n_games", 0),
                    )
                    continue
    
                _shits = await _fetch_and_compute_hit_rates(
                    db, _sp, _ssport, _st, _line_val,
                    cycle_timing=_cycle_timing,
                    source_provider="Underdog",
                )
                _sdec = make_ud_bet_decision(
                    score        = _sscore,
                    validation   = _sval,
                    current_line = _line_val,
                    prev_line    = None,
                    hit_rates    = _shits,
                    sport        = _ssport,
                )
                if _sdec is None or _sdec.recommendation not in ("OVER", "UNDER"):
                    continue

                # Only directional evaluations enter the opportunity/evidence
                # stream. The active snapshot itself remains continuously
                # monitored regardless of whether a direction is available.
                try:
                    await db.log_prop_opportunity(
                        external_id = getattr(_ssnap, "external_id", None) or getattr(_ssnap, "id", None) or "",
                        player_name       = _sp,
                        team              = _ssnap.team or "",
                        sport             = _ssport,
                        stat_type         = _st,
                        line_value        = _line_val,
                        recommendation    = _sdec.recommendation,
                        decision_tier     = _sdec.decision_tier,
                        confidence        = _sdec.confidence,
                        game_time         = _ssnap.game_time,
                        game_id           = getattr(_ssnap, "game_id", "") or "",
                        provider          = "Underdog",
                        bet_quality_score = _sdec.confidence,
                        reason_codes      = _compute_reason_codes(_sscore, _sdec),
                        watchlist_state   = None,
                        downstream_intelligence = _build_downstream_intel(
                            line_delta=getattr(_ssnap, "line_delta", None),
                            evidence={
                                "historical": getattr(_sval, "to_dict", lambda: {})(),
                                "market": getattr(_sdec, "evidence", None)
                                or getattr(_sdec, "bet_evidence", None),
                            },
                            line=_line_val,
                            bet_quality=getattr(_sscore, "total", None),
                            bet_confidence=_sdec.confidence,
                            market_quality=getattr(_sscore, "market_quality", None),
                        ),
                    )
                except Exception as _pol_exc:
                    logger.warning(
                        "underdog_job: log_prop_opportunity [standing] failed: %s", _pol_exc
                    )

                # Game-live hard gate for standing plays — block if game already started (#5).
                if _is_game_live_or_past(_ssnap, now):
                    logger.debug(
                        "UD live_gate [standing]: %s | %s | game started/live, alert blocked",
                        _sp, _st,
                    )
                    continue

                _smq = _cmq(_st, _line_val, _sscore)
                _smp = _dmp(None, _shist)
                # Prop Intelligence trace for standing alerts
                _s_intel_trace: Optional[dict] = None
                if _shist:
                    try:
                        _s_intel = _compute_intel(_sp, _ssport, _st, _line_val, _shist)
                        _s_intel_trace = _s_intel.intelligence_trace
                    except Exception:
                        pass
                # OddsAPI market confirmation is optional and non-blocking.
                _s_odds_confirm: Optional[dict] = None
                if _sdec.recommendation in ("OVER", "UNDER"):
                    try:
                        _s_odds_confirm = await _get_odds_api_confirmation(
                            _ssport,
                            _sp,
                            _st,
                            _sdec.recommendation,
                            _line_val,
                            team=getattr(_snap, "team", "") or "",
                            event=getattr(_snap, "event", "") or "",
                            game_time=getattr(_snap, "game_time", None),
                            db=db,
                            event_id=str(getattr(_snap, "game_id", "") or ""),
                        )
                    except Exception:
                        pass
                # Pre-delivery freshness guard — candidate line must match latest scan line.
                if not _ud_line_fresh(_line_val, _sp, _st, _current_scan_line_map):
                    logger.warning(
                        "UD freshness_guard [sp]: %s | %s | alert_line=%.1f"
                        " scan line diverged — BLOCKED",
                        _sp, _st, _line_val,
                    )
                    continue

                # ── Tier delivery gate [standing] ────────────────────────
                _s_mq_score  = float(getattr(_smq, "score", 0) if _smq else 0)
                _s_bq_score  = float(_sscore.total if _sscore else 0)
                _s_direction = _sdec.recommendation if _sdec else ""
                if not _tier_delivery_gate(
                    _ssport, _s_direction, _s_bq_score, _s_mq_score,
                    _has_strong_delivery_evidence(
                        score=_sscore, validation=_sval, decision=_sdec,
                        intelligence_trace=_s_intel_trace,
                    ),
                    score_tier=getattr(_sdec, "decision_tier", None),
                    confidence=getattr(_sdec, "confidence", None),
                ):
                    logger.debug(
                        "UD tier_gate [standing]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                        _sp, _st, _ssport, _s_bq_score, _s_mq_score, _s_direction,
                    )
                    continue

                # Collect into delivery queue for ranked delivery below.
                _s_ext_id_q = getattr(_ssnap, "external_id", None) or getattr(_ssnap, "id", None) or ""
                _delivery_queue.append({
                    "path":               "standing",
                    "provider":            "Underdog",
                    "player":             _sp,
                    "snap":               _ssnap,
                    "stat_type":          _st,
                    "line_val":           _line_val,
                    "prev_line":          None,
                    "score":              _sscore,
                    "decision":           _sdec,
                    "validation":         _sval,
                    "market_quality":     _smq,
                    "market_pressure":    _smp,
                    "intel_trace":        _s_intel_trace,
                    "odds_confirm":       _s_odds_confirm,
                    "pp_line":            _prizepicks_line_from_confirmation(_s_odds_confirm),
                    "is_reentry":         False,
                    "ext_id":             _s_ext_id_q,
                    "record":             None,   # standing path has no new snapshot record
                    "sport":              _ssport,
                    "tier":               _sdec.decision_tier if _sdec else "B",
                    "conf":               float(_sdec.confidence if _sdec else 0),
                    "bq":                 float(_sscore.total if _sscore else 0),
                    "mq":                 float(getattr(_smq, "score", 0) if _smq else 0),
                    "is_meaningful_change": False,  # standing = stable line
                    "_sent":              False,
                })

            if _n_standing_sent:
                logger.info("underdog_job: standing opportunities fired — sent=%d", _n_standing_sent)
    
        # ── End-of-cycle new-prop digest — SUPPRESSED ─────────────────────────────
        # New props are silently stored and scored.  Telegram only receives a
        # notification when a prop passes the full qualification gate:
        #   score + validation + decision engine + sport whitelist.
        # To restore the discovery dump: uncomment the block below and restart.
            # Digest suppressed intentionally.
            # New props are stored/scored silently.
            # Telegram only fires on qualified alerts.
        #     logger.info(
        #         "underdog_job: new-prop digest sent — total=%d immediate=%d summary_only=%d",
        #         len(_new_props_batch),
        #         _n_new_prop_sent,
        #         len(_new_props_batch) - _n_new_prop_sent,
        #     )
        if _new_props_batch:
            logger.info(
                "underdog_job: %d new props stored silently (digest suppressed — "
                "Telegram only fires on qualified alerts)",
                len(_new_props_batch),
            )

        # ── Ranked delivery phase ──────────────────────────────────────────────────
        # All qualified candidates from new-prop, line-change, and standing paths
        # compete for rate-limit slots in priority order.  Higher-quality picks get
        # first access; no candidate is hard-rejected — lower-priority ones are deferred
        # and remain available in /picks and the DB.
        if _delivery_queue and chat_ids:
            _delivery_queue[:] = select_strongest_actionable_picks(_delivery_queue)
            _apply_delivery_diversification(_delivery_queue)
            _n_dq_deferred   = 0
            _dq_deferred_log: list[str] = []
            for _dq in _delivery_queue:
                _dq_player    = _dq["player"]
                _dq_snap      = _dq["snap"]
                _dq_st        = _dq["stat_type"]
                _dq_path      = _dq["path"]
                _dq_score     = _dq["score"]
                _dq_dec       = _dq["decision"]
                _dq_val       = _dq["validation"]
                _dq_mq        = _dq["market_quality"]
                _dq_mp        = _dq["market_pressure"]
                _dq_intel     = _dq["intel_trace"]
                _dq_odds      = _dq["odds_confirm"]
                _dq_pp_line   = _dq.get("pp_line")
                _dq_record    = _dq.get("record")
                _dq_ext_id    = _dq["ext_id"]
                _dq_sport     = _dq["sport"]
                _dq_line_val  = _dq["line_val"]
                _dq_prev      = _dq.get("prev_line")
                _dq_is_reent  = _dq.get("is_reentry", False)
                _dq_is_new    = (_dq_path == "new" or _dq_is_reent)

                # ── Tier delivery gate backstop ───────────────────────────────
                # Belt-and-suspenders: any candidate that slipped through the
                # collection-point gate is caught here before Telegram.
                # Final backstop: preserve the direction-only qualification
                # contract before any Telegram delivery callback.
                _dq_mq_score  = float(getattr(_dq_mq, "score", 0) if _dq_mq else _dq.get("mq", 0))
                _dq_bq_score  = float(_dq.get("bq", 0))
                _dq_direction = _dq_dec.recommendation if _dq_dec else ""
                if not _tier_delivery_gate(
                    _dq_sport, _dq_direction, _dq_bq_score, _dq_mq_score,
                    _has_strong_delivery_evidence(
                        score=_dq_score, validation=_dq_val, decision=_dq_dec,
                        intelligence_trace=_dq_intel,
                    ),
                    score_tier=getattr(_dq_dec, "decision_tier", None),
                    confidence=getattr(_dq_dec, "confidence", None),
                ):
                    _n_dq_deferred += 1
                    _dq_deferred_log.append(
                        f"{_dq_player}/{_dq_st}"
                        f" [sport={_dq_sport} bq={_dq_bq_score:.0f}"
                        f" mq={_dq_mq_score:.0f} dir={_dq_direction}]: tier gate (backstop)"
                    )
                    continue

                # ── Atomic dedup claim ─────────────────────────────────────────────────
                # Check and record under _prop_dedup_lock in one operation so a
                # concurrent SR/WL/FPR job cannot also pass the dedup check and
                # deliver the same candidate (root cause of the Nimmo duplicates).
                # The pre-claim is recorded BEFORE deliver_underdog so other jobs
                # see it immediately during the network I/O of the Telegram send.
                if not await _try_claim_delivery_slot(
                    _dq_player, _dq_sport, _dq_st, _dq_line_val,
                    provider=getattr(_dq_snap, "provider", None) or "Underdog",
                    source_id=str(_dq_ext_id or ""),
                    event_id=str(
                        getattr(_dq_snap, "event", "")
                        or getattr(_dq_snap, "game_id", "")
                        or ""
                    ),
                    direction=str(_dq_direction or ""),
                ):
                    _n_dq_deferred += 1
                    _dq_deferred_log.append(
                        f"{_dq_player}/{_dq_st}"
                        f" [sport={_dq_sport}]: concurrent path already claimed delivery slot"
                    )
                    continue

                _dq_delivery  = AlertDelivery(db, bot, chat_ids)
                try:
                    _dq_result    = await _dq_delivery.deliver_underdog(
                        player_name         = _dq_player,
                        team                = _dq_snap.team or "",
                        sport               = _dq_snap.sport,
                        stat_type           = _dq_st,
                        old_line            = _dq_prev or _dq_line_val,
                        new_line            = _dq_line_val,
                        game_time           = _dq_snap.game_time,
                        game_status         = (
                            getattr(_dq_snap, "game_status", None)
                            or getattr(_dq_snap, "provider_status", None)
                        ),
                        fetched_at          = getattr(_dq_snap, "fetched_at", None),
                        score               = _dq_score,
                        new_prop            = _dq_is_new,
                        validation          = _dq_val,
                        decision            = _dq_dec,
                        market_quality      = _dq_mq,
                        market_pressure     = _dq_mp,
                        pp_line             = _dq_pp_line,
                        intelligence_trace  = _dq_intel,
                        market_confirmation = _dq_odds,
                        source_id           = str(_dq_ext_id or ""),
                        event_id            = str(
                            getattr(_dq_snap, "event", "")
                            or getattr(_dq_snap, "game_id", "")
                            or ""
                        ),
                        delivery_path       = (
                            "new" if _dq_path == "new"
                            else "line_change" if _dq_path == "lc"
                            else "standing" if _dq_path == "standing"
                            else "underdog"
                        ),
                        standing            = (_dq_path == "standing"),
                        strict_pregame      = True,
                    )
                except Exception as _dq_delivery_exc:
                    _release_prop_delivery_claim(
                        _prop_market_alerted,
                        _dq_player,
                        _dq_sport,
                        _dq_st,
                        _dq_line_val,
                        provider=getattr(_dq_snap, "provider", None) or "Underdog",
                        source_id=str(_dq_ext_id or ""),
                        event_id=str(
                            getattr(_dq_snap, "event", "")
                            or getattr(_dq_snap, "game_id", "")
                            or ""
                        ),
                        direction=str(_dq_direction or ""),
                    )
                    logger.warning(
                        "underdog_job: delivery failed; claim released for %s/%s: %s",
                        _dq_player, _dq_st, type(_dq_delivery_exc).__name__,
                    )
                    _dq_result = DeliveryResult(sent=False)

                if _dq_result.sent:
                    _dq["_sent"] = True
                    # Update snapshot record fields before bulk-save
                    if _dq_record is not None:
                        _dq_record.alert_sent    = True
                        _dq_record.alert_outcome = (
                            "new_prop_sent" if _dq_path == "new" else "sent"
                        )
                    # Per-path: counter, dedup update, lifecycle transition.
                    # Each path is handled inline so source-inspection tests can
                    # locate _record_prop_alerted and _lifecycle_alerted.append
                    # relative to the per-path counter landmark.
                    # _record_prop_alerted is NOT called here — it was already
                    # recorded by _try_claim_delivery_slot before the send.
                    if _dq_path == "new":
                        _n_new_prop_sent += 1
                        _lifecycle_alerted.append((_dq_player, _dq_sport, _dq_st))
                    elif _dq_path == "lc":
                        _n_lc_sent += 1
                        _lifecycle_alerted.append((_dq_player, _dq_sport, _dq_st))
                    elif _dq_path == "standing":
                        _n_standing_sent += 1
                        _sp = _dq_player  # standing-path alias (mirrors outer loop)
                        _lifecycle_alerted.append((_sp, _dq_sport, _dq_st))
                    # Performance tracking — path-specific for log clarity
                    try:
                        if getattr(_dq_result, "authoritative_recorded", True):
                            await db.mark_opportunity_alert_sent(
                                _dq_ext_id, _dq_st, provider="Underdog"
                            )
                    except Exception as _dq_mark_exc:
                        if _dq_path == "lc":
                            logger.warning(
                                "underdog_job: mark_opportunity_alert_sent [lc] failed: %s",
                                _dq_mark_exc,
                            )
                        elif _dq_path == "standing":
                            logger.warning(
                                "underdog_job: mark_opportunity_alert_sent [standing] failed: %s",
                                _dq_mark_exc,
                            )
                        else:
                            logger.warning(
                                "underdog_job: mark_opportunity_alert_sent [%s] failed: %s",
                                _dq_path, _dq_mark_exc,
                            )
                    # CLV seed — S/A picks with confirmed OddsAPI odds
                    if _dq_odds is not None and _dq_odds.get("avg_odds") is not None:
                        try:
                            _dq_snap_id = int(getattr(_dq_snap, "id", 0) or 0)
                            await db.seed_clv_from_ud_confirmation(
                                source_id   = _dq_snap_id,
                                sport       = _dq_sport,
                                stat_type   = _dq_st,
                                player_name = _dq_player,
                                line        = _dq_line_val,
                                game_time   = _dq_snap.game_time,
                                tier        = _dq_dec.decision_tier if _dq_dec else "",
                                avg_odds    = _dq_odds["avg_odds"],
                            )
                        except Exception as _dq_clv_exc:
                            logger.debug(
                                "seed_clv_from_ud_confirmation [%s] failed: %s | %s — %s",
                                _dq_path, _dq_player, _dq_st, _dq_clv_exc,
                            )
                    # Market first-alert time tracking
                    _mfa_key = f"{_dq_player}__{_dq_st}"
                    if _mfa_key not in _MARKET_FIRST_ALERT:
                        _MARKET_FIRST_ALERT[_mfa_key] = now
                    logger.info(
                        "🎯 PICK CREATED | Player: %s | Sport: %s | Market: %s"
                        " | Line: %.1f | Direction: %s | Confidence: %d"
                        " | Quality: %s | Telegram: SENT | Path: %s",
                        _dq_player, _dq_sport, _dq_st, _dq_line_val,
                        _dq_dec.recommendation if _dq_dec else "UNKNOWN",
                        _dq_score.total if _dq_score else 0,
                        getattr(_dq_score, "bet_quality_label", "—") if _dq_score else "—",
                        _dq_path,
                    )
                else:
                    _release_prop_delivery_claim(
                        _prop_market_alerted,
                        _dq_player,
                        _dq_sport,
                        _dq_st,
                        _dq_line_val,
                        provider=getattr(_dq_snap, "provider", None) or "Underdog",
                        source_id=str(_dq_ext_id or ""),
                        event_id=str(
                            getattr(_dq_snap, "event", "")
                            or getattr(_dq_snap, "game_id", "")
                            or ""
                        ),
                        direction=str(_dq_direction or ""),
                    )
                    _n_dq_deferred += 1
                    _dq_deferred_log.append(
                        f"{_dq_player}/{_dq_st} [tier={_dq.get('tier', '?')}]"
                        f" bq={_dq.get('bq',0):.0f} path={_dq_path}]:"
                        f"{str(_dq_result)}"
                    )
                                    

            if _n_dq_deferred:
                _dq_sent_log = [
                    f"{c['player']}/{c['stat_type']}"
                    f" [{c.get('tier','?')} bq={c.get('bq',0):.0f}]"
                    for c in _delivery_queue if c.get("_sent")
                ]
                logger.warning(
                    "underdog_job: ranked delivery — %d/%d sent, %d deferred\n"
                    "  Selected: %s\n"
                    "  Deferred: %s",
                    len(_delivery_queue) - _n_dq_deferred,
                    len(_delivery_queue),
                    _n_dq_deferred,
                    ", ".join(_dq_sent_log) or "none",
                    "; ".join(_dq_deferred_log[:10]),
                )

        _cycle_timing.finish("priority_selection")
        _cycle_timing.start("ranking_delivery")
        # ── Bulk-save incremental snapshots ───────────────────────────────────────
        # Runs AFTER the ranked delivery phase so that alert_sent=True is persisted
        # for props that were just delivered.  The incremental records have their
        # alert_sent / alert_outcome fields updated in-place by the delivery phase.
        if _incremental_records and db:
            # Pass a copy so that the .clear() below does not retroactively
            # empty the list object that was handed to the mock in tests.
            _incr_snapshot = list(_incremental_records)
            _incremental_records.clear()
            _snapshot_intended += len(_incr_snapshot)
            try:
                _write_result = await _cycle_timing.await_db(
                    "write",
                    "save_underdog_snapshots_bulk",
                    db.save_underdog_snapshots_bulk(_incr_snapshot),
                )
                _snapshot_persisted += (
                    int(_write_result.get("records_written", len(_incr_snapshot)))
                    if isinstance(_write_result, dict)
                    else len(_incr_snapshot)
                )
                _snapshot_retry_count += (
                    int(_write_result.get("retry_count", 0))
                    if isinstance(_write_result, dict)
                    else 0
                )
            except Exception as _bulk_exc:
                _snapshot_failed_batches += 1
                _snapshot_failed_records += len(_incr_snapshot)
                _persistence_ok = False
                logger.error(
                    "underdog_job: bulk snapshot save permanently failed "
                    "after database retries (%d records): %s",
                    len(_incr_snapshot), _bulk_exc,
                )
            finally:
                del _incr_snapshot

        # ── Cold-start bulk save + latch — runs once after the prop loop ──────────
        if is_cold_start:
            if _cold_start_records:
                # Chunk saves to avoid holding thousands of ORM objects in memory
                # simultaneously — saves 200 at a time and releases each batch.
                _CS_CHUNK = 200
                _cs_total = len(_cold_start_records)
                for _cs_i in range(0, _cs_total, _CS_CHUNK):
                    _cs_batch = _cold_start_records[_cs_i: _cs_i + _CS_CHUNK]
                    _snapshot_intended += len(_cs_batch)
                    try:
                        _write_result = await _cycle_timing.await_db(
                            "write",
                            "save_underdog_snapshots_bulk",
                            db.save_underdog_snapshots_bulk(_cs_batch),
                        )
                        _snapshot_persisted += (
                            int(_write_result.get("records_written", len(_cs_batch)))
                            if isinstance(_write_result, dict)
                            else len(_cs_batch)
                        )
                        _snapshot_retry_count += (
                            int(_write_result.get("retry_count", 0))
                            if isinstance(_write_result, dict)
                            else 0
                        )
                    except Exception as _cs_exc:
                        _snapshot_failed_batches += 1
                        _snapshot_failed_records += len(_cs_batch)
                        _persistence_ok = False
                        logger.error(
                            "underdog_job: cold-start bulk snapshot save permanently "
                            "failed after database retries (%d records): %s",
                            len(_cs_batch), _cs_exc,
                        )
                    del _cs_batch  # release this batch immediately
                _cold_start_records.clear()  # release full list
                logger.info(
                    "underdog_job: cold-start bulk save — intended=%d persisted=%d "
                    "failed_records=%d retries=%d (%d chunks)",
                    _cs_total,
                    _snapshot_persisted,
                    _snapshot_failed_records,
                    _snapshot_retry_count,
                    (_cs_total + _CS_CHUNK - 1) // _CS_CHUNK,
                )
            # Do not latch cold-start complete after an exhausted snapshot batch.
            # The next feed cycle must retry the cold-start path rather than
            # falsely treating incomplete persistence as recovered.
            if _persistence_ok:
                _cold_start_done = True
            else:
                _cold_start_done = False
            logger.info(
                "underdog_job: cold-start complete — evaluated %d active props",
                _n_cold_start_scored,
            )
    
        # Keep only compact previous-prop state needed by the next poll.
        # This replaces a full grouped-history query against the raw snapshot
        # table while preserving durable snapshots and known-prop semantics.
        for _state_snap in ud_snaps:
            _state_player = _state_snap.player or "Unknown"
            _state_stat = _extract_ud_stat_type(
                _state_snap.selection, _state_snap.player, _state_snap.line,
            )
            _state_key = (_state_player, _state_stat)
            if _state_key not in _next_ud_scan_state:
                _state_prev = recent_by_key.get(_state_key)
                _next_ud_scan_state[_state_key] = SimpleNamespace(
                    line_value=float(_state_snap.line or 0.0),
                    score_tier=getattr(_state_prev, "score_tier", None),
                    score_total=getattr(_state_prev, "score_total", None),
                    alert_sent=False,
                    fetched_at=now,
                )
        _ud_scan_state = _next_ud_scan_state
        _current_scan_keys: set[tuple[str, str]] = set()
        for _state_snap in ud_snaps:
            _state_player = _state_snap.player or "Unknown"
            _state_stat = _extract_ud_stat_type(
                _state_snap.selection, _state_snap.player, _state_snap.line,
            )
            _state_key = (_state_player, _state_stat)
            _current_scan_keys.add(_state_key)
        _ud_known_keys.update(_current_scan_keys)

        _cycle_timing.finish("ranking_delivery")
        _cycle_timing.start("final_bookkeeping")
        # ── Persist scan checkpoint ────────────────────────────────────────────────
        # Written after every successful scan for health monitoring.
        # Non-fatal if health tracker is missing.
        try:
            if _health:
                _health.record_scan_checkpoint()
        except Exception as _ckpt_exc:
            logger.debug("underdog_job: checkpoint record skipped — %s", _ckpt_exc)

        logger.info(
            "underdog_job: fetched=%d evaluated=%d cold_start=%d "
            "actionable=%d removed=%d new=%d new_sent=%d",
            len(ud_snaps),
            _n_scored,
            _n_cold_start_scored,
            _n_qualified,
            _n_removed,
            _n_new_prop,
            _n_new_prop_sent,
        )
        # ── Debug: scored prop detail — logged every cycle ────────────────────────
        _dbg_lines: list[str] = [
            (
                f"  received={len(ud_snaps)}  analyzed={len(_scored_props)}"
                f"  (new={_n_new_prop}  line_change={_n_scored}"
                + (f"  cold_start={_n_cold_start_scored}" if _n_cold_start_scored else "")
                + f"  removed={_n_removed})"
            ),
        ]
        if _scored_props:
            # Group by sport first — never compare scores across sports
            from collections import defaultdict as _dd
            _sport_buckets: dict = _dd(list)
            for _p in _scored_props:
                _sport_buckets[_p.get("sport", "?")].append(_p)
            _top = []
            for _sn in sorted(_sport_buckets.keys()):
                _top.extend(
                    sorted(_sport_buckets[_sn], key=lambda x: x["total"], reverse=True)[:3]
                )
            _dbg_lines.append(f"  top 3/sport by score (sports: {', '.join(sorted(_sport_buckets.keys()))}):")
            for _i, _p in enumerate(_top, 1):
                _direction = _p.get("direction") or "—"
                _evidence = "yes" if _p.get("evidence_available") else "no"
                # Header line contains neutral evaluation metrics only.
                _dbg_lines.append(
                    f"    {_i:2d}. {_p['player'][:24]:<24} | {_p['stat_type']:<22}"
                    f" | {_p['sport']:<5} | score={_p['total']:3d}/100"
                    f" | direction={_direction} | evidence={_evidence}"
                    f" | path={_p['path']}"
                )
                # Component line: each dimension with its cap, plus n and current line
                _has_comp = "vel" in _p
                if _has_comp:
                    _dbg_lines.append(
                        f"        vel={_p['vel']:2d}/25"
                        f"  act={_p['act']:2d}/25"
                        f"  avg={_p['avg']:2d}/20"
                        f"  con={_p['con']:2d}/15"
                        f"  sta={_p['sta']:2d}/15"
                        f"  n={_p['n']}"
                        f"  line={_p['line']}"
                    )
        # Per-sport pipeline diagnostics (#7) — shows where props drop off each stage
        if _sport_raw or _sport_futures or _sport_move_detected or _sport_gated:
            _all_sports = sorted(set(
                list(_sport_raw.keys()) + list(_sport_futures.keys())
                + list(_sport_move_detected.keys()) + list(_sport_gated.keys())
            ))
            _dbg_lines.append("  per-sport pipeline  daily → move_det → gated  (+futures tracked):")
            for _sp in _all_sports:
                _daily    = _sport_raw.get(_sp, 0)
                _futures  = _sport_futures.get(_sp, 0)
                _move_det = _sport_move_detected.get(_sp, 0)
                _gated    = _sport_gated.get(_sp, 0)
                _fut_note = f"  +{_futures}f" if _futures else ""
                _dbg_lines.append(
                    f"    {_sp:<8}  daily={_daily:4d}{_fut_note:<6}"
                    f"  move_det={_move_det:3d}"
                    f"  gated={_gated:2d}"
                )

        logger.info("underdog_job [debug summary]\n%s", "\n".join(_dbg_lines))
        # Debug summary consumed — release the MarketSnapshot list; the scored-prop
        # list is still needed for the PropCandidateLog write below.
        ud_snaps.clear()

        # ── PropCandidateLog batch write — edge transparency (Phase 4) ───────
        if _scored_props and db:
            try:
                import json as _json
                _now_ts    = datetime.utcnow()
                _CAND_CHUNK = 100   # write 100 rows at a time to cap peak memory
                _cand_total = 0
                _cand_rows: list[dict] = []
                for _cp in _scored_props:
                    # No-direction evaluations stay in the canonical active
                    # pool and are rescanned; candidate/evidence rows remain
                    # directional only.
                    if _cp.get("direction") not in ("OVER", "UNDER"):
                        continue
                    _ccodes = _compute_reason_codes_from_scored_dict(_cp)
                    _cand_rows.append({
                        "scan_ts":              _now_ts,
                        "player_name":          _cp.get("player", ""),
                        "team":                 _cp.get("team", ""),
                        "sport":                _cp.get("sport", ""),
                        "stat_type":            _cp.get("stat_type", ""),
                        "line_value":           float(_cp.get("line") or 0.0),
                        "provider":             "Underdog",
                        "score_total":          _cp.get("total"),
                        "score_tier":           _cp.get("score_tier"),
                        "confidence":           _cp.get("decision_conf"),
                        # Legacy column retained for schema compatibility.
                        # Current monitoring uses one neutral evaluated state;
                        # delivery is measured separately in ScanCycleLog.
                        "gate_decision":        "EVALUATED",
                        "rejection_reason":     None,
                        "reason_codes":         _json.dumps(_ccodes) if _ccodes else None,
                        "snapshot_external_id": _cp.get("external_id"),
                    })
                    # Flush chunk to DB and release memory
                    if len(_cand_rows) >= _CAND_CHUNK:
                        await _cycle_timing.await_db(
                            "write",
                            "log_prop_candidate_batch",
                            db.log_prop_candidate_batch(_cand_rows),
                        )
                        _cand_total += len(_cand_rows)
                        _cand_rows = []
                # Flush remaining rows
                if _cand_rows:
                    await _cycle_timing.await_db(
                        "write",
                        "log_prop_candidate_batch",
                        db.log_prop_candidate_batch(_cand_rows),
                    )
                    _cand_total += len(_cand_rows)
                    _cand_rows = []
                if _cand_total:
                    logger.debug(
                        "underdog_job: logged %d candidates to PropCandidateLog",
                        _cand_total,
                    )
            except Exception as _cand_exc:
                logger.debug("underdog_job: PropCandidateLog write skipped: %s", _cand_exc)
            finally:
                # Release the scored-prop accumulator regardless of success/failure.
                # At 5000+ props this is the largest single allocation that stays alive
                # through the end of the job; clearing it now lets the GC reclaim it
                # before the next cycle starts.
                _scored_props.clear()
                # Force a GC collection so Python returns unreferenced pages to the
                # OS allocator before the next scan cycle.  Without this, the allocator
                # holds freed memory as its own pool, which looks like growth in RSS.
                gc.collect()

        # ── Bulk MQ score write — update PropLineHistory for /picks and /slip filtering ──
        # Non-fatal: write failure must never abort the cycle.
        if _plh_mq_scores and db:
            try:
                await _cycle_timing.await_db(
                    "write",
                    "update_ud_props_mq_scores_bulk",
                    db.update_ud_props_mq_scores_bulk(_plh_mq_scores),
                )
            except Exception as _mq_bulk_exc:
                logger.debug("underdog_job: mq_scores bulk write failed: %s", _mq_bulk_exc)
        
    # ── Scan cycle log — full pipeline evidence ──────────────────────────────────
        # Non-fatal: a write failure must not abort the cycle or mask real exceptions.
        # Proves that all ~4,600 active props are monitored each poll (not just scored ones).
        try:
            if db:
                await _cycle_timing.await_db(
                    "write",
                    "log_scan_cycle",
                    db.log_scan_cycle(
                        scan_ts         = now,
                        fetched         = _n_ud_snaps_this_cycle,
                        removed         = _n_removed,
                        futures         = sum(_sport_futures.values()),
                        active          = sum(_sport_raw.values()),
                        unchanged       = _n_unchanged_skipped,
                        new_props       = _n_new_prop,
                        line_changed    = _n_scored,
                        cold_start      = _n_cold_start_scored,
                        analyzed        = _n_new_prop + _n_scored + _n_cold_start_scored,
                        qualified       = _n_qualified,
                        alert_delivered = _n_new_prop_sent + _n_lc_sent + _n_standing_sent,
                        actionable_by_sport = _sport_actionable,
                        removed_by_sport    = _sport_removed,
                    ),
                )
        except Exception as _scl_exc:
            logger.debug("underdog_job: scan_cycle_log write skipped: %s", _scl_exc)

    except Exception as _job_exc:
            logger.exception("underdog_job: processing error: %s", _job_exc)
            if _health:
                _health.record_job_fail("underdog_job", str(_job_exc))
                _health.record_pipeline_fail(
                    stage="prop_scoring",
                    module="market_engine.underdog_job",
                    error=str(_job_exc),
                )
            # The guard is claimed before this try block.  Always release it
            # on a processing failure so one malformed row cannot stall every
            # subsequent scheduled scan.
            _release_underdog_scan_slot()
            _job_activity_end("underdog")
            return

    # ── Bridge to PropLineHistory (lifecycle tracking) ─────────────────────────
    # Must run AFTER the main try block so snapshots exist in UnderdogSnapshotRecord.
    # A bridge failure is surfaced as a job failure so /health can alert on it.
    try:
        bridged = await db.sync_underdog_snapshots_to_prop_history(
            limit=6000, since_hours=0.17   # ~10 min window covers the current cycle's rows
            # limit=200 / since_hours=4 previously caused only ~200 of 5,210 active props
            # to be synced per cycle (oldest-first ordering always hit the same small batch).
            # 0.17 h ≈ 10 min catches the current cycle's saves; limit=6000 covers all props.
        )
        if bridged:
            logger.debug("underdog_job: bridged %d rows to PropLineHistory", bridged)
    except Exception as _bridge_exc:
        logger.warning("underdog_job: PropLineHistory sync failed: %s", _bridge_exc)
        _persistence_ok = False

    # ── Apply lifecycle state transitions (AFTER bridge creates/updates the rows) ──
    # Queued in _lifecycle_alerted / _lifecycle_removed during the main loop.
    # Applying here guarantees PropLineHistory rows exist before we try to update them.
    _lc_fail_count = 0
    for _lc_player, _lc_sport, _lc_stat in _lifecycle_alerted:
        try:
            await db.update_prop_lifecycle_state(
                "Underdog", _lc_player, _lc_sport, _lc_stat,
                "ACTIVE_ALERTED", first_alert_sent_at=now,
            )
        except Exception as _lc_exc:
            logger.debug(
                "underdog_job: ACTIVE_ALERTED update failed %s/%s: %s",
                _lc_player, _lc_stat, _lc_exc,
            )
            _lc_fail_count += 1
    for _lc_player, _lc_sport, _lc_stat in _lifecycle_removed:
        try:
            await db.update_prop_lifecycle_state(
                "Underdog", _lc_player, _lc_sport, _lc_stat, "REMOVED",
            )
        except Exception as _lc_exc:
            logger.debug(
                "underdog_job: REMOVED update failed %s/%s: %s",
                _lc_player, _lc_stat, _lc_exc,
            )
            _lc_fail_count += 1
    if _lifecycle_alerted or _lifecycle_removed:
        logger.debug(
            "underdog_job: lifecycle updated — ACTIVE_ALERTED=%d  REMOVED=%d  lc_fails=%d",
            len(_lifecycle_alerted), len(_lifecycle_removed), _lc_fail_count,
        )
    if _lc_fail_count > 0:
        _persistence_ok = False

    _snapshot_fetched = (
        _snapshot_intentionally_skipped + _snapshot_intended
    )
    _cycle_timing.count(
        "snapshot_intentionally_skipped", _snapshot_intentionally_skipped
    )
    _cycle_timing.count("snapshot_intended", _snapshot_intended)
    _cycle_timing.count("snapshot_persisted", _snapshot_persisted)
    _cycle_timing.count("snapshot_failed_batches", _snapshot_failed_batches)
    _cycle_timing.count("snapshot_failed_records", _snapshot_failed_records)
    _cycle_timing.count("snapshot_retries", _snapshot_retry_count)
    logger.info(
        "underdog_snapshot_persistence: fetched=%d intentionally_skipped=%d "
        "intended=%d persisted=%d failed_batches=%d failed_records=%d retries=%d",
        _snapshot_fetched,
        _snapshot_intentionally_skipped,
        _snapshot_intended,
        _snapshot_persisted,
        _snapshot_failed_batches,
        _snapshot_failed_records,
        _snapshot_retry_count,
    )

    # ── Player Prop Market engine (post-bridge, post-lifecycle) ──────────────
    # DISABLED: run_player_prop_market_cycle sends "🟣 PLAYER PROP MARKET ALERT"
    # availability-comparison messages that require a live reference provider
    # (PrizePicks, DraftKings, FanDuel) to produce meaningful output.
    # With all reference providers currently off it generates empty-data spam.
    # Re-enable by uncommenting the block below when a reference provider
    # comes back online.
    #
    # if chat_ids and _scored_props:
    #     try:
    #         from engine.player_prop_market import run_player_prop_market_cycle
    #         await run_player_prop_market_cycle(
    #             db           = db,
    #             bot          = bot,
    #             chat_ids     = chat_ids,
    #             scored_props = _scored_props,
    #             alerted_set  = _prop_market_alerted,
    #             now          = now,
    #         )
    #     except Exception as _ref_exc:
    #         logger.debug("underdog_job: player_prop_market cycle error: %s", _ref_exc)

    # Memory after-job log — helps size the container and catch leaks.
    # VmRSS is the *current* live RSS; delta shows whether this cycle allocated
    # memory that the GC/allocator has not yet returned to the OS.
    _mem_after = _rss_mb()
    if _mem_before is not None and _mem_after is not None:
        logger.info(
            "underdog_job: memory VmRSS before=%.1f MB  after=%.1f MB  delta=%+.1f MB  "
            "_MARKET_FIRST_ALERT=%d  _prop_market_alerted=%d",
            _mem_before, _mem_after, _mem_after - _mem_before,
            len(_MARKET_FIRST_ALERT), len(_prop_market_alerted),
        )

    # Record job outcome — failure if any persistence stage raised.
    if _health:
        if _persistence_ok:
            _health.record_job_run("underdog_job")
            _health.record_underdog_scan(
                props_count = _n_ud_snaps_this_cycle if "_n_ud_snaps_this_cycle" in dir() else 0,
                alerts_sent = (
                    (_n_new_prop_sent  if "_n_new_prop_sent"  in dir() else 0)
                    + (_n_standing_sent if "_n_standing_sent" in dir() else 0)
                ),
            )
            _health.record_database_write("lifecycle_bridge")
        else:
            _health.record_job_fail(
                "underdog_job",
                "snapshot persistence, bridge, or lifecycle stage failed",
            )
            _health.record_pipeline_fail(
                stage  = "lifecycle_bridge",
                module = "market_engine",
                error  = "bridge or lifecycle update failed",
            )
    _cycle_timing.finish("final_bookkeeping")
    logger.info(
        "underdog_cycle_timing: %s",
        _cycle_timing.summary(
            _n_ud_snaps_this_cycle if "_n_ud_snaps_this_cycle" in dir() else 0,
            _n_ud_snaps_this_cycle if "_n_ud_snaps_this_cycle" in dir() else 0,
        ),
    )
    _job_activity_end("underdog")
    # Release only after health, timing, and activity bookkeeping are complete.
    # The deferred FPR handoff must never overlap this final write/diagnostic
    # window, even though the main scan work has already finished.
    _release_underdog_scan_slot()


# ── Legacy Stable Refresh implementation ──────────────────────────────────────
# Retained for compatibility with direct callers and historical state helpers,
# but intentionally not registered as a background job. FPR is the sole
# full-pool rotation and uses the current active monitoring pipeline.
#
# Compatibility entry point:
#   • Loads all active props from DB via get_latest_underdog_snapshot_per_prop().
#   • Does not own a cursor, rescan queue, promotion queue, or removal queue.
#   • FPR owns the only scheduled full-pool rotation.
#
# The live monitoring path and FPR remain completely unaffected.

_STABLE_REFRESH_BATCH_SIZE:   int = 10_000

# Per-stable-refresh-cycle dedup — prevents sending 95+ priority alert more
# than once per cycle for the same prop.  Reset each time _stable_refresh_job
# is called (local variable within the function).


async def _stable_refresh_job(context: "ContextTypes.DEFAULT_TYPE") -> None:
    """
    Legacy direct-call compatibility entry point.

    This function is not independently scheduled. The live monitoring and FPR
    lifecycle own active-pool processing; direct callers remain supported for
    compatibility with existing maintenance/tests.

    It intentionally does not create a second active-prop rotation.
    """
    from engine.player_validator import validate_player_prop as _sr_validate
    from engine.ud_bet_decision import make_ud_bet_decision as _sr_decide
    from engine.ud_scoring import (
        score_ud_prop          as _sr_score_fn,
        compute_market_quality as _sr_cmq,
        detect_market_pressure as _sr_dmp,
    )

    db: Database = context.bot_data.get("db")
    bot          = context.bot
    if db is None:
        return
    _job_activity_start("stable_refresh")

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()
    _health  = get_health_tracker()

    if _health:
        _health.record_job_started("stable_refresh_job")

    # Do not compete with the first Underdog scan while it restores state and
    # establishes the canonical active pool.  The next two-minute trigger will
    # retry after the scan releases the slot.
    if _ud_full_scan_running:
        logger.info(
            "stable_refresh: deferred while Underdog full scan is running"
        )
        if _health:
            _health.record_job_run("stable_refresh_job")
        _job_activity_end("stable_refresh")
        return

    # Stats for console/health
    pool_size    = 0
    cursor       = 0
    next_cursor  = 0
    end_cursor   = 0
    batch_keys: list = []
    sr_rescored  = 0
    sr_actionable = 0
    sr_not_delivered = 0
    sr_sent      = 0

    # ────────────────────────────────────────────────────────────────────────────
    # PART 1 — Rolling stable-pool rescan
    # ────────────────────────────────────────────────────────────────────────────
    try:
        # Fetch full active pool from DB — zero Underdog API calls.
        # get_active_underdog_snapshot_per_prop() takes MAX(id) over ALL rows
        # (including removals) then keeps only non-removed latest rows.
        # This ensures props whose most-recent feed record is a removal are
        # absent from the pool, preventing false rescores and alerts.
        active_pool: dict = await db.get_all_active_underdog_snapshots_by_line()
        # Underdog has priority over this DB-only companion job.  It may have
        # claimed the shared slot while this pool read was in flight; stop
        # before any rescore loop so the two jobs do not contend for SQLite.
        if _ud_full_scan_running:
            logger.info("stable_refresh: yielding to Underdog after pool read")
            if _health:
                _health.record_job_run("stable_refresh_job")
            _job_activity_end("stable_refresh")
            return
        # Keep direct callers with older test/database doubles functional
        # while production always uses the canonical by-line universe.
        if not isinstance(active_pool, dict):
            active_pool = await db.get_active_underdog_snapshot_per_prop()
            if _ud_full_scan_running:
                logger.info("stable_refresh: yielding to Underdog after fallback pool read")
                if _health:
                    _health.record_job_run("stable_refresh_job")
                _job_activity_end("stable_refresh")
                return
        if active_pool and all(len(key) == 2 for key in active_pool):
            active_pool = {
                (player, stat, float(getattr(snap, "line_value", 0.0) or 0.0)): snap
                for (player, stat), snap in active_pool.items()
            }
        active_pool = {
            key: snap
            for key, snap in active_pool.items()
            if not is_football_related_sport(getattr(snap, "sport", ""))
        }
        pool_keys: list   = sorted(
            active_pool,
            key=lambda key: (
                (getattr(active_pool[key], "sport", None) or "").upper(),
                key[0], key[1], key[2],
            ),
        )
        # FPR owns the only full-pool rotation. Stable Refresh still loads
        # the active universe for compatibility, but it must not maintain a
        # second cursor or rescore a batch of every prop.
        # Keep these compatibility stats at zero rather than reporting a
        # misleading second rotation in health.json.
        pool_keys         = []
        pool_size         = 0
        cursor            = 0
        end_cursor        = 0
        batch_keys        = []
        next_cursor       = 0

        # Build line-map from pool for the freshness guard.  Uses line_value —
        # the canonical ORM field on UnderdogSnapshotRecord.  No divergence is
        # possible here since we score from the same snapshot we just fetched.
        _sr_line_map: dict = {
            (snap.player_name, snap.stat_type): (snap.line_value or 0.0)
            for snap in active_pool.values()
        }

        # ── Bulk dedup pre-load ───────────────────────────────────────────────
        # Single DB query for the entire batch — replaces one has_recent_ud_alert
        # query per prop (which would produce tens of thousands of sequential
        # SQLite sessions for a 10k-prop batch).  Props in this set were already
        # alerted within the last 24 h; skip them without further DB I/O.
        #
        # _sr_bulk_dedup_ok tracks whether the single bulk query SUCCEEDED (even
        # if it returned an empty frozenset — that is a valid "nothing alerted
        # recently" result and must NOT fall back to per-prop queries).
        # Per-prop fallback is only used when the bulk query raises.
        _sr_bulk_dedup_ok: bool  = False
        _sr_db_alerted:  frozenset = frozenset()
        try:
            _sr_db_alerted  = await db.get_recently_alerted_prop_keys(
                within_seconds=86400
            )
            _sr_bulk_dedup_ok = True
        except Exception as _sr_bulk_exc:
            logger.debug(
                "stable_refresh: bulk dedup pre-load failed (%s); falling back "
                "to per-prop has_recent_ud_alert", _sr_bulk_exc,
            )

        for (_sr_player, _sr_stat, _sr_pool_line) in batch_keys:
            _sr_snap = active_pool.get((_sr_player, _sr_stat, _sr_pool_line))
            if _sr_snap is None:
                continue

            # get_active_underdog_snapshot_per_prop() already filters removed rows;
            # defensive guard in case a caller passes a mixed pool.
            if getattr(_sr_snap, "removed", False):
                continue

            _sr_sport = _sr_snap.sport or "UNKNOWN"
            if _sr_sport not in config.ud_alert_sports:
                continue
            if _is_futures_stat(_sr_stat):
                continue

            _sr_line = _sr_snap.line_value or 0.0
            sr_rescored += 1

            # ── In-memory dedup — skip if recently alerted this session ───────
            if _is_prop_deduped(
                _prop_market_alerted, _sr_player, _sr_sport, _sr_stat, _sr_line,
                dedup_window_seconds = config.UD_ALERT_DEDUP_WINDOW,
                min_line_change      = config.MIN_UNDERDOG_LINE_CHANGE,
                provider             = getattr(_sr_snap, "provider", None) or "Underdog",
                source_id            = str(getattr(_sr_snap, "external_id", None) or getattr(_sr_snap, "id", None) or ""),
                event_id             = str(getattr(_sr_snap, "event", "") or getattr(_sr_snap, "game_id", "") or ""),
                direction            = str(getattr(_sr_snap, "bet_recommendation", "") or ""),
            ):
                sr_not_delivered += 1
                continue

            # ── DB dedup — bulk pre-loaded; no per-prop query ────────────────
            # _sr_bulk_dedup_ok==True means the single query succeeded (even if
            # it returned frozenset() — that just means nothing was alerted
            # recently).  Only when the query raised do we fall back per-prop.
            if _sr_bulk_dedup_ok:
                if (_sr_player, _sr_stat) in _sr_db_alerted:
                    sr_not_delivered += 1
                    continue
            else:
                # Bulk load raised — per-prop fallback for this cycle only
                if await db.has_recent_ud_alert(_sr_player, _sr_stat, within_seconds=86400):
                    sr_not_delivered += 1
                    continue

            # ── Score + validate ──────────────────────────────────────────────
            _sr_hist  = await db.get_ud_prop_history(_sr_player, _sr_stat, limit=30)
            _sr_history_snap = None
            try:
                from providers.player_history import get_player_history_provider
                _sr_history_snap = await get_player_history_provider().get_snapshot(
                    db,
                    _sr_player,
                    _sr_sport,
                    _sr_stat,
                    _sr_line,
                    ud_history=_sr_hist,
                    source_provider="Underdog",
                    ensure_real_results=False,
                )
            except Exception:
                _sr_history_snap = None
            _sr_score = _sr_score_fn(
                player_name  = _sr_player,
                stat_type    = _sr_stat,
                sport        = _sr_sport,
                current_line = _sr_line,
                prev_line    = None,
                history      = _sr_hist,
            )
            _sr_val = _sr_validate(
                player_name  = _sr_player,
                stat_type    = _sr_stat,
                current_line = _sr_line,
                history      = _sr_hist,
                min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                history_snap = _sr_history_snap,
            )

            # ── Normal quality gates ──────────────────────────────────────────
            if not _sr_val.has_supporting_data:
                sr_not_delivered += 1
                continue

            _sr_hits = await _fetch_and_compute_hit_rates(
                db, _sr_player, _sr_sport, _sr_stat, _sr_line,
                allow_remote=False,
                source_provider="Underdog",
            )
            _sr_dec = _sr_decide(
                score        = _sr_score,
                validation   = _sr_val,
                current_line = _sr_line,
                prev_line    = None,
                hit_rates    = _sr_hits,
                sport        = _sr_sport,
            )

            # Persist directional opportunities only. A missing direction is a
            # transient evaluation result; the active prop stays in rotation.
            _sr_ext_id = (
                getattr(_sr_snap, "external_id", None)
                or str(getattr(_sr_snap, "id", None) or "")
            )
            if (
                _sr_dec is not None
                and _sr_dec.recommendation in ("OVER", "UNDER")
            ):
                try:
                    await db.log_prop_opportunity(
                        external_id       = _sr_ext_id,
                        player_name       = _sr_player,
                        team              = _sr_snap.team or "",
                        sport             = _sr_sport,
                        stat_type         = _sr_stat,
                        line_value        = _sr_line,
                        recommendation    = _sr_dec.recommendation,
                        decision_tier     = _sr_dec.decision_tier,
                        confidence        = _sr_dec.confidence,
                        game_time         = _sr_snap.game_time,
                        game_id           = getattr(_sr_snap, "game_id", "") or "",
                        provider          = "Underdog",
                        bet_quality_score = _sr_dec.confidence,
                        reason_codes      = _compute_reason_codes(_sr_score, _sr_dec),
                    )
                except Exception as _sr_pol_exc:
                    logger.debug("stable_refresh: log_prop_opportunity failed: %s", _sr_pol_exc)

            # ── Normal gate sequence ──────────────────────────────────────────
            if (
                _sr_dec is None
                or _sr_dec.recommendation not in ("OVER", "UNDER")
            ):
                sr_not_delivered += 1
                continue

            # All scan-time gates are non-blocking. Missing/invalid direction was
            # handled above; directional picks proceed to the Telegram gate.
            sr_actionable += 1
            _sr_mq = _sr_cmq(_sr_stat, _sr_line, _sr_score)
            _sr_mp = _sr_dmp(None, _sr_hist)

            # ── Tier delivery gate [stable-refresh] ──────────────────────────
            _sr_mq_score = float(getattr(_sr_mq, "score", 0) if _sr_mq else 0)
            _sr_bq_score = float(_sr_score.total if _sr_score else 0)
            if not _tier_delivery_gate(
                _sr_sport, _sr_dec.recommendation, _sr_bq_score, _sr_mq_score,
                _has_strong_delivery_evidence(
                    score=_sr_score, intelligence_trace=getattr(_sr_dec, "evidence", None),
                ),
                score_tier=getattr(_sr_dec, "decision_tier", None),
            ):
                sr_not_delivered += 1
                sr_actionable -= 1
                logger.debug(
                    "UD tier_gate [sr]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                    _sr_player, _sr_stat, _sr_sport, _sr_bq_score, _sr_mq_score, _sr_dec.recommendation,
                )
                continue

            _sr_intel_trace: Optional[dict] = None
            if _sr_hist:
                try:
                    _sr_intel       = _compute_intel(
                        _sr_player, _sr_sport, _sr_stat, _sr_line, _sr_hist
                    )
                    _sr_intel_trace = _sr_intel.intelligence_trace
                except Exception:
                    pass

            _sr_odds_confirm: Optional[dict] = None
            if _sr_dec.recommendation in ("OVER", "UNDER"):
                try:
                    _sr_odds_confirm = await _get_odds_api_confirmation(
                        _sr_sport, _sr_player, _sr_stat, _sr_dec.recommendation, _sr_line,
                        team=getattr(_sr_snap, "team", "") or "",
                        event=getattr(_sr_snap, "event", "") or "",
                        game_time=getattr(_sr_snap, "game_time", None),
                        db=db,
                        event_id=str(getattr(_sr_snap, "game_id", "") or ""),
                    )
                except Exception:
                    pass

            # NBA / MLB / NFL props remain in the active pool; the shared
            # delivery gate applies the stricter quality rules at send time.
            # ── Atomic dedup claim [stable-refresh] ──────────────────────────────
            # Claim before delivery so the delivery-queue path cannot also send.
            if not await _try_claim_delivery_slot(
                _sr_player, _sr_sport, _sr_stat, _sr_line,
                provider="Underdog",
                source_id=str(getattr(_sr_snap, "external_id", None) or getattr(_sr_snap, "id", None) or ""),
                event_id=str(getattr(_sr_snap, "event", "") or getattr(_sr_snap, "game_id", "") or ""),
                direction=str(getattr(_sr_dec, "recommendation", "") or ""),
            ):
                sr_not_delivered += 1
                sr_actionable -= 1
                logger.debug(
                    "UD dedup_claim [sr]: %s | %s | concurrent path already claimed",
                    _sr_player, _sr_stat,
                )
                continue

            _sr_delivery = AlertDelivery(db, bot, chat_ids)
            try:
                _sr_result   = await _sr_delivery.deliver_underdog(
                    player_name         = _sr_player,
                    team                = _sr_snap.team or "",
                    sport               = _sr_sport,
                    stat_type           = _sr_stat,
                    old_line            = _sr_line,
                    new_line            = _sr_line,
                    game_time           = _sr_snap.game_time,
                    game_status         = (
                        getattr(_sr_snap, "game_status", None)
                        or getattr(_sr_snap, "provider_status", None)
                    ),
                    fetched_at          = getattr(_sr_snap, "fetched_at", None),
                    score               = _sr_score,
                    validation          = _sr_val,
                    decision            = _sr_dec,
                    market_quality      = _sr_mq,
                    market_pressure     = _sr_mp,
                     pp_line             = _prizepicks_line_from_confirmation(_sr_odds_confirm),
                    standing            = True,
                    strict_pregame      = True,
                    intelligence_trace  = _sr_intel_trace,
                    market_confirmation = _sr_odds_confirm,
                     source_id            = str(
                         getattr(_sr_snap, "external_id", None)
                         or getattr(_sr_snap, "id", None)
                         or ""
                     ),
                     event_id            = str(
                         getattr(_sr_snap, "event", "")
                         or getattr(_sr_snap, "game_id", "")
                         or ""
                     ),
                     delivery_path       = "standing",
                )
            except Exception as _sr_delivery_exc:
                _release_prop_delivery_claim(
                    _prop_market_alerted,
                    _sr_player,
                    _sr_sport,
                    _sr_stat,
                    _sr_line,
                    provider="Underdog",
                    source_id=str(
                        getattr(_sr_snap, "external_id", None)
                        or getattr(_sr_snap, "id", None)
                        or ""
                    ),
                    event_id=str(
                        getattr(_sr_snap, "event", "")
                        or getattr(_sr_snap, "game_id", "")
                        or ""
                    ),
                    direction=str(getattr(_sr_dec, "recommendation", "") or ""),
                )
                logger.warning(
                    "stable_refresh: delivery failed; claim released for %s/%s: %s",
                    _sr_player, _sr_stat, type(_sr_delivery_exc).__name__,
                )
                _sr_result = DeliveryResult(sent=False)
            if _sr_result.sent:
                sr_sent += 1
                # _record_prop_alerted already called by _try_claim_delivery_slot.
                try:
                    await db.mark_ud_snapshot_alert_sent(_sr_player, _sr_stat)
                    if getattr(_sr_result, "authoritative_recorded", True):
                        await db.mark_opportunity_alert_sent(
                            _sr_ext_id, _sr_stat, provider="Underdog"
                        )
                except Exception as _sr_mark_exc:
                    logger.debug(
                        "stable_refresh: mark_alert_sent failed: %s", _sr_mark_exc,
                    )
                # CLV seed for S/A picks
                if (
                    _sr_odds_confirm is not None
                    and _sr_odds_confirm.get("avg_odds") is not None
                ):
                    try:
                        await db.seed_clv_from_ud_confirmation(
                            source_id   = int(getattr(_sr_snap, "id", 0) or 0),
                            sport       = _sr_sport,
                            stat_type   = _sr_stat,
                            player_name = _sr_player,
                            line        = _sr_line,
                            game_time   = _sr_snap.game_time,
                            tier        = _sr_dec.decision_tier,
                            avg_odds    = _sr_odds_confirm["avg_odds"],
                        )
                    except Exception:
                        pass
                logger.info(
                    "🎯 STABLE PICK | Player: %s | Sport: %s | Market: %s"
                    " | Line: %.1f | Dir: %s | Tier: %s | Score: %d/100",
                    _sr_player, _sr_sport, _sr_stat, _sr_line,
                    _sr_dec.recommendation, _sr_score.tier, int(_sr_score.total),
                )
            else:
                # deliver_underdog returned not-sent (rate-limited, disabled sport, etc.)
                sr_not_delivered += 1
                sr_actionable -= 1
                _release_prop_delivery_claim(
                    _prop_market_alerted,
                    _sr_player,
                    _sr_sport,
                    _sr_stat,
                    _sr_line,
                    provider="Underdog",
                    source_id=str(
                        getattr(_sr_snap, "external_id", None)
                        or getattr(_sr_snap, "id", None)
                        or ""
                    ),
                    event_id=str(
                        getattr(_sr_snap, "event", "")
                        or getattr(_sr_snap, "game_id", "")
                        or ""
                    ),
                    direction=str(getattr(_sr_dec, "recommendation", "") or ""),
                )

    except Exception as _sr_exc:
        logger.exception("stable_refresh_job [part1]: error: %s", _sr_exc)
        # Reset counters so the console log shows zeros rather than partial data
        sr_rescored = sr_actionable = sr_not_delivered = sr_sent = 0
        if _health:
            _health.record_job_fail("stable_refresh_job", str(_sr_exc))

    # Stable Refresh is retained only as an unscheduled compatibility entry
    # point. FPR owns the sole full-pool rotation and no secondary queue exists.
    wl_active = 0

    # ── Console log ──────────────────────────────────────────────────────────────
    _thick = "━" * 24
    _sr_pct = (
        f"{(end_cursor / pool_size * 100):.1f}%" if pool_size > 0 else "—"
    )
    logger.info(
        "\n%s\n🔄 Stable Refresh\n%s\n"
        "Batch:              %6d\n"
        "Progress:          %s\n"
        "Coverage:  %s / %s active props\n"
        "🔬 Rescored:  %6d\n"
        "🎯 Directional deliveries: %6d  (sent: %d)\n"
        "⏸ Not delivered: %6d\n"
        "⏱ Next refresh:   ~2 min\n"
        "%s",
        _thick, _thick,
        len(batch_keys),
        _sr_pct,
        f"{end_cursor:,}", f"{pool_size:,}",
        sr_rescored,
        sr_actionable, sr_sent,
        sr_not_delivered,
        _thick,
    )

    # ── Persist stats to health sidecar ──────────────────────────────────────────
    if _health:
        _health.set_stable_refresh_stats({
            "pool_size":    pool_size,
            "batch_size":   len(batch_keys),
            "cursor_start": cursor,
            "cursor_end":   end_cursor,   # FIX: store end_cursor (actual batch end), not next_cursor (which wraps to 0)
            "sr_rescored":  sr_rescored,
            "sr_actionable": sr_actionable,
            "sr_not_delivered": sr_not_delivered,
            "sr_sent":      sr_sent,
            # Compatibility-only counter; no secondary queue is maintained.
            "wl_active":    0,
        })
        _health.record_job_run("stable_refresh_job")

    logger.info(
        "stable_refresh_job: pool=%d batch=%d rescored=%d actionable=%d sent=%d",
        pool_size, len(batch_keys), sr_rescored, sr_actionable, sr_sent,
    )
    _job_activity_end("stable_refresh")


# ──────────────────────────────────────────────────────────────────────────────
# FULL-POOL RESCAN ROTATION
# ──────────────────────────────────────────────────────────────────────────────
#
# Independently covers the entire active provider pool in bounded batches,
# guaranteeing every prop (unchanged or previously evaluated) is eventually
# rescored. Completely separate from the stable-refresh job —
# uses its own cursor, rotation counter, and stats keys in health.json.
#
# Rotation order is deterministic and unified across all supported sports.
# New observations and line movements may receive bounded prompt priority, but
# the ordinary cursor slice is always the primary rotation population.
#
# No prop is permanently excluded. A previous evaluation, rejection, missing
# evidence, or delivery attempt does not prevent rescoring in a later rotation.
# Delivery protections apply only at the Telegram boundary.
#
# Rotation state (persisted in health.json via HealthTracker):
#   fpr_cursor   — current position in the stable canonical pool
#   fpr_rotation — 1-based rotation number; auto-increments at 100% coverage
#   fpr_stats    — metrics from the last completed cycle
#
# Progress display (human-readable; internal cursor values are never shown):
#   Rotation: #1 | Progress: 18.4% | Coverage: 412,000 / 2,240,000 active props


async def _full_pool_rescan_job(context: "ContextTypes.DEFAULT_TYPE") -> None:
    """
    Full-Pool Rescan Rotation: covers the entire active prop pool in batches.

    • Batch size: config.FPR_BATCH_SIZE (default 10,000)
    • Interval:   config.FPR_INTERVAL   (default 300 s / 5 min)
    • When all props in the current pool have been covered the rotation counter
      increments and the cursor resets to 0 automatically.
    • Removed/inactive props are excluded by the canonical provider-aware
      active pool.
    • Existing delivery protections (dedup, direction, evidence, and live-game)
      apply only at the Telegram boundary; they do not remove active props from
      the rotation or prevent their evaluation.
    """
    from engine.player_validator import validate_player_prop as _fpr_validate
    from engine.ud_bet_decision import make_ud_bet_decision as _fpr_decide
    from engine.ud_scoring import (
        score_ud_prop          as _fpr_score_fn,
        compute_market_quality as _fpr_cmq,
        detect_market_pressure as _fpr_dmp,
    )

    db: Database = context.bot_data.get("db")
    bot          = context.bot
    if db is None:
        return

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()
    _health  = get_health_tracker()

    if _health:
        _health.record_job_started("full_pool_rescan_job")

    global _fpr_execution_running, _ud_full_scan_running
    if _fpr_execution_running:
        logger.info("full_pool_rescan: deferred trigger already running")
        if _health:
            _health.record_job_run("full_pool_rescan_job")
        return

    _job_activity_start("fpr")

    # The Underdog cold-start/full scan owns the database while it restores
    # state and ingests the live feed.  Defer this rotation batch so the
    # canonical FPR remains the only full-pool rotation once it starts.
    if _ud_full_scan_running:
        logger.info(
            "full_pool_rescan: deferred while Underdog full scan is running"
        )
        _queue_deferred_fpr(context)
        if _health:
            _health.record_job_run("full_pool_rescan_job")
        _job_activity_end("fpr")
        return

    # Claim the same serialized full-scan slot for the duration of the FPR
    # batch.  This prevents the post-scan handoff from overlapping a new
    # Underdog scan and keeps Stable Refresh from competing with this rotation.
    _fpr_execution_running = True
    _ud_full_scan_running = True

    # ── Mutable cycle stats ───────────────────────────────────────────────────
    fpr_pool_size       = 0
    fpr_unique_source_ids = 0
    fpr_batch_source_ids  = 0
    fpr_line_movers       = 0
    fpr_cursor          = 0
    fpr_end_cursor      = 0
    fpr_next_cursor     = 0
    fpr_rotation        = 1
    fpr_batch_size      = 0
    fpr_rescored        = 0
    fpr_sent            = 0
    fpr_not_delivered   = 0
    fpr_total_rescanned = 0
    fpr_by_sport        = {}
    rotation_complete   = False

    try:
        # ── Fetch full active pool from DB (zero provider API calls) ──────────
        # PropLineHistory is the canonical shared stream for both primary
        # providers. Keep the legacy Underdog query as a compatibility fallback
        # for older test doubles and databases.
        _generic_pool = getattr(
            db, "get_all_active_prop_line_history_by_line", None
        )
        if asyncio.iscoroutinefunction(_generic_pool):
            active_pool: dict = await _generic_pool(
                providers=("Underdog", "PrizePicks")
            )
            # Underdog's live feed still owns the legacy snapshot table. Merge
            # it into the same serialized rotation instead of dropping it when
            # normalized PrizePicks rows are present. A normalized Underdog row
            # may already represent the same source ID, so do not add a second
            # legacy-key entry for that underlying prop.
            _normalized_ud_source_ids = {
                str(getattr(_normalized_snap, "external_id", "") or "")
                for _normalized_key, _normalized_snap in active_pool.items()
                if (
                    str(getattr(_normalized_snap, "provider", "") or "").lower()
                    in {"", "underdog"}
                    and str(getattr(_normalized_snap, "external_id", "") or "")
                )
                or (
                    len(_normalized_key) >= 1
                    and str(_normalized_key[0]).lower() == "underdog"
                    and str(getattr(_normalized_snap, "external_id", "") or "")
                )
            }
            _normalized_ud_legacy_keys = {
                (
                    str(getattr(_normalized_snap, "player_name", "") or ""),
                    str(getattr(_normalized_snap, "sport", "") or "").upper(),
                    str(getattr(_normalized_snap, "stat_type", "") or ""),
                    float(getattr(_normalized_snap, "line_value", 0.0) or 0.0),
                )
                for _normalized_key, _normalized_snap in active_pool.items()
                if (
                    str(getattr(_normalized_snap, "provider", "") or "").lower()
                    in {"", "underdog"}
                    or (
                        len(_normalized_key) >= 1
                        and str(_normalized_key[0]).lower() == "underdog"
                    )
                )
            }
            _legacy_ud_pool = await db.get_all_active_underdog_snapshots_by_line()
            for _ud_key, _ud_snap in _legacy_ud_pool.items():
                _ud_source_id = str(getattr(_ud_snap, "external_id", "") or "")
                if _ud_source_id and _ud_source_id in _normalized_ud_source_ids:
                    continue
                _ud_legacy_key = (
                    str(getattr(_ud_snap, "player_name", "") or _ud_key[0] or ""),
                    str(getattr(_ud_snap, "sport", "") or "").upper(),
                    str(getattr(_ud_snap, "stat_type", "") or _ud_key[1] or ""),
                    float(getattr(_ud_snap, "line_value", _ud_key[2]) or 0.0),
                )
                if not _ud_source_id and _ud_legacy_key in _normalized_ud_legacy_keys:
                    continue
                active_pool.setdefault(("Underdog", *_ud_key), _ud_snap)
        else:
            active_pool = await db.get_all_active_underdog_snapshots_by_line()
        active_pool = {
            key: snap
            for key, snap in active_pool.items()
            if (
                not is_football_related_sport(getattr(snap, "sport", ""))
                and not _is_futures_stat(getattr(snap, "stat_type", ""))
            )
        }
        _fpr_prizepicks_lines = _active_prizepicks_lines(active_pool)
        fpr_pool_size = len(active_pool)
        if fpr_pool_size == 0:
            _fpr_execution_running = False
            _ud_full_scan_running = False
            if _health:
                _health.record_job_run("full_pool_rescan_job")
            _job_activity_end("fpr")
            return

        def _fpr_parts(key, snap) -> tuple[str, str, str, str, float, str]:
            """Normalize provider-scoped keys and legacy Underdog keys."""
            _key_provider = (
                len(key) >= 4
                and str(key[0]).strip().lower() in {"underdog", "prizepicks"}
            )
            if _key_provider and len(key) >= 6:
                provider, player, sport, stat, line, source_id = key[:6]
            elif _key_provider and len(key) == 5:
                provider, player, sport, stat, line = key[:5]
                source_id = ""
            elif _key_provider:
                provider, player, stat, line = key[:4]
                sport = getattr(snap, "sport", None) or "UNKNOWN"
                source_id = ""
            else:
                _snap_provider = getattr(snap, "provider", None)
                provider = (
                    _snap_provider
                    if str(_snap_provider).strip().lower()
                    in {"underdog", "prizepicks"}
                    else "Underdog"
                )
                player, stat, line = key[:3]
                sport = getattr(snap, "sport", None) or "UNKNOWN"
                source_id = ""
            return (
                str(provider),
                str(player),
                str(stat),
                str(sport).upper(),
                float(line or getattr(snap, "line_value", 0.0) or 0.0),
                str(source_id or getattr(snap, "external_id", None) or ""),
            )

        def _fpr_sort_key(item):
            """Return stable identity ordering for the active universe.

            Recommendation, decision tier, confidence, evidence, rejection,
            and delivery state are evaluation/reporting concerns. None may
            influence the canonical rotation order.
            """
            key, snap = item
            provider, player, stat, sport, line, _source_id = _fpr_parts(key, snap)
            external_id = str(getattr(snap, "external_id", None) or "")
            return (
                provider,
                sport,
                player,
                stat,
                line,
                external_id,
            )

        sorted_items = sorted(active_pool.items(), key=_fpr_sort_key)
        pool_keys    = [k for k, _ in sorted_items]
        pool_snaps   = {k: v for k, v in sorted_items}
        fpr_unique_source_ids = len({
            (
                _fpr_parts(key, snap)[0],
                str(getattr(snap, "external_id", None) or key[:3]),
            )
            for key, snap in sorted_items
        })

        # ── Load and clamp cursor ─────────────────────────────────────────────
        fpr_rotation = _health.get_fpr_rotation() if _health else 1
        fpr_cursor   = _health.get_fpr_cursor()   if _health else 0
        fpr_cursor   = fpr_cursor % fpr_pool_size  # safe if pool shrank

        # ── Slice batch ───────────────────────────────────────────────────────
        # The cursor window is the guaranteed primary population. New and
        # moving rows outside that window may use a bounded prompt lane, but
        # they never replace the cursor boundary and never affect its order.
        fpr_limit = max(1, int(config.FPR_BATCH_SIZE))
        cursor_window = pool_keys[
            fpr_cursor:min(fpr_cursor + fpr_limit, fpr_pool_size)
        ]
        def _fpr_prompt_info(key, snap) -> tuple[int, float]:
            """Classify only fetch freshness/movement, never bet quality."""
            _line = float(
                getattr(snap, "line_value", None)
                or _fpr_parts(key, snap)[4]
            )
            _prev_line = getattr(snap, "prev_line", None)
            try:
                _movement = (
                    abs(float(_prev_line) - _line)
                    if isinstance(_prev_line, (int, float))
                    else 0.0
                )
            except (TypeError, ValueError):
                _movement = 0.0
            _is_mover = (
                getattr(snap, "line_moved", False) is True
                or _movement >= 0.01
            )
            _first_seen = getattr(snap, "first_seen", None)
            _last_seen = getattr(snap, "last_seen", None)
            _is_new = (
                getattr(snap, "new_prop", False) is True
                or (
                    isinstance(_first_seen, datetime)
                    and isinstance(_last_seen, datetime)
                    and _first_seen == _last_seen
                )
            )
            return (0 if _is_new else (1 if _is_mover else 2), -_movement)

        prompt_keys = sorted(
            (
                key for key, snap in sorted_items
                if key not in cursor_window
                and _fpr_prompt_info(key, snap)[0] < 2
            ),
            key=lambda key: (*_fpr_prompt_info(key, pool_snaps[key]), key),
        )
        # At least half of each batch remains ordinary cursor work. A one-item
        # batch is entirely cursor work so the cursor cannot stall.
        prompt_limit = min(len(prompt_keys), max(0, fpr_limit // 2))
        batch_keys = (
            prompt_keys[:prompt_limit]
            + [key for key in cursor_window if key not in prompt_keys[:prompt_limit]]
        )[:fpr_limit]
        fpr_line_movers = sum(
            1 for key in batch_keys
            if _fpr_prompt_info(key, pool_snaps[key])[0] == 1
        )

        # Advance by the complete cursor window, independent of prompt work.
        # This makes every active row reachable even when prompt rows persist.
        _cursor_advanced = len(cursor_window)
        fpr_end_cursor  = min(fpr_cursor + _cursor_advanced, fpr_pool_size)
        fpr_batch_size  = len(batch_keys)
        fpr_batch_source_ids = len({
            str(getattr(pool_snaps[key], "external_id", None) or key[:3])
            for key in batch_keys
        })
        fpr_next_cursor = fpr_end_cursor % fpr_pool_size  # wraps to 0 at pool end

        # Rotation completes when the batch reaches the last prop in the pool
        if fpr_end_cursor >= fpr_pool_size:
            rotation_complete = True

        # Alternate lines share player/stat history.  Load each history once
        # for this bounded batch instead of opening one SQLite session per line.
        _fpr_history_by_prop: dict[tuple, list] = {}
        _fpr_history_bulk_loaded = False
        _fpr_generic_bulk_history = getattr(db, "get_prop_line_history_bulk", None)
        if asyncio.iscoroutinefunction(_fpr_generic_bulk_history):
            try:
                _fpr_history_by_prop = await _fpr_generic_bulk_history(
                    [
                        (
                            _fpr_parts(_key, pool_snaps[_key])[0],
                            _fpr_parts(_key, pool_snaps[_key])[1],
                            _fpr_parts(_key, pool_snaps[_key])[3],
                            _fpr_parts(_key, pool_snaps[_key])[2],
                        )
                        for _key in batch_keys
                    ],
                    limit=30,
                )
                _fpr_history_bulk_loaded = True
            except Exception as _fpr_bulk_history_exc:
                logger.warning(
                    "full_pool_rescan: bulk history read failed; "
                    "falling back to per-prop reads: %s",
                    _fpr_bulk_history_exc,
                )
        else:
            _fpr_bulk_history = getattr(db, "get_ud_prop_history_bulk", None)
            if asyncio.iscoroutinefunction(_fpr_bulk_history):
                try:
                    _fpr_history_by_prop = await _fpr_bulk_history(
                        [
                            (
                                _fpr_parts(_key, pool_snaps[_key])[1],
                                _fpr_parts(_key, pool_snaps[_key])[2],
                            )
                            for _key in batch_keys
                        ],
                        limit=30,
                    )
                    _fpr_history_bulk_loaded = True
                except Exception as _fpr_bulk_history_exc:
                    logger.warning(
                        "full_pool_rescan: Underdog bulk history read failed; "
                        "falling back to per-prop reads: %s",
                        _fpr_bulk_history_exc,
                    )

        # Evidence is also shared by alternate lines for a player/stat.  The
        # bulk result is keyed by the exact stat spelling used by the decision
        # layer, matching _fetch_and_compute_hit_rates' existing lookup.
        _fpr_results_by_prop: dict[tuple[str, str, str], list] = {}
        _fpr_bulk_results = getattr(db, "get_player_results_bulk", None)
        if asyncio.iscoroutinefunction(_fpr_bulk_results):
            try:
                _fpr_results_by_prop = await _fpr_bulk_results(
                    [
                        (
                            _fpr_parts(_key, pool_snaps[_key])[1],
                            _fpr_parts(_key, pool_snaps[_key])[3],
                            _fpr_parts(_key, pool_snaps[_key])[2],
                        )
                        for _key in batch_keys
                    ],
                    limit=30,
                )
            except Exception as _fpr_bulk_results_exc:
                logger.warning(
                    "full_pool_rescan: bulk evidence read failed; "
                    "falling back to per-prop reads: %s",
                    _fpr_bulk_results_exc,
                )

        _fpr_opportunity_rows: list[dict] = []
        _fpr_delivery_candidates: list[dict] = []

        # ── Score each prop in this batch ─────────────────────────────────────
        for _fpr_key in batch_keys:
            _fpr_snap  = pool_snaps.get(_fpr_key)
            if _fpr_snap is None:
                continue

            (
                _fpr_provider,
                _fpr_player,
                _fpr_stat,
                _fpr_sport,
                _fpr_line_val,
                _fpr_source_id,
            ) = _fpr_parts(_fpr_key, _fpr_snap)
            _fpr_pp_line = (
                _fpr_line_val
                if _fpr_provider == "PrizePicks"
                else _fpr_prizepicks_lines.get(
                    (
                        normalize_name(_fpr_player),
                        _fpr_sport.upper(),
                        _normalize_prop_stat(_fpr_stat),
                    )
                )
            )
            _fpr_line = float(
                getattr(_fpr_snap, "line_value", None) or _fpr_line_val
            )

            try:
                if _fpr_history_bulk_loaded:
                    _fpr_hist = _fpr_history_by_prop.get(
                        (
                            _fpr_provider,
                            _fpr_player,
                            _fpr_sport,
                            _fpr_stat,
                        ),
                        [],
                    )
                    # Legacy Underdog bulk history is keyed only by
                    # player/stat and remains supported for test doubles.
                    if not _fpr_hist and _fpr_provider == "Underdog":
                        _fpr_hist = _fpr_history_by_prop.get(
                            (_fpr_player, _fpr_stat), []
                        )
                elif _fpr_history_by_prop:
                    _fpr_hist = _fpr_history_by_prop.get(
                        (_fpr_player, _fpr_stat), []
                    )
                else:
                    if _fpr_provider == "Underdog":
                        _fpr_hist = await db.get_ud_prop_history(
                            _fpr_player, _fpr_stat, limit=30
                        )
                    else:
                        _fpr_hist = await db.get_prop_line_history(
                            _fpr_provider,
                            _fpr_player,
                            _fpr_sport,
                            _fpr_stat,
                            limit=30,
                        )

                # Rescanning DOES count toward API/fetch totals per requirement
                fpr_total_rescanned += 1

                _fpr_score = _fpr_score_fn(
                    player_name  = _fpr_player,
                    stat_type    = _fpr_stat,
                    sport        = _fpr_sport,
                    current_line = _fpr_line,
                    prev_line    = None,
                    history      = _fpr_hist,
                )

                _fpr_history_snap = None
                try:
                    from providers.player_history import get_player_history_provider
                    _fpr_history_snap = await get_player_history_provider().get_snapshot(
                        db,
                        _fpr_player,
                        _fpr_sport,
                        _fpr_stat,
                        _fpr_line,
                        source_provider=_fpr_provider,
                        ensure_real_results=False,
                    )
                except Exception:
                    _fpr_history_snap = None
                _fpr_val = _fpr_validate(
                    player_name  = _fpr_player,
                    stat_type    = _fpr_stat,
                    current_line = _fpr_line,
                    history      = _fpr_hist,
                    min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                    history_snap = _fpr_history_snap,
                )
                _fpr_hits = await _fetch_and_compute_hit_rates(
                    db, _fpr_player, _fpr_sport, _fpr_stat, _fpr_line,
                    allow_remote=False,
                    source_provider=_fpr_provider,
                    preloaded_results=_fpr_results_by_prop.get(
                        (_fpr_player, _fpr_sport, _fpr_stat.lower().strip())
                    ) if _fpr_results_by_prop else None,
                )
                _fpr_dec = _fpr_decide(
                    score        = _fpr_score,
                    validation   = _fpr_val,
                    current_line = _fpr_line,
                    prev_line    = None,
                    hit_rates    = _fpr_hits,
                    sport        = _fpr_sport,
                )

                # Keep the canonical monitoring/evidence stream complete for
                # every directional evaluation. Buffer rows and persist them
                # in one bounded upsert after scoring; unavailable direction
                # remains a transient evaluation result.
                _fpr_ext_id = (
                    getattr(_fpr_snap, "external_id", None)
                    or str(getattr(_fpr_snap, "id", None) or "")
                )
                if getattr(_fpr_dec, "recommendation", None) in ("OVER", "UNDER"):
                    _fpr_opportunity_rows.append({
                        "external_id": f"{_fpr_ext_id}:{_fpr_line:g}",
                        "player_name": _fpr_player,
                        "team": getattr(_fpr_snap, "team", "") or "",
                        "sport": _fpr_sport,
                        "stat_type": _fpr_stat,
                        "line_value": _fpr_line,
                        "recommendation": getattr(_fpr_dec, "recommendation", None),
                        "decision_tier": getattr(_fpr_dec, "decision_tier", None),
                        "confidence": getattr(_fpr_dec, "confidence", 0),
                        "game_time": getattr(_fpr_snap, "game_time", None),
                        "game_id": getattr(_fpr_snap, "game_id", "") or "",
                        "provider": _fpr_provider,
                        "bet_quality_score": float(getattr(_fpr_score, "total", 0) or 0),
                        "reason_codes": _compute_reason_codes(_fpr_score, _fpr_dec),
                        "watchlist_state": None,
                    })

                # Scan-time eligibility is non-blocking. Props without a
                # direction remain in the monitoring stream for the next rotation.
                _fpr_has_direction = (
                    _fpr_dec is not None
                    and _fpr_dec.recommendation in ("OVER", "UNDER")
                )

                # Evaluation and persistence remain independent of delivery.
                # A stale/direct-call pool row may already be live; score it and
                # preserve its directional opportunity, but never send it.
                _fpr_game_live = _is_game_live_or_past(_fpr_snap, now)
                if _fpr_has_direction and chat_ids and not _fpr_game_live:
                    _fpr_mq       = _fpr_cmq(_fpr_stat, _fpr_line, _fpr_score)
                    _fpr_mp       = _fpr_dmp(None, _fpr_hist)
                    # ── Tier delivery gate [full-pool-rescan] ────────────────
                    _fpr_mq_score = float(getattr(_fpr_mq, "score", 0) if _fpr_mq else 0)
                    _fpr_bq_score = float(_fpr_score.total if _fpr_score else 0)
                    _fpr_mq_ok    = _tier_delivery_gate(
                        _fpr_sport, _fpr_dec.recommendation, _fpr_bq_score, _fpr_mq_score,
                        _has_strong_delivery_evidence(
                            score=_fpr_score, intelligence_trace=getattr(_fpr_dec, "evidence", None),
                        ),
                        score_tier=getattr(_fpr_dec, "decision_tier", None),
                        confidence=getattr(_fpr_dec, "confidence", None),
                    )
                    if not _fpr_mq_ok:
                        fpr_not_delivered += 1
                        logger.debug(
                            "UD tier_gate [fpr]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                            _fpr_player, _fpr_stat, _fpr_sport, _fpr_bq_score, _fpr_mq_score, _fpr_dec.recommendation,
                        )
                    if _fpr_mq_ok:
                        _fpr_delivery_candidates.append({
                            "provider": _fpr_provider,
                            "player": _fpr_player,
                            "sport": _fpr_sport,
                            "stat_type": _fpr_stat,
                            "line_val": _fpr_line,
                            "score": _fpr_score,
                            "validation": _fpr_val,
                            "decision": _fpr_dec,
                            "market_quality": _fpr_mq,
                            "market_pressure": _fpr_mp,
                            "snap": _fpr_snap,
                            "pp_line": _fpr_pp_line,
                            "ext_id": _fpr_ext_id,
                            "event_id": str(
                                getattr(_fpr_snap, "event", "")
                                or getattr(_fpr_snap, "game_id", "")
                                or ""
                            ),
                        })
                        # The legacy inline branch below is retained for
                        # compatibility, but delivery now occurs after the
                        # complete batch has been strongest-pick filtered.
                        _fpr_mq_ok = False
                    if _fpr_mq_ok:
                     # ── Atomic dedup claim [full-pool-rescan] ─────────────────
                     if not await _try_claim_delivery_slot(
                         _fpr_player, _fpr_sport, _fpr_stat, _fpr_line,
                         provider=_fpr_provider,
                         source_id=str(getattr(_fpr_snap, "external_id", None) or getattr(_fpr_snap, "id", None) or ""),
                         event_id=str(getattr(_fpr_snap, "event", "") or getattr(_fpr_snap, "game_id", "") or ""),
                         direction=str(getattr(_fpr_dec, "recommendation", "") or ""),
                     ):
                         fpr_not_delivered += 1
                         logger.debug(
                             "UD dedup_claim [fpr]: %s | %s | concurrent path already claimed",
                             _fpr_player, _fpr_stat,
                         )
                         _fpr_mq_ok = False
                    if _fpr_mq_ok:
                     _fpr_delivery = AlertDelivery(db, bot, chat_ids)
                     try:
                      _fpr_result   = await _fpr_delivery.deliver_underdog(
                         player_name     = _fpr_player,
                         team            = getattr(_fpr_snap, "team", "") or "",
                         sport           = _fpr_sport,
                         stat_type       = _fpr_stat,
                         old_line        = _fpr_line,
                         new_line        = _fpr_line,
                         game_time       = _fpr_snap.game_time,
                         game_status     = (
                             getattr(_fpr_snap, "game_status", None)
                             or getattr(_fpr_snap, "provider_status", None)
                         ),
                        fetched_at      = getattr(_fpr_snap, "fetched_at", None),
                         score           = _fpr_score,
                         validation      = _fpr_val,
                         decision        = _fpr_dec,
                         market_quality  = _fpr_mq,
                         market_pressure = _fpr_mp,
                          pp_line         = _fpr_pp_line,
                         standing        = True,
                         strict_pregame   = True,
                         source          = _fpr_provider,
                          source_id       = str(
                              getattr(_fpr_snap, "external_id", None)
                              or getattr(_fpr_snap, "id", None)
                              or ""
                          ),
                          event_id        = str(
                              getattr(_fpr_snap, "event", "")
                              or getattr(_fpr_snap, "game_id", "")
                              or ""
                          ),
                          delivery_path   = "full_pool",
                      )
                     except Exception as _fpr_delivery_exc:
                      _release_prop_delivery_claim(
                          _prop_market_alerted,
                          _fpr_player,
                          _fpr_sport,
                          _fpr_stat,
                          _fpr_line,
                          provider=_fpr_provider,
                          source_id=str(
                              getattr(_fpr_snap, "external_id", None)
                              or getattr(_fpr_snap, "id", None)
                              or ""
                          ),
                          event_id=str(
                              getattr(_fpr_snap, "event", "")
                              or getattr(_fpr_snap, "game_id", "")
                              or ""
                          ),
                          direction=str(
                              getattr(_fpr_dec, "recommendation", "") or ""
                          ),
                      )
                      logger.warning(
                          "full_pool_rescan: delivery failed; claim released for "
                          "%s/%s/%s: %s",
                          _fpr_provider,
                          _fpr_player,
                          _fpr_stat,
                          type(_fpr_delivery_exc).__name__,
                      )
                      _fpr_result = DeliveryResult(sent=False)
                    if _fpr_mq_ok and _fpr_result.sent:
                        fpr_sent      += 1
                        # _record_prop_alerted already called by _try_claim_delivery_slot.
                        try:
                            if getattr(_fpr_result, "authoritative_recorded", True):
                                await db.mark_opportunity_alert_sent(
                                    _fpr_ext_id,
                                    _fpr_stat,
                                    provider=_fpr_provider,
                                )
                            await db.update_prop_lifecycle_state(
                                _fpr_provider,
                                _fpr_player,
                                _fpr_sport,
                                _fpr_stat,
                                "ACTIVE_ALERTED",
                                first_alert_sent_at=datetime.utcnow(),
                            )
                        except Exception as _fpr_bookkeeping_exc:
                            logger.debug(
                                "full_pool_rescan: delivery bookkeeping failed "
                                "for %s/%s/%s: %s",
                                _fpr_provider,
                                _fpr_player,
                                _fpr_stat,
                                _fpr_bookkeeping_exc,
                            )
                    elif _fpr_mq_ok:
                        # deliver_underdog returned not-sent (rate-limited, etc.)
                        fpr_not_delivered += 1
                        _release_prop_delivery_claim(
                            _prop_market_alerted,
                            _fpr_player,
                            _fpr_sport,
                            _fpr_stat,
                            _fpr_line,
                            provider=_fpr_provider,
                            source_id=str(
                                getattr(_fpr_snap, "external_id", None)
                                or getattr(_fpr_snap, "id", None)
                                or ""
                            ),
                            event_id=str(
                                getattr(_fpr_snap, "event", "")
                                or getattr(_fpr_snap, "game_id", "")
                                or ""
                            ),
                            direction=str(
                                getattr(_fpr_dec, "recommendation", "") or ""
                            ),
                        )
                elif not _fpr_has_direction:
                    fpr_not_delivered += 1
                elif _fpr_game_live:
                    fpr_not_delivered += 1

                fpr_rescored += 1
                fpr_by_sport[_fpr_sport] = fpr_by_sport.get(_fpr_sport, 0) + 1

            except Exception as _fpr_row_exc:
                logger.debug(
                    "full_pool_rescan: %s/%s: %s",
                    _fpr_player, _fpr_stat, _fpr_row_exc,
                )
                fpr_rescored        += 1
                fpr_total_rescanned += 1
                fpr_by_sport[_fpr_sport] = fpr_by_sport.get(_fpr_sport, 0) + 1

        # Selection happens only after the complete qualified batch has been
        # evaluated.  This prevents a stronger later-processed prop from
        # losing to an earlier weaker prop for the same provider/player.
        _fpr_selected = select_strongest_actionable_picks(
            _fpr_delivery_candidates
        )
        _fpr_selected_ids = {id(candidate) for candidate in _fpr_selected}
        for _fpr_candidate in _fpr_delivery_candidates:
            if id(_fpr_candidate) not in _fpr_selected_ids:
                fpr_not_delivered += 1
                logger.debug(
                    "strongest_pick: suppressed %s/%s/%s for provider=%s",
                    _fpr_candidate["player"],
                    _fpr_candidate["sport"],
                    _fpr_candidate["stat_type"],
                    _fpr_candidate["provider"],
                )

        for _fpr_candidate in _fpr_selected:
            _fpr_result, _fpr_claimed = await _deliver_full_pool_candidate(
                _fpr_candidate,
                db=db,
                bot=bot,
                chat_ids=chat_ids,
            )
            if _fpr_result.sent:
                fpr_sent += 1
                try:
                    if getattr(_fpr_result, "authoritative_recorded", True):
                        await db.mark_opportunity_alert_sent(
                            _fpr_candidate["ext_id"],
                            _fpr_candidate["stat_type"],
                            provider=_fpr_candidate["provider"],
                        )
                    await db.update_prop_lifecycle_state(
                        _fpr_candidate["provider"],
                        _fpr_candidate["player"],
                        _fpr_candidate["sport"],
                        _fpr_candidate["stat_type"],
                        "ACTIVE_ALERTED",
                        first_alert_sent_at=datetime.utcnow(),
                    )
                except Exception as _fpr_bookkeeping_exc:
                    logger.debug(
                        "full_pool_rescan: delivery bookkeeping failed "
                        "for %s/%s/%s: %s",
                        _fpr_candidate["provider"],
                        _fpr_candidate["player"],
                        _fpr_candidate["stat_type"],
                        _fpr_bookkeeping_exc,
                    )
            else:
                fpr_not_delivered += 1
                if _fpr_claimed:
                    _fpr_snap = _fpr_candidate["snap"]
                    _release_prop_delivery_claim(
                        _prop_market_alerted,
                        _fpr_candidate["player"],
                        _fpr_candidate["sport"],
                        _fpr_candidate["stat_type"],
                        _fpr_candidate["line_val"],
                        provider=_fpr_candidate["provider"],
                        source_id=str(
                            getattr(_fpr_snap, "external_id", None)
                            or getattr(_fpr_snap, "id", None)
                            or ""
                        ),
                        event_id=_fpr_candidate.get("event_id", ""),
                        direction=str(
                            getattr(
                                _fpr_candidate["decision"],
                                "recommendation",
                                "",
                            )
                            or ""
                        ),
                    )

        if _fpr_opportunity_rows:
            _fpr_bulk_opportunity = getattr(db, "log_prop_opportunity_bulk", None)
            if asyncio.iscoroutinefunction(_fpr_bulk_opportunity):
                try:
                    await _fpr_bulk_opportunity(_fpr_opportunity_rows)
                except Exception as _fpr_bulk_log_exc:
                    logger.warning(
                        "full_pool_rescan: bulk opportunity write failed: %s",
                        _fpr_bulk_log_exc,
                    )
            else:
                # Compatibility path for lightweight test doubles and older
                # database objects that do not expose the bulk method.
                for _fpr_row in _fpr_opportunity_rows:
                    try:
                        await db.log_prop_opportunity(**_fpr_row)
                    except Exception as _fpr_log_exc:
                        logger.debug(
                            "full_pool_rescan: opportunity log failed: %s",
                            _fpr_log_exc,
                        )

        # ── Advance cursor; increment rotation when pool is fully covered ──────
        if rotation_complete:
            fpr_rotation = fpr_rotation + 1
            fpr_next_cursor = 0
            if _health:
                _health.set_fpr_rotation(fpr_rotation)
                logger.info(
                    "full_pool_rescan: rotation #%d complete — starting rotation #%d",
                    fpr_rotation - 1, fpr_rotation,
                )

        if _health:
            _health.set_fpr_cursor(fpr_next_cursor)

    except Exception as _fpr_exc:
        logger.exception("full_pool_rescan_job: error: %s", _fpr_exc)
        _fpr_execution_running = False
        _ud_full_scan_running = False
        if _health:
            _health.record_job_fail("full_pool_rescan_job", str(_fpr_exc))
        _job_activity_end("fpr")
        return

    # ── Console log (human-readable — internal cursor values never shown) ─────
    _thick = "━" * 24
    _display_rotation = fpr_rotation - 1 if rotation_complete else fpr_rotation
    _pct = (
        f"{(fpr_end_cursor / fpr_pool_size * 100):.1f}%"
        if fpr_pool_size > 0 else "—"
    )
    _status_tag = "✅ COMPLETE → rotation #%d started" % fpr_rotation if rotation_complete else "🔄 in progress"
    logger.info(
        "\n%s\n🌐 Full-Pool Rescan\n%s\n"
        "Rotation:  #%d  (%s)\n"
        "Progress:  %s\n"
        "Coverage:  %s / %s active props\n"
        "Priority:  Unified active-prop rotation\n"
        "Batch:     %6d\n"
        "🔬 Rescored:   %6d\n"
        "✅ Sent:       %6d\n"
        "⏸ Not delivered: %6d\n"
        "%s",
        _thick, _thick,
        _display_rotation, _status_tag,
        _pct,
        f"{fpr_end_cursor:,}", f"{fpr_pool_size:,}",
        fpr_batch_size,
        fpr_rescored,
        fpr_sent,
        fpr_not_delivered,
        _thick,
    )

    # ── Persist stats to health sidecar ──────────────────────────────────────
    if _health:
        _health.set_fpr_stats({
            "rotation":             _display_rotation,
            "pool_size":            fpr_pool_size,
            "cursor_start":         fpr_cursor,
            "cursor_end":           fpr_end_cursor,
            "batch_size":           fpr_batch_size,
            "pct_complete":         round(fpr_end_cursor / fpr_pool_size * 100, 1)
                                    if fpr_pool_size else 0.0,
            "fpr_rescored":         fpr_rescored,
            "active_source_ids":    fpr_unique_source_ids,
            "batch_source_ids":     fpr_batch_source_ids,
            "line_movers_prioritized": fpr_line_movers,
            "uncovered_props":      max(0, fpr_pool_size - fpr_end_cursor),
            "next_rotation":        fpr_rotation,
            "fpr_sent":             fpr_sent,
            "fpr_not_delivered":    fpr_not_delivered,
            "fpr_total_rescanned":  fpr_total_rescanned,
            "fpr_by_sport":         dict(fpr_by_sport),
            "rotation_complete":    rotation_complete,
        })
        _health.record_job_run("full_pool_rescan_job")

    logger.info(
        "full_pool_rescan_job: rotation=#%d pool=%d batch=%d rescored=%d sent=%d",
        _display_rotation, fpr_pool_size, fpr_batch_size, fpr_rescored, fpr_sent,
    )
    _fpr_execution_running = False
    _ud_full_scan_running = False
    _job_activity_end("fpr")
