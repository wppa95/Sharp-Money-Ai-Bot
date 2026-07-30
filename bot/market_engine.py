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

# In-memory snapshot cache: market_key -> list[MarketSnapshot]
_snapshot_cache: dict[tuple, list[MarketSnapshot]] = {}


def init_market_engine(registry: ConnectorRegistry) -> None:
    """Call once at startup to register the connector registry."""
    global _registry
    _registry = registry
    logger.info("Market engine initialized with %d connectors", len(registry.connectors))


# ── Internal helpers ──────────────────────────────────────────────────────────

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

    chat_ids = list(config.allowed_user_ids)
    now      = datetime.utcnow()

    snapshots = await _registry.fetch_pickem()
    if not snapshots:
        logger.debug("underdog_job: no pick'em snapshots")
        return

    ud_snaps = [s for s in snapshots if s.sportsbook == "Underdog"]

    # Summary counters — emitted as a single INFO line after the loop
    _n_scored:        int       = 0
    _tier_counts:     dict      = {"S": 0, "A": 0, "B": 0, "PASS": 0}
    _n_qualified:     int       = 0   # line-change props that passed the scoring gate
    _n_removed:       int       = 0   # props with [REMOVED] marker
    _n_new_prop:      int       = 0   # first-appearance props detected this cycle
    _n_new_prop_sent: int       = 0   # immediate new-prop alerts delivered
    # Batch for the end-of-cycle new-prop digest sent after the loop.
    # Each entry: {player, stat_type, sport, team, line, score, immediate, game_time}
    _new_props_batch: list[dict] = []

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
        from engine.ud_scoring import score_ud_prop, UDPropScore
        from alerts import DeliveryResult
        score:        Optional[UDPropScore] = None
        ud_result:    DeliveryResult        = DeliveryResult(sent=False)
        np_immediate: bool                  = False   # set inside is_new_prop branch

        if is_new_prop:
            # ── New-prop path ────────────────────────────────────────────────
            # Score with no history (n_history=0 — all dimensions return neutral).
            _n_new_prop += 1
            line_val = snap.line or 0.0
            score = score_ud_prop(
                player_name  = player,
                stat_type    = stat_type,
                sport        = snap.sport or "UNKNOWN",
                current_line = line_val,
                prev_line    = None,
                history      = [],
            )
            # Immediate individual alert criteria (strict):
            #   - 0.5 line AND it is a supported betting category
            #   - OR score reaches the quality threshold
            # Priority stat category alone (without a 0.5 line) does NOT trigger
            # an individual alert — it goes into the digest instead.
            np_immediate = (
                (line_val <= config.UD_NEW_PROP_IMMEDIATE_LINE_THRESHOLD
                 and stat_type in config.UD_PRIORITY_STAT_CATEGORIES)
                or score.stars >= config.UD_MIN_STARS_TO_ALERT
            )
            # Always add to the cycle batch — even non-immediate props appear in summary
            _new_props_batch.append({
                "player":    player,
                "stat_type": stat_type,
                "sport":     snap.sport or "UNKNOWN",
                "team":      snap.team or "",
                "line":      line_val,
                "score":     score,
                "immediate": np_immediate,
                "game_time": snap.game_time,
            })
            if np_immediate and chat_ids:
                delivery  = AlertDelivery(db, bot, chat_ids)
                ud_result = await delivery.deliver_underdog(
                    player_name = player,
                    team        = snap.team or "",
                    sport       = snap.sport,
                    stat_type   = stat_type,
                    old_line    = line_val,
                    new_line    = line_val,
                    game_time   = snap.game_time,
                    score       = score,
                    new_prop    = True,
                )
                if ud_result.sent:
                    _n_new_prop_sent += 1
                elif ud_result.filtered:
                    logger.debug(
                        "Underdog new-prop filtered: %s | %s | %s",
                        player, stat_type, ud_result.filtered_reason,
                    )

        else:
            # ── Line-change / removal path (existing logic) ──────────────────
            # Score line-change props that pass the raw magnitude pre-filter.
            # Removal notices bypass scoring and always qualify.
            if not is_removed and line_changed and prev_line is not None:
                ud_history = await db.get_ud_prop_history(player, stat_type, limit=20)
                score = score_ud_prop(
                    player_name  = player,
                    stat_type    = stat_type,
                    sport        = snap.sport or "UNKNOWN",
                    current_line = snap.line or 0.0,
                    prev_line    = prev_line,
                    history      = ud_history,
                )
                _n_scored += 1
                _tier_counts[score.tier] = _tier_counts.get(score.tier, 0) + 1
                logger.debug(
                    "UD score: %s | %s | %s (tier=%s stars=%d n=%d)",
                    player, stat_type, score.total, score.tier, score.stars, score.n_history,
                )

            # Qualify for alert delivery.
            # Removal notices: only Telegram-alert for three conditions.
            # All removals are still saved to the DB regardless.
            if is_removed:
                is_qualified = (
                    prev_record is not None and (
                        prev_record.alert_sent                        # previously sent a quality alert
                        or prev_record.score_tier in ("S", "A", "B") # strong grade
                        or (                                          # was a 0.5 opportunity
                            prev_record.line_value is not None
                            and prev_record.line_value <= 0.5
                        )
                    )
                )
            else:
                # Line-change props: require B-tier or better.
                is_qualified = (
                    score is not None and score.stars >= config.UD_MIN_STARS_TO_ALERT
                )
                if is_qualified:
                    _n_qualified += 1

            should_alert = is_qualified and (is_removed or (
                line_changed
                and prev_line is not None
                and abs(snap.line - prev_line) >= config.MIN_UNDERDOG_LINE_CHANGE
            ))

            if should_alert and chat_ids:
                delivery  = AlertDelivery(db, bot, chat_ids)
                ud_result = await delivery.deliver_underdog(
                    player_name = player,
                    team        = snap.team or "",
                    sport       = snap.sport,
                    stat_type   = stat_type,
                    old_line    = prev_line or (snap.line or 0.0),
                    new_line    = snap.line or 0.0,
                    game_time   = snap.game_time,
                    score       = score,
                    removed     = is_removed,
                )
                if ud_result.filtered:
                    logger.debug(
                        "Underdog alert filtered: %s | %s | %s",
                        player, stat_type, ud_result.filtered_reason,
                    )

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
            _alert_outcome = "removal_skipped" if is_removed else "skipped"
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
            score_total   = score.total  if score is not None else None,
            score_tier    = score.tier   if score is not None else None,
            score_stars   = score.stars  if score is not None else None,
            alert_outcome = _alert_outcome,
            fetched_at    = now,
        )
        await db.save_underdog_snapshot(record)

    # ── End-of-cycle new-prop digest ──────────────────────────────────────────
    # One batched summary per cycle — only when new props were detected and there
    # are registered chat IDs to send to.  Individual alerts were already sent
    # above for high-priority props (line ≤ 0.5, score gate, priority stat).
    if _new_props_batch and chat_ids:
        from alerts_multiplatform import format_underdog_new_prop_cycle_summary
        from alerts import broadcast_alert
        digest = format_underdog_new_prop_cycle_summary(_new_props_batch)
        await broadcast_alert(bot, chat_ids, digest)
        logger.info(
            "underdog_job: new-prop digest sent — total=%d immediate=%d summary_only=%d",
            len(_new_props_batch),
            _n_new_prop_sent,
            len(_new_props_batch) - _n_new_prop_sent,
        )

    logger.info(
        "underdog_job: fetched=%d scored=%d S=%d A=%d B=%d PASS=%d "
        "qualified=%d removed=%d new=%d new_sent=%d",
        len(ud_snaps),
        _n_scored,
        _tier_counts.get("S",    0),
        _tier_counts.get("A",    0),
        _tier_counts.get("B",    0),
        _tier_counts.get("PASS", 0),
        _n_qualified,
        _n_removed,
        _n_new_prop,
        _n_new_prop_sent,
    )
