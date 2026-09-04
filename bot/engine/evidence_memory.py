"""Bounded in-memory evidence used by live prop monitoring.

SQLite remains the durable source for restarts, grading, and reporting. The
live scan only performs keyed lookups here; background collectors hydrate and
refresh the cache outside the monitoring slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

_MAX_RESULT_KEYS = 12_000
_MAX_LINE_KEYS = 12_000
_RESULTS_PER_KEY = 40
_LINES_PER_KEY = 30

_player_results: dict[tuple[str, str, str], list[object]] = {}
_line_history: dict[tuple[str, str], list["LineHistoryRecord"]] = {}


@dataclass(frozen=True)
class LineHistoryRecord:
    line_value: float | None
    prev_line: float | None
    removed: bool
    line_moved: bool
    fetched_at: datetime


def _result_key(player: str, sport: str, stat: str) -> tuple[str, str, str]:
    return (
        str(player or "").strip().casefold(),
        str(sport or "UNKNOWN").strip().upper(),
        str(stat or "").strip().lower(),
    )


def _line_key(player: str, stat: str) -> tuple[str, str]:
    return (
        str(player or "").strip().casefold(),
        str(stat or "").strip().lower(),
    )


def _result_identity(record: object) -> tuple[str, str, str]:
    return (
        str(getattr(record, "game_id", "") or ""),
        str(getattr(record, "game_date", "") or ""),
        str(getattr(record, "source", "") or ""),
    )


def _result_sort_key(record: object) -> str:
    return str(getattr(record, "game_date", "") or "")


def put_player_results(
    player: str,
    sport: str,
    stat: str,
    records: Iterable[object],
) -> None:
    key = _result_key(player, sport, stat)
    if not key[0] or not key[2]:
        return
    merged = {
        _result_identity(record): record
        for record in [*_player_results.get(key, ()), *list(records)]
    }
    ordered = sorted(merged.values(), key=_result_sort_key, reverse=True)
    if key not in _player_results and len(_player_results) >= _MAX_RESULT_KEYS:
        _player_results.pop(next(iter(_player_results)))
    _player_results[key] = ordered[:_RESULTS_PER_KEY]


def put_player_result_batch(
    results_by_key: dict[tuple[str, str, str], list[object]],
) -> None:
    for (player, sport, stat), records in results_by_key.items():
        put_player_results(player, sport, stat, records)


def get_player_results(
    player: str,
    sport: str,
    stat: str,
    limit: int = 30,
) -> list[object]:
    return list(_player_results.get(_result_key(player, sport, stat), ()))[:limit]


def put_line_observation(
    player: str,
    stat: str,
    line_value: float | None,
    *,
    removed: bool = False,
    fetched_at: datetime | None = None,
) -> None:
    key = _line_key(player, stat)
    if not key[0] or not key[1]:
        return
    history = _line_history.get(key, [])
    previous_line = history[0].line_value if history else None
    record = LineHistoryRecord(
        line_value=line_value,
        prev_line=previous_line,
        removed=removed,
        line_moved=(
            previous_line is not None
            and line_value is not None
            and previous_line != line_value
        ),
        fetched_at=fetched_at or datetime.utcnow(),
    )
    if key not in _line_history and len(_line_history) >= _MAX_LINE_KEYS:
        _line_history.pop(next(iter(_line_history)))
    _line_history[key] = [record, *history][:_LINES_PER_KEY]


def get_line_history(player: str, stat: str, limit: int = 30) -> list[LineHistoryRecord]:
    return list(_line_history.get(_line_key(player, stat), ()))[:limit]


def clear() -> None:
    _player_results.clear()
    _line_history.clear()
