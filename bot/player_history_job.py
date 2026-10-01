"""Background job: collect real player game results for supported sports."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import re
import time
from datetime import datetime, timedelta
from dataclasses import asdict, is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from engine.sport_policy import (
    canonical_sport_key,
    is_football_related_sport,
)

logger = logging.getLogger(__name__)

# Sports in the supported player-prop universe. Every configured default must
# have either a public PlayerStatsProvider route or a mapped scheduled
# PropLine history route.
_SUPPORTED_HISTORY_SPORTS = frozenset({
    "MLB", "NBA", "NFL", "NCAAF", "CFB",
    "WNBA", "NHL", "CS", "DOTA", "TENNIS",
    "MLS", "NCAAB", "FIFA", "SOCCER",
    "EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL",
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
# Default bound for cycle-local operations not given a more specific timeout.
_PLAYER_HISTORY_OPERATION_TIMEOUT_SECONDS = 5.0
# Sport providers have request timeouts between 6 and 12 seconds. Give one
# player/stat target enough time to complete its provider route, while the
# collector's absolute cadence deadline remains the hard upper bound.
_PLAYER_HISTORY_PROVIDER_TIMEOUT_SECONDS = 12.0
# Active-target discovery reads the shared SQLite database while full-feed
# persistence may be draining. Give that read its own bounded window; provider
# calls use the separate per-target timeout above.
_PLAYER_HISTORY_TARGET_DISCOVERY_TIMEOUT_SECONDS = 30.0

# Retained for the legacy rotating-target helper and callers that import it.
# The active collector below does not use this value to limit targets or rows:
# it fetches every active supported target and persists every row returned.
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
_PLAYER_PROP_PROVIDERS = tuple(
    provider
    for provider, enabled in (
        ("Underdog", True),
        ("PrizePicks", True),
        ("DraftKings", bool(getattr(_config, "DRAFTKINGS_ENABLED", False))),
        ("FanDuel", bool(getattr(_config, "FANDUEL_ENABLED", False))),
    )
    if enabled
)
_PRIORITY_TARGETS_KEY = "_player_history_priority_targets"
# PropLine's Pro allowance is shared by every authenticated endpoint and
# client process.  The collector owns the history-target portion of that
# allowance; the client limiter is the final admission gate for all endpoint
# kinds.  At the default three-minute cadence this selects a quota-sized target
# batch per cycle, rather than the old one-target throttle.
_PROPLINE_REQUESTS_PER_HOUR = 1_020
_PLAYER_HISTORY_PROPLINE_TARGETS_PER_CYCLE = max(
    1,
    int(
        _PROPLINE_REQUESTS_PER_HOUR
        * max(
            1.0,
            float(getattr(_config, "PLAYER_HISTORY_POLL_INTERVAL", 180))
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
    """Use PropLine history for every sport in the supported player universe."""
    canonical = canonical_sport_key(sport)
    return canonical in _SUPPORTED_HISTORY_SPORTS


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
        float(getattr(_config, "PLAYER_HISTORY_POLL_INTERVAL", 180)),
    )
    headroom = max(
        0.0,
        float(getattr(_config, "PLAYER_HISTORY_CADENCE_HEADROOM_SECONDS", 15)),
    )
    return max(1.0, interval - headroom)


def _propline_value(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            candidate = value.get(name)
            if candidate not in (None, ""):
                return candidate
    for name in names:
        candidate = getattr(value, name, None)
        if candidate not in (None, ""):
            return candidate
    return None


def _is_explicit_propline_final(value: Any) -> bool:
    status = str(
        _propline_value(value, "status", "state", "game_status") or ""
    ).strip().lower().replace("-", "_")
    return status in {
        "final", "completed", "complete", "settled", "finished", "ended",
    } or status.startswith("status_final")


def _propline_game_records(payload: Any) -> list[Any]:
    while isinstance(payload, dict):
        nested = next(
            (
                payload[key]
                for key in ("games", "data", "results", "items")
                if key in payload
            ),
            None,
        )
        if nested is None or nested is payload:
            break
        payload = nested
    if isinstance(payload, (list, tuple)):
        return list(payload)
    if isinstance(payload, dict) and _propline_value(
        payload, "status", "state", "game_status"
    ) is not None:
        return [payload]
    return []


def _filter_completed_propline_game_payload(payload: Any) -> Any:
    """Keep only explicitly final player-game rows in persisted raw payloads."""
    if isinstance(payload, dict):
        for key in ("games", "data", "results", "items"):
            if key not in payload:
                continue
            filtered = _filter_completed_propline_game_payload(payload[key])
            if filtered is None:
                return None
            result = deepcopy(payload)
            result[key] = filtered
            return result
        return deepcopy(payload) if _is_explicit_propline_final(payload) else None
    if isinstance(payload, (list, tuple)):
        rows = [
            deepcopy(item)
            for item in payload
            if _is_explicit_propline_final(item)
        ]
        return rows or None
    return None


def _completed_propline_game_rows(
    target: Any,
    result: Any,
) -> list[Any]:
    """Convert final, identified player-game stats into shared history rows."""
    import math

    if is_football_related_sport(
        getattr(target, "canonical_sport", "") or getattr(target, "official_sport", "")
    ):
        return []

    from providers.player_stats import RawGameResult
    from providers.propline import (
        _normalize_stat,
        _parse_datetime,
        _propline_raw_player_game_stat_matches,
        _team_names_match,
    )

    rows_by_identity: dict[tuple[str, str, str, str, str], Any] = {}
    conflicting_identities: set[tuple[str, str, str, str, str]] = set()
    duplicates = 0
    target_stat = _normalize_stat(target.market)
    for game in _propline_game_records(getattr(result, "payload", None)):
        if not _is_explicit_propline_final(game):
            continue
        declared_sport = _propline_value(
            game, "canonical_sport", "sport", "sport_key", "league"
        )
        if declared_sport:
            declared = str(declared_sport).strip()
            expected_canonical = str(
                getattr(target, "canonical_sport", "") or ""
            ).strip().casefold()
            expected_official = str(
                getattr(target, "official_sport", "") or ""
            ).strip().casefold()
            if (
                declared.casefold() not in {expected_canonical, expected_official}
                and canonical_sport_key(declared).casefold()
                != expected_canonical
            ):
                continue
        declared_player = _propline_value(
            game, "player_name", "athlete_name", "player"
        )
        if isinstance(declared_player, dict):
            declared_player = _propline_value(
                declared_player, "name", "full_name", "display_name"
            )
        if declared_player:
            expected_player = " ".join(
                str(getattr(target, "player_name", "") or "").casefold().split()
            )
            returned_player = " ".join(str(declared_player).casefold().split())
            if not expected_player or returned_player != expected_player:
                continue

        game_id = _propline_value(game, "event_id", "game_id", "id")
        game_date = _parse_datetime(
            _propline_value(
                game, "game_date", "date", "start_time", "commence_time"
            )
        )
        stats = _propline_value(game, "stats", "statistics", "player_stats")
        actual = None
        if isinstance(stats, dict):
            for stat_name, stat_value in stats.items():
                if not _propline_raw_player_game_stat_matches(
                    target.canonical_sport, target_stat, stat_name
                ):
                    continue
                if isinstance(stat_value, dict):
                    actual = _propline_value(
                        stat_value, "actual_value", "value", "stat_value"
                    )
                else:
                    actual = stat_value
                break
        elif isinstance(stats, (list, tuple)):
            for stat_item in stats:
                stat_name = _propline_value(
                    stat_item, "stat_type", "stat", "name", "market"
                )
                if not _propline_raw_player_game_stat_matches(
                    target.canonical_sport, target_stat, stat_name
                ):
                    continue
                actual = _propline_value(
                    stat_item, "actual_value", "value", "stat_value"
                )
                break
        if actual is None:
            declared_stat = _propline_value(
                game, "stat_type", "stat", "market", "market_name"
            )
            if not declared_stat or not _propline_raw_player_game_stat_matches(
                target.canonical_sport, target_stat, declared_stat
            ):
                continue
            actual = _propline_value(
                game, "actual_value", "value", "stat_value"
            )
        if game_id is None or game_date is None or actual is None:
            continue
        if isinstance(actual, bool):
            continue
        try:
            actual_value = float(actual)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(actual_value):
            continue
        team = _propline_value(game, "team", "player_team", "team_name")
        opponent = _propline_value(game, "opponent", "opponent_name")
        home = _propline_value(game, "home_team", "home", "home_name")
        away = _propline_value(game, "away_team", "away", "away_name")
        if opponent is None and team and home and away:
            if _team_names_match(team, home):
                opponent = away
            elif _team_names_match(team, away):
                opponent = home

        row = RawGameResult(
            player_name=target.player_name,
            sport=target.canonical_sport,
            stat_type=target_stat,
            game_date=game_date.date().isoformat(),
            actual_value=actual_value,
            opponent=opponent,
            source="propline_player_games",
            game_id=str(game_id),
        )
        identity = (
            row.player_name.casefold().strip(),
            row.sport.upper().strip(),
            row.stat_type.casefold().strip(),
            row.game_date,
            str(row.game_id),
        )
        if identity in rows_by_identity:
            duplicates += 1
            if rows_by_identity[identity].actual_value != actual_value:
                conflicting_identities.add(identity)
            continue
        rows_by_identity[identity] = row
        logger.debug(
            "propline_player_game_result sport=%s league=%s player=%s "
            "player_id=%s event_id=%s market=%s value=%s team=%s opponent=%s",
            target.canonical_sport,
            getattr(target, "official_sport", ""),
            target.player_name,
            _propline_value(game, "player_id", "athlete_id", "participant_id") or "",
            game_id,
            target.market,
            actual_value,
            team or "",
            opponent or "",
        )
    for identity in conflicting_identities:
        rows_by_identity.pop(identity, None)
    if duplicates or conflicting_identities:
        logger.info(
            "propline_player_game_result sport=%s player=%s market=%s "
            "duplicate_skipped=%d ambiguous_conflicts_rejected=%d",
            target.canonical_sport,
            target.player_name,
            target.market,
            duplicates,
            len(conflicting_identities),
        )
    return list(rows_by_identity.values())


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
        bot_data.pop(_PRIORITY_TARGETS_KEY, None)


def queue_priority_history_targets(
    bot_data: dict | None,
    targets: "list[tuple[str, str, str]] | tuple[tuple[str, str, str], ...]",
) -> int:
    """Queue newly observed props for the next durable history collection.

    PropLine/PrizePicks observations are persisted before they are handed to
    evaluation.  Keeping a small in-process priority queue means a newly
    observed player is still collected when the active-universe read happened
    just before that feed was persisted.  The queue is only a scheduling hint;
    results are written by the normal history collector and never attributed
    to Underdog.
    """
    if not isinstance(bot_data, dict):
        return 0
    queued = bot_data.setdefault(_PRIORITY_TARGETS_KEY, set())
    if not isinstance(queued, set):
        queued = set(queued or ())
        bot_data[_PRIORITY_TARGETS_KEY] = queued
    added = 0
    for target in targets or ():
        if not isinstance(target, (tuple, list)) or len(target) != 3:
            continue
        player, sport, stat = (
            str(target[0] or "").strip(),
            canonical_sport_key(target[1]),
            str(target[2] or "").strip().lower(),
        )
        normalized = (player, sport, stat)
        if (
            player
            and sport not in {"", "UNKNOWN"}
            and stat
            and normalized not in queued
        ):
            queued.add(normalized)
            added += 1
    return added


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
        "result_row_cap=none legacy_target_slice_limit=%d cadence_budget=%.1fs",
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
                    operation_timeout=_PLAYER_HISTORY_TARGET_DISCOVERY_TIMEOUT_SECONDS,
                )
                active_rows = active.items()
            else:
                active = await await_in_cycle(
                    "loading active player-history targets",
                    db.get_active_underdog_snapshot_per_prop,
                    operation_timeout=_PLAYER_HISTORY_TARGET_DISCOVERY_TIMEOUT_SECONDS,
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
            logger.info(
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
                            operation_timeout=_PLAYER_HISTORY_TARGET_DISCOVERY_TIMEOUT_SECONDS,
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
                        operation_timeout=_PLAYER_HISTORY_TARGET_DISCOVERY_TIMEOUT_SECONDS,
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
                logger.info(
                    "player_history_collector_job: cadence boundary reached during %s",
                    exc,
                )
                return
            except Exception as exc:
                logger.warning(
                    "player_history_collector_job: prop_line_history fallback failed: %s",
                    exc,
                )

        # A PropLine feed can be persisted just after active-target discovery
        # starts.  Merge the ingestion handoff explicitly so newly observed
        # players still receive a history fetch in this collector invocation,
        # even when the current-day active query did not see them yet.
        priority_targets = tuple(
            bot_data.get(_PRIORITY_TARGETS_KEY, ()) or ()
        )
        for priority_target in priority_targets:
            if not isinstance(priority_target, (tuple, list)) or len(priority_target) != 3:
                continue
            key = (
                str(priority_target[0] or "").strip(),
                canonical_sport_key(priority_target[1]),
                str(priority_target[2] or "").strip().lower(),
            )
            if (
                not key[0]
                or key[1] in {"", "UNKNOWN"}
                or not key[2]
            ):
                continue
            target_sources.setdefault(key, set()).add("PrizePicks")
            if key not in seen:
                seen.add(key)
                targets.append(key)
        queued_targets = bot_data.get(_PRIORITY_TARGETS_KEY)
        if isinstance(queued_targets, set):
            queued_targets.difference_update(priority_targets)
            if not queued_targets:
                bot_data.pop(_PRIORITY_TARGETS_KEY, None)

        logger.info(
            "player_history_collector_job: provider_targets=%d "
            "round_robin_sports=%s mode=independent_full_collection "
            "result_row_cap=none legacy_target_slice_limit=%d",
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
        budget_exhausted = False
        deadline_targets = 0
        provider_timeout_targets = 0
        cycle_deadline_targets = 0
        provider_timeout_by_sport: dict[str, int] = {}
        cycle_deadline_by_sport: dict[str, int] = {}
        provider_timeout_examples: list[str] = []
        fetch_semaphore = asyncio.Semaphore(_PLAYER_HISTORY_FETCH_CONCURRENCY)
        completed_targets: list[tuple[str, str, str]] = []
        history_provider = bot_data.get("player_history_provider")

        async def fetch_dota_propline(
            player: str,
            stat: str,
            source_provider: str,
            remaining: float,
        ):
            """Use PropLine for DOTA only when its market is documented.

            OpenDota requires a resolvable public handle and is not the source
            of truth for PropLine observations.  PropLine's esports event
            stats path joins final events and player stats by explicit IDs,
            which covers feed names such as aliases. If PropLine has no exact
            market or usable rows, the caller falls back to OpenDota history.
            """
            from providers.propline import official_player_market_for

            # DOTA currently has no documented PropLine player-market mapping.
            # Do not spend a quota request on a guessed market; None tells the
            # caller to use the public OpenDota player-stats route instead.
            if not official_player_market_for(stat, sport="DOTA"):
                return None
            supplement = getattr(
                history_provider, "supplement_missing_results", None
            )
            if not asyncio.iscoroutinefunction(supplement):
                return None
            client = getattr(history_provider, "_propline_client", None)
            if client is None:
                try:
                    from providers.propline import PropLineClient

                    client = PropLineClient()
                    history_provider._propline_client = client
                except Exception:
                    return None
            if not bool(getattr(client, "is_configured", False)):
                return None
            market = str(stat or "").strip().lower() or "unknown"
            propline_attempts_by_sport["DOTA"] = (
                propline_attempts_by_sport.get("DOTA", 0) + 1
            )
            sport_markets = propline_attempts_by_sport_market.setdefault(
                "DOTA", {}
            )
            sport_markets[market] = sport_markets.get(market, 0) + 1
            try:
                rows = await asyncio.wait_for(
                    supplement(
                        db,
                        player,
                        "DOTA",
                        stat,
                        min_samples=5,
                        source_provider=source_provider or "Underdog",
                    ),
                    timeout=min(_PLAYER_HISTORY_PROVIDER_TIMEOUT_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                return "supplement_timeout", None
            except Exception as exc:
                logger.debug(
                    "player_history_collector_job: PropLine DOTA history "
                    "failed for %s/%s: %s",
                    player,
                    stat,
                    type(exc).__name__,
                )
                return "error", exc
            row_count = len(rows or ())
            sport_coverage = propline_coverage_by_sport.setdefault("DOTA", {})
            sport_coverage[market] = sport_coverage.get(market, 0) + row_count
            market_coverage = propline_coverage_by_sport_market.setdefault(
                "DOTA", {}
            ).setdefault(market, {})
            market_coverage["rows"] = market_coverage.get("rows", 0) + row_count
            if not row_count:
                # An empty/unsupported history response is not a successful
                # provider route. Let PlayerStatsProvider try OpenDota below.
                return None
            return "ok", SimpleNamespace(
                rows=list(rows or ()),
                status="usable" if rows else "empty",
                provider="PropLine",
            )

        async def fetch_target(player: str, sport: str, stat: str):
            async with fetch_semaphore:
                target_started = time.monotonic()
                operation_deadline = (
                    target_started + _PLAYER_HISTORY_PROVIDER_TIMEOUT_SECONDS
                )
                target_deadline = min(cycle_deadline, operation_deadline)
                timeout_status = (
                    "cycle_deadline"
                    if cycle_deadline <= operation_deadline
                    else "provider_timeout"
                )
                try:
                    # Bound the complete target route, including optional
                    # supplements, rather than granting each provider a fresh
                    # timeout. The cycle deadline always takes precedence.
                    remaining = target_deadline - time.monotonic()
                    if remaining <= 0:
                        return timeout_status, None
                    from providers.player_stats import validate_player_result_rows

                    if sport == "DOTA":
                        propline_result = await fetch_dota_propline(
                            player,
                            stat,
                            source_scope((player, sport, stat)),
                            remaining,
                        )
                        if propline_result is not None:
                            propline_status, propline_coverage = propline_result
                            propline_rows = validate_player_result_rows(
                                getattr(propline_coverage, "rows", None),
                                player_name=player,
                                sport=sport,
                                stat_type=stat,
                            )
                            if propline_status == "ok" and propline_rows:
                                return "ok", SimpleNamespace(
                                    rows=propline_rows,
                                    status="usable",
                                    provider="PropLine",
                                )
                            # A failed or empty supplement must not suppress
                            # the public DOTA route. A supplement timeout is
                            # terminal only if it consumed this target's budget.
                            if propline_status == "supplement_timeout":
                                remaining = target_deadline - time.monotonic()
                                if remaining <= 0:
                                    return timeout_status, None
                    remaining = target_deadline - time.monotonic()
                    if remaining <= 0:
                        return timeout_status, None
                    raw = await asyncio.wait_for(
                        provider.fetch_results(player, sport, stat),
                        timeout=remaining,
                    )
                    usable_rows = validate_player_result_rows(
                        raw,
                        player_name=player,
                        sport=sport,
                        stat_type=stat,
                    )
                    if len(usable_rows) != len(raw or []):
                        logger.debug(
                            "player_history_collector_job: rejected %d "
                            "unusable result row(s) for %s/%s/%s",
                            len(raw or []) - len(usable_rows),
                            player,
                            sport,
                            stat,
                        )
                    coverage = SimpleNamespace(
                        rows=usable_rows,
                        status="usable" if usable_rows else "",
                    )
                    return "ok", coverage
                except asyncio.TimeoutError:
                    return timeout_status, None
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
                batch_outcomes = await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )
                for outcome in batch_outcomes:
                    if (
                        isinstance(outcome, tuple)
                        and len(outcome) == 2
                        and isinstance(outcome[0], str)
                    ):
                        fetch_results.append(outcome)
                    else:
                        fetch_results.append(("cycle_deadline", None))
                break
            fetch_results.extend(batch_results)

        for (player, sport, stat), (fetch_status, fetch_value) in zip(
            cycle_targets, fetch_results
        ):
            target_counts[sport] = target_counts.get(sport, 0) + 1
            processed += 1
            if fetch_status in {"provider_timeout", "cycle_deadline"}:
                deadline_targets += 1
                if fetch_status == "provider_timeout":
                    provider_timeout_targets += 1
                    provider_timeout_by_sport[sport] = (
                        provider_timeout_by_sport.get(sport, 0) + 1
                    )
                    if len(provider_timeout_examples) < 10:
                        provider_timeout_examples.append(
                            f"{player}/{sport}/{stat}"
                        )
                else:
                    budget_exhausted = True
                    cycle_deadline_targets += 1
                    cycle_deadline_by_sport[sport] = (
                        cycle_deadline_by_sport.get(sport, 0) + 1
                    )
                logger.debug(
                    "player_history_collector_job: %s for %s/%s/%s",
                    fetch_status,
                    player,
                    sport,
                    stat,
                )
                continue
            if fetch_status == "error":
                failed += 1
                logger.debug(
                    "player_history_collector_job: %s/%s/%s failed: %s",
                    player, sport, stat, fetch_value,
                )
                continue

            coverage = fetch_value
            raw = getattr(coverage, "rows", coverage)
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
                    # between batches so the full-pool rotation can run promptly.
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
            logger.info(
                "player_history_collector_job: provider coverage gap "
                "empty_results=%d by_sport=%s",
                no_result_targets,
                no_result_by_sport,
            )
        logger.info(
            "player_history_collector_job: done players_ok=%d rows=%d "
            "persisted_rows=%d targets_processed=%d rows_processed=%d "
            "remaining_targets=%d processed=%d remaining=%d errors=%d "
            "deadline_targets=%d provider_timeout_targets=%d "
            "provider_timeout_by_sport=%s provider_timeout_examples=%s "
            "cycle_deadline_targets=%d "
            "cycle_deadline_by_sport=%s empty_results=%d budget_exhausted=%s "
            "result_row_cap=none targets_total=%d elapsed=%.1fs "
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
            provider_timeout_targets, provider_timeout_by_sport,
            provider_timeout_examples,
            cycle_deadline_targets, cycle_deadline_by_sport,
            no_result_targets, budget_exhausted, len(cycle_targets),
            time.monotonic() - cycle_started, target_counts,
            ok_by_sport, rows_by_sport, no_result_by_sport,
            propline_attempts_by_sport,
            propline_coverage_by_sport, propline_attempts_by_sport_market,
            propline_coverage_by_sport_market,
        )
    except Exception as exc:
        logger.exception("player_history_collector_job: %s", exc)


async def _persist_propline_player_history_rows(db, target, result) -> int:
    """Persist only explicitly settled entries from an already-fetched rotation result."""
    if is_football_related_sport(
        getattr(target, "canonical_sport", "") or getattr(target, "sport", "")
    ):
        logger.info(
            "propline_read_only_rotation_job: INSUFFICIENT_EVIDENCE "
            "sport=%s player=%s market=%s reason=football_excluded_from_propline",
            getattr(target, "canonical_sport", ""),
            getattr(target, "player_name", ""),
            getattr(target, "market", ""),
        )
        return 0
    if (
        str(getattr(target, "family", "") or "") != "player_history"
        or not bool(getattr(result, "succeeded", False))
        or not bool(getattr(result, "complete", False))
        or bool(getattr(result, "redacted", False))
    ):
        return 0

    from providers.player_history import propline_player_history_rows

    rows = propline_player_history_rows(
        getattr(result, "entries", ()) or (),
        player_name=getattr(target, "player_name", ""),
        sport=getattr(target, "canonical_sport", ""),
        stat_type=getattr(target, "market", ""),
    )
    if not rows:
        return 0

    bulk_writer = getattr(db, "upsert_player_results_bulk", None)
    if callable(bulk_writer):
        await bulk_writer(rows)
    else:
        for row in rows:
            await db.upsert_player_result(row)
    return len(rows)


async def propline_read_only_rotation_job(context) -> None:
    """Rotate supplemental PropLine endpoints without entering alert logic.

    Discovery and the endpoint calls share PropLine's persistent limiter.  The
    checkpoint lives beside that limiter, so a restart can repeat at most the
    last request rather than dropping a family or target.
    """
    bot_data = context.bot_data
    db = bot_data.get("history_db") or bot_data.get("db")
    if not db:
        logger.warning("propline_read_only_rotation_job: db not ready")
        return
    try:
        from providers.player_history import propline_result_rows
        from providers.propline import (
            PropLineClient,
            PropLineReadOnlyRotation,
            audit_propline_discovery,
            build_propline_read_only_targets,
        )

        client = bot_data.get("propline_read_only_client")
        if client is None:
            client = PropLineClient()
            bot_data["propline_read_only_client"] = client
        if not client.is_configured:
            logger.info("propline_read_only_rotation_job: PropLine disabled")
            return

        active_rows = []
        generic_active = getattr(
            db, "get_all_active_prop_line_history_by_line", None
        )
        if asyncio.iscoroutinefunction(generic_active):
            active = await generic_active(providers=_PLAYER_PROP_PROVIDERS)
            active_rows = list(active.values()) if hasattr(active, "values") else list(active or [])
        else:
            active = await db.get_active_underdog_snapshot_per_prop()
            active_rows = list(active.values()) if hasattr(active, "values") else list(active or [])

        def _source_value(value, *names):
            if isinstance(value, dict):
                for name in names:
                    candidate = value.get(name)
                    if candidate not in (None, ""):
                        return candidate
                return None
            for name in names:
                candidate = getattr(value, name, None)
                if candidate not in (None, ""):
                    return candidate
            return None

        active_before_football_filter = len(active_rows)
        active_rows = [
            row
            for row in active_rows
            if not is_football_related_sport(
                _source_value(row, "sport", "sport_key", "league")
            )
        ]
        excluded_football_active = (
            active_before_football_filter - len(active_rows)
        )

        async def cached_discovery(family: str, target_key: str, official: str):
            reader = getattr(db, "get_propline_evidence", None)
            if not asyncio.iscoroutinefunction(reader):
                return None
            try:
                row = await reader(
                    family, target_key, official, fresh_only=True
                )
                return (row or {}).get("payload") if row else None
            except Exception:
                return None

        def _json_safe(value):
            if is_dataclass(value):
                return asdict(value)
            if isinstance(value, (list, tuple)):
                return [_json_safe(item) for item in value]
            if isinstance(value, dict):
                return {key: _json_safe(item) for key, item in value.items()}
            return value

        discovered_sports = await cached_discovery(
            "discovery_sports", "catalog", "GLOBAL"
        )
        sports_from_cache = discovered_sports is not None
        if discovered_sports is None:
            discovered_sports = await client.discover_sports()
            evidence_writer = getattr(db, "upsert_propline_evidence_batch", None)
            if asyncio.iscoroutinefunction(evidence_writer):
                await evidence_writer([{
                    "family": "discovery_sports",
                    "target_key": "catalog",
                    "official_sport": "GLOBAL",
                    "source": "PropLine",
                    "fetched_at": datetime.utcnow(),
                    "expires_at": datetime.utcnow() + timedelta(minutes=30),
                    "complete": not bool(
                        getattr(client, "sports_discovery_failed", False)
                    ),
                    "redacted": False,
                    "payload": _json_safe(discovered_sports),
                }])
        if getattr(client, "sports_discovery_failed", False):
            discovery_audit = audit_propline_discovery(
                None,
                active_props=active_rows,
                complete=False,
            )
        else:
            audit_method = getattr(client, "audit_discovery", None)
            if not sports_from_cache and callable(audit_method):
                discovery_audit = audit_method(active_props=active_rows)
            else:
                discovery_audit = audit_propline_discovery(
                    discovered_sports,
                    active_props=active_rows,
                )
        propline_sports = [
            sport
            for sport in discovered_sports or ()
            if not is_football_related_sport(
                _source_value(
                    sport,
                    "canonical_key",
                    "canonical_sport",
                    "sport",
                    "official_key",
                    "sport_key",
                    "key",
                )
            )
        ]
        excluded_football_sports = (
            len(discovered_sports or ()) - len(propline_sports)
        )
        if discovery_audit.pending:
            logger.warning(
                "propline_read_only_rotation_job: discovery audit pending "
                "status=%s reason=%s",
                discovery_audit.status,
                discovery_audit.reason,
            )
        events_by_sport = {}
        discover_events = getattr(client, "discover_events", None)
        if callable(discover_events):
            # Event discovery is deliberately sequential: each request uses
            # the client's persistent limiter and a restart can safely repeat
            # the last discovery without creating an unbounded burst.
            for sport in propline_sports:
                official_sport = (
                    getattr(sport, "official_key", None)
                    or getattr(sport, "sport_key", None)
                    or ""
                )
                if not official_sport:
                    continue
                events = await cached_discovery(
                    "discovery_events", official_sport, official_sport
                )
                if events is None:
                    try:
                        events = await discover_events(official_sport)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "propline_read_only_rotation_job: event discovery "
                            "failed sport=%s error=%s",
                            official_sport,
                            type(exc).__name__,
                        )
                        continue
                    evidence_writer = getattr(
                        db, "upsert_propline_evidence_batch", None
                    )
                    if asyncio.iscoroutinefunction(evidence_writer):
                        await evidence_writer([{
                            "family": "discovery_events",
                            "target_key": official_sport,
                            "official_sport": official_sport,
                            "source": "PropLine",
                            "fetched_at": datetime.utcnow(),
                            "expires_at": datetime.utcnow() + timedelta(minutes=15),
                            "complete": True,
                            "redacted": False,
                            "payload": _json_safe(list(events or ())),
                        }])
                if events:
                    events_by_sport[official_sport] = tuple(events)
        targets = build_propline_read_only_targets(
            active_rows,
            sports=propline_sports,
            events_by_sport=events_by_sport,
        )

        # Keep every discovered sport in the shared read-only rotation. Event
        # stats still need explicit final status before persistence.
        targets = [
            target
            for target in targets
            if not (
                target.family == "event_stats"
                and not _is_explicit_propline_final(target.event_status)
            )
        ]
        fresh_reader = getattr(
            db, "get_fresh_propline_evidence_target_keys", None
        )
        if asyncio.iscoroutinefunction(fresh_reader):
            fresh: set[tuple[str, str]] = set()
            families: dict[str, list[str]] = {}
            for target in targets:
                families.setdefault(target.family, []).append(target.identity)
            for family, keys in families.items():
                try:
                    fresh.update(
                        (family, key)
                        for key in await fresh_reader(family, keys)
                    )
                except Exception:
                    continue
            targets = [
                target for target in targets
                if (target.family, target.identity) not in fresh
            ]

        # Catalog drift is operational evidence only.  Persist it separately
        # from player history so an unknown identity can never enter scoring or
        # delivery through a normalized prop row.
        discovery_audit_payload = discovery_audit.as_dict()
        catalog_audit_summary: dict[str, int] = {}
        catalog_audit_writer = getattr(db, "save_propline_catalog_audit", None)
        if (
            discovery_audit.status == "complete"
            and asyncio.iscoroutinefunction(catalog_audit_writer)
        ):
            try:
                catalog_audit_summary = await catalog_audit_writer(
                    [
                        identity.as_dict()
                        for identity in discovery_audit.catalog_identities
                    ]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "propline_read_only_rotation_job: catalog audit persistence "
                    "failed: %s",
                    exc,
                )

        catalog_history: list[dict[str, Any]] = []
        catalog_history_reader = getattr(
            db, "get_propline_catalog_audit_history", None
        )
        if asyncio.iscoroutinefunction(catalog_history_reader):
            try:
                catalog_history = list(
                    await catalog_history_reader(limit=20)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "propline_read_only_rotation_job: catalog audit history "
                    "read failed: %s",
                    exc,
                )
        discovery_audit_payload["catalog_audit_summary"] = catalog_audit_summary
        discovery_audit_payload["catalog_history"] = catalog_history

        def _evidence_payload(result):
            payload = getattr(result, "payload", None)
            if payload is not None:
                return payload
            if hasattr(result, "results"):
                return list(getattr(result, "results", ()) or ())
            if hasattr(result, "entries"):
                return list(getattr(result, "entries", ()) or ())
            if is_dataclass(result):
                return asdict(result)
            return result

        def _evidence_record(target, result, *, payload_override=None):
            fetched = getattr(result, "fetched_at", None) or datetime.utcnow()
            if target.family == "event_results":
                outcomes = tuple(getattr(result, "results", ()) or ())
                fully_settled = bool(outcomes) and all(
                    getattr(outcome, "is_settled", False) for outcome in outcomes
                )
                ttl = timedelta(hours=24) if fully_settled else timedelta(minutes=2)
            else:
                ttl = {
                    "event_stats": timedelta(hours=24),
                    "player_games": timedelta(hours=12),
                    "player_history": timedelta(hours=12),
                    "player_trends": timedelta(hours=1),
                    "event_markets": timedelta(minutes=30),
                    "event_odds_history": timedelta(minutes=15),
                    "event_movement": timedelta(minutes=5),
                    "event_best_line": timedelta(minutes=5),
                    "event_ev": timedelta(minutes=5),
                    "scores": timedelta(minutes=3),
                }.get(target.family, timedelta(minutes=15))
            return {
                "family": target.family,
                "target_key": target.identity,
                "official_sport": target.official_sport,
                "canonical_sport": target.canonical_sport,
                "event_id": target.event_id or None,
                "player_id": target.player_id or None,
                "player_name": target.player_name or None,
                "market": target.market or None,
                "bookmaker": target.bookmaker or None,
                "source": "PropLine",
                "fetched_at": fetched,
                "expires_at": fetched + ttl,
                "complete": True,
                "redacted": False,
                "payload": _json_safe(
                    _evidence_payload(result)
                    if payload_override is None
                    else payload_override
                ),
            }

        async def persist(target, result):
            evidence_writer = getattr(db, "upsert_propline_evidence_batch", None)
            completed_payload = None
            if target.family == "player_games":
                completed_payload = _filter_completed_propline_game_payload(
                    getattr(result, "payload", None)
                )
            if asyncio.iscoroutinefunction(evidence_writer):
                if target.family != "player_games" or completed_payload is not None:
                    await evidence_writer(
                        [
                            _evidence_record(
                                target,
                                result,
                                payload_override=completed_payload,
                            )
                        ]
                    )

            if target.family != "event_results":
                if target.family == "player_games":
                    rows = _completed_propline_game_rows(target, result)
                    if rows:
                        writer = getattr(db, "upsert_player_results_bulk", None)
                        if callable(writer):
                            stored = await writer(rows)
                        else:
                            for row in rows:
                                await db.upsert_player_result(row)
                            stored = len(rows)
                        logger.info(
                            "PropLine player-game evidence sport=%s league=%s "
                            "player=%s market=%s rows_after_identity_dedupe=%d "
                            "normalized=%d db_upsert_rows=%s "
                            "duplicate_policy=unique_game_identity "
                            "result_source=propline_player_games",
                            target.canonical_sport,
                            target.official_sport,
                            target.player_name,
                            target.market,
                            len(rows),
                            len(rows),
                            stored if stored is not None else len(rows),
                        )
                    else:
                        logger.info(
                            "PropLine INSUFFICIENT_EVIDENCE sport=%s league=%s "
                            "player=%s event_id=%s market=%s "
                            "reason=no_explicit_final_player_stat",
                            target.canonical_sport,
                            target.official_sport,
                            target.player_name,
                            target.event_id or "not_available",
                            target.market,
                        )
                elif target.family == "player_history":
                    await _persist_propline_player_history_rows(
                        db, target, result
                    )
                return True
            rows = [
                row for row in propline_result_rows(
                    getattr(result, "results", ())
                )
                if str(getattr(row, "source", "") or "")
                != "propline_event_results_void"
            ]
            if not rows:
                return True
            writer = getattr(db, "upsert_player_results_bulk", None)
            if callable(writer):
                await writer(rows)
            else:
                for row in rows:
                    await db.upsert_player_result(row)
            return True

        rotation = PropLineReadOnlyRotation(
            client,
            max_requests_per_cycle=4,
        )
        report = await rotation.run(targets, persist=persist)
        for provenance in report.provenance:
            if (
                provenance.get("family") == "player_games"
                and provenance.get("status") != "completed"
            ):
                logger.info(
                    "PropLine INSUFFICIENT_EVIDENCE family=player_games "
                    "target=%s reason=%s",
                    provenance.get("target", ""),
                    provenance.get("reason") or "no_complete_response",
                )
        report = replace(
            report,
            discovery_audit=discovery_audit_payload,
        )
        bot_data["propline_read_only_report"] = report.as_dict()
        bot_data["propline_discovery_audit"] = discovery_audit_payload
        logger.info(
            "propline_read_only_rotation_job: attempted=%d completed=%d "
            "persisted=%d pending=%d skipped=%d errors=%d next_family=%s "
            "families=%s excluded_football_active_props=%d "
            "excluded_football_sports=%d",
            report.attempted,
            report.completed,
            report.persisted,
            report.pending,
            report.skipped,
            report.errors,
            report.next_family,
            report.family_counts,
            excluded_football_active,
            excluded_football_sports,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("propline_read_only_rotation_job: %s", exc)
