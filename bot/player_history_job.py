"""Background job: collect real player game results."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def player_history_collector_job(context) -> None:
    """Every 15 min: fetch real game results for active Underdog props."""
    from engine.health import get_health_tracker

    db = context.bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    ht = get_health_tracker()
    if ht:
        ht.record_job_started("player_history_collector_job")
    logger.info("player_history_collector_job: starting")
    try:
        from providers.player_stats import PlayerStatsProvider

        provider = PlayerStatsProvider()
        targets = []
        seen = set()
        try:
            active = await db.get_active_underdog_snapshot_per_prop()
            for (player, stat), s in list(active.items())[:40]:
                sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
                key = (player.strip(), sport, (stat or "").lower().strip())
                if key[0] and key[2] and key not in seen:
                    seen.add(key)
                    targets.append(key)
        except Exception as exc:
            logger.warning(
                "player_history_collector_job: active snapshot load failed: %s",
                exc,
            )
        logger.info("player_history_collector_job: targets=%d", len(targets))
        ok = 0
        rows = 0
        for player, sport, stat in targets:
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
    except Exception as exc:
        logger.exception("player_history_collector_job: %s", exc)
        if ht:
            ht.record_job_fail("player_history_collector_job", str(exc))