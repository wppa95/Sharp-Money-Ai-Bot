"""Background job: collect real player game results for supported sports."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# The scheduler cadence remains 240 seconds. Reserve a safety margin so the
# collector can finish cleanly before the next scheduled run (max_instances=1).
# The provider's own request timeout bounds any individual call.
_CYCLE_TIME_BUDGET_SECONDS = 210.0

# Sports for which PlayerStatsProvider.fetch_results() has a working data
# path.  Unsupported sports (LOL, VALORANT/VAL, PGA/GOLF, MMA, TT, and
# BADMINTON) would always return [] — filtering them avoids wasting API-call
# budget on sports that can never produce DB rows.
_SUPPORTED_HISTORY_SPORTS = frozenset({
    "MLB", "NBA", "NFL",
    "WNBA", "NHL", "CS", "DOTA", "TENNIS",
    "NCAAF", "CFB",   # CFB is the Underdog identifier for NCAAF
    "MLS", "NCAAB", "FIFA",
    "SOCCER",         # Requires FOOTBALL_DATA_API_KEY; returns [] gracefully without it
})


def _underdog_scan_running() -> bool:
    """Return whether the shared Underdog scan currently owns the DB-heavy slot."""
    try:
        from market_engine import _ud_full_scan_running
        return bool(_ud_full_scan_running)
    except (ImportError, AttributeError):
        # Keep the collector usable in isolated tests and older integrations
        # where the market engine module is not installed.
        return False


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
    """Fetch real game results for active Underdog props with providers.

    Selects all active players whose sport has a working
    PlayerStatsProvider data path, then calls fetch_results() until the
    cadence-safe time budget is exhausted. Player count and API calls are
    uncapped; the run duration is bounded instead.
    """
    from engine.health import get_health_tracker

    db = context.bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    ht = get_health_tracker()
    # Player history is enrichment, not the live monitoring path.  Do not
    # start another pool read while Underdog owns the shared scan slot.
    if _underdog_scan_running():
        logger.info(
            "player_history_collector_job: Underdog scan active — yielding this cycle"
        )
        if ht:
            ht.record_job_started("player_history_collector_job")
            ht.record_job_run("player_history_collector_job")
        return
    _activity_end = None
    try:
        from market_engine import _job_activity_start, _job_activity_end
        _job_activity_start("player_history")
        _activity_end = _job_activity_end
    except ImportError:
        pass
    if ht:
        ht.record_job_started("player_history_collector_job")
    logger.info(
        "player_history_collector_job: starting (time_budget=%.0fs)",
        _CYCLE_TIME_BUDGET_SECONDS,
    )
    try:
        from providers.player_stats import PlayerStatsProvider

        provider = PlayerStatsProvider()
        targets: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()

        # ── Primary path: active snapshot (full Tier-1 pool, no player cap) ──
        try:
            active = await db.get_active_underdog_snapshot_per_prop()
            for (player, stat), s in active.items():
                sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
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

        # Underdog may have claimed the slot while the active-pool query was
        # waiting on SQLite.  Yield before the fallback query or any provider
        # work so the live scan gets exclusive access to the hot path.
        if _underdog_scan_running():
            logger.info(
                "player_history_collector_job: Underdog scan claimed slot "
                "after active-pool load — yielding"
            )
            if ht:
                ht.record_job_run("player_history_collector_job")
            if _activity_end:
                _activity_end("player_history")
            return

        # ── Fallback: recent PropLineHistory when no active snapshot ──────────
        if not targets:
            try:
                plh = await db.get_latest_props_for_provider("Underdog", since_hours=48)
                for s in plh:
                    sport = (getattr(s, "sport", None) or "UNKNOWN").upper()
                    if sport not in _SUPPORTED_HISTORY_SPORTS:
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
            "round_robin_sports=%s time_budget=%.0fs",
            len(targets),
            sorted({sport for _, sport, _ in targets}),
            _CYCLE_TIME_BUDGET_SECONDS,
        )

        # ── Fetch results until the cadence-safe time budget is exhausted ───
        ok = 0
        rows = 0
        cycle_targets = _fair_cycle_targets(targets, len(targets))
        target_counts: dict[str, int] = {}
        ok_by_sport: dict[str, int] = {}
        rows_by_sport: dict[str, int] = {}
        cycle_started = time.monotonic()
        stopped_for_budget = False
        stopped_for_underdog = False
        for player, sport, stat in cycle_targets:
            if _underdog_scan_running():
                stopped_for_underdog = True
                break
            if time.monotonic() - cycle_started >= _CYCLE_TIME_BUDGET_SECONDS:
                stopped_for_budget = True
                break
            target_counts[sport] = target_counts.get(sport, 0) + 1
            try:
                raw = await provider.fetch_results(player, sport, stat)
                # A provider request can overlap the instant Underdog claims
                # the slot.  Do not begin the next SQLite write in that case;
                # the next history cycle will retry this target.
                if _underdog_scan_running():
                    stopped_for_underdog = True
                    break
                for r in raw:
                    await db.upsert_player_result(r)
                if raw:
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
            "targets_attempted=%d elapsed=%.1fs stopped_for_budget=%s "
            "stopped_for_underdog=%s targets_by_sport=%s "
            "players_ok_by_sport=%s rows_by_sport=%s",
            ok, rows, sum(target_counts.values()), time.monotonic() - cycle_started,
            stopped_for_budget, stopped_for_underdog, target_counts,
            ok_by_sport, rows_by_sport,
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
