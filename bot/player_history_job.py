"""Background job: collect real player game results for supported sports."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from types import SimpleNamespace

from engine.sport_policy import canonical_sport_key, is_football_related_sport

logger = logging.getLogger(__name__)

# Sports in the supported player-prop universe.  Several of these sports do
# not have a public PlayerStatsProvider path; they remain here because the
# scheduled PropLine supplement is their documented history path.
_SUPPORTED_HISTORY_SPORTS = frozenset({
    "MLB", "NBA", "NFL", "NCAAF", "CFB",
    "WNBA", "NHL", "CS", "DOTA", "TENNIS",
    "MLS", "NCAAB", "FIFA",
    "SOCCER",         # Requires FOOTBALL_DATA_API_KEY; returns [] gracefully without it
    "LOL", "VAL", "VALORANT",
    "TT", "TABLE TENNIS", "BADMINTON",
    "MMA", "UFC", "GOLF", "PGA",
})

# Keep transactions bounded below SQLite's variable/statement limits while
# making a large history pass use far fewer commits than one-row writes.
# Keep history persistence bulk-oriented: one upsert transaction handles a
# substantial result set while the bounded loop still commits progress instead
# of holding the database for the entire full-book refresh.
_PLAYER_HISTORY_WRITE_BATCH_SIZE = 1000
_PLAYER_HISTORY_WRITE_YIELD_SECONDS = 0.02
# Fetch cadence protects the provider side of this job. Once results exist in
# memory, persistence is a durable evidence obligation and must be allowed to
# outlive that cadence. Same-name scheduler coalescing prevents another
# collector from overlapping this write drain.
_PLAYER_HISTORY_PERSIST_TIMEOUT_SECONDS = 900.0
# Process the complete active book in one invocation without opening one
# request per target at once. These workers belong to this invocation; there
# is no carryover queue or deferred target list.
_PLAYER_HISTORY_FETCH_CONCURRENCY = 256
# Do not let a busy SQLite writer consume the whole cadence. Provider calls
# use the providers' own request timeouts and all selected targets are awaited;
# target data is never intentionally dropped by this collector.
_PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS = 5.0

# Retained as a compatibility/configuration value for callers that import it.
# It is deliberately not used as a selection limit: every active supported
# target is fetched on every cadence. The default is sized above the current
# approximately 5,200-target Underdog book.
try:
    from config import config as _config

    _API_CALL_TARGET = max(
        5_200, int(getattr(_config, "PLAYER_HISTORY_BATCH_SIZE", 6_000))
    )
except (ImportError, TypeError, ValueError):
    # Keep this module usable in focused tests and older integrations that do
    # not load the application configuration module.
    _API_CALL_TARGET = 5_200

_NEXT_TARGET_KEY = "_player_history_next_target"
_CURSOR_KEY = "_player_history_cursor"
_PROPLINE_CURSOR_KEY = "_player_history_propline_cursor"
_PRIMARY_HISTORY_PROVIDERS = frozenset({"underdog", "prizepicks"})
_PLAYER_PROP_PROVIDERS = (
    "Underdog",
    "PrizePicks",
    "DraftKings",
    "FanDuel",
)
# PropLine's Pro allowance is shared by every authenticated endpoint and
# client process.  The collector owns the history-target portion of that
# allowance; the client limiter is the final admission gate for all endpoint
# kinds.  At the default five-minute cadence this selects a quota-sized target
# batch per cycle, rather than the old one-target throttle.
_PROPLINE_REQUESTS_PER_HOUR = 1_020
_PLAYER_HISTORY_PROPLINE_TARGETS_PER_CYCLE = max(
    1,
    int(
        _PROPLINE_REQUESTS_PER_HOUR
        * max(
            1.0,
            float(getattr(_config, "PLAYER_HISTORY_POLL_INTERVAL", 300))
            - max(
                0.0,
                float(
                    getattr(
                        _config,
                        "PLAYER_HISTORY_CADENCE_HEADROOM_SECONDS",
                        15,
                    )
                ),
            ),
        )
        / 3600.0
    ),
)


def _uses_propline_history(sport: str) -> bool:
    """Reserve PropLine history calls for non-football sports."""
    canonical = canonical_sport_key(sport)
    return not is_football_related_sport(canonical)


def _history_sport_order(sport: str) -> tuple[int, str]:
    """Prioritize non-baseball evidence without dropping MLB progress."""
    normalized = str(sport or "").strip().upper()
    return (1 if normalized in {"MLB", "BASEBALL"} else 0, normalized)


class _CadenceBoundaryReached(Exception):
    """Internal signal that this collector invocation must stop now."""


def _player_history_cadence_budget() -> float:
    """Return the fetch budget while leaving room before the next scheduler tick."""
    interval = max(
        1.0,
        float(getattr(_config, "PLAYER_HISTORY_POLL_INTERVAL", 300)),
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
        for sport in sorted(tuple(by_sport), key=_history_sport_order):
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
        bot_data.pop(_PROPLINE_CURSOR_KEY, None)


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

    stable_targets = sorted(
        targets,
        key=lambda target: (_history_sport_order(target[1]), target[0], target[2]),
    )
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
    """Fetch real game results for every active supported primary prop.

    The active target universe is rebuilt on every cadence and the public/stat
    provider is fetched in one bounded-concurrency batch. There is intentionally
    no primary-provider rotation or carryover queue: a prop that is active now
    is eligible now, and newly active props do not wait for a later rotation.
    The smaller PropLine supplement window rotates independently so its shared
    limiter cannot turn a full-book history pass into a request burst.
    This is an independent storage lane: player monitoring neither starts,
    stops, pauses, nor reports this work.
    """
    bot_data = context.bot_data
    db = bot_data.get("history_db") or bot_data.get("db")
    if not db:
        logger.warning("player_history_collector_job: db not ready")
        return
    logger.info(
        "player_history_collector_job: independent full-book collection starting "
        "target_floor=%d cadence_budget=%.1fs",
        _API_CALL_TARGET,
        _player_history_cadence_budget(),
    )
    try:
        cycle_started = time.monotonic()
        cadence_budget = _player_history_cadence_budget()
        cycle_deadline = cycle_started + cadence_budget

        async def await_in_cycle(
            operation: str,
            awaitable_factory,
            *,
            operation_timeout: float | None = _PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS,
        ):
            """Await one operation without allowing it past this cycle's deadline."""
            remaining = cycle_deadline - time.monotonic()
            if remaining <= 0:
                raise _CadenceBoundaryReached(operation)
            try:
                timeout = remaining
                if operation_timeout is not None:
                    timeout = min(timeout, operation_timeout)
                return await asyncio.wait_for(
                    awaitable_factory(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise _CadenceBoundaryReached(operation) from exc

        from providers.player_stats import PlayerStatsProvider

        provider = PlayerStatsProvider()
        targets: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        target_sources: dict[tuple[str, str, str], set[str]] = {}

        def source_for_row(row, key, default: str) -> str:
            source = (
                getattr(row, "provider", None)
                or getattr(row, "source_provider", None)
                or ""
            )
            if not source and isinstance(key, (tuple, list)) and key:
                candidate = str(key[0] or "").strip()
                if candidate.lower() in _PRIMARY_HISTORY_PROVIDERS:
                    source = candidate
            normalized = str(source or default).strip().lower()
            return (
                {
                    "underdog": "Underdog",
                    "prizepicks": "PrizePicks",
                    "draftkings": "DraftKings",
                    "fanduel": "FanDuel",
                }.get(normalized, str(source or default).strip() or default)
            )

        def source_scope(target: tuple[str, str, str]) -> str:
            sources = target_sources.get(target, set())
            if sources == {"PrizePicks"}:
                return "PrizePicks"
            if sources == {"Underdog"}:
                return "Underdog"
            # A target shared by both books is intentionally requested without
            # a bookmaker filter so one read-only response covers both.
            return ""

        # ── Primary path: canonical active pool (both primary providers) ──
        try:
            _generic_active = getattr(
                db, "get_all_active_prop_line_history_by_line", None
            )
            if asyncio.iscoroutinefunction(_generic_active):
                active = await await_in_cycle(
                    "loading active player-history targets",
                    lambda: _generic_active(
                        providers=_PLAYER_PROP_PROVIDERS
                    ),
                )
                active_rows = active.items()
            else:
                active = await await_in_cycle(
                    "loading active player-history targets",
                    db.get_active_underdog_snapshot_per_prop,
                )
                active_rows = (
                    active.items()
                    if hasattr(active, "items")
                    else ((None, row) for row in active)
                )
            for active_key, s in active_rows:
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
                sport = canonical_sport_key(getattr(s, "sport", None) or "UNKNOWN")
                key = (player.strip(), sport, (stat or "").lower().strip())
                target_sources.setdefault(
                    key,
                    set(),
                ).add(source_for_row(s, active_key, "Underdog"))
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
                    plh: list[tuple[str, object]] = []
                    for _provider in _PLAYER_PROP_PROVIDERS:
                        provider_rows = await await_in_cycle(
                            f"loading {_provider} player-history targets",
                            lambda provider_name=_provider: (
                                db.get_latest_props_for_provider(
                                    provider_name, since_hours=48
                                )
                            ),
                            )
                        plh.extend(
                            (_provider, row) for row in (provider_rows or [])
                        )
                else:
                    provider_rows = await await_in_cycle(
                        "loading Underdog player-history targets",
                        lambda: db.get_latest_props_for_provider(
                            "Underdog", since_hours=48
                        ),
                    )
                    plh = [("Underdog", row) for row in (provider_rows or [])]
                for _provider, s in plh:
                    sport = canonical_sport_key(getattr(s, "sport", None) or "UNKNOWN")
                    key = (
                        (s.player_name or "").strip(),
                        sport,
                        (s.stat_type or "").lower().strip(),
                    )
                    target_sources.setdefault(key, set()).add(
                        _provider
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
            "round_robin_sports=%s mode=independent_full_collection "
            "target_floor=%d",
            len(targets),
            sorted({sport for _, sport, _ in targets}),
            _API_CALL_TARGET,
        )

        # Stable ordering keeps request/load distribution reproducible, but
        # there is no slice limit: every active target is attempted this run.
        cycle_targets = _fair_cycle_targets(
            sorted(
                targets,
                key=lambda target: (
                    _history_sport_order(target[1]), target[0], target[2]
                ),
            ),
            len(targets),
        )
        # ── Fetch this run's complete supported target set ──────────────────
        ok = 0
        rows = 0
        completed_results: list[object] = []
        bulk_writer = getattr(db, "upsert_player_results_bulk", None)
        failed = 0
        processed = 0
        target_counts: dict[str, int] = {}
        ok_by_sport: dict[str, int] = {}
        rows_by_sport: dict[str, int] = {}
        no_result_by_sport: dict[str, int] = {}
        propline_attempts_by_sport: dict[str, int] = {}
        propline_coverage_by_sport: dict[str, dict[str, int]] = {}
        propline_attempts_by_sport_market: dict[str, dict[str, int]] = {}
        propline_coverage_by_sport_market: dict[
            str, dict[str, dict[str, int]]
        ] = {}
        propline_cursor_advanced = False
        budget_exhausted = False
        fetch_semaphore = asyncio.Semaphore(_PLAYER_HISTORY_FETCH_CONCURRENCY)
        completed_targets: list[tuple[str, str, str]] = []
        from providers.player_history import get_player_history_provider

        configured_history_provider = bot_data.get("player_history_provider")
        history_provider = (
            configured_history_provider or get_player_history_provider()
        )

        def propline_market_for(
            sport: str,
            stat: str,
            coverage: object = None,
        ) -> str:
            reported_market = str(
                getattr(coverage, "market", "") or ""
            ).strip()
            if reported_market:
                return reported_market
            try:
                from providers.propline import official_player_market_for

                return (
                    official_player_market_for(stat, sport=sport)
                    or "unmapped"
                )
            except Exception:
                return "unmapped"

        propline_eligible_targets = [
            target
            for target in cycle_targets
            if _uses_propline_history(target[1])
        ]
        if propline_eligible_targets:
            try:
                propline_start = (
                    int(bot_data.get(_PROPLINE_CURSOR_KEY, 0))
                    % len(propline_eligible_targets)
                )
            except (TypeError, ValueError):
                propline_start = 0
            propline_count = min(
                _PLAYER_HISTORY_PROPLINE_TARGETS_PER_CYCLE,
                len(propline_eligible_targets),
            )
            propline_targets = {
                propline_eligible_targets[
                    (propline_start + offset) % len(propline_eligible_targets)
                ]
                for offset in range(propline_count)
            }
            propline_cursor_next = (
                propline_start + propline_count
            ) % len(propline_eligible_targets)
            for _player, _sport, _stat in propline_targets:
                propline_attempts_by_sport[_sport] = (
                    propline_attempts_by_sport.get(_sport, 0) + 1
                )
                market = propline_market_for(_sport, _stat)
                sport_attempts = propline_attempts_by_sport_market.setdefault(
                    _sport, {}
                )
                sport_attempts[market] = sport_attempts.get(market, 0) + 1
        else:
            propline_targets = set()

        async def fetch_target(player: str, sport: str, stat: str):
            async with fetch_semaphore:
                try:
                    # All provider work belongs to this invocation. PropLine
                    # resolved-history lookups remain on their own cadence.
                    remaining = cycle_deadline - time.monotonic()
                    if remaining <= 0:
                        return "deadline", None
                    raw = await asyncio.wait_for(
                        provider.fetch_results(player, sport, stat),
                        timeout=min(
                            _PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS,
                            remaining,
                        ),
                    )
                    if configured_history_provider is None:
                        # Direct/unit-test callers do not have the production
                        # shared history provider in bot_data. Do not make
                        # them perform an unbounded supplemental network read;
                        # the primary provider result is already valid evidence.
                        coverage = SimpleNamespace(
                            rows=list(raw or []),
                            status="usable" if raw else "",
                        )
                    else:
                        remaining = cycle_deadline - time.monotonic()
                        if remaining <= 0:
                            return "deadline", None
                        # PropLine requests are admitted by the shared
                        # rolling-hour limiter.  They may wait for their
                        # quota slot; the invocation-level deadline, rather
                        # than the five-second public-provider timeout,
                        # bounds the complete batch.
                        scheduled_timeout = (
                            remaining
                            if (player, sport, stat) in propline_targets
                            else min(
                                _PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS,
                                remaining,
                            )
                        )
                        coverage = await asyncio.wait_for(
                            history_provider.fetch_scheduled_results(
                                raw or [],
                                player,
                                sport,
                                stat,
                                source_provider=source_scope((player, sport, stat)),
                                allow_propline=(
                                    (player, sport, stat) in propline_targets
                                ),
                                force_propline=(
                                    (player, sport, stat) in propline_targets
                                ),
                            ),
                            timeout=scheduled_timeout,
                        )
                    return "ok", coverage
                except asyncio.TimeoutError:
                    return "deadline", None
                except Exception as exc:
                    return "error", exc

        # A slow provider cannot make this job serially consume the next
        # scheduler interval: target calls run in bounded batches under one
        # absolute deadline. Creating one task for every active prop at once
        # caused excessive memory use and made cancellation unreliable.
        fetch_results: list[tuple[str, object]] = []
        for batch_start in range(
            0,
            len(cycle_targets),
            _PLAYER_HISTORY_FETCH_CONCURRENCY,
        ):
            remaining = cycle_deadline - time.monotonic()
            if remaining <= 0:
                budget_exhausted = True
                break
            batch = cycle_targets[
                batch_start: batch_start + _PLAYER_HISTORY_FETCH_CONCURRENCY
            ]
            tasks = [
                asyncio.create_task(fetch_target(player, sport, stat))
                for player, sport, stat in batch
            ]
            try:
                batch_results = await asyncio.wait_for(
                    asyncio.gather(*tasks),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                budget_exhausted = True
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                fetch_results.extend(
                    [("deadline", None)] * len(batch)
                )
                break
            fetch_results.extend(batch_results)

        deadline_targets = 0
        for (player, sport, stat), (fetch_status, fetch_value) in zip(
            cycle_targets, fetch_results
        ):
            target_counts[sport] = target_counts.get(sport, 0) + 1
            processed += 1
            if fetch_status == "deadline":
                budget_exhausted = True
                deadline_targets += 1
                logger.debug(
                    "player_history_collector_job: cadence deadline reached "
                    "for %s/%s/%s",
                    player, sport, stat,
                )
                continue
            if fetch_status == "error":
                failed += 1
                logger.debug(
                    "player_history_collector_job: %s/%s/%s failed: %s",
                    player, sport, stat, fetch_value,
                )
                continue

            if (player, sport, stat) in propline_targets:
                propline_cursor_advanced = True
            coverage = fetch_value
            raw = getattr(coverage, "rows", coverage)
            coverage_status = getattr(coverage, "status", "")
            if (player, sport, stat) in propline_targets:
                market = propline_market_for(sport, stat, coverage)
                sport_markets = propline_coverage_by_sport_market.setdefault(
                    sport, {}
                )
                market_coverage = sport_markets.setdefault(market, {})
                if coverage_status in {
                    "usable",
                    "redacted",
                    "unavailable",
                    "malformed",
                }:
                    market_coverage[coverage_status] = (
                        market_coverage.get(coverage_status, 0) + 1
                    )
            if (player, sport, stat) in propline_targets and coverage_status == (
                "not_needed"
            ):
                sport_coverage = propline_coverage_by_sport.setdefault(sport, {})
                sport_coverage["not_needed"] = (
                    sport_coverage.get("not_needed", 0) + 1
                )
            if coverage_status in {
                "usable",
                "redacted",
                "unavailable",
                "malformed",
            }:
                sport_coverage = propline_coverage_by_sport.setdefault(sport, {})
                sport_coverage[coverage_status] = (
                    sport_coverage.get(coverage_status, 0) + 1
                )
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
                # An empty provider response is not a successful evidence
                # collection. Keep it separate from transport errors so
                # health/report consumers can see coverage gaps by sport.
                no_result_by_sport[sport] = (
                    no_result_by_sport.get(sport, 0) + 1
                )

        # Keep the report shape stable even when a provider returns no rows,
        # a sport is out of season, or the cadence budget is exhausted.  A
        # missing key used to make configured sports disappear from Telegram
        # reports, which looked like those sports were not being attempted.
        report_sports = {
            str(sport).upper()
            for sport in (
                set(target_counts)
                | {sport for _player, sport, _stat in cycle_targets}
            )
        }
        for sport in report_sports:
            target_counts.setdefault(sport, 0)
            ok_by_sport.setdefault(sport, 0)
            rows_by_sport.setdefault(sport, 0)
            no_result_by_sport.setdefault(sport, 0)
            propline_coverage_by_sport.setdefault(sport, {})

        # A timed-out PropLine request must remain the next target. Advancing
        # the cursor before the request would otherwise lose a target for a
        # full rotation of the active book (many hours) whenever the shared
        # endpoint is still inside its rolling interval.
        if cycle_targets:
            if propline_cursor_advanced:
                bot_data[_PROPLINE_CURSOR_KEY] = propline_cursor_next
            else:
                bot_data[_PROPLINE_CURSOR_KEY] = propline_start

        async def write_batch(batch: list[object]) -> None:
            """Persist one completed result batch without the fetch deadline."""
            # Provider collection stays independent so it cannot occupy the
            # Underdog scheduler slot for minutes. Before claiming SQLite for
            # a durable result batch, yield while the live Underdog scan owns
            # its full-feed persistence window.
            try:
                from market_engine import wait_for_underdog_persistence_idle

                await wait_for_underdog_persistence_idle()
            except ImportError:
                # Lightweight compatibility environments may not load the
                # monitoring engine; their database doubles remain valid.
                pass
            if asyncio.iscoroutinefunction(bulk_writer):
                await bulk_writer(batch)
            else:
                # Compatibility path for lightweight database doubles and
                # older database implementations.
                for result in batch:
                    await db.upsert_player_result(result)
            await asyncio.sleep(0)

        persisted_rows = 0
        persist_deadline = time.monotonic() + _PLAYER_HISTORY_PERSIST_TIMEOUT_SECONDS
        _acquire_history_slot = None
        _release_history_slot = None
        try:
            from market_engine import (
                acquire_player_history_persistence_slot,
                release_player_history_persistence_slot,
            )

            _acquire_history_slot = acquire_player_history_persistence_slot
            _release_history_slot = release_player_history_persistence_slot
        except ImportError:
            # Lightweight compatibility environments may not load the
            # monitoring engine; their database doubles remain valid.
            pass

        for start in range(
            0, len(completed_results), _PLAYER_HISTORY_WRITE_BATCH_SIZE
        ):
            batch = completed_results[
                start: start + _PLAYER_HISTORY_WRITE_BATCH_SIZE
            ]
            _history_slot_acquired = False
            try:
                if _acquire_history_slot is not None:
                    await _acquire_history_slot()
                    _history_slot_acquired = True
                try:
                    remaining = persist_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    await asyncio.wait_for(write_batch(batch), timeout=remaining)
                    persisted_rows += len(batch)
                    # Keep each transaction short and release the window
                    # between batches so the two-minute FPR can run promptly.
                    await asyncio.sleep(_PLAYER_HISTORY_WRITE_YIELD_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning(
                        "player_history_collector_job: persistence timeout; "
                        "completed_rows=%d "
                        "unpersisted_rows=%d",
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
            finally:
                if _history_slot_acquired and _release_history_slot is not None:
                    _release_history_slot()
        no_result_targets = sum(no_result_by_sport.values())
        if no_result_targets:
            logger.warning(
                "player_history_collector_job: provider coverage gap "
                "empty_results=%d by_sport=%s",
                no_result_targets,
                no_result_by_sport,
            )
        logger.info(
            "player_history_collector_job: done players_ok=%d rows=%d "
            "persisted_rows=%d targets_processed=%d rows_processed=%d "
            "remaining_targets=%d processed=%d remaining=%d errors=%d "
            "deadline_targets=%d empty_results=%d budget_exhausted=%s "
            "targets_total=%d elapsed=%.1fs "
            "targets_by_sport=%s "
            "players_ok_by_sport=%s rows_by_sport=%s "
            "no_result_by_sport=%s "
             "propline_attempts_by_sport=%s "
            "propline_coverage_by_sport=%s "
            "propline_attempts_by_sport_market=%s "
            "propline_coverage_by_sport_market=%s",
            ok, rows, persisted_rows, processed, rows,
            len(cycle_targets) - processed, processed,
            len(cycle_targets) - processed, failed, deadline_targets,
            no_result_targets, budget_exhausted, len(cycle_targets),
            time.monotonic() - cycle_started, target_counts,
            ok_by_sport, rows_by_sport, no_result_by_sport,
            propline_attempts_by_sport,
            propline_coverage_by_sport, propline_attempts_by_sport_market,
            propline_coverage_by_sport_market,
        )
    except Exception as exc:
        logger.exception("player_history_collector_job: %s", exc)
