"""Background job: collect real player game results for supported sports."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from engine.sport_policy import is_football_related_sport

logger = logging.getLogger(__name__)

# Sports for which PlayerStatsProvider.fetch_results() has a working data
# path.  Unsupported sports (LOL, VALORANT/VAL, PGA/GOLF, MMA, TT, and
# BADMINTON) would always return [] — filtering them avoids wasting API-call
# budget on sports that can never produce DB rows.
_SUPPORTED_HISTORY_SPORTS = frozenset({
    "MLB", "NBA",
    "WNBA", "NHL", "CS", "DOTA", "TENNIS",
    "MLS", "NCAAB", "FIFA",
    "SOCCER",         # Requires FOOTBALL_DATA_API_KEY; returns [] gracefully without it
})


def _fair_cycle_targets(
    targets: list[tuple[str, str, str]],
    limit: int,
) -> list[tuple[str, str, str]]:
    """Select a bounded, round-robin slice so one sport cannot monopolize a cycle."""
    by_sport: dict[str, list[tuple[str, str, str]]] = {}
    for target in targets:
        by_sport.setdefault(target[1], []).append(target)
    selected: list[tuple[str, str, str]] = []
    while len(selected) < limit and by_sport:
        for sport in sorted(tuple(by_sport)):
            bucket = by_sport.get(sport)
            if not bucket:
                by_sport.pop(sport, None)
                continue
            selected.append(bucket.pop(0))
            if len(selected) >= limit:
                break
        for sport in tuple(by_sport):
            if not by_sport[sport]:
                by_sport.pop(sport, None)
    return selected


async def player_history_collector_job(context) -> None:
    """Fetch real game results for active primary-provider props.

    Selects all active players whose sport has a working
    PlayerStatsProvider data path, then calls fetch_results() until the
    complete target set is exhausted. It is an independent storage lane:
    player monitoring neither starts, stops, pauses, nor reports this work.
    """
    db = context.bot_data.get("history_db") or context.bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    logger.info("player_history_collector_job: independent full collection starting")
    try:
        from providers.player_stats import PlayerStatsProvider

        provider = PlayerStatsProvider()
        targets: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        # ── Primary path: canonical active pool (both primary providers) ──
        try:
            _generic_active = getattr(
                db, "get_all_active_prop_line_history_by_line", None
            )
            if asyncio.iscoroutinefunction(_generic_active):
                active = await _generic_active(
                    providers=("Underdog", "PrizePicks")
                )
                active_rows = active.values()
            else:
                active = await db.get_active_underdog_snapshot_per_prop()
                active_rows = active.values()
            for s in active_rows:
                player = (
                    getattr(s, "player_name", None)
                    or getattr(s, "player", None)
                    or ""
                )
                stat = getattr(s, "stat_type", "") or ""
                if not stat:
                    selection = str(getattr(s, "selection", "") or "").strip()
                    # Legacy Underdog snapshots expose "Stat Line" rather
                    # than separate stat/line fields.
                    stat = re.sub(r"\s+-?\d+(?:\.\d+)?\s*$", "", selection)
                    stat = stat or selection
                sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
                if (
                    sport not in _SUPPORTED_HISTORY_SPORTS
                    or is_football_related_sport(sport)
                ):
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

        # ── Fallback: recent normalized history when no active rows ──────────
        if not targets:
            try:
                _generic_active = getattr(
                    db, "get_all_active_prop_line_history_by_line", None
                )
                if asyncio.iscoroutinefunction(_generic_active):
                    plh = []
                    for _provider in ("Underdog", "PrizePicks"):
                        plh.extend(
                            await db.get_latest_props_for_provider(
                                _provider, since_hours=48
                            )
                        )
                else:
                    plh = await db.get_latest_props_for_provider(
                        "Underdog", since_hours=48
                    )
                for s in plh:
                    sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
                    if (
                        sport not in _SUPPORTED_HISTORY_SPORTS
                        or is_football_related_sport(sport)
                    ):
                        continue  # No provider data path — skip
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
            "player_history_collector_job: provider_targets=%d "
            "round_robin_sports=%s mode=independent_full_collection",
            len(targets),
            sorted({sport for _, sport, _ in targets}),
        )

        # Hydrate live evidence memory in one bounded background read. The
        # Underdog scan never performs these reads itself.
        bulk_reader = getattr(db, "get_player_results_bulk", None)
        if targets and asyncio.iscoroutinefunction(bulk_reader):
            try:
                from engine.evidence_memory import put_player_result_batch

                existing_results = await bulk_reader(targets, limit=40)
                put_player_result_batch(existing_results)
                logger.info(
                    "player_history_collector_job: evidence cache hydrated "
                    "keys=%d rows=%d",
                    len(existing_results),
                    sum(len(rows) for rows in existing_results.values()),
                )
            except Exception as exc:
                logger.warning(
                    "player_history_collector_job: evidence cache hydration failed: %s",
                    exc,
                )

        # ── Fetch the complete supported target set ─────────────────────────
        ok = 0
        rows = 0
        cycle_targets = _fair_cycle_targets(targets, len(targets))
        target_counts: dict[str, int] = {}
        ok_by_sport: dict[str, int] = {}
        rows_by_sport: dict[str, int] = {}
        cycle_started = time.monotonic()

        for player, sport, stat in cycle_targets:
            target_counts[sport] = target_counts.get(sport, 0) + 1
            try:
                raw = await provider.fetch_results(player, sport, stat)
                # Some non-football sports have no working public
                # PlayerStatsProvider path.  Let final PropLine event stats
                # fill those missing rows; that adapter persists accepted
                # rows through the same database upsert below.
                if (
                    not raw
                    and asyncio.iscoroutinefunction(
                        getattr(db, "get_player_results", None)
                    )
                ):
                    try:
                        from providers.player_history import (
                            get_player_history_provider,
                        )
                        raw = await get_player_history_provider().supplement_missing_results(
                            db,
                            player,
                            sport,
                            stat,
                            min_samples=5,
                            source_provider="Underdog",
                        )
                    except Exception as exc:
                        logger.debug(
                            "player_history_collector_job: PropLine fallback "
                            "%s/%s/%s failed: %s",
                            player,
                            sport,
                            stat,
                            exc,
                        )
                for r in raw:
                    await db.upsert_player_result(r)
                if raw:
                    from engine.evidence_memory import put_player_results

                    put_player_results(player, sport, stat, raw)
                    ok += 1
                    rows += len(raw)
                    ok_by_sport[sport] = ok_by_sport.get(sport, 0) + 1
                    rows_by_sport[sport] = rows_by_sport.get(sport, 0) + len(raw)
            except Exception as exc:
                logger.debug(
                    "player_history_collector_job: %s/%s/%s failed: %s",
                    player, sport, stat, exc,
                )

        logger.info(
            "player_history_collector_job: done players_ok=%d rows=%d "
            "targets_attempted=%d targets_total=%d elapsed=%.1fs "
            "targets_by_sport=%s "
            "players_ok_by_sport=%s rows_by_sport=%s",
            ok, rows, sum(target_counts.values()), len(cycle_targets),
            time.monotonic() - cycle_started, target_counts,
            ok_by_sport, rows_by_sport,
        )
    except Exception as exc:
        logger.exception("player_history_collector_job: %s", exc)
