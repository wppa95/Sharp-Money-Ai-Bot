"""Background job: collect real player game results for supported sports."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from engine.sport_policy import is_football_related_sport

logger = logging.getLogger(__name__)

# The scheduler cadence is two minutes. Reserve a safety margin so the
# collector can finish cleanly before the next scheduled run (coordinator
# overlap protection also prevents concurrent collectors).
# The provider's own request timeout bounds any individual call.
_CYCLE_TIME_BUDGET_SECONDS = 90.0
# This is an API-call target, not a cap on the number of active players
# retained while building the rotation.
_API_CALL_TARGET = 250

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


def _underdog_scan_running() -> bool:
    """Return whether the shared Underdog scan currently owns the DB-heavy slot."""
    try:
        from market_engine import _ud_full_scan_running
        return bool(_ud_full_scan_running)
    except (ImportError, AttributeError):
        # Keep the collector usable in isolated tests and older integrations
        # where the market engine module is not installed.
        return False


# A live Underdog scan can take longer than this job's four-minute cadence
# when the SQLite pool is under load. Waiting for the current scan is
# preferable to yielding every scheduled run indefinitely: otherwise non-MLB
# sports never accumulate the real game results required for a pick.
_SCAN_SLOT_POLL_SECONDS = 5.0
_SCAN_SLOT_MAX_WAIT_SECONDS = 900.0


async def _wait_for_underdog_scan_slot() -> bool:
    """Wait for the live scan to release the shared database-heavy slot."""
    waited = 0.0
    while True:
        scan_running = _underdog_scan_running()
        if not scan_running:
            break
        if waited >= _SCAN_SLOT_MAX_WAIT_SECONDS:
            logger.warning(
                "player_history_collector_job: Underdog scan held slot for "
                "%.0fs — yielding without starting provider work",
                waited,
            )
            return False
        await asyncio.sleep(_SCAN_SLOT_POLL_SECONDS)
        waited += _SCAN_SLOT_POLL_SECONDS
    if waited:
        logger.info(
            "player_history_collector_job: resumed after Underdog scan "
            "released slot (waited %.0fs)",
            waited,
        )
    return True


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
    cadence-safe time budget is exhausted. Player count and API calls are
    uncapped; the run duration is bounded instead.
    """
    from engine.health import get_health_tracker

    db = context.bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    ht = get_health_tracker()
    # Player history is enrichment, not the live monitoring path. Do not
    # start another pool read while Underdog owns the shared scan slot, but
    # wait for the current scan instead of throwing away every cadence tick.
    if not await _wait_for_underdog_scan_slot():
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
            "round_robin_sports=%s time_budget=%.0fs",
            len(targets),
            sorted({sport for _, sport, _ in targets}),
            _CYCLE_TIME_BUDGET_SECONDS,
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

        # ── Fetch results until the cadence-safe time budget is exhausted ───
        ok = 0
        rows = 0
        cycle_targets = _fair_cycle_targets(
            targets, min(len(targets), _API_CALL_TARGET)
        )
        target_counts: dict[str, int] = {}
        ok_by_sport: dict[str, int] = {}
        rows_by_sport: dict[str, int] = {}
        cycle_started = time.monotonic()
        stopped_for_budget = False
        stopped_for_underdog = False

        async def _await_history_budget(awaitable, operation: str):
            remaining = _CYCLE_TIME_BUDGET_SECONDS - (
                time.monotonic() - cycle_started
            )
            if remaining <= 0:
                if asyncio.iscoroutine(awaitable):
                    awaitable.close()
                return []
            try:
                return await asyncio.wait_for(awaitable, timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning(
                    "player_history_collector_job: %s exceeded remaining "
                    "cycle budget; yielding to the next cycle",
                    operation,
                )
                return []

        for player, sport, stat in cycle_targets:
            if _underdog_scan_running():
                stopped_for_underdog = True
                break
            if time.monotonic() - cycle_started >= _CYCLE_TIME_BUDGET_SECONDS:
                stopped_for_budget = True
                break
            target_counts[sport] = target_counts.get(sport, 0) + 1
            try:
                raw = await _await_history_budget(
                    provider.fetch_results(player, sport, stat),
                    f"provider fetch {sport}/{stat}",
                )
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
                        raw = await _await_history_budget(
                            get_player_history_provider().supplement_missing_results(
                                db,
                                player,
                                sport,
                                stat,
                                min_samples=5,
                                source_provider="Underdog",
                            ),
                            f"PropLine fallback {sport}/{stat}",
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
                # A provider request can overlap the instant Underdog claims
                # the slot.  Do not begin the next SQLite write in that case;
                # the next history cycle will retry this target.
                if _underdog_scan_running():
                    stopped_for_underdog = True
                    break
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
