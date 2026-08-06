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

import logging
from datetime import datetime, timedelta
from typing import Optional

from config import config
from engine.health import get_health_tracker
from engine.prop_intelligence import compute_prop_intelligence as _compute_intel
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
)

logger = logging.getLogger(__name__)

# Module-level registry — set by init_market_engine()
_registry: Optional[ConnectorRegistry] = None

# ── Player results integration ────────────────────────────────────────────────
# Singleton provider and per-day fetch dedup cache.
# Cache key: (player_name, sport, stat_type_lower, date_iso)
# The date component means stale entries are automatically bypassed next day.
_player_stats_provider = None
_player_result_fetch_cache: set = set()
# Set to True after the first complete Underdog prop scan.  The first cycle
# scores every active prop (cold-start mode); subsequent cycles use incremental
# scoring (new props and line-change events only).
_cold_start_done: bool = False

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
        return compute_hit_rates(db_results, current_line)

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

    try:
        snapshots = await _registry.fetch_pickem()
    except Exception as _fetch_exc:
        if _health:
            _health.record_provider_error("Underdog", str(_fetch_exc))
            _health.record_job_fail("underdog_job", str(_fetch_exc))
        logger.exception("underdog_job: fetch_pickem failed: %s", _fetch_exc)
        return

    if not snapshots:
        logger.debug("underdog_job: no pick'em snapshots")
        if _health:
            _health.record_provider_fetch("Underdog")
            _health.record_job_run("underdog_job")   # empty response = successful run
        return

    if _health:
        _health.record_provider_fetch("Underdog")

    ud_snaps = [s for s in snapshots if s.sportsbook == "Underdog"]

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
    _cold_start_records:  list       = []  # records buffered for bulk save at end of cold-start
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
            player     = snap.player or "Unknown"
    
            # Extract the stable stat-type category from the selection string
            stat_type = _extract_ud_stat_type(snap.selection, snap.player, snap.line)
    
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
            score:           Optional[UDPropScore] = None
            ud_result:       DeliveryResult        = DeliveryResult(sent=False)
            np_immediate:    bool                  = False   # set inside is_new_prop branch
            validation:      Optional[object]      = None    # PlayerPropValidation or None
            decision:        Optional[object]      = None    # UDBetDecision or None
            hit_rates:       Optional[object]      = None    # PlayerHitRates or None
            market_quality:  Optional[object]      = None    # MarketQuality — display context
            market_pressure: Optional[object]      = None    # MarketPressureFlag — warning only
    
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
                # Immediate criteria (strict):
                #   - 0.5 line AND supported betting category
                #   - OR score reaches quality threshold
                np_immediate = (
                    (line_val <= config.UD_NEW_PROP_IMMEDIATE_LINE_THRESHOLD
                     and stat_type in config.UD_PRIORITY_STAT_CATEGORIES)
                    or score.stars >= config.UD_MIN_STARS_TO_ALERT
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
                    except Exception:
                        pass  # never block alert flow
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
                # in the betting-alert whitelist (NBA/NFL are tracking-only).
                _np_bet_ready = (
                    np_immediate
                    and decision is not None
                    and decision.recommendation != "PASS"
                    and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                )
                # Per-tier confidence gate — filter weak B/A/S picks before alert
                if _np_bet_ready and decision is not None:
                    _np_min_conf = {
                        "S": config.UD_MIN_CONF_S,
                        "A": config.UD_MIN_CONF_A,
                        "B": config.UD_MIN_CONF_B,
                    }.get(decision.decision_tier, 0)
                    if decision.confidence < _np_min_conf:
                        _np_bet_ready = False
                        logger.debug(
                            "UD conf_gate [new]: %s | %s | conf=%d < min=%d (tier=%s)",
                            player, stat_type,
                            decision.confidence, _np_min_conf, decision.decision_tier,
                        )

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
                    delivery  = AlertDelivery(db, bot, chat_ids)
                    ud_result = await delivery.deliver_underdog(
                        player_name        = player,
                        team               = snap.team or "",
                        sport              = snap.sport,
                        stat_type          = stat_type,
                        old_line           = line_val,
                        new_line           = line_val,
                        game_time          = snap.game_time,
                        score              = score,
                        new_prop           = True,
                        validation         = validation,
                        decision           = decision,
                        market_quality     = market_quality,
                        market_pressure    = market_pressure,
                        intelligence_trace = _np_intel_trace,
                    )
                    if ud_result.sent:
                        _n_new_prop_sent += 1
                    elif ud_result.filtered:
                        logger.debug(
                            "Underdog new-prop filtered: %s | %s | %s",
                            player, stat_type, ud_result.filtered_reason,
                        )
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
                        logger.info(
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
                    _processed_keys.add((player, stat_type))
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
                        except Exception:
                            pass  # never block alert flow
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

                # Qualify for alert delivery.
                # Removal notices: only Telegram-alert for three conditions.
                # All removals are still saved to the DB regardless.
                if is_removed:
                    # Removal alerts: only for props that previously triggered a
                    # user-visible Telegram alert. Score-only / DB-only tracking
                    # does NOT qualify — no removal spam for unseen props.
                    is_qualified = (
                        prev_record is not None
                        and prev_record.alert_sent  # a Telegram alert was previously sent
                    )
                else:
                    # Line-change props: require A-tier or better, a real directional
                    # pick from the decision engine, and sport in betting whitelist.
                    # Cold-start props always fail this gate — alerts suppressed.
                    # Re-entries qualify via is_reentry_qualified (set above) regardless
                    # of decision engine result — there is no previous line to compare.
                    # Every non-removal alert requires a real directional pick.
                    # Re-entries no longer bypass the decision engine.
                    is_qualified = (
                        not is_cold_start
                        and score is not None
                        and score.stars >= config.UD_MIN_STARS_TO_ALERT
                        and decision is not None
                        and decision.recommendation != "PASS"
                        and decision.decision_tier in ("S", "A", "B")
                        and (snap.sport or "UNKNOWN") in config.ud_alert_sports
                    )
                    if is_qualified and not is_reentry_qualified:
                        _n_qualified += 1
                    # ── Debug tracking (line-change / cold-start) ─────────────────
                    if score is not None:
                        if is_cold_start:
                            _lc_rej = "cold_start"
                        elif is_qualified:
                            _lc_rej = "qualified"
                        elif score.stars < config.UD_MIN_STARS_TO_ALERT:
                            _lc_rej = (
                                f"below_threshold"
                                f" ({score.stars}★ < {config.UD_MIN_STARS_TO_ALERT}★)"
                            )
                        elif decision is None:
                            _lc_rej = "no_decision (PASS tier)"
                        elif decision.recommendation == "PASS":
                            _lc_rej = "decision_pass"
                            logger.info(
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
    
                should_alert = is_qualified and (is_reentry or is_removed or (
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
                    delivery  = AlertDelivery(db, bot, chat_ids)
                    # Derive removal reason from game-time context
                    _removal_reason: Optional[str] = None
                    if is_removed:
                        from datetime import datetime as _dtnow
                        _now_utc = _dtnow.utcnow()
                        if snap.game_time and snap.game_time.replace(tzinfo=None) < _now_utc:
                            _removal_reason = "Game started / market closed"
                        else:
                            _removal_reason = "Market no longer available from provider"
                    ud_result = await delivery.deliver_underdog(
                        player_name        = player,
                        team               = snap.team or "",
                        sport              = snap.sport,
                        stat_type          = stat_type,
                        old_line           = prev_line or (snap.line or 0.0),
                        new_line           = snap.line or 0.0,
                        game_time          = snap.game_time,
                        score              = score,
                        removed            = is_removed,
                        new_prop           = is_reentry_qualified and not is_removed,
                        validation         = validation,
                        decision           = decision,
                        market_quality     = market_quality,
                        market_pressure    = market_pressure,
                        removal_reason     = _removal_reason,
                        intelligence_trace = _lc_intel_trace,
                    )
                    if ud_result.filtered:
                        logger.debug(
                            "Underdog alert filtered: %s | %s | %s",
                            player, stat_type, ud_result.filtered_reason,
                        )
    
            # Queue lifecycle transitions — applied after bridge so PropLineHistory rows exist
            if ud_result.sent:
                _sport_key = snap.sport or "UNKNOWN"
                if is_removed:
                    _lifecycle_removed.append((player, _sport_key, stat_type))
                else:
                    _lifecycle_alerted.append((player, _sport_key, stat_type))
    
            # Resolve alert_outcome for historical analysis
            if is_new_prop:
                if ud_result.sent:
                    _alert_outcome: Optional[str] = "new_prop_sent"
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
            # Incremental: commit immediately (rare, no lock-contention risk).
            if is_cold_start:
                _cold_start_records.append(record)
            else:
                await db.save_underdog_snapshot(record)
    
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
    
                if _st not in _HFS:
                    continue
                if _sport not in config.ud_alert_sports:
                    continue
                if (_sp, _st) in _processed_keys:
                    continue  # already handled in the main loop this cycle
    
                _prev = recent_by_key.get((_sp, _st))
                if _prev is None or _prev.score_tier not in ("A", "S"):
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
    
                # 24 h dedup — skip if already alerted today
                if await db.has_recent_ud_alert(_sp, _st, within_seconds=86400):
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
                    continue
                if _sscore.stars < config.UD_MIN_STARS_TO_ALERT:
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
                except Exception:
                    pass  # never block alert flow
                if _sdec is None or _sdec.recommendation == "PASS":
                    continue

                # Per-tier confidence gate for standing plays
                _s_min_conf = {
                    "S": config.UD_MIN_CONF_S,
                    "A": config.UD_MIN_CONF_A,
                    "B": config.UD_MIN_CONF_B,
                }.get(_sdec.decision_tier, 0)
                if _sdec.confidence < _s_min_conf:
                    logger.debug(
                        "UD conf_gate [standing]: %s | %s | conf=%d < min=%d (tier=%s)",
                        _sp, _st, _sdec.confidence, _s_min_conf, _sdec.decision_tier,
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
                delivery   = AlertDelivery(db, bot, chat_ids)
                _sresult   = await delivery.deliver_underdog(
                    player_name        = _sp,
                    team               = _ssnap.team or "",
                    sport              = _ssnap.sport,
                    stat_type          = _st,
                    old_line           = _line_val,
                    new_line           = _line_val,
                    game_time          = _ssnap.game_time,
                    score              = _sscore,
                    validation         = _sval,
                    decision           = _sdec,
                    market_quality     = _smq,
                    market_pressure    = _smp,
                    standing           = True,
                    intelligence_trace = _s_intel_trace,
                )
                if _sresult.sent:
                    _n_standing_sent += 1
                    logger.info(
                        "Underdog standing alert sent: %s | %s | %s | score=%d",
                        _sp, _st, _ssport, _sscore.total,
                    )
    
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
    
        # ── Cold-start bulk save + latch — runs once after the prop loop ──────────
        if is_cold_start:
            if _cold_start_records:
                await db.save_underdog_snapshots_bulk(_cold_start_records)
                logger.info(
                    "underdog_job: cold-start bulk save — %d records written",
                    len(_cold_start_records),
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
        logger.info("underdog_job [debug summary]\n%s", "\n".join(_dbg_lines))

        # ── PropCandidateLog batch write — edge transparency (Phase 4) ───────
        if _scored_props and db:
            try:
                import json as _json
                _now_ts   = datetime.utcnow()
                _cand_rows: list[dict] = []
                for _cp in _scored_props:
                    _ctier = _cp.get("tier", "PASS")
                    _crej  = _cp.get("rejection")
                    # gate_decision from tier + rejection flag
                    if _ctier == "PASS":
                        _cgd = "REJECTED"
                    elif _ctier == "B":
                        _cgd = "WATCHLIST"   # B-tier: may or may not have alerted
                    elif _crej is None and _ctier in ("S", "A"):
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
                if _cand_rows:
                    await db.log_prop_candidate_batch(_cand_rows)
                    logger.debug(
                        "underdog_job: logged %d candidates to PropCandidateLog",
                        len(_cand_rows),
                    )
            except Exception as _cand_exc:
                logger.debug("underdog_job: PropCandidateLog write skipped: %s", _cand_exc)

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
            limit=200, since_hours=4
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

    # Record job outcome — failure if any persistence stage raised.
    if _health:
        if _persistence_ok:
            _health.record_job_run("underdog_job")
            _health.record_underdog_scan(
                props_count = len(ud_snaps) if "ud_snaps" in dir() else 0,
                alerts_sent = (
                    (_n_new_prop_sent if "_n_new_prop_sent" in dir() else 0)
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
