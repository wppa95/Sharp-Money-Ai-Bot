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

# Keep transactions short enough that live monitoring and reporting can get
# the SQLite writer slot, while committing often enough that a long provider
# collection never keeps all fetched rows only in memory.
_PLAYER_HISTORY_WRITE_BATCH_SIZE = 25
# Keep provider work concurrent enough to finish the full per-cycle slice.
# These workers belong to one invocation; this is not a carryover queue.
_PLAYER_HISTORY_FETCH_CONCURRENCY = 64
# Do not let a busy SQLite writer consume the whole cadence. Provider calls
# use the providers' own request timeouts and all selected targets are awaited;
# target data is never intentionally dropped by this collector.
_PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS = 5.0

# This is an API-call/target budget per scheduler run, not a cap on the
# active-player universe. The full target list is rebuilt so newly active
# props can join the rotation, while the cursor below ensures that existing
# targets continue from where the previous run stopped.
try:
    from config import config as _config

    _API_CALL_TARGET = max(
        1, int(getattr(_config, "PLAYER_HISTORY_BATCH_SIZE", 250))
    )
except (ImportError, TypeError, ValueError):
    # Keep this module usable in focused tests and older integrations that do
    # not load the application configuration module.
    _API_CALL_TARGET = 250

_NEXT_TARGET_KEY = "_player_history_next_target"
_CURSOR_KEY = "_player_history_cursor"


class _CadenceBoundaryReached(Exception):
    """Internal signal that this collector invocation must stop now."""


def _player_history_cadence_budget() -> float:
    """Return the fetch budget while leaving room before the next scheduler tick."""
    interval = max(
        1.0,
        float(getattr(_config, "PLAYER_HISTORY_POLL_INTERVAL", 120)),
    )
    headroom = max(
        0.0,
        float(getattr(_config, "PLAYER_HISTORY_CADENCE_HEADROOM_SECONDS", 15)),
    )
    return max(1.0, interval - headroom)


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


def reset_player_history_runtime_state(bot_data: dict | None = None) -> None:
    """Forget the ephemeral rotation cursor for a fresh application session."""
    if bot_data is not None:
        bot_data.pop(_NEXT_TARGET_KEY, None)
        bot_data.pop(_CURSOR_KEY, None)


def _rotating_target_slice(
    targets: list[tuple[str, str, str]],
    *,
    next_target: object = None,
    cursor: int = 0,
    limit: int = _API_CALL_TARGET,
) -> tuple[list[tuple[str, str, str]], int, tuple[str, str, str] | None]:
    """Return one fair slice and the cursor position after that slice.

    Targets are sorted before the round-robin order is built so a database
    result-order change cannot make the collector repeatedly revisit one
    portion of the pool. A target identity is retained alongside the numeric
    cursor so additions/removals between runs do not shift the next target
    unnecessarily.
    """
    if not targets:
        return [], 0, None

    stable_targets = sorted(targets, key=lambda target: (target[1], target[0], target[2]))
    rotation = _fair_cycle_targets(stable_targets, len(stable_targets))
    if not rotation:
        return [], 0, None

    start = None
    if isinstance(next_target, (tuple, list)) and len(next_target) == 3:
        candidate = tuple(str(part) for part in next_target)
        try:
            start = rotation.index(candidate)
        except ValueError:
            start = None
    if start is None:
        try:
            start = int(cursor) % len(rotation)
        except (TypeError, ValueError):
            start = 0

    slice_size = min(max(1, int(limit)), len(rotation))
    selected = [
        rotation[(start + offset) % len(rotation)]
        for offset in range(slice_size)
    ]
    next_index = (start + slice_size) % len(rotation)
    return selected, next_index, rotation[next_index]


async def player_history_collector_job(context) -> None:
    """Fetch real game results for active primary-provider props.

    Selects all active players whose sport has a working
    PlayerStatsProvider data path, then fetches one fair, bounded target
    slice. The next invocation resumes at the following target. It is an
    independent storage lane: player monitoring neither starts, stops,
    pauses, nor reports this work.
    """
    bot_data = context.bot_data
    db = bot_data.get("history_db") or bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    logger.info(
        "player_history_collector_job: independent rotating collection starting "
        "batch_limit=%d cadence_budget=%.1fs",
        _API_CALL_TARGET,
        _player_history_cadence_budget(),
    )
    try:
        cycle_started = time.monotonic()
        cadence_budget = _player_history_cadence_budget()
        cycle_deadline = cycle_started + cadence_budget

        async def await_in_cycle(operation: str, awaitable_factory):
            """Await one operation without allowing it past this cycle's deadline."""
            remaining = cycle_deadline - time.monotonic()
            if remaining <= 0:
                raise _CadenceBoundaryReached(operation)
            try:
                return await asyncio.wait_for(
                    awaitable_factory(),
                    timeout=min(
                        remaining,
                        _PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS,
                    ),
                )
            except asyncio.TimeoutError as exc:
                raise _CadenceBoundaryReached(operation) from exc

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
                active = await await_in_cycle(
                    "loading active player-history targets",
                    lambda: _generic_active(
                        providers=("Underdog", "PrizePicks")
                    ),
                )
                active_rows = active.values()
            else:
                active = await await_in_cycle(
                    "loading active player-history targets",
                    db.get_active_underdog_snapshot_per_prop,
                )
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
        except _CadenceBoundaryReached as exc:
            logger.warning(
                "player_history_collector_job: cadence boundary reached during %s",
                exc,
            )
            return
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
                            await await_in_cycle(
                                f"loading {_provider} player-history targets",
                                lambda provider_name=_provider: (
                                    db.get_latest_props_for_provider(
                                        provider_name, since_hours=48
                                    )
                                ),
                            )
                        )
                else:
                    plh = await await_in_cycle(
                        "loading Underdog player-history targets",
                        lambda: db.get_latest_props_for_provider(
                            "Underdog", since_hours=48
                        ),
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
            except _CadenceBoundaryReached as exc:
                logger.warning(
                    "player_history_collector_job: cadence boundary reached during %s",
                    exc,
                )
                return
            except Exception as exc:
                logger.warning(
                    "player_history_collector_job: prop_line_history fallback failed: %s",
                    exc,
                )

        logger.info(
            "player_history_collector_job: provider_targets=%d "
            "round_robin_sports=%s mode=independent_rotating_collection "
            "batch_limit=%d",
            len(targets),
            sorted({sport for _, sport, _ in targets}),
            _API_CALL_TARGET,
        )

        cycle_targets, _, _ = _rotating_target_slice(
            targets,
            next_target=bot_data.get(_NEXT_TARGET_KEY),
            cursor=bot_data.get(_CURSOR_KEY, 0),
            limit=_API_CALL_TARGET,
        )
        stable_targets = sorted(
            targets, key=lambda target: (target[1], target[0], target[2])
        )
        target_rotation = _fair_cycle_targets(stable_targets, len(stable_targets))
        rotation_indexes = {
            target: index for index, target in enumerate(target_rotation)
        }
        # Hydrate live evidence memory in one bounded background read. The
        # Underdog scan never performs these reads itself. Only this run's
        # target slice is loaded; the remaining targets are covered by later
        # rotations rather than replaying the full active set every time.
        bulk_reader = getattr(db, "get_player_results_bulk", None)
        if cycle_targets and asyncio.iscoroutinefunction(bulk_reader):
            try:
                from engine.evidence_memory import put_player_result_batch

                existing_results = await await_in_cycle(
                    "hydrating player-history evidence",
                    lambda: bulk_reader(cycle_targets, limit=40),
                )
                put_player_result_batch(existing_results)
                logger.info(
                    "player_history_collector_job: evidence cache hydrated "
                    "keys=%d rows=%d",
                    len(existing_results),
                    sum(len(rows) for rows in existing_results.values()),
                )
            except _CadenceBoundaryReached as exc:
                logger.warning(
                    "player_history_collector_job: cadence boundary reached during %s",
                    exc,
                )
                return
            except Exception as exc:
                logger.warning(
                    "player_history_collector_job: evidence cache hydration failed: %s",
                    exc,
                )

        # ── Fetch this run's bounded supported target slice ─────────────────
        ok = 0
        rows = 0
        completed_results: list[object] = []
        bulk_writer = getattr(db, "upsert_player_results_bulk", None)
        failed = 0
        processed = 0
        target_counts: dict[str, int] = {}
        ok_by_sport: dict[str, int] = {}
        rows_by_sport: dict[str, int] = {}
        budget_exhausted = False
        fetch_semaphore = asyncio.Semaphore(_PLAYER_HISTORY_FETCH_CONCURRENCY)
        completed_targets: list[tuple[str, str, str]] = []

        def advance_target(target: tuple[str, str, str]) -> None:
            """Move rotation only after a target has no unpersisted rows."""
            if not targets:
                return
            try:
                current_index = rotation_indexes[target]
                bot_data[_CURSOR_KEY] = (current_index + 1) % len(targets)
                bot_data[_NEXT_TARGET_KEY] = (
                    target_rotation[(current_index + 1) % len(targets)]
                )
            except (KeyError, IndexError):
                pass

        async def fetch_target(player: str, sport: str, stat: str):
            async with fetch_semaphore:
                try:
                    # All provider work belongs to this invocation. PropLine
                    # resolved-history lookups remain on their own cadence.
                    raw = await provider.fetch_results(player, sport, stat)
                    return "ok", raw or []
                except Exception as exc:
                    return "error", exc

        # A slow provider cannot make this job serially consume the next
        # scheduler interval: all target calls share one absolute deadline.
        fetch_results: list[tuple[str, object]] = []
        if cycle_targets and time.monotonic() < cycle_deadline:
            fetch_results = list(
                await asyncio.gather(
                    *(
                        fetch_target(player, sport, stat)
                        for player, sport, stat in cycle_targets
                    )
                )
            )
        elif cycle_targets:
            budget_exhausted = True

        for (player, sport, stat), (fetch_status, fetch_value) in zip(
            cycle_targets, fetch_results
        ):
            target_counts[sport] = target_counts.get(sport, 0) + 1
            processed += 1
            if fetch_status == "error":
                failed += 1
                logger.debug(
                    "player_history_collector_job: %s/%s/%s failed: %s",
                    player, sport, stat, fetch_value,
                )
                advance_target((player, sport, stat))
                continue

            raw = fetch_value
            if raw:
                from engine.evidence_memory import put_player_results

                put_player_results(player, sport, stat, raw)
                completed_results.extend(raw)
                ok += 1
                rows += len(raw)
                ok_by_sport[sport] = ok_by_sport.get(sport, 0) + 1
                rows_by_sport[sport] = rows_by_sport.get(sport, 0) + len(raw)
                completed_targets.append((player, sport, stat))
            else:
                advance_target((player, sport, stat))

        async def write_batch(batch: list[object]) -> None:
            """Persist one completed result batch within this cycle."""
            if asyncio.iscoroutinefunction(bulk_writer):
                await await_in_cycle(
                    "persisting player-history results",
                    lambda: bulk_writer(batch),
                )
            else:
                # Compatibility path for lightweight database doubles and
                # older database implementations.
                for result in batch:
                    await await_in_cycle(
                        "persisting player-history result",
                        lambda result=result: db.upsert_player_result(result),
                    )
            await asyncio.sleep(0)

        persisted_rows = 0
        for start in range(
            0, len(completed_results), _PLAYER_HISTORY_WRITE_BATCH_SIZE
        ):
            batch = completed_results[start: start + _PLAYER_HISTORY_WRITE_BATCH_SIZE]
            try:
                await write_batch(batch)
                persisted_rows += len(batch)
            except _CadenceBoundaryReached as exc:
                budget_exhausted = True
                logger.warning(
                    "player_history_collector_job: SQLite writer busy or blocked; "
                    "cadence boundary reached during %s; completed_rows=%d "
                    "unpersisted_rows=%d",
                    exc,
                    persisted_rows,
                    len(completed_results) - persisted_rows,
                )
                break
            except Exception as exc:
                logger.warning(
                    "player_history_collector_job: result persistence failed "
                    "after completed_rows=%d: %s",
                    persisted_rows,
                    exc,
                )
                break
        else:
            # A target with returned rows advances only after every result
            # batch from this invocation has been committed successfully.
            for target in completed_targets:
                advance_target(target)

        logger.info(
            "player_history_collector_job: done players_ok=%d rows=%d "
            "persisted_rows=%d processed=%d remaining=%d errors=%d "
            "budget_exhausted=%s targets_total=%d elapsed=%.1fs "
            "targets_by_sport=%s "
            "players_ok_by_sport=%s rows_by_sport=%s",
            ok, rows, persisted_rows, processed, len(targets) - processed,
            failed, budget_exhausted, len(targets),
            time.monotonic() - cycle_started, target_counts,
            ok_by_sport, rows_by_sport,
        )
    except Exception as exc:
        logger.exception("player_history_collector_job: %s", exc)
