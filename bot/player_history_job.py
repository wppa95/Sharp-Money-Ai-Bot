"""Background job: collect real player game results for Tier-1 sports."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Maximum PlayerStats API calls per 2-minute cycle.
# Player count is unlimited — the full Tier-1 active pool is targeted.
# This cap limits API spend per cycle, not the number of players tracked.
_API_CALL_TARGET = 1000

# Sports excluded from background player-history collection.
# MLB/NBA remain excluded here (on-demand enrichment still available).
# NFL is intentionally included so ESPN/Sleeper history is collected
# into the existing PlayerResult path for L5/L10/L20/L30/Season.
_TIER2_SPORTS = frozenset({"MLB", "NBA"})

# Sports for which PlayerStatsProvider.fetch_results() has a working data
# path.  Unsupported sports (LOL, VALORANT/VAL, PGA/GOLF, MMA, TT,
# BADMINTON, FIFA) would always return [] — filtering them avoids wasting
# API-call budget on sports that can never produce DB rows.
_SUPPORTED_HISTORY_SPORTS = frozenset({
    "WNBA", "NHL", "CS", "DOTA", "TENNIS",
    "NCAAF", "CFB",   # CFB is the Underdog identifier for NCAAF
    "MLS", "NCAAB",
    "SOCCER",         # Requires FOOTBALL_DATA_API_KEY; returns [] gracefully without it
    "NFL",            # ESPN gamelog + optional Sleeper supplement
})


async def player_history_collector_job(context) -> None:
    """Fetch real game results for active Tier-1 Underdog props.

    Selects players from the active snapshot whose sport has a working
    PlayerStatsProvider data path (including NFL), then calls
    fetch_results() for up to _API_CALL_TARGET of them per cycle.
    Player count is unlimited — only API calls are capped.
    """
    from engine.health import get_health_tracker

    db = context.bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    _activity_end = None
    try:
        from market_engine import _job_activity_start, _job_activity_end
        _job_activity_start("player_history")
        _activity_end = _job_activity_end
    except ImportError:
        pass
    ht = get_health_tracker()
    if ht:
        ht.record_job_started("player_history_collector_job")
    logger.info("player_history_collector_job: starting (api_call_target=%d)", _API_CALL_TARGET)
    try:
        from providers.player_stats import PlayerStatsProvider

        provider = PlayerStatsProvider()
        targets: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        # ── Primary path: active snapshot (full pool, no player cap) ──
        try:
            active = await db.get_active_underdog_snapshot_per_prop()
            for (player, stat), s in active.items():
                sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
                if sport in _TIER2_SPORTS:
                    continue  # MLB/NBA — skip background collection
                if sport not in _SUPPORTED_HISTORY_SPORTS:
                    continue  # No provider data path — skip to avoid wasting API calls
                key = (player.strip(), sport, (stat or "").lower().strip())
                if key[0] and key[2] and key not in seen:
                    seen.add(key)
                    targets.append(key)
        except Exception as exc:
            logger.warning(
                "player_history_collector_job: active snapshot load failed: %s",
                exc,
            )

        # ── Fallback: recent PropLineHistory when no active snapshot ──────────
        if not targets:
            try:
                plh = await db.get_latest_props_for_provider("Underdog", since_hours=48)
                for s in plh:
                    sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
                    if sport in _TIER2_SPORTS:
                        continue
                    if sport not in _SUPPORTED_HISTORY_SPORTS:
                        continue
                    key = (
                        (s.player_name or "").strip(),
                        sport,
                        (s.stat_type or "").lower().strip(),
                    )
                    if key[0] and key[2] and key not in seen:
                        seen.add(key)
                        targets.append(key)
            except Exception as exc:
                logger.warning(
                    "player_history_collector_job: prop_line_history fallback failed: %s",
                    exc,
                )

        logger.info(
            "player_history_collector_job: targets=%d calling_up_to=%d",
            len(targets),
            min(len(targets), _API_CALL_TARGET),
        )

        # ── Fetch results — capped at _API_CALL_TARGET API calls per cycle ───
        ok = 0
        rows = 0
        for player, sport, stat in targets[:_API_CALL_TARGET]:
            try:
                raw = await provider.fetch_results(player, sport, stat)
                for r in raw:
                    await db.upsert_player_result(r)
                if raw:
                    ok += 1
                    rows += len(raw)
            except Exception as exc:
                logger.debug(
                    "player_history_collector_job: %s/%s/%s failed: %s",
                    player, sport, stat, exc,
                )

        logger.info(
            "player_history_collector_job: done players_ok=%d rows=%d",
            ok, rows,
        )
        if ht:
            ht.record_job_run("player_history_collector_job")
        if _activity_end:
            _activity_end("player_history")
    except Exception as exc:
        logger.exception("player_history_collector_job: %s", exc)
        if ht:
            ht.record_job_fail("player_history_collector_job", str(exc))
        if _activity_end:
            _activity_end("player_history")
