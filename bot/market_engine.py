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
import logging
from datetime import datetime, timedelta
from typing import Optional

from config import config
from engine.health import get_health_tracker
from engine.prop_intelligence import compute_prop_intelligence as _compute_intel
from engine.player_prop_market import _is_prop_deduped, _record_prop_alerted
from engine.score_validation import clamp_score
from database import (
    Database,
    MarketSnapshotRecord,
    CLVRecord,
    UnderdogSnapshotRecord,
    SteamRecord,
)
from connectors import ConnectorRegistry, MarketSnapshot
from engine.consensus import compute_consensus, find_inefficiencies, build_multi_book_steam_inputs
from engine.clv import build_clv_opportunity
from engine.steam import compute_steam_simple
from alerts import AlertDelivery, broadcast_alert, identify_sharp_books
from alerts_multiplatform import (
    format_steam_multibook_alert,
    format_inefficiency_alert,
    format_clv_opportunity_alert,
    format_underdog_change_alert,
    format_market_move_detected,  # noqa: F401 — imported for availability check
)

logger = logging.getLogger(__name__)

# Module-level registry — set by init_market_engine()
_registry: Optional[ConnectorRegistry] = None

# ── OddsAPI confirmation engine ───────────────────────────────────────────────
# Set once at startup via init_odds_confirmation(); used by
# _get_odds_api_confirmation() to call fetch_player_prop_lines().
_analysis_engine: Optional[object] = None


def init_odds_confirmation(engine: object) -> None:
    """Store the AnalysisEngine reference for OddsAPI player prop confirmation calls."""
    global _analysis_engine
    _analysis_engine = engine

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

# Set to True after the first complete Underdog prop scan.  The first cycle
# scores every active prop (cold-start mode); subsequent cycles use incremental
# scoring (new props and line-change events only).
_cold_start_done: bool = False

# ── Market availability tracking ─────────────────────────────────────────────
# Maps "player__stat_type" → datetime of first alert (bet pick).
# Used to compute how long a market was available before removal.
# Internal only — no Telegram alert is sent on removal (doc #4).
_MARKET_FIRST_ALERT: dict = {}
# Evict entries older than this many hours each scan cycle (prevents unbounded growth).
_MARKET_FIRST_ALERT_TTL_H: int = 24

# ── 95+ S-tier priority override ─────────────────────────────────────────────
# Tracks (player, sport, stat_type) tuples for which a 95+ override alert was
# already sent this session.  Persists across scan cycles; cleared on bot restart.
# Prevents the same exceptional prop from firing the override repeatedly.
_priority_override_sent: set = set()

# ── Underdog full-scan concurrency guard ──────────────────────────────────────
# Set to True while underdog_job is executing a full scan (fetch + score + deliver).
# A second instance (via max_instances=2) that finds this flag set will run only the
# fast new-prop detection path and return immediately, keeping the 2-minute polling
# cadence alive without duplicating the heavy scoring work.
_ud_full_scan_running: bool = False


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
    """Format the Telegram message for a 95+ Bet Quality S-tier priority alert.

    Triggered when decision.confidence ≥ 95 AND decision_tier == "S".
    Confidence + Quality gates apply; both OVER and UNDER are valid for all sports.
    """
    sport  = getattr(snap, "sport", None) or "UNKNOWN"
    rec    = getattr(decision, "recommendation", "PASS") if decision is not None else "PASS"
    conf   = getattr(decision, "confidence", 0)      if decision is not None else 0
    stars  = _bq_stars(int(conf))  # V3.4: stars from Bet Quality, not raw score.total

    dir_line = ""
    if rec not in ("PASS", None):
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
        f"\n<i>⚠️ Verify current line on Underdog before placing.</i>"
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
    _BLOCKED_STATUSES = {"live", "in_progress", "final", "completed", "closed"}
    _status = getattr(snap, "game_status", None)
    if isinstance(_status, str) and _status.lower() in _BLOCKED_STATUSES:
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
        for (player, sport, stat), (ts_unix, line_f) in recent_alerted.items():
            key = (player, sport, stat)
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
    player_name: str,
    stat_type: str,
    direction: str,
    line: float,
) -> Optional[dict]:
    """
    Query OddsAPI player prop lines as a non-blocking market confirmation signal.

    Only fires for sports/stats configured in _UD_TO_ODDS_API_MARKET (NBA + MLB).
    Uses the cached OddsApiCache — no extra quota if already fetched this cycle.

    Returns None when:
      • sport/stat not mapped, or _analysis_engine not initialised
      • player not found on any sportsbook
      • any exception or 5-second timeout

    Returns dict with keys: num_books, avg_line (float|None), notes (str), confirmed (bool).
    """
    if _analysis_engine is None:
        return None
    market_key = _UD_TO_ODDS_API_MARKET.get(stat_type.lower().strip())
    if not market_key:
        return None

    try:
        import asyncio as _asyncio
        from engine.analysis import Sport as _Sport
        try:
            sport_enum = _Sport(sport)
        except ValueError:
            return None

        lines = await _asyncio.wait_for(
            _analysis_engine.fetch_player_prop_lines(sport_enum),
            timeout=5.0,
        )
    except Exception:
        return None

    # Search for this player's market across all bookmakers
    player_lower   = player_name.lower().strip()
    direction_label = direction.strip().lower()  # "over" | "under"

    book_lines: list[float] = []
    book_odds:  list[int]   = []   # American odds for this direction (CLV seed use)
    sportsbooks:  set[str]  = set()

    for pl in lines:
        if pl.market_key != market_key:
            continue
        pl_name = (pl.player_name or "").lower().strip()
        # Accept exact match or surname suffix match (e.g. "Caminero" in "Junior Caminero")
        if pl_name != player_lower:
            parts_ud  = player_lower.split()
            parts_pl  = pl_name.split()
            if not (parts_ud and parts_pl and (
                parts_ud[-1] in pl_name or parts_pl[-1] in player_lower
            )):
                continue
        if pl.line is None:
            continue
        sportsbooks.add(pl.sportsbook)
        if (pl.description or "").lower().strip() == direction_label:
            book_lines.append(pl.line)
            if pl.american_odds:
                book_odds.append(pl.american_odds)

    if not sportsbooks:
        return None

    num_books = len(sportsbooks)
    avg_line  = sum(book_lines) / len(book_lines) if book_lines else None
    # avg_odds: average American odds across books for this direction.
    # Used for CLV seeding when the pick is stored as an AlertCLVSeed.
    avg_odds  = round(sum(book_odds) / len(book_odds)) if book_odds else None

    if avg_line is not None:
        diff = avg_line - line
        if abs(diff) < 0.05:
            notes = f"{num_books} book{'s' if num_books != 1 else ''} · avg {avg_line:.1f} ✅"
        elif diff > 0:
            notes = f"{num_books} book{'s' if num_books != 1 else ''} · avg {avg_line:.1f} ({diff:+.1f} vs UD)"
        else:
            notes = f"{num_books} book{'s' if num_books != 1 else ''} · avg {avg_line:.1f} ({diff:+.1f} vs UD)"
    else:
        notes = f"{num_books} book{'s' if num_books != 1 else ''} · no direct line match"

    return {
        "num_books": num_books,
        "avg_line":  avg_line,
        "avg_odds":  avg_odds,   # American odds for this direction (CLV seeding support)
        "notes":     notes,
        "confirmed": avg_line is not None,
    }


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
) -> "Optional[object]":
    """
    Fetch fresh game results (at most once per calendar day per player/stat),
    upsert to DB, then compute and return PlayerHitRates.

    Returns None on any failure so callers can always fall back to PASS.
    """
    from engine.player_results import compute_hit_rates

    provider  = _get_player_stats_provider()
    today     = datetime.utcnow().date().isoformat()
    cache_key = (player_name, sport, stat_type.lower().strip(), today)

    try:
        if cache_key not in _player_result_fetch_cache:
            # Guard against unbounded growth: clear the whole set when it exceeds the
            # ceiling.  The date component in each key means old entries are already
            # bypassed by logic; this just frees the memory they occupy.
            if len(_player_result_fetch_cache) >= _PLAYER_RESULT_CACHE_MAX:
                logger.info(
                    "_fetch_and_compute_hit_rates: result cache hit %d entries — clearing",
                    len(_player_result_fetch_cache),
                )
                _player_result_fetch_cache.clear()
            raw_results = await provider.fetch_results(player_name, sport, stat_type)
            for r in raw_results:
                await db.upsert_player_result(r)
            _player_result_fetch_cache.add(cache_key)
            if raw_results:
                logger.debug(
                    "player_results: fetched %d results for %s / %s",
                    len(raw_results), player_name, stat_type,
                )

        db_results = await db.get_player_results(player_name, sport, stat_type, limit=30)
        if not db_results:
            return None
        result = compute_hit_rates(db_results, current_line)
        # Defensive guard: compute_hit_rates must return PlayerHitRates, never a list.
        # If it somehow does, log a warning and return None so the caller falls back to PASS.
        if not hasattr(result, "has_real_data"):
            logger.warning(
                "_fetch_and_compute_hit_rates: unexpected type %s for %s/%s — returning None",
                type(result).__name__, player_name, stat_type,
            )
            return None
        return result

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_fetch_and_compute_hit_rates: %s / %s: %s", player_name, stat_type, exc
        )
        return None

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

    # Decision gate
    if decision is not None:
        _rec = getattr(decision, "recommendation", "PASS")
        if _rec in ("OVER", "UNDER"):
            codes.append(f"GATE:{_rec}")
        else:
            codes.append("GATE:DECISION_PASS")

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
    tier = p.get("tier", "PASS")

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
        await broadcast_alert(bot, chat_ids, message)
        logger.info(
            "Inefficiency alert: %s | %s | %s | dev=%+d",
            ineff.event, ineff.selection, ineff.sportsbook, ineff.deviation,
        )
        # Persist dedup marker only when alert was actually sent
        await db.save_market_snapshot(MarketSnapshotRecord(
            sportsbook  = ineff.sportsbook,
            sport       = ineff.sport,
            league      = ineff.sport,
            event       = ineff.event,
            market_type = ineff.market_type,
            selection   = ineff.selection,
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
        await broadcast_alert(bot, chat_ids, message)
        logger.info(
            "Multi-book steam alert: %s | %s | score=%d",
            event, selection, result.steam_score,
        )
        # Persist SteamRecord only when alert was actually sent
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

_DELIVERY_TIER_BASE: dict[str, float] = {
    "S": 10000.0, "A": 5000.0, "B": 1000.0, "C": 200.0, "PASS": 0.0,
}


# Sports that are Tier 2 (major sports — secondary delivery priority).
# Tier 1 = EVERY supported sport NOT in this set (WNBA, NHL, Tennis, Soccer,
# CS2, Dota 2, LoL, VAL, MMA, Badminton, Table Tennis, Racing, CFB, CFL, KBO,
# NPB, and every other supported non-NBA/MLB/NFL sport).
# This is the CANONICAL source of truth — do NOT expand it.
_TIER2_SPORTS: frozenset[str] = frozenset({"NBA", "MLB", "NFL"})


def _is_tier2_sport(sport: str) -> bool:
    """Return True iff sport is Tier 2 (NBA, MLB, or NFL only)."""
    return (sport or "").upper() in _TIER2_SPORTS


def _tier_delivery_gate(
    sport: str,
    direction: str,
    bq_score: float,
    mq_score: float,
) -> bool:
    """
    Canonical delivery eligibility gate used by ALL Telegram delivery paths.

    Tier 1 = every supported sport except NBA, MLB, NFL.
    Tier 2 = ONLY NBA, MLB, NFL.

    Tier 1 rules (analysis-first):
        • Valid OVER or UNDER direction is required.
        • BQ and MQ are ranking signals only — NOT hard blockers.
        • Do NOT reject Tier 1 for BQ < 85 or MQ < 85.

    Tier 2 rules (stricter):
        • Valid OVER or UNDER direction is required.
        • BQ ≥ 85 is required.
        • MQ ≥ 85 is required.
        • BOTH are mandatory — failing either blocks Telegram delivery.

    Returns True if the candidate may proceed to delivery; False to block.
    """
    if direction.upper() not in ("OVER", "UNDER"):
        return False
    if _is_tier2_sport(sport):
        return bq_score >= 85.0 and mq_score >= 85.0
    return True  # Tier 1: valid direction is the only mandatory numeric filter


async def _try_claim_delivery_slot(
    player: str,
    sport: str,
    stat: str,
    line: float,
) -> bool:
    """Atomically check the dedup dict and claim the delivery slot if available.

    Must be called under the asyncio event loop (i.e. with ``await``).

    Returns True  → slot claimed; caller should proceed with delivery.
    Returns False → another path already claimed this candidate; skip delivery.

    The claim is recorded in ``_prop_market_alerted`` BEFORE the actual
    ``deliver_underdog`` call so that a concurrent job (SR / WL / FPR / delivery
    queue) checking the same candidate during network I/O cannot also pass the
    dedup check and cause a duplicate Telegram alert.

    If delivery subsequently fails (rate-limited, Telegram error, etc.) the
    claim intentionally remains — the prop is treated as "recently attempted"
    and will not be retried within the dedup window.  This prevents a flurry
    of retry attempts when the rate-limiter is saturated.
    """
    async with _prop_dedup_lock:
        if _is_prop_deduped(
            _prop_market_alerted, player, sport, stat, line,
            dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
            min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
        ):
            return False
        # Pre-claim: record immediately so concurrent paths see it as alerted.
        _record_prop_alerted(_prop_market_alerted, player, sport, stat, line)
        return True


def _cand_priority(c: dict) -> float:
    """Raw priority score for a delivery candidate (higher = deliver first)."""
    base = _DELIVERY_TIER_BASE.get(c.get("tier", "PASS"), 0.0)
    conf = float(c.get("conf") or 0)
    bq   = float(c.get("bq")   or 0)
    mq   = float(c.get("mq")   or 0)
    t1   = 500.0 if c.get("is_tier1") else 0.0
    mc   = 200.0 if c.get("is_meaningful_change") else 0.0
    return base + conf * 0.5 + bq * 0.3 + mq * 0.2 + t1 + mc


def _apply_delivery_diversification(queue: list) -> list:
    """
    Split candidates into Tier 1 (non-NBA/MLB/NFL) and Tier 2 (NBA/MLB/NFL) groups,
    rank within each group by priority with a soft diversification penalty, then
    interleave so Tier 1 candidates always precede Tier 2 candidates in the final
    delivery order.

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

    # Split by sport tier — Tier 2 = NBA/MLB/NFL only, everything else is Tier 1.
    t1 = [c for c in queue if (c.get("sport") or "").upper() not in _TIER2_SPORTS]
    t2 = [c for c in queue if (c.get("sport") or "").upper() in _TIER2_SPORTS]

    # Rank within each tier independently.
    _rank_group(t1)
    _rank_group(t2)

    # Tier 1 first, then Tier 2 — delivery loop enforces the per-tier hard cap.
    result = t1 + t2
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
            await broadcast_alert(bot, chat_ids, message)
            logger.info(
                "CLV opportunity: %s | %s | %s | lead=%+d",
                opp.event, opp.selection, opp.sportsbook, opp.clv_lead,
            )
            # Persist dedup marker only when alert was actually sent
            await db.save_market_snapshot(MarketSnapshotRecord(
                sportsbook  = snap.sportsbook,
                sport       = snap.sport,
                league      = snap.league,
                event       = snap.event,
                market_type = snap.market_type,
                selection   = snap.selection,
                odds        = snap.odds,
                recorded_at = now,
                alert_sent  = True,
            ))

            break  # one alert per market key per cycle


# ── Underdog pick'em job ───────────────────────────────────────────────────────

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

    global _cold_start_done
    is_cold_start = not _cold_start_done   # True only on the very first successful fetch

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()

    _health = get_health_tracker()
    if _health:
        _health.record_job_started("underdog_job")

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
        await _init_state_from_db(db)

    # ── Concurrency guard — fast new-prop path when full scan is already running ──
    # max_instances=2 allows a second underdog_job to start while the first is still
    # scoring.  If the primary scan is active, the secondary instance only fetches +
    # detects new props (fast, ~2–5 s) and returns immediately so the 2-min cadence
    # is preserved without duplicating heavy scoring work.
    global _ud_full_scan_running
    if _ud_full_scan_running:
        logger.info("underdog_job: full scan in progress — running fast new-prop fetch only")
        try:
            _fp_snaps = await _registry.fetch_pickem()
            if _fp_snaps:
                _fp_ud = [s for s in _fp_snaps if s.sportsbook == "Underdog"]
                _fp_known = await db.get_known_underdog_prop_keys()
                _fp_new_count = sum(
                    1 for s in _fp_ud
                    if "[REMOVED]" not in (s.selection or "")
                    and (
                        s.player or "",
                        _extract_ud_stat_type(s.selection, s.player, s.line),
                    ) not in _fp_known
                )
                if _fp_new_count:
                    logger.info(
                        "underdog_job [fast-fetch]: %d new props detected — "
                        "will be processed when primary scan completes",
                        _fp_new_count,
                    )
            if _health:
                _health.record_provider_fetch("Underdog")
                _health.record_job_run("underdog_job")
        except Exception as _fp_exc:
            logger.warning("underdog_job [fast-fetch]: error: %s", _fp_exc)
        return

    _ud_full_scan_running = True

    # Memory baseline — log current RSS before the heavy scan to diagnose OOM kills.
    # Uses VmRSS (/proc/self/status) which reflects actual live RSS rather than the
    # all-time high-water mark returned by resource.ru_maxrss.
    _mem_before = _rss_mb()

    try:
        snapshots = await _registry.fetch_pickem()
    except Exception as _fetch_exc:
        if _health:
            _health.record_provider_error("Underdog", str(_fetch_exc))
            _health.record_job_fail("underdog_job", str(_fetch_exc))
        logger.exception("underdog_job: fetch_pickem failed: %s", _fetch_exc)
        _ud_full_scan_running = False
        return

    if not snapshots:
        logger.debug("underdog_job: no pick'em snapshots")
        if _health:
            _health.record_provider_fetch("Underdog")
            _health.record_job_run("underdog_job")   # empty response = successful run
        _ud_full_scan_running = False
        return

    if _health:
        _health.record_provider_fetch("Underdog")

    ud_snaps = [s for s in snapshots if s.sportsbook == "Underdog"]
    # Capture count before the list is cleared later for memory recovery.
    # record_underdog_scan() at end-of-cycle reads this variable so it
    # always reflects the actual number of snapshots processed this cycle.
    _n_ud_snaps_this_cycle: int = len(ud_snaps)

    # Reset per-cycle delivery counters in the global rate limiter so the
    # end-of-cycle summary (log_cycle_summary) reflects only this scan.
    try:
        from engine.telegram_rate_limiter import get_rate_limiter as _get_rl_job
        _get_rl_job().reset_cycle_counters()
    except Exception:
        pass

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
        if _health:
            _health.record_job_run("underdog_job")
        return

    # Summary counters — emitted as a single INFO line after the loop
    _n_scored:        int       = 0
    _tier_counts:     dict      = {"S": 0, "A": 0, "B": 0, "PASS": 0}
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
    _cs_before_tiers:     dict       = {}  # tier counts under old calibration (before)
    _cs_after_tiers:      dict       = {}  # tier counts under new calibration (after)
    _scored_props:        list[dict] = []  # all scored props this cycle — for end-of-cycle debug log
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
    # Scoring data for every scored prop — bulk-written to PropLineHistory at end of
    # scan cycle so /picks and /slip can apply tier-aware delivery gates.
    # Key: (player, sport, stat_type) → (mq_score: int, bq_score: float).
    _plh_mq_scores:      dict                       = {}
    # Per-sport pipeline stage counters — emitted in debug summary (#7 diagnostics)
    _sport_raw:           dict[str, int] = {}  # daily props: non-removed, non-futures
    _sport_futures:       dict[str, int] = {}  # season-long/futures props (tracked, not scored)
    _sport_move_detected: dict[str, int] = {}  # significant line moves (≥MIN_LINE_CHANGE)
    _sport_gated:         dict[str, int] = {}  # passed full scoring gate (actionable)

    try:
        # Two DB round-trips before the loop — both O(unique props), no LIMIT.
        # 1. Most-recent non-removed snapshot per (player, stat) for line-change detection.
        # 2. All ever-seen (player, stat) keys (incl. removed) to detect first appearances.
        recent_by_key: dict[tuple[str, str], UnderdogSnapshotRecord] = (
            await db.get_latest_underdog_snapshot_per_prop()
        )
        known_keys: set[tuple[str, str]] = await db.get_known_underdog_prop_keys()
    
        for snap in ud_snaps:
            is_removed = "[REMOVED]" in snap.selection
            if is_removed:
                _n_removed += 1
            player = snap.player or "Unknown"

            # Extract stat-type before any counters so futures are correctly separated.
            stat_type = _extract_ud_stat_type(snap.selection, snap.player, snap.line)

            # Separate season-long futures / award markets from daily props.
            # Count them in _sport_futures so diagnostics show the split clearly.
            if not is_removed and _is_futures_stat(stat_type):
                _sp_fut = snap.sport or "UNKNOWN"
                _sport_futures[_sp_fut] = _sport_futures.get(_sp_fut, 0) + 1
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
                if prev_record.line_value != snap.line:
                    line_changed = True
                    prev_line    = prev_record.line_value

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
            market_quality:     Optional[object]      = None    # MarketQuality — display context
            market_pressure:    Optional[object]      = None    # MarketPressureFlag — warning only
            _dq_collected:      bool                  = False   # True when this snap is queued for ranked delivery
    
            if is_new_prop:
                # ── New-prop path ────────────────────────────────────────────────
                _n_new_prop += 1
                line_val = snap.line or 0.0
                # Load history even for new props — needed for validation gate.
                # Truly first-ever props return empty list → validation blocks alert.
                np_history = await db.get_ud_prop_history(player, stat_type, limit=30)
                score = score_ud_prop(
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
                # Validation: require min supporting history before any immediate alert.
                # Props with zero history (first appearance) always go to digest.
                validation = validate_player_prop(
                    player_name  = player,
                    stat_type    = stat_type,
                    current_line = line_val,
                    history      = np_history,
                    min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                )
                # Immediate criteria — sport-aware:
                #   - 0.5 line AND supported betting category (all sports)
                #   - OR score reaches the sport-specific quality threshold:
                #       Tier 2 (MLB/NFL) → UD_MIN_STARS_TO_ALERT (default 3)
                #       Tier 1 (others)  → UD_NON_STRICT_MIN_STARS (default 2)
                #     Uses the same min_stars_for_sport() helper as line-change
                #     and standing paths so the star floor is consistent across
                #     all three alert paths.
                np_immediate = (
                    (line_val <= config.UD_NEW_PROP_IMMEDIATE_LINE_THRESHOLD
                     and stat_type in config.UD_PRIORITY_STAT_CATEGORIES)
                    or score.stars >= config.UD_NON_STRICT_MIN_STARS
                )
                # Validation gate: block if insufficient player history
                if np_immediate and not validation.has_supporting_data:
                    np_immediate = False
                    logger.debug(
                        "UD new-prop validation blocked: %s | %s | %s",
                        player, stat_type, validation.reason,
                    )
                # Fetch real game results — required before any directional pick.
                # Also fetch for np_immediate props (e.g. 0.5-line priority stats)
                # even when they score PASS tier (no prev_line → low movement score).
                hit_rates = None
                if validation.has_supporting_data and (score.tier != "PASS" or np_immediate):
                    hit_rates = await _fetch_and_compute_hit_rates(
                        db, player, snap.sport or "UNKNOWN", stat_type, line_val
                    )
                    decision = make_ud_bet_decision(
                        score        = score,
                        validation   = validation,
                        current_line = line_val,
                        prev_line    = None,
                        hit_rates    = hit_rates,
                    )
                    # Log every evaluated opportunity (PLAY or PASS) for tracking
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
                            provider          = "Underdog",
                            bet_quality_score = decision.confidence,
                            reason_codes      = _compute_reason_codes(score, decision),
                            watchlist_state   = (
                                "Qualified" if decision.recommendation != "PASS" else "Rejected"
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
                # Send only when: S/A score, real directional pick, and sport is
                # in the betting-alert whitelist.
                _np_bet_ready = (
                    np_immediate
                    and decision is not None
                    and decision.recommendation != "PASS"
                    and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                )
                # Per-tier confidence gate — sport-conditional: MLB/NFL use strict thresholds;
                # all other sports use relaxed thresholds to surface more opportunities (#2).
                if _np_bet_ready and decision is not None:
                    _np_min_conf = config.min_conf_for_sport_tier(
                        snap.sport or "", decision.decision_tier
                    )
                    if decision.confidence < _np_min_conf:
                        _np_bet_ready = False
                        logger.debug(
                            "UD conf_gate [new]: %s | %s | conf=%d < min=%d (tier=%s sport=%s)",
                            player, stat_type,
                            decision.confidence, _np_min_conf,
                            decision.decision_tier, snap.sport or "UNKNOWN",
                        )
                # Strict-sport tier gate — MLB and NFL are S-tier only (default).
                # All other sports follow normal S/A/B/C tier rules.
                if _np_bet_ready and decision is not None:
                    _sport_up = (snap.sport or "").upper()
                    if (_sport_up in config.ud_strict_alert_sports
                            and decision.decision_tier not in config.ud_mlb_alert_tiers):
                        _np_bet_ready = False
                        logger.debug(
                            "UD sport_tier_gate [new]: %s | %s | sport=%s tier=%s blocked (min=%s)",
                            player, stat_type, _sport_up,
                            decision.decision_tier, config.UD_MLB_MIN_TIER,
                        )
                # Direction gate for Tier 2 (MLB/NFL):
                #   NFL UNDER → fully allowed (all markets)
                #   MLB UNDER → restricted to whitelist markets only
                #   MLB/NFL OVER → always allowed
                if _np_bet_ready and decision is not None:
                    _np_sport_strict = (snap.sport or "").upper()
                    if (_np_sport_strict == "MLB"
                            and decision.recommendation == "UNDER"
                            and not config.is_mlb_under_allowed(stat_type)):
                        _np_bet_ready = False
                        logger.debug(
                            "UD mlb_under_gate [new]: %s | %s | market not in MLB UNDER whitelist",
                            player, stat_type,
                        )
                    # NFL UNDER: no gate — fully allowed per spec
                # BQ gate removed: decision_tier (S/A) enforces quality.
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
                    # OddsAPI market confirmation — non-blocking, S/A tier only
                    _np_odds_confirm: Optional[dict] = None
                    if (decision is not None
                            and decision.decision_tier in ("S", "A")
                            and decision.recommendation != "PASS"):
                        try:
                            _np_odds_confirm = await _get_odds_api_confirmation(
                                snap.sport or "UNKNOWN",
                                player,
                                stat_type,
                                decision.recommendation,
                                line_val,
                            )
                        except Exception:
                            pass
                    # ── Tier delivery gate [new-prop] ────────────────────────
                    # Tier 1 (all except NBA/MLB/NFL): direction only.
                    # Tier 2 (NBA/MLB/NFL): BQ ≥ 75 AND MQ ≥ 75 AND direction.
                    _np_mq_score  = float(getattr(market_quality, "score", 0) if market_quality else 0)
                    _np_bq_score  = float(score.total if score else 0)
                    _np_direction = decision.recommendation if decision else ""
                    _np_gate_ok   = _tier_delivery_gate(
                        snap.sport or "UNKNOWN", _np_direction, _np_bq_score, _np_mq_score,
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
                            "is_reentry":         False,
                            "ext_id":             _np_ext_id,
                            "record":             None,   # linked after record construction
                            "sport":              snap.sport or "UNKNOWN",
                            "tier":               decision.decision_tier if decision else "B",
                            "conf":               float(decision.confidence if decision else 0),
                            "bq":                 float(score.total if score else 0),
                            "mq":                 float(getattr(market_quality, "score", 0) if market_quality else 0),
                            "is_meaningful_change": False,  # new props have no prev line
                            "is_tier1":           (snap.sport or "UNKNOWN") in config.ud_tier1_sports,
                            "_sent":              False,
                        })
                        _dq_collected = True
                        # ud_result stays DeliveryResult(sent=False) — delivery happens below.
                # ── Debug tracking (new-prop) ─────────────────────────────────────
                if score is not None:
                    if _np_bet_ready:
                        _np_rej = "sent" if ud_result.sent else (
                            "filtered" if ud_result.filtered else "new_prop_failed"
                        )
                    elif not np_immediate:
                        _np_rej = (
                            "validation_blocked" if not validation.has_supporting_data
                            else "not_immediate"
                        )
                    elif decision is None:
                        _np_rej = "no_decision"
                    elif decision.recommendation == "PASS":
                        _np_rej = "decision_pass"
                        logger.debug(
                            "UD decision_pass [new]: %s | %s | %s | line=%.1f"
                            " | %d★ %d/100 | reason=%s"
                            " | l5=%s l10=%s games=%s val_data=%s",
                            player, stat_type, snap.sport or "UNKNOWN", line_val,
                            score.stars, score.total,
                            decision.reason,
                            (f"{decision.l5_hit_rate:.0%}({decision.l5_games}g)"
                             if decision.l5_hit_rate is not None else "N/A"),
                            (f"{decision.l10_hit_rate:.0%}({decision.l10_games}g)"
                             if decision.l10_hit_rate is not None else "N/A"),
                            (hit_rates.total_games if hit_rates is not None else "no_data"),
                            getattr(validation, "has_supporting_data", "?"),
                        )
                    elif (snap.sport or "UNKNOWN") not in config.ud_alert_sports:
                        _np_rej = f"sport_blocked ({snap.sport})"
                    else:
                        _np_rej = "unknown"
                    _scored_props.append({
                        "player":          player,
                        "stat_type":       stat_type,
                        "sport":           snap.sport or "UNKNOWN",
                        "total":           score.total,
                        "tier":            score.tier,
                        "stars":           score.stars,
                        "stars_d":         getattr(score, "stars_display", "?????"),
                        "rejection":       _np_rej,
                        "path":            "new",
                        "decision_reason": (decision.reason if decision is not None else None),
                        # decision_tier is the bet-decision confidence tier (S≥85/A≥70/B≥50),
                        # distinct from score.tier (composite UDPropScore tier).
                        # MLB/NFL gate uses decision_tier; score.tier is the composite label.
                        "decision_tier":   (decision.decision_tier if decision is not None else None),
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
                    ud_history = await db.get_ud_prop_history(player, stat_type, limit=30)
                    score = score_ud_prop(
                        player_name  = player,
                        stat_type    = stat_type,
                        sport        = snap.sport or "UNKNOWN",
                        current_line = snap.line or 0.0,
                        prev_line    = prev_line,
                        history      = ud_history,
                    )
                    _lc_magnitude   = abs(snap.line - prev_line) if (snap.line is not None and prev_line is not None) else None
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
                    )
                    # Fetch real game results — required before any directional pick
                    hit_rates = None
                    if validation.has_supporting_data and score.tier != "PASS":
                        hit_rates = await _fetch_and_compute_hit_rates(
                            db, player, snap.sport or "UNKNOWN", stat_type, snap.line or 0.0
                        )
                        decision = make_ud_bet_decision(
                            score        = score,
                            validation   = validation,
                            current_line = snap.line or 0.0,
                            prev_line    = prev_line,
                            hit_rates    = hit_rates,
                        )
                        # Log every evaluated opportunity (PLAY or PASS) for tracking
                        try:
                            await db.log_prop_opportunity(
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
                                provider          = "Underdog",
                                bet_quality_score = decision.confidence,
                                reason_codes      = _compute_reason_codes(score, decision),
                                watchlist_state   = (
                                    "Qualified" if decision.recommendation != "PASS" else "Rejected"
                                ),
                            )
                        except Exception as _pol_exc:
                            logger.warning(
                                "underdog_job: log_prop_opportunity [lc] failed: %s", _pol_exc
                            )
                    elif score.tier == "PASS":
                        # P4: Log PASS-scored props even without a directional decision
                        # so all evaluated props are tracked in prop_opportunity_log.
                        try:
                            await db.log_prop_opportunity(
                                external_id    = getattr(snap, "external_id", None) or getattr(snap, "id", None) or "",
                                player_name    = player,
                                team           = snap.team or "",
                                sport          = snap.sport or "UNKNOWN",
                                stat_type      = stat_type,
                                line_value     = snap.line or 0.0,
                                recommendation = "PASS",
                                decision_tier  = "PASS",
                                confidence     = 0,
                                game_time      = snap.game_time,
                                provider       = "Underdog",
                                watchlist_state = "Rejected",
                            )
                        except Exception as _pol_exc:
                            logger.warning(
                                "underdog_job: log_prop_opportunity [lc-pass] failed: %s", _pol_exc
                            )
                    _n_scored += 1
                    _tier_counts[score.tier] = _tier_counts.get(score.tier, 0) + 1

                    logger.debug(
                        "UD score: %s | %s | %s (tier=%s stars=%d n=%d has_data=%s)",
                        player, stat_type, score.total, score.tier, score.stars,
                        score.n_history, validation.has_supporting_data,
                    )
    
                elif not is_removed and is_cold_start:
                    # ── Cold-start path ───────────────────────────────────────────
                    # First cycle only: score every active prop so the DB has fresh
                    # tier / stars / validation data from startup.  hit_rates are
                    # intentionally skipped — fetching them for ~1000 props on boot
                    # would hammer the stats API; they are populated lazily on the
                    # first qualifying incremental event.  No alerts are sent.
                    ud_history = await db.get_ud_prop_history(player, stat_type, limit=30)
                    score = score_ud_prop(
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
                    )
                    # ── Before/after tier comparison for the completion log ───────
                    _legacy_act   = _score_historical_activity_legacy(ud_history)
                    _legacy_total = 0 + _legacy_act + score.avg_vs_line + score.consistency + score.stability
                    if   _legacy_total >= 80: _legacy_tier = "S"
                    elif _legacy_total >= 65: _legacy_tier = "A"
                    elif _legacy_total >= 50: _legacy_tier = "B"
                    else:                     _legacy_tier = "PASS"
                    _cs_before_tiers[_legacy_tier] = _cs_before_tiers.get(_legacy_tier, 0) + 1
                    _cs_after_tiers[score.tier]    = _cs_after_tiers.get(score.tier, 0) + 1
    
                    _n_cold_start_scored += 1
                    _tier_counts[score.tier] = _tier_counts.get(score.tier, 0) + 1
    
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
                    score = score_ud_prop(
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
                    # Line-change props: require A-tier or better, a real directional
                    # pick from the decision engine, and sport in betting whitelist.
                    # Cold-start props always fail this gate — alerts suppressed.
                    # Re-entries qualify via is_reentry_qualified (set above) regardless
                    # of decision engine result — there is no previous line to compare.
                    # Every non-removal alert requires a real directional pick.
                    # Re-entries no longer bypass the decision engine.
                    _lc_sport_up = (snap.sport or "").upper()
                    # MLB UNDER direction gate — restricted to whitelist markets only.
                    # MLB OVER: unrestricted. NFL UNDER: no gate (fully allowed).
                    _lc_mlb_ok = (
                        _lc_sport_up != "MLB"
                        or decision is None
                        or decision.recommendation != "UNDER"
                        or config.is_mlb_under_allowed(stat_type)
                    )
                    # Tier gate: B or better → actionable (S/A/B); C → watchlist.
                    # Tier 1 vs Tier 2 affects priority/ranking, not A/B actionability.
                    is_qualified = (
                        not is_cold_start
                        and score is not None
                        and decision is not None
                        and decision.recommendation != "PASS"
                        and decision.decision_tier in ("S", "A", "B", "C")
                        and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                        and _lc_mlb_ok
                    )
                    if is_qualified and not is_reentry_qualified:
                        _n_qualified += 1
                    # ── Debug tracking (line-change / cold-start) ─────────────────
                    if score is not None:
                        if is_cold_start:
                            _lc_rej = "cold_start"
                        elif is_qualified:
                            _lc_rej = "qualified"
                        elif decision is None:
                            _lc_rej = "no_decision (PASS tier)"
                        elif decision.recommendation == "PASS":
                            _lc_rej = "decision_pass"
                            logger.debug(
                                "UD decision_pass [lc]: %s | %s | %s | line=%.1f"
                                " | %d★ %d/100 | reason=%s"
                                " | l5=%s l10=%s games=%s val_data=%s",
                                player, stat_type, snap.sport or "UNKNOWN",
                                snap.line or 0.0,
                                score.stars, score.total,
                                decision.reason,
                                (f"{decision.l5_hit_rate:.0%}({decision.l5_games}g)"
                                 if decision.l5_hit_rate is not None else "N/A"),
                                (f"{decision.l10_hit_rate:.0%}({decision.l10_games}g)"
                                 if decision.l10_hit_rate is not None else "N/A"),
                                (hit_rates.total_games if hit_rates is not None else "no_data"),
                                getattr(validation, "has_supporting_data", "?"),
                            )
                        elif (snap.sport or "UNKNOWN") not in config.ud_alert_sports:
                            _lc_rej = f"sport_blocked ({snap.sport})"
                        elif not _lc_mlb_ok:
                            if decision.recommendation == "UNDER":
                                _lc_rej = f"mlb_under_blocked ({decision.decision_tier})"
                            else:
                                _lc_rej = f"mlb_tier_blocked ({decision.decision_tier}, MLB min=S)"
                        else:
                            _lc_rej = "unknown"
                        _scored_props.append({
                            "player":          player,
                            "stat_type":       stat_type,
                            "sport":           snap.sport or "UNKNOWN",
                            "team":            snap.team or "",
                            "total":           score.total,
                            "tier":            score.tier,
                            "stars":           score.stars,
                            "stars_d":         getattr(score, "stars_display", "?????"),
                            "rejection":       _lc_rej,
                            "path":            "cs" if is_cold_start else "lc",
                            "decision_reason": (decision.reason if decision is not None else None),
                            # decision_tier is the bet-decision confidence tier (S≥85/A≥70/B≥50),
                            # distinct from score.tier (composite UDPropScore tier).
                            # MLB/NFL gate uses decision_tier; score.tier is the composite label.
                            "decision_tier":   (decision.decision_tier if decision is not None else None),
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
                # Per-tier confidence gate for directional picks (not removals)
                if should_alert and not is_removed and decision is not None and decision.recommendation != "PASS":
                    _lc_min_conf = {
                        "S": config.UD_MIN_CONF_S,
                        "A": config.UD_MIN_CONF_A,
                        "B": config.UD_MIN_CONF_B,
                    }.get(decision.decision_tier, 0)
                    if decision.confidence < _lc_min_conf:
                        should_alert = False
                        logger.debug(
                            "UD conf_gate [lc]: %s | %s | conf=%d < min=%d (tier=%s)",
                            player, stat_type,
                            decision.confidence, _lc_min_conf, decision.decision_tier,
                        )

                # BQ gate removed — decision_tier (S/A only) already enforces quality.
                # MLB/NFL UNDER is now allowed per Tier 2 spec.

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
                    ):
                        should_alert = False
                        logger.debug(
                            "UD dedup_gate [lc]: %s | %s | already alerted recently at line=%.1f",
                            player, stat_type, snap.line or 0.0,
                        )

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
                    # OddsAPI market confirmation — non-blocking, S/A tier only, non-removal
                    if (not is_removed
                            and decision is not None
                            and decision.decision_tier in ("S", "A")
                            and decision.recommendation != "PASS"):
                        try:
                            _lc_odds_confirm = await _get_odds_api_confirmation(
                                snap.sport or "UNKNOWN",
                                player,
                                stat_type,
                                decision.recommendation,
                                snap.line or 0.0,
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
                            "is_reentry":         bool(is_reentry_qualified),
                            "ext_id":             _lc_ext_id_q,
                            "record":             None,   # linked after record construction
                            "sport":              snap.sport or "UNKNOWN",
                            "tier":               decision.decision_tier if decision else "B",
                            "conf":               float(decision.confidence if decision else 0),
                            "bq":                 float(score.total if score else 0),
                            "mq":                 float(getattr(market_quality, "score", 0) if market_quality else 0),
                            "is_meaningful_change": _lc_is_mc,
                            "is_tier1":           (snap.sport or "UNKNOWN") in config.ud_tier1_sports,
                            "_sent":              False,
                        })
                        _dq_collected = True
                    # ud_result stays DeliveryResult(sent=False) — delivery happens below.
    
            # Post-send callbacks (dedup, lifecycle, CLV, marks) for non-removal props
            # are handled in the ranked delivery phase below.
            # Removals: track lifecycle independently (no Telegram alert).
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
            record = UnderdogSnapshotRecord(
                external_id   = f"{player}_{stat_type}"[:64],  # stable identity key
                player_name   = player,
                team          = snap.team or "",
                sport         = snap.sport,
                stat_type     = stat_type,              # actual stat, not "Pick'em"
                line_value    = snap.line or 0.0,
                game_id       = snap.event,
                game_time     = snap.game_time,
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
                # Derive effective tier: if the latest snapshot has score_tier=NULL (stored
                # during a no-change cycle without re-scoring), fall back to deriving tier
                # from score_total so stable high-quality Tier 1 props aren't silently
                # dropped merely because a later no-change snapshot has score_tier=NULL.
                _prev_eff_tier = _prev.score_tier
                # Only apply score_total fallback when score_tier is NULL (no-change
                # cycle stored without re-scoring).  An explicitly stored "B" or "PASS"
                # tier must NOT be promoted by the fallback.
                if _prev_eff_tier is None and _prev.score_total is not None:
                    if _prev.score_total >= 80:
                        _prev_eff_tier = "S"
                    elif _prev.score_total >= 65:
                        _prev_eff_tier = "A"
                    elif _prev.score_total >= 50:
                        _prev_eff_tier = "B"
                if _prev_eff_tier not in ("A", "S", "B"):
                    continue
    
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
                if await db.has_recent_ud_alert(_sp, _st, within_seconds=86400):
                    continue

                # In-memory dedup — catches same-session props sent via 95+ broadcast_alert.
                # broadcast_alert does NOT set UnderdogSnapshotRecord.alert_sent, so
                # has_recent_ud_alert misses them.  _prop_market_alerted IS set by all 95+
                # paths, so this check closes the gap within the current bot session.
                if _is_prop_deduped(
                    _prop_market_alerted, _sp, _ssport, _st, _line_val,
                    dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
                    min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
                ):
                    logger.debug(
                        "standing dedup [in-memory]: %s | %s | %s — skipped (already alerted this session)",
                        _sp, _ssport, _st,
                    )
                    continue

                _shist = await db.get_ud_prop_history(_sp, _st, limit=30)
                _sscore = score_ud_prop(
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
                )
                if not _sval.has_supporting_data:
                    logger.debug(
                        "UD standing_gate [no_data]: %s | %s | %s — validation has no supporting data (n=%d)",
                        _sp, _st, _ssport, getattr(_sval, "n_games", 0),
                    )
                    continue
    
                _shits = await _fetch_and_compute_hit_rates(
                    db, _sp, _ssport, _st, _line_val
                )
                _sdec = make_ud_bet_decision(
                    score        = _sscore,
                    validation   = _sval,
                    current_line = _line_val,
                    prev_line    = None,
                    hit_rates    = _shits,
                )
                # Log every evaluated opportunity (PLAY or PASS) for tracking
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
                        provider          = "Underdog",
                        bet_quality_score = _sdec.confidence,
                        reason_codes      = _compute_reason_codes(_sscore, _sdec),
                        watchlist_state   = (
                            "Qualified" if _sdec.recommendation != "PASS" else "Rejected"
                        ),
                    )
                except Exception as _pol_exc:
                    logger.warning(
                        "underdog_job: log_prop_opportunity [standing] failed: %s", _pol_exc
                    )

                if _sdec is None or _sdec.recommendation == "PASS":
                    logger.debug(
                        "UD standing_gate [decision_pass]: %s | %s | %s — decision=%s reason=%s",
                        _sp, _st, _ssport,
                        (_sdec.recommendation if _sdec is not None else "None"),
                        (_sdec.reason if _sdec is not None else "no_decision"),
                    )
                    continue

                # Per-tier confidence gate — sport-conditional for standing plays (#2)
                _s_min_conf = config.min_conf_for_sport_tier(_ssport, _sdec.decision_tier)
                if _sdec.confidence < _s_min_conf:
                    logger.debug(
                        "UD conf_gate [standing]: %s | %s | conf=%d < min=%d (tier=%s sport=%s)",
                        _sp, _st, _sdec.confidence, _s_min_conf, _sdec.decision_tier, _ssport,
                    )
                    continue

                # Strict-sport tier gate for standing plays — MLB and NFL are S-tier only.
                if (_ssport.upper() in config.ud_strict_alert_sports
                        and _sdec.decision_tier not in config.ud_mlb_alert_tiers):
                    logger.debug(
                        "UD sport_tier_gate [standing]: %s | %s | sport=%s tier=%s blocked (min=%s)",
                        _sp, _st, _ssport.upper(), _sdec.decision_tier, config.UD_MLB_MIN_TIER,
                    )
                    continue

                # Direction gate for Tier 2:
                #   NFL UNDER → fully allowed (no market restriction)
                #   MLB UNDER → restricted to whitelist markets only
                if (_ssport.upper() == "MLB"
                        and _sdec.recommendation == "UNDER"
                        and not config.is_mlb_under_allowed(_st)):
                    logger.debug(
                        "UD mlb_under_gate [standing]: %s | %s | market not in MLB UNDER whitelist",
                        _sp, _st,
                    )
                    continue
                # NFL UNDER: no gate — fully allowed per spec

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
                # OddsAPI market confirmation — non-blocking, S/A tier only.
                # Strong UNDER candidates (direction=UNDER) are included because
                # they must pass S/A-tier qualification to reach this path.
                # B/PASS candidates are explicitly excluded.
                _s_odds_confirm: Optional[dict] = None
                if (_sdec.decision_tier in ("S", "A")
                        and _sdec.recommendation != "PASS"):
                    try:
                        _s_odds_confirm = await _get_odds_api_confirmation(
                            _ssport,
                            _sp,
                            _st,
                            _sdec.recommendation,
                            _line_val,
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
                if not _tier_delivery_gate(_ssport, _s_direction, _s_bq_score, _s_mq_score):
                    logger.debug(
                        "UD tier_gate [standing]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                        _sp, _st, _ssport, _s_bq_score, _s_mq_score, _s_direction,
                    )
                    continue

                # Collect into delivery queue for ranked delivery below.
                _s_ext_id_q = getattr(_ssnap, "external_id", None) or getattr(_ssnap, "id", None) or ""
                _delivery_queue.append({
                    "path":               "standing",
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
                    "is_reentry":         False,
                    "ext_id":             _s_ext_id_q,
                    "record":             None,   # standing path has no new snapshot record
                    "sport":              _ssport,
                    "tier":               _sdec.decision_tier if _sdec else "B",
                    "conf":               float(_sdec.confidence if _sdec else 0),
                    "bq":                 float(_sscore.total if _sscore else 0),
                    "mq":                 float(getattr(_smq, "score", 0) if _smq else 0),
                    "is_meaningful_change": False,  # standing = stable line
                    "is_tier1":           _ssport in config.ud_tier1_sports,
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
                _dq_record    = _dq.get("record")
                _dq_ext_id    = _dq["ext_id"]
                _dq_sport     = _dq["sport"]
                _dq_line_val  = _dq["line_val"]
                _dq_prev      = _dq.get("prev_line")
                _dq_is_reent  = _dq.get("is_reentry", False)
                _dq_is_new    = (_dq_path == "new" or _dq_is_reent)
                _dq_is_t1     = not _is_tier2_sport(_dq_sport)

                # ── Tier delivery gate backstop ───────────────────────────────
                # Belt-and-suspenders: any candidate that slipped through the
                # collection-point gate is caught here before Telegram.
                # Tier 1: direction only.  Tier 2: BQ ≥ 75 AND MQ ≥ 75.
                _dq_mq_score  = float(getattr(_dq_mq, "score", 0) if _dq_mq else _dq.get("mq", 0))
                _dq_bq_score  = float(_dq.get("bq", 0))
                _dq_direction = _dq_dec.recommendation if _dq_dec else ""
                if not _tier_delivery_gate(_dq_sport, _dq_direction, _dq_bq_score, _dq_mq_score):
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
                ):
                    _n_dq_deferred += 1
                    _dq_deferred_log.append(
                        f"{_dq_player}/{_dq_st}"
                        f" [sport={_dq_sport}]: concurrent path already claimed delivery slot"
                    )
                    continue

                _dq_delivery  = AlertDelivery(db, bot, chat_ids)
                _dq_result    = await _dq_delivery.deliver_underdog(
                    player_name         = _dq_player,
                    team                = _dq_snap.team or "",
                    sport               = _dq_snap.sport,
                    stat_type           = _dq_st,
                    old_line            = _dq_prev or _dq_line_val,
                    new_line            = _dq_line_val,
                    game_time           = _dq_snap.game_time,
                    score               = _dq_score,
                    new_prop            = _dq_is_new,
                    validation          = _dq_val,
                    decision            = _dq_dec,
                    market_quality      = _dq_mq,
                    market_pressure     = _dq_mp,
                    intelligence_trace  = _dq_intel,
                    market_confirmation = _dq_odds,
                    standing            = (_dq_path == "standing"),
                )

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
                        await db.mark_opportunity_alert_sent(_dq_ext_id, _dq_st)
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
                        " | Line: %.1f | Direction: %s | Tier: %s | Confidence: %d"
                        " | Quality: %s | Telegram: SENT | Path: %s | T1: %s",
                        _dq_player, _dq_sport, _dq_st, _dq_line_val,
                        _dq_dec.recommendation if _dq_dec else "UNKNOWN",
                        _dq_score.tier if _dq_score else "UNKNOWN",
                        _dq_score.total if _dq_score else 0,
                        getattr(_dq_score, "bet_quality_label", "—") if _dq_score else "—",
                        _dq_path,
                        "yes" if _dq_is_t1 else "no",
                    )
                else:
                    _n_dq_deferred += 1
                    _dq_deferred_log.append(
                        f"{_dq_player}/{_dq_st} [tier={_dq.get('tier','?')}"
                        f" bq={_dq.get('bq',0):.0f} path={_dq_path}]:"
                        f" {_dq_result.reason}"
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

        # ── Bulk-save incremental snapshots ───────────────────────────────────────
        # Runs AFTER the ranked delivery phase so that alert_sent=True is persisted
        # for props that were just delivered.  The incremental records have their
        # alert_sent / alert_outcome fields updated in-place by the delivery phase.
        if _incremental_records and db:
            # Pass a copy so that the .clear() below does not retroactively
            # empty the list object that was handed to the mock in tests.
            _incr_snapshot = list(_incremental_records)
            _incremental_records.clear()
            try:
                await db.save_underdog_snapshots_bulk(_incr_snapshot)
            except Exception as _bulk_exc:
                logger.warning(
                    "underdog_job: bulk snapshot save failed (%d records): %s",
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
                    await db.save_underdog_snapshots_bulk(_cs_batch)
                    del _cs_batch  # release this batch immediately
                _cold_start_records.clear()  # release full list
                logger.info(
                    "underdog_job: cold-start bulk save — %d records written (%d chunks)",
                    _cs_total, (_cs_total + _CS_CHUNK - 1) // _CS_CHUNK,
                )
            _cold_start_done = True
            logger.info(
                    "underdog_job: cold-start complete — scored %d props\n"
                    "  before (old cal):  S=%d  A=%d  B=%d  PASS=%d\n"
                    "   after (new cal):  S=%d  A=%d  B=%d  PASS=%d",
                    _n_cold_start_scored,
                    _cs_before_tiers.get("S",    0),
                    _cs_before_tiers.get("A",    0),
                    _cs_before_tiers.get("B",    0),
                    _cs_before_tiers.get("PASS", 0),
                    _cs_after_tiers.get("S",    0),
                    _cs_after_tiers.get("A",    0),
                    _cs_after_tiers.get("B",    0),
                    _cs_after_tiers.get("PASS", 0),
                )
    
        # ── Persist scan checkpoint ────────────────────────────────────────────────
        # Written after every successful scan for health monitoring.
        # Non-fatal if health tracker is missing.
        try:
            if _health:
                _health.record_scan_checkpoint()
        except Exception as _ckpt_exc:
            logger.debug("underdog_job: checkpoint record skipped — %s", _ckpt_exc)

        logger.info(
            "underdog_job: fetched=%d scored=%d cold_start=%d S=%d A=%d B=%d PASS=%d "
            "qualified=%d removed=%d new=%d new_sent=%d",
            len(ud_snaps),
            _n_scored,
            _n_cold_start_scored,
            _tier_counts.get("S",    0),
            _tier_counts.get("A",    0),
            _tier_counts.get("B",    0),
            _tier_counts.get("PASS", 0),
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
            from collections import Counter
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
                # Header line: rank, player, stat, sport, total, tier, stars, path, rejection
                _dbg_lines.append(
                    f"    {_i:2d}. {_p['player'][:24]:<24} | {_p['stat_type']:<22}"
                    f" | {_p['sport']:<5} | {_p['total']:3d}/100 {_p['tier']}"
                    f" {_p['stars_d']} [{_p['path']}] → {_p['rejection']}"
                )
                # Extra line for decision_pass: show the engine's own reason string
                if _p["rejection"] == "decision_pass" and _p.get("decision_reason"):
                    _dbg_lines.append(
                        f"        ↳ engine: {_p['decision_reason']}"
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
            _rej_counts = Counter(_p["rejection"] for _p in _scored_props)
            _rej_str = "  ".join(
                f"{_k}={_v}"
                for _k, _v in sorted(_rej_counts.items(), key=lambda x: -x[1])
            )
            _dbg_lines.append(f"  rejection breakdown:  {_rej_str}")
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
                    _ctier = _cp.get("tier", "PASS")
                    _crej  = _cp.get("rejection")
                    _csport = (_cp.get("sport") or "").upper()
                    _is_strict_sport = _csport in {"MLB", "NFL"}
                    _accepted_rejections = (
                        "qualified", "sent", "filtered", "new_prop_failed", "cold_start"
                    )
                    if _ctier == "PASS":
                        _cgd = "REJECTED"
                    elif _ctier == "B":
                        # Tier 1 (non-MLB/NFL): B-tier props are ACCEPTED when they
                        # have a legitimate qualifying reason — mirrors the alert engine
                        # which allows S/A/B/C for non-strict sports.
                        # Tier 2 (MLB/NFL): B-tier is WATCHLIST (strict sports never
                        # alert at B-tier so they should not appear as ACCEPTED).
                        if not _is_strict_sport and _crej in _accepted_rejections:
                            _cgd = "ACCEPTED"
                        else:
                            _cgd = "WATCHLIST"
                    elif _crej in _accepted_rejections and _ctier in ("S", "A"):
                        # "qualified"       — is_qualified=True (S/A tier, passed scoring gate, eligible for alert)
                        # "sent"            — new-prop path: alert delivered to Telegram
                        # "filtered"        — new-prop path: reached delivery, filtered by dedup/reversal
                        # "new_prop_failed" — new-prop path: passed all gates, delivery failed
                        # "cold_start"      — S/A tier prop scored during init; standing path may deliver it
                        _cgd = "ACCEPTED"
                    else:
                        _cgd = "REJECTED"
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
                        "score_tier":           _ctier,
                        "confidence":           _cp.get("decision_conf"),
                        "gate_decision":        _cgd,
                        "rejection_reason":     _crej,
                        "reason_codes":         _json.dumps(_ccodes) if _ccodes else None,
                        "snapshot_external_id": _cp.get("external_id"),
                    })
                    # Flush chunk to DB and release memory
                    if len(_cand_rows) >= _CAND_CHUNK:
                        await db.log_prop_candidate_batch(_cand_rows)
                        _cand_total += len(_cand_rows)
                        _cand_rows = []
                # Flush remaining rows
                if _cand_rows:
                    await db.log_prop_candidate_batch(_cand_rows)
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
                await db.update_ud_props_mq_scores_bulk(_plh_mq_scores)
            except Exception as _mq_bulk_exc:
                logger.debug("underdog_job: mq_scores bulk write failed: %s", _mq_bulk_exc)

        # ── Scan cycle log — full pipeline evidence ──────────────────────────────────
        # Non-fatal: a write failure must not abort the cycle or mask real exceptions.
        # Proves that all ~4,600 active props are monitored each poll (not just scored ones).
        try:
            if db:
                await db.log_scan_cycle(
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
                )
        except Exception as _scl_exc:
            logger.debug("underdog_job: scan_cycle_log write skipped: %s", _scl_exc)

    except Exception as _job_exc:
        logger.exception("underdog_job: processing error: %s", _job_exc)
        if _health:
            _health.record_job_fail("underdog_job", str(_job_exc))
            _health.record_pipeline_fail(
                stage  = "prop_scoring",
                module = "market_engine.underdog_job",
                error  = str(_job_exc),
            )
        return

    # ── Bridge to PropLineHistory (lifecycle tracking) ─────────────────────────
    # Must run AFTER the main try block so snapshots exist in UnderdogSnapshotRecord.
    # A bridge failure is surfaced as a job failure so /health can alert on it.
    _persistence_ok = True
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

    # Clear the full-scan concurrency guard so future scheduled instances run full scans.
    _ud_full_scan_running = False

    # Emit a flood-protection summary if any alerts were rate-limited this cycle.
    try:
        from engine.telegram_rate_limiter import get_rate_limiter as _get_rl_end
        _get_rl_end().log_cycle_summary("underdog_job")
    except Exception:
        pass

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
                "persistence stage failed (bridge or lifecycle update)",
            )
            _health.record_pipeline_fail(
                stage  = "lifecycle_bridge",
                module = "market_engine",
                error  = "bridge or lifecycle update failed",
            )


# ── Stable Refresh Job ─────────────────────────────────────────────────────────
# Runs every 2 minutes — separate from underdog_job — using only data already
# stored in the local DB (zero additional Underdog API calls).
#
# PART 1 — Rolling stable-pool rescan:
#   • Loads all active props from DB via get_latest_underdog_snapshot_per_prop().
#   • Sorts them by (player, stat_type) for a stable, reproducible cursor.
#   • Takes the next ≤10,000 props from the cursor position.
#   • Rescores each using the existing engine + all existing alert gates.
#   • Sends qualifying Telegram alerts through AlertDelivery.deliver_underdog().
#   • Applies the Watchlist UNDER rule: score 30–40 with UNDER recommendation
#     → watchlist_state='Watchlist' instead of silent rejection.
#   • Advances the cursor; wraps to 0 at end of pool.
#
# PART 2 — Watchlist rescan:
#   • Queries PropOpportunityLog rows where watchlist_state='Watchlist'.
#   • Rescores each.  Promotion → alert; score decline → Rejected; prop gone → Removed.
#
# Both parts use the existing dedup, direction, BQ, conf, and live-game gates.
# The fast path (underdog_job) is completely unaffected.

_STABLE_REFRESH_BATCH_SIZE:   int = 10_000
_STABLE_WATCHLIST_BATCH_SIZE: int = 200    # max watchlist candidates rescanned per cycle

# Per-stable-refresh-cycle dedup — prevents sending 95+ priority alert more
# than once per cycle for the same prop.  Reset each time _stable_refresh_job
# is called (local variable within the function).


async def _stable_refresh_job(context: "ContextTypes.DEFAULT_TYPE") -> None:
    """
    Rolling stable-prop rescore + watchlist rescan.  Runs every 2 minutes.

    See module-level docstring above for the full design.
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

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()
    _health  = get_health_tracker()

    if _health:
        _health.record_job_started("stable_refresh_job")

    # Stats for console/health
    pool_size    = 0
    cursor       = 0
    next_cursor  = 0
    end_cursor   = 0
    batch_keys: list = []
    sr_rescored  = 0
    sr_qualified = 0
    sr_watchlist = 0
    sr_rejected  = 0
    sr_sent      = 0
    wl_active    = 0
    wl_rescored  = 0
    wl_improved  = 0
    wl_unchanged = 0
    wl_declined  = 0
    wl_promoted  = 0
    wl_removed   = 0

    # ────────────────────────────────────────────────────────────────────────────
    # PART 1 — Rolling stable-pool rescan
    # ────────────────────────────────────────────────────────────────────────────
    try:
        # Fetch full active pool from DB — zero Underdog API calls.
        # get_active_underdog_snapshot_per_prop() takes MAX(id) over ALL rows
        # (including removals) then keeps only non-removed latest rows.
        # This ensures props whose most-recent feed record is a removal are
        # absent from the pool, preventing false rescores and alerts.
        active_pool: dict = await db.get_active_underdog_snapshot_per_prop()
        pool_keys: list   = sorted(active_pool.keys())   # stable sort → consistent cursor
        pool_size         = len(pool_keys)

        # Load cursor; wrap to current pool size so a shrinking pool never OOBs
        cursor = _health.get_stable_refresh_cursor() if _health else 0
        cursor = (cursor % pool_size) if pool_size > 0 else 0

        # Slice batch — wraps at end of pool
        end_cursor  = min(cursor + _STABLE_REFRESH_BATCH_SIZE, pool_size)
        batch_keys  = pool_keys[cursor:end_cursor]
        next_cursor = end_cursor % pool_size if pool_size > 0 else 0

        # Build line-map from pool for the freshness guard.  Uses line_value —
        # the canonical ORM field on UnderdogSnapshotRecord.  No divergence is
        # possible here since we score from the same snapshot we just fetched.
        _sr_line_map: dict = {
            (pn, st): (snap.line_value or 0.0)
            for (pn, st), snap in active_pool.items()
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

        for (_sr_player, _sr_stat) in batch_keys:
            _sr_snap = active_pool.get((_sr_player, _sr_stat))
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
            ):
                sr_rejected += 1
                continue

            # ── DB dedup — bulk pre-loaded; no per-prop query ────────────────
            # _sr_bulk_dedup_ok==True means the single query succeeded (even if
            # it returned frozenset() — that just means nothing was alerted
            # recently).  Only when the query raised do we fall back per-prop.
            if _sr_bulk_dedup_ok:
                if (_sr_player, _sr_stat) in _sr_db_alerted:
                    sr_rejected += 1
                    continue
            else:
                # Bulk load raised — per-prop fallback for this cycle only
                if await db.has_recent_ud_alert(_sr_player, _sr_stat, within_seconds=86400):
                    sr_rejected += 1
                    continue

            # ── Score + validate ──────────────────────────────────────────────
            _sr_hist  = await db.get_ud_prop_history(_sr_player, _sr_stat, limit=30)
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
            )

            # ── Watchlist UNDER rule: score 30–40 + UNDER recommendation ─────
            # Low-scoring UNDER candidates are saved as watchlist rather than
            # silently dropped so they can be promoted if they improve later.
            if 30 <= _sr_score.total <= 40:
                try:
                    _sr_hits_wl = await _fetch_and_compute_hit_rates(
                        db, _sr_player, _sr_sport, _sr_stat, _sr_line
                    )
                    _sr_dec_wl = _sr_decide(
                        score        = _sr_score,
                        validation   = _sr_val,
                        current_line = _sr_line,
                        prev_line    = None,
                        hit_rates    = _sr_hits_wl,
                    )
                    if _sr_dec_wl is not None and _sr_dec_wl.recommendation == "UNDER":
                        _sr_ext_id_wl = (
                            getattr(_sr_snap, "external_id", None)
                            or str(getattr(_sr_snap, "id", None) or "")
                        )
                        try:
                            await db.log_prop_opportunity(
                                external_id       = _sr_ext_id_wl,
                                player_name       = _sr_player,
                                team              = _sr_snap.team or "",
                                sport             = _sr_sport,
                                stat_type         = _sr_stat,
                                line_value        = _sr_line,
                                recommendation    = _sr_dec_wl.recommendation,
                                decision_tier     = _sr_dec_wl.decision_tier,
                                confidence        = _sr_dec_wl.confidence,
                                game_time         = _sr_snap.game_time,
                                provider          = "Underdog",
                                bet_quality_score = _sr_dec_wl.confidence,
                                reason_codes      = _compute_reason_codes(_sr_score, _sr_dec_wl),
                                watchlist_state   = "Watchlist",
                            )
                        except Exception:
                            pass
                        sr_watchlist += 1
                        continue
                except Exception:
                    pass
                sr_rejected += 1
                continue

            # ── Normal quality gates ──────────────────────────────────────────
            if not _sr_val.has_supporting_data:
                sr_rejected += 1
                continue

            _sr_hits = await _fetch_and_compute_hit_rates(
                db, _sr_player, _sr_sport, _sr_stat, _sr_line
            )
            _sr_dec = _sr_decide(
                score        = _sr_score,
                validation   = _sr_val,
                current_line = _sr_line,
                prev_line    = None,
                hit_rates    = _sr_hits,
            )

            # Log opportunity for tracking (non-fatal)
            _sr_ext_id = (
                getattr(_sr_snap, "external_id", None)
                or str(getattr(_sr_snap, "id", None) or "")
            )
            try:
                await db.log_prop_opportunity(
                    external_id       = _sr_ext_id,
                    player_name       = _sr_player,
                    team              = _sr_snap.team or "",
                    sport             = _sr_sport,
                    stat_type         = _sr_stat,
                    line_value        = _sr_line,
                    recommendation    = (_sr_dec.recommendation if _sr_dec else "PASS"),
                    decision_tier     = (_sr_dec.decision_tier  if _sr_dec else "PASS"),
                    confidence        = (_sr_dec.confidence     if _sr_dec else 0),
                    game_time         = _sr_snap.game_time,
                    provider          = "Underdog",
                    bet_quality_score = (_sr_dec.confidence if _sr_dec else 0),
                    reason_codes      = _compute_reason_codes(_sr_score, _sr_dec),
                    watchlist_state   = (
                        "Qualified"
                        if (_sr_dec is not None and _sr_dec.recommendation != "PASS")
                        else "Rejected"
                    ),
                )
            except Exception as _sr_pol_exc:
                logger.debug("stable_refresh: log_prop_opportunity failed: %s", _sr_pol_exc)

            # ── Normal gate sequence ──────────────────────────────────────────
            if _sr_dec is None or _sr_dec.recommendation == "PASS":
                sr_rejected += 1
                continue

            _sr_min_conf = config.min_conf_for_sport_tier(_sr_sport, _sr_dec.decision_tier)
            if _sr_dec.confidence < _sr_min_conf:
                sr_rejected += 1
                continue

            if (
                _sr_sport.upper() in config.ud_strict_alert_sports
                and _sr_dec.decision_tier not in config.ud_mlb_alert_tiers
            ):
                sr_rejected += 1
                continue

            if (
                _sr_sport.upper() == "MLB"
                and _sr_dec.recommendation == "UNDER"
                and not config.is_mlb_under_allowed(_sr_stat)
            ):
                sr_rejected += 1
                continue

            if _is_game_live_or_past(_sr_snap, now):
                sr_rejected += 1
                continue

            if not _ud_line_fresh(_sr_line, _sr_player, _sr_stat, _sr_line_map):
                sr_rejected += 1
                continue

            # ── All gates passed — deliver ────────────────────────────────────
            sr_qualified += 1
            _sr_mq = _sr_cmq(_sr_stat, _sr_line, _sr_score)
            _sr_mp = _sr_dmp(None, _sr_hist)

            # ── Tier delivery gate [stable-refresh] ──────────────────────────
            _sr_mq_score = float(getattr(_sr_mq, "score", 0) if _sr_mq else 0)
            _sr_bq_score = float(_sr_score.total if _sr_score else 0)
            if not _tier_delivery_gate(_sr_sport, _sr_dec.recommendation, _sr_bq_score, _sr_mq_score):
                sr_rejected  += 1
                sr_qualified -= 1
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
            if _sr_dec.decision_tier in ("S", "A") and _sr_dec.recommendation != "PASS":
                try:
                    _sr_odds_confirm = await _get_odds_api_confirmation(
                        _sr_sport, _sr_player, _sr_stat, _sr_dec.recommendation, _sr_line,
                    )
                except Exception:
                    pass

            # ── Atomic dedup claim [stable-refresh] ──────────────────────────────
            # Claim before delivery so the delivery-queue path cannot also send.
            if not await _try_claim_delivery_slot(_sr_player, _sr_sport, _sr_stat, _sr_line):
                sr_rejected  += 1
                sr_qualified -= 1
                logger.debug(
                    "UD dedup_claim [sr]: %s | %s | concurrent path already claimed",
                    _sr_player, _sr_stat,
                )
                continue

            _sr_delivery = AlertDelivery(db, bot, chat_ids)
            _sr_result   = await _sr_delivery.deliver_underdog(
                player_name         = _sr_player,
                team                = _sr_snap.team or "",
                sport               = _sr_sport,
                stat_type           = _sr_stat,
                old_line            = _sr_line,
                new_line            = _sr_line,
                game_time           = _sr_snap.game_time,
                score               = _sr_score,
                validation          = _sr_val,
                decision            = _sr_dec,
                market_quality      = _sr_mq,
                market_pressure     = _sr_mp,
                standing            = True,
                intelligence_trace  = _sr_intel_trace,
                market_confirmation = _sr_odds_confirm,
            )
            if _sr_result.sent:
                sr_sent += 1
                # _record_prop_alerted already called by _try_claim_delivery_slot.
                try:
                    await db.mark_ud_snapshot_alert_sent(_sr_player, _sr_stat)
                    await db.mark_opportunity_alert_sent(_sr_ext_id, _sr_stat)
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
                sr_rejected  += 1
                sr_qualified -= 1

        # Advance cursor — persisted to health.json so it survives restarts
        if _health:
            _health.set_stable_refresh_cursor(next_cursor)

    except Exception as _sr_exc:
        logger.exception("stable_refresh_job [part1]: error: %s", _sr_exc)
        # Reset counters so the console log shows zeros rather than partial data
        sr_rescored = sr_qualified = sr_watchlist = sr_rejected = sr_sent = 0
        if _health:
            _health.record_job_fail("stable_refresh_job", str(_sr_exc))

    # ────────────────────────────────────────────────────────────────────────────
    # PART 2 — Watchlist rescan
    # ────────────────────────────────────────────────────────────────────────────
    try:
        _wl_all       = await db.get_active_watchlist_candidates()  # FIFO order (id ASC)
        wl_active     = len(_wl_all)

        # Rotating cursor — ensures every watchlist entry is eventually rescanned
        # even when the watchlist exceeds the per-cycle batch cap.
        # FIFO ordering (id ASC in get_active_watchlist_candidates) guarantees
        # that advancing the cursor each cycle gives every entry a fair turn.
        _wl_cursor     = _health.get_wl_refresh_cursor() if _health else 0
        _wl_cursor     = (_wl_cursor % wl_active) if wl_active > 0 else 0
        _wl_end        = min(_wl_cursor + _STABLE_WATCHLIST_BATCH_SIZE, wl_active)
        wl_candidates  = _wl_all[_wl_cursor:_wl_end]
        _wl_next_cursor = _wl_end % wl_active if wl_active > 0 else 0

        # If part 1 failed (active_pool wasn't set), re-fetch using the same
        # correct method: MAX(id) over ALL rows then filter removed=False.
        # Never use get_latest_underdog_snapshot_per_prop here — that method's
        # MAX-over-non-removed semantics can return a stale active snapshot for
        # a prop whose latest feed record is a removal, causing false promotions.
        try:
            _wl_pool = active_pool  # type: ignore[used-before-def]
        except NameError:
            _wl_pool = await db.get_active_underdog_snapshot_per_prop()
        try:
            _wl_line_map = _sr_line_map  # type: ignore[used-before-def]
        except NameError:
            _wl_line_map = {
                (pn, st): (s.line_value or 0.0) for (pn, st), s in _wl_pool.items()
            }

        from engine.ud_scoring import score_ud_prop as _wl_score_fn  # alias
        from engine.player_validator import validate_player_prop as _wl_validate
        from engine.ud_bet_decision import make_ud_bet_decision as _wl_decide

        for _wl_row in wl_candidates:
            _wl_player = _wl_row.player_name  or ""
            _wl_stat   = _wl_row.stat_type    or ""
            _wl_sport  = _wl_row.sport        or "UNKNOWN"
            _wl_line   = float(_wl_row.line_value or 0.0)
            _wl_ext_id = _wl_row.external_id  or ""
            _wl_conf   = int(_wl_row.confidence or 0)

            # Check if prop is still in the active pool
            _wl_snap = _wl_pool.get((_wl_player, _wl_stat))
            if _wl_snap is None:
                # Prop removed from Underdog → mark Removed
                try:
                    await db.log_prop_opportunity(
                        external_id       = _wl_ext_id,
                        player_name       = _wl_player,
                        team              = getattr(_wl_row, "team", "") or "",
                        sport             = _wl_sport,
                        stat_type         = _wl_stat,
                        line_value        = _wl_line,
                        recommendation    = getattr(_wl_row, "recommendation", "PASS"),
                        decision_tier     = getattr(_wl_row, "decision_tier",  "PASS"),
                        confidence        = _wl_conf,
                        game_time         = getattr(_wl_row, "game_time", None),
                        provider          = "Underdog",
                        bet_quality_score = _wl_conf,
                        watchlist_state   = "Removed",
                    )
                except Exception:
                    pass
                wl_removed  += 1
                wl_rescored += 1
                continue

            wl_rescored     += 1
            # line_value is the canonical ORM field on UnderdogSnapshotRecord
            _wl_cur_line     = _wl_snap.line_value or 0.0

            try:
                _wl_hist  = await db.get_ud_prop_history(_wl_player, _wl_stat, limit=30)
                _wl_score = _wl_score_fn(
                    player_name  = _wl_player,
                    stat_type    = _wl_stat,
                    sport        = _wl_sport,
                    current_line = _wl_cur_line,
                    prev_line    = None,
                    history      = _wl_hist,
                )
                _wl_cur_total = float(_wl_score.total)

                # Track improvement vs previous confidence
                if _wl_cur_total > _wl_conf + 3:
                    wl_improved  += 1
                elif _wl_cur_total < _wl_conf - 3:
                    wl_declined  += 1
                else:
                    wl_unchanged += 1

                _wl_val = _wl_validate(
                    player_name  = _wl_player,
                    stat_type    = _wl_stat,
                    current_line = _wl_cur_line,
                    history      = _wl_hist,
                    min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                )
                _wl_hits = await _fetch_and_compute_hit_rates(
                    db, _wl_player, _wl_sport, _wl_stat, _wl_cur_line,
                )
                _wl_dec = _wl_decide(
                    score        = _wl_score,
                    validation   = _wl_val,
                    current_line = _wl_cur_line,
                    prev_line    = None,
                    hit_rates    = _wl_hits,
                )

                # Check if the candidate now passes the full alert gate
                _wl_qualifies = (
                    _wl_dec is not None
                    and _wl_dec.recommendation != "PASS"
                    and _wl_val.has_supporting_data
                    and _wl_score.stars >= config.min_stars_for_sport(_wl_sport)
                    and _wl_dec.confidence >= config.min_conf_for_sport_tier(
                        _wl_sport, _wl_dec.decision_tier
                    )
                    and not _is_game_live_or_past(_wl_snap, now)
                    and not _is_prop_deduped(
                        _prop_market_alerted, _wl_player, _wl_sport, _wl_stat, _wl_cur_line,
                        dedup_window_seconds = config.UD_ALERT_DEDUP_WINDOW,
                        min_line_change      = config.MIN_UNDERDOG_LINE_CHANGE,
                    )
                    and not await db.has_recent_ud_alert(
                        _wl_player, _wl_stat, within_seconds=86400,
                    )
                )
                if _wl_qualifies and _wl_sport.upper() in config.ud_strict_alert_sports:
                    if _wl_dec.decision_tier not in config.ud_mlb_alert_tiers:
                        _wl_qualifies = False
                if _wl_qualifies and _wl_sport.upper() == "MLB":
                    if (
                        _wl_dec.recommendation == "UNDER"
                        and not config.is_mlb_under_allowed(_wl_stat)
                    ):
                        _wl_qualifies = False

                if _wl_qualifies and chat_ids:
                    # Promote — deliver alert through normal path
                    _wl_mq       = _sr_cmq(_wl_stat, _wl_cur_line, _wl_score)
                    _wl_mp       = _sr_dmp(None, _wl_hist)
                    # ── Tier delivery gate [watchlist] ───────────────────────
                    _wl_mq_score = float(getattr(_wl_mq, "score", 0) if _wl_mq else 0)
                    _wl_bq_score = float(_wl_score.total if _wl_score else 0)
                    _wl_mq_ok    = _tier_delivery_gate(
                        _wl_sport, _wl_dec.recommendation, _wl_bq_score, _wl_mq_score,
                    )
                    if not _wl_mq_ok:
                        logger.debug(
                            "UD tier_gate [watchlist]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                            _wl_player, _wl_stat, _wl_sport, _wl_bq_score, _wl_mq_score, _wl_dec.recommendation,
                        )
                    if _wl_mq_ok:
                     # ── Atomic dedup claim [watchlist] ────────────────────────
                     if not await _try_claim_delivery_slot(
                         _wl_player, _wl_sport, _wl_stat, _wl_cur_line,
                     ):
                         logger.debug(
                             "UD dedup_claim [wl]: %s | %s | concurrent path already claimed",
                             _wl_player, _wl_stat,
                         )
                         _wl_mq_ok = False
                    if _wl_mq_ok:
                     _wl_delivery = AlertDelivery(db, bot, chat_ids)
                     _wl_result   = await _wl_delivery.deliver_underdog(
                        player_name    = _wl_player,
                        team           = _wl_snap.team or "",
                        sport          = _wl_sport,
                        stat_type      = _wl_stat,
                        old_line       = _wl_cur_line,
                        new_line       = _wl_cur_line,
                        game_time      = _wl_snap.game_time,
                        score          = _wl_score,
                        validation     = _wl_val,
                        decision       = _wl_dec,
                        market_quality = _wl_mq,
                        market_pressure= _wl_mp,
                        standing       = True,
                    )
                    if _wl_mq_ok and _wl_result.sent:
                        wl_promoted += 1
                        # _record_prop_alerted already called by _try_claim_delivery_slot.
                        try:
                            await db.log_prop_opportunity(
                                external_id       = _wl_ext_id,
                                player_name       = _wl_player,
                                team              = _wl_snap.team or "",
                                sport             = _wl_sport,
                                stat_type         = _wl_stat,
                                line_value        = _wl_cur_line,
                                recommendation    = _wl_dec.recommendation,
                                decision_tier     = _wl_dec.decision_tier,
                                confidence        = _wl_dec.confidence,
                                game_time         = _wl_snap.game_time,
                                provider          = "Underdog",
                                bet_quality_score = _wl_dec.confidence,
                                watchlist_state   = "Qualified",
                            )
                            await db.mark_ud_snapshot_alert_sent(_wl_player, _wl_stat)
                            await db.mark_opportunity_alert_sent(_wl_ext_id, _wl_stat)
                        except Exception:
                            pass
                        logger.info(
                            "🔥 WATCHLIST PROMOTED | %s | %s | %s"
                            " | Line: %.1f | Tier: %s | Score: %d/100",
                            _wl_player, _wl_sport, _wl_stat,
                            _wl_cur_line, _wl_score.tier, int(_wl_score.total),
                        )

                elif _wl_cur_total < 30:
                    # Score fell below the watchlist floor → demote to Rejected
                    try:
                        await db.log_prop_opportunity(
                            external_id       = _wl_ext_id,
                            player_name       = _wl_player,
                            team              = getattr(_wl_row, "team", "") or "",
                            sport             = _wl_sport,
                            stat_type         = _wl_stat,
                            line_value        = _wl_cur_line,
                            recommendation    = (_wl_dec.recommendation if _wl_dec else "PASS"),
                            decision_tier     = (_wl_dec.decision_tier  if _wl_dec else "PASS"),
                            confidence        = (_wl_dec.confidence     if _wl_dec else 0),
                            game_time         = _wl_snap.game_time,
                            provider          = "Underdog",
                            bet_quality_score = (_wl_dec.confidence if _wl_dec else 0),
                            watchlist_state   = "Rejected",
                        )
                    except Exception:
                        pass

            except Exception as _wl_row_exc:
                logger.debug(
                    "stable_refresh [watchlist]: %s/%s: %s",
                    _wl_player, _wl_stat, _wl_row_exc,
                )

    except Exception as _wl_all_exc:
        logger.exception(
            "stable_refresh_job [part2/watchlist]: error: %s", _wl_all_exc,
        )

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
        "✅ Qualified: %6d  (sent: %d)\n"
        "👁 Watchlist: %6d\n"
        "❌ Rejected:  %6d\n"
        "%s\n"
        "👁 Watchlist Refresh\n"
        "%s\n"
        "Active watchlist:  %6d\n"
        "Re-scored:         %6d\n"
        "⬆️  Improved:       %6d\n"
        "➡️  Unchanged:      %6d\n"
        "⬇️  Declined:       %6d\n"
        "🔥 Promoted:       %6d\n"
        "🚫 Removed:        %6d\n"
        "⏱ Next refresh:   ~2 min\n"
        "%s",
        _thick, _thick,
        len(batch_keys),
        _sr_pct,
        f"{end_cursor:,}", f"{pool_size:,}",
        sr_rescored,
        sr_qualified, sr_sent,
        sr_watchlist,
        sr_rejected,
        _thick, _thick,
        wl_active,
        wl_rescored,
        wl_improved,
        wl_unchanged,
        wl_declined,
        wl_promoted,
        wl_removed,
        _thick,
    )

    # ── Persist cursors to health sidecar ────────────────────────────────────────
    if _health:
        try:
            _health.set_wl_refresh_cursor(_wl_next_cursor)  # type: ignore[used-before-def]
        except (NameError, Exception):
            pass  # Part 2 may not have set _wl_next_cursor if it raised early

    # ── Persist stats to health sidecar ──────────────────────────────────────────
    if _health:
        _health.set_stable_refresh_stats({
            "pool_size":    pool_size,
            "batch_size":   len(batch_keys),
            "cursor_start": cursor,
            "cursor_end":   end_cursor,   # FIX: store end_cursor (actual batch end), not next_cursor (which wraps to 0)
            "sr_rescored":  sr_rescored,
            "sr_qualified": sr_qualified,
            "sr_watchlist": sr_watchlist,
            "sr_rejected":  sr_rejected,
            "sr_sent":      sr_sent,
            "wl_active":    wl_active,
            "wl_rescored":  wl_rescored,
            "wl_improved":  wl_improved,
            "wl_unchanged": wl_unchanged,
            "wl_declined":  wl_declined,
            "wl_promoted":  wl_promoted,
            "wl_removed":   wl_removed,
        })
        _health.record_job_run("stable_refresh_job")

    logger.info(
        "stable_refresh_job: pool=%d batch=%d rescored=%d qualified=%d sent=%d "
        "watchlist=%d wl_active=%d wl_promoted=%d",
        pool_size, len(batch_keys), sr_rescored, sr_qualified, sr_sent,
        sr_watchlist, wl_active, wl_promoted,
    )


# ──────────────────────────────────────────────────────────────────────────────
# FULL-POOL RESCAN ROTATION
# ──────────────────────────────────────────────────────────────────────────────
#
# Independently covers the entire active Underdog prop pool in bounded batches,
# guaranteeing every prop (stable, rejected, near-miss, watchlist, unchanged)
# is eventually rescored.  Completely separate from the stable-refresh job —
# uses its own cursor, rotation counter, and stats keys in health.json.
#
# Priority order (scheduling only — not an alert filter):
#   1. Tier 1 sports + all other non-low-priority sports (sorted alphabetically)
#   2. NFL / MLB (lowest scheduling priority — always rescanned, just last)
#
# No prop is permanently excluded.  A previous Rejected result does not
# prevent rescoring in a later rotation; the prop must pass the normal
# scoring, direction, confidence, BQ, tier, live/pre-game, and Telegram
# deduplication gates before an alert is delivered.
#
# Rotation state (persisted in health.json via HealthTracker):
#   fpr_cursor   — current position in the priority-sorted pool
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
    • Removed/inactive props are excluded by get_all_active_underdog_snapshots_by_line().
    • All existing alert gates (dedup, direction, BQ, tier, live-game) apply.
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

    # ── Mutable cycle stats ───────────────────────────────────────────────────
    fpr_pool_size       = 0
    fpr_cursor          = 0
    fpr_end_cursor      = 0
    fpr_next_cursor     = 0
    fpr_rotation        = 1
    fpr_batch_size      = 0
    fpr_rescored        = 0
    fpr_qualified       = 0
    fpr_sent            = 0
    fpr_rejected        = 0
    fpr_watchlist       = 0
    fpr_total_rescanned = 0
    rotation_complete   = False

    try:
        # ── Fetch full active pool from DB (zero Underdog API calls) ──────────
        # get_all_active_underdog_snapshots_by_line() groups by (player_name,
        # stat_type, line_value) so every alt-line variant for the same player+stat
        # is a separate entry.  This returns the complete eligible active-prop
        # universe (all ~100k+ alt-line props) rather than the ~9k unique
        # player+stat pairs returned by get_active_underdog_snapshot_per_prop().
        # Removed/inactive props are excluded (latest row per triplet, removed=False).
        active_pool: dict = await db.get_all_active_underdog_snapshots_by_line()
        fpr_pool_size = len(active_pool)
        if fpr_pool_size == 0:
            if _health:
                _health.record_job_run("full_pool_rescan_job")
            return

        # ── Priority sort: Tier 1 + other sports first, low-priority last ─────
        _low = config.fpr_low_priority_sports  # frozenset of sport codes (upper-case)

        def _fpr_sort_key(item):
            """0 = high-priority (Tier 1 + other), 1 = low-priority (NFL/MLB)."""
            (player, stat, line), snap = item
            sport = (getattr(snap, "sport", None) or "").upper()
            return (1 if sport in _low else 0, player, stat, line)

        sorted_items = sorted(active_pool.items(), key=_fpr_sort_key)
        pool_keys    = [k for k, _ in sorted_items]
        pool_snaps   = {k: v for k, v in sorted_items}

        # ── Load and clamp cursor ─────────────────────────────────────────────
        fpr_rotation = _health.get_fpr_rotation() if _health else 1
        fpr_cursor   = _health.get_fpr_cursor()   if _health else 0
        fpr_cursor   = fpr_cursor % fpr_pool_size  # safe if pool shrank

        # ── Slice batch ───────────────────────────────────────────────────────
        fpr_end_cursor  = min(fpr_cursor + config.FPR_BATCH_SIZE, fpr_pool_size)
        batch_keys      = pool_keys[fpr_cursor:fpr_end_cursor]
        fpr_batch_size  = len(batch_keys)
        fpr_next_cursor = fpr_end_cursor % fpr_pool_size  # wraps to 0 at pool end

        # Rotation completes when the batch reaches the last prop in the pool
        if fpr_end_cursor >= fpr_pool_size:
            rotation_complete = True

        # ── Bulk dedup pre-load (one DB query for the whole batch) ────────────
        _fpr_bulk_ok    = True
        _fpr_bulk_dedup: frozenset = frozenset()
        try:
            _fpr_recent = await db.get_recent_alerted_props_for_dedup(
                within_seconds=int(config.UD_ALERT_DEDUP_WINDOW),
            )
            _fpr_bulk_dedup = frozenset(
                (r.player_name, r.stat_type) for r in (_fpr_recent or [])
            )
        except Exception:
            _fpr_bulk_ok = False

        # ── Score each prop in this batch ─────────────────────────────────────
        for _fpr_key in batch_keys:
            _fpr_player, _fpr_stat, _fpr_line_val = _fpr_key
            _fpr_snap  = pool_snaps.get(_fpr_key)
            if _fpr_snap is None:
                continue

            _fpr_sport  = (getattr(_fpr_snap, "sport", None) or "UNKNOWN").upper()
            _fpr_line   = float(_fpr_snap.line_value or 0.0)

            # Bulk dedup pre-check: skip props alerted very recently
            if _fpr_bulk_ok and (_fpr_player, _fpr_stat) in _fpr_bulk_dedup:
                fpr_rescored        += 1
                fpr_total_rescanned += 1
                continue

            try:
                _fpr_hist  = await db.get_ud_prop_history(_fpr_player, _fpr_stat, limit=30)

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

                # Track props in watchlist score range
                if (
                    _fpr_score is not None
                    and 30 <= float(_fpr_score.total) <= 40
                ):
                    fpr_watchlist += 1

                _fpr_val = _fpr_validate(
                    player_name  = _fpr_player,
                    stat_type    = _fpr_stat,
                    current_line = _fpr_line,
                    history      = _fpr_hist,
                    min_samples  = config.UD_VALIDATION_MIN_SAMPLES,
                )
                _fpr_hits = await _fetch_and_compute_hit_rates(
                    db, _fpr_player, _fpr_sport, _fpr_stat, _fpr_line,
                )
                _fpr_dec = _fpr_decide(
                    score        = _fpr_score,
                    validation   = _fpr_val,
                    current_line = _fpr_line,
                    prev_line    = None,
                    hit_rates    = _fpr_hits,
                )

                # ── Full alert gate (identical to stable-refresh path) ─────────
                _fpr_qualifies = (
                    _fpr_dec is not None
                    and _fpr_dec.recommendation != "PASS"
                    and _fpr_val.has_supporting_data
                    and _fpr_dec.confidence >= config.min_conf_for_sport_tier(
                        _fpr_sport, _fpr_dec.decision_tier
                    )
                    and not _is_game_live_or_past(_fpr_snap, now)
                    and not _is_prop_deduped(
                        _prop_market_alerted,
                        _fpr_player, _fpr_sport, _fpr_stat, _fpr_line,
                        dedup_window_seconds=config.UD_ALERT_DEDUP_WINDOW,
                        min_line_change=config.MIN_UNDERDOG_LINE_CHANGE,
                    )
                    and not await db.has_recent_ud_alert(
                        _fpr_player, _fpr_stat, within_seconds=86400,
                    )
                )
                # MLB UNDER direction gate (whitelist markets only)
                if _fpr_qualifies and _fpr_sport == "MLB":
                    if (
                        _fpr_dec.recommendation == "UNDER"
                        and not config.is_mlb_under_allowed(_fpr_stat)
                    ):
                        _fpr_qualifies = False

                if _fpr_qualifies and chat_ids:
                    _fpr_mq       = _fpr_cmq(_fpr_stat, _fpr_line, _fpr_score)
                    _fpr_mp       = _fpr_dmp(None, _fpr_hist)
                    # ── Tier delivery gate [full-pool-rescan] ────────────────
                    _fpr_mq_score = float(getattr(_fpr_mq, "score", 0) if _fpr_mq else 0)
                    _fpr_bq_score = float(_fpr_score.total if _fpr_score else 0)
                    _fpr_mq_ok    = _tier_delivery_gate(
                        _fpr_sport, _fpr_dec.recommendation, _fpr_bq_score, _fpr_mq_score,
                    )
                    if not _fpr_mq_ok:
                        fpr_rejected += 1
                        logger.debug(
                            "UD tier_gate [fpr]: %s | %s | sport=%s bq=%.0f mq=%.0f dir=%s — blocked",
                            _fpr_player, _fpr_stat, _fpr_sport, _fpr_bq_score, _fpr_mq_score, _fpr_dec.recommendation,
                        )
                    if _fpr_mq_ok:
                     # ── Atomic dedup claim [full-pool-rescan] ─────────────────
                     if not await _try_claim_delivery_slot(
                         _fpr_player, _fpr_sport, _fpr_stat, _fpr_line,
                     ):
                         fpr_rejected += 1
                         logger.debug(
                             "UD dedup_claim [fpr]: %s | %s | concurrent path already claimed",
                             _fpr_player, _fpr_stat,
                         )
                         _fpr_mq_ok = False
                    if _fpr_mq_ok:
                     _fpr_delivery = AlertDelivery(db, bot, chat_ids)
                     _fpr_result   = await _fpr_delivery.deliver_underdog(
                        player_name     = _fpr_player,
                        team            = getattr(_fpr_snap, "team", "") or "",
                        sport           = _fpr_sport,
                        stat_type       = _fpr_stat,
                        old_line        = _fpr_line,
                        new_line        = _fpr_line,
                        game_time       = _fpr_snap.game_time,
                        score           = _fpr_score,
                        validation      = _fpr_val,
                        decision        = _fpr_dec,
                        market_quality  = _fpr_mq,
                        market_pressure = _fpr_mp,
                        standing        = True,
                    )
                    if _fpr_mq_ok and _fpr_result.sent:
                        fpr_sent      += 1
                        fpr_qualified += 1
                        # _record_prop_alerted already called by _try_claim_delivery_slot.
                    elif _fpr_mq_ok:
                        # deliver_underdog returned not-sent (rate-limited, etc.)
                        fpr_rejected += 1
                elif not _fpr_qualifies:
                    fpr_rejected += 1

                fpr_rescored += 1

            except Exception as _fpr_row_exc:
                logger.debug(
                    "full_pool_rescan: %s/%s: %s",
                    _fpr_player, _fpr_stat, _fpr_row_exc,
                )
                fpr_rescored        += 1
                fpr_total_rescanned += 1

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
        if _health:
            _health.record_job_fail("full_pool_rescan_job", str(_fpr_exc))
        return

    # ── Console log (human-readable — internal cursor values never shown) ─────
    _thick = "━" * 24
    _display_rotation = fpr_rotation - 1 if rotation_complete else fpr_rotation
    _pct = (
        f"{(fpr_end_cursor / fpr_pool_size * 100):.1f}%"
        if fpr_pool_size > 0 else "—"
    )
    _low_names = " / ".join(sorted(config.fpr_low_priority_sports)) or "none"
    _status_tag = "✅ COMPLETE → rotation #%d started" % fpr_rotation if rotation_complete else "🔄 in progress"
    logger.info(
        "\n%s\n🌐 Full-Pool Rescan\n%s\n"
        "Rotation:  #%d  (%s)\n"
        "Progress:  %s\n"
        "Coverage:  %s / %s active props\n"
        "Priority:  Tier 1 + Other → %s (last)\n"
        "Batch:     %6d\n"
        "🔬 Rescored:   %6d\n"
        "✅ Sent:       %6d\n"
        "❌ Rejected:   %6d\n"
        "%s",
        _thick, _thick,
        _display_rotation, _status_tag,
        _pct,
        f"{fpr_end_cursor:,}", f"{fpr_pool_size:,}",
        _low_names,
        fpr_batch_size,
        fpr_rescored,
        fpr_sent,
        fpr_rejected,
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
            "fpr_qualified":        fpr_qualified,
            "fpr_sent":             fpr_sent,
            "fpr_rejected":         fpr_rejected,
            "fpr_total_rescanned":  fpr_total_rescanned,
            "rotation_complete":    rotation_complete,
        })
        _health.record_job_run("full_pool_rescan_job")

    logger.info(
        "full_pool_rescan_job: rotation=#%d pool=%d batch=%d rescored=%d sent=%d",
        _display_rotation, fpr_pool_size, fpr_batch_size, fpr_rescored, fpr_sent,
    )
