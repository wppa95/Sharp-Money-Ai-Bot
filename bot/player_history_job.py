"""Background job: collect real player game results for sports with providers."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Maximum PlayerStats API calls per 2-minute cycle.
# Player count is unlimited — the full active pool is targeted.
# This cap limits API spend per cycle, not the number of players tracked.
_API_CALL_TARGET = 1000

# Sports for which PlayerStatsProvider.fetch_results() has a legitimate
# machine-readable data path.  Collection is based on provider capability,
# not old Tier-1/Tier-2 delivery classification.
#
# Supported (real gamelog/history providers exist):
#   MLB, NBA, NFL, WNBA, NHL, NCAAF/CFB, NCAAB, MLS, TENNIS, CS, DOTA, SOCCER
#
# Unsupported (no legitimate provider — do not invent one):
#   LOL, VAL/VALORANT, PGA/GOLF, MMA, BOXING, TT, BADMINTON, FIFA,
#   CRICKET, RUGBY, AFL, AFLW, KBO, NPB, CFL
_SUPPORTED_HISTORY_SPORTS = frozenset({
    "MLB",            # MLB Stats API
    "NBA",            # ESPN
    "NFL",            # ESPN + optional Sleeper
    "WNBA",           # ESPN
    "NHL",            # NHL public API
    "NCAAF", "CFB",   # ESPN (CFB = Underdog id for NCAAF)
    "NCAAB",          # ESPN
    "MLS",            # ESPN
    "TENNIS",         # JeffSackmann CSV
    "CS",             # PandaScore (key optional)
    "DOTA",           # OpenDota
    "SOCCER",         # football-data.org (key optional)
})


async def player_history_collector_job(context) -> None:
    """Fetch real game results for active Underdog props with providers.

    Selects players from the active snapshot whose sport has a working
    PlayerStatsProvider data path, then calls fetch_results() for up to
    _API_CALL_TARGET of them per cycle.
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
                if sport not in _SUPPORTED_HISTORY_SPORTS:
                    continue  # No legitimate provider — skip (do not invent evidence)
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
                    if sport not in _SUPPORTED_HISTORY_SPORTS:
                        continue  # No legitimate provider
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
