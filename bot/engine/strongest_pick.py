"""Provider-scoped strongest-actionable-pick selection.

This module deliberately operates only on already-qualified candidates.  It
does not score, qualify, mutate lines, or create opportunities.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from engine.ud_scoring import _HIGH_FLOOR_STATS


_PRIMARY_PROVIDERS = {"prizepicks", "underdog"}
_LOW_LINE_GREEN_GOBLIN_SPORTS = {"NFL", "NBA", "MLB"}
_GREEN_GOBLIN_STAT_KEYS = frozenset(
    stat.casefold() for stat in _HIGH_FLOOR_STATS
)


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _provider(candidate: dict) -> str:
    prop = candidate.get("prop")
    return str(
        candidate.get("provider")
        or getattr(prop, "provider", None)
        or "Underdog"
    ).strip().lower()


def _sport(candidate: dict) -> str:
    prop = candidate.get("prop")
    return str(
        candidate.get("sport")
        or candidate.get("league")
        or getattr(prop, "sport", None)
        or ""
    ).strip().upper()


def _stat(candidate: dict) -> str:
    prop = candidate.get("prop")
    return str(
        candidate.get("stat_type")
        or candidate.get("stat")
        or getattr(prop, "stat_type", None)
        or ""
    ).strip()


def _line(candidate: dict) -> float:
    value = (
        candidate.get("line_val")
        if candidate.get("line_val") is not None
        else candidate.get("line")
    )
    return _number(value)


def _direction(candidate: dict) -> str:
    decision = candidate.get("decision")
    return str(
        getattr(decision, "recommendation", None)
        or candidate.get("direction")
        or ""
    ).strip().upper()


def _directional_strength(candidate: dict) -> float | None:
    decision = candidate.get("decision")
    raw_confidence = (
        candidate.get("confidence")
        if candidate.get("confidence") is not None
        else getattr(decision, "confidence", None)
    )
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None
    direction = _direction(candidate)
    return confidence if direction == "OVER" else 100.0 - confidence


def _is_low_line_green_goblin(candidate: dict) -> bool:
    """Return whether an NFL/NBA/MLB candidate is a strong goblin pick."""
    direction = _direction(candidate)
    strength = _directional_strength(candidate)
    return (
        _sport(candidate) in _LOW_LINE_GREEN_GOBLIN_SPORTS
        and _stat(candidate).casefold()
        in _GREEN_GOBLIN_STAT_KEYS
        and direction in {"OVER", "UNDER"}
        and strength is not None
        and strength >= 75.0
    )


def _player(candidate: dict) -> str:
    prop = candidate.get("prop")
    return str(
        candidate.get("player")
        or candidate.get("player_name")
        or getattr(prop, "player_name", None)
        or ""
    ).strip().casefold()


def _decision(candidate: dict):
    return candidate.get("decision")


def _validation_evidence(candidate: dict) -> tuple[int, float]:
    validation = candidate.get("validation")
    has_evidence = bool(
        candidate.get("evidence_available")
        or getattr(validation, "has_supporting_data", False)
    )
    sample_count = _number(
        candidate.get("evidence_count")
        or getattr(validation, "n_real_games", 0)
        + getattr(validation, "n_supplemental_games", 0)
    )
    return int(has_evidence), sample_count


def _ranking_key(candidate: dict) -> tuple[float, ...]:
    """Evidence-only ordering for already-qualified candidates."""
    evidence, sample_count = _validation_evidence(candidate)
    return (
        float(evidence),
        sample_count,
        float(bool(candidate.get("is_meaningful_change"))),
    )


def _tie_key(candidate: dict) -> tuple[str, str, float, str, str]:
    """Stable tie-break using only existing identity fields."""
    prop = candidate.get("prop")
    stat = str(
        candidate.get("stat_type")
        or candidate.get("stat")
        or getattr(prop, "stat_type", None)
        or ""
    ).strip().casefold()
    direction = str(
        getattr(_decision(candidate), "recommendation", None)
        or candidate.get("direction")
        or ""
    ).strip().casefold()
    line = _number(
        candidate.get("line_val")
        if candidate.get("line_val") is not None
        else candidate.get("line")
    )
    source_id = str(
        candidate.get("ext_id")
        or candidate.get("external_id")
        or getattr(prop, "external_id", None)
        or ""
    ).strip().casefold()
    event_id = str(
        candidate.get("event_id")
        or getattr(prop, "event_id", None)
        or getattr(prop, "game_id", None)
        or ""
    ).strip().casefold()
    return stat, direction, line, source_id, event_id


def select_strongest_actionable_picks(
    candidates: Iterable[dict],
) -> list[dict]:
    """Keep one strongest qualified opportunity per player/provider.

    Candidates are grouped by provider and normalized player identity.  The
    selector intentionally ignores all non-primary providers, so supplemental
    PropLine evidence can never become a standalone pick.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        provider = _provider(candidate)
        player = _player(candidate)
        if provider not in _PRIMARY_PROVIDERS or not player:
            continue
        groups[(provider, player)].append(candidate)

    selected: list[dict] = []
    for group in groups.values():
        # NFL, NBA, and MLB delivery are intentionally narrower than the
        # general selector: only strong directional high-floor ("green
        # goblin") markets are eligible, and the lowest available line wins
        # before evidence tie-breakers.
        low_line_group = [
            candidate for candidate in group
            if _sport(candidate) in _LOW_LINE_GREEN_GOBLIN_SPORTS
        ]
        if low_line_group:
            green_goblin_group = [
                candidate for candidate in low_line_group
                if _is_low_line_green_goblin(candidate)
            ]
            if not green_goblin_group:
                continue
            winner = min(
                green_goblin_group,
                key=lambda candidate: (
                    _line(candidate),
                    tuple(-value for value in _ranking_key(candidate)),
                    _tie_key(candidate),
                ),
            )
            selected.append(winner)
            continue
        winner = min(
            group,
            key=lambda candidate: (
                tuple(-value for value in _ranking_key(candidate)),
                _tie_key(candidate),
            ),
        )
        selected.append(winner)

    # Selection must not depend on provider/dictionary/input order.  This also
    # makes diagnostics and direct callers deterministic.
    selected.sort(
        key=lambda candidate: (
            _provider(candidate),
            _player(candidate),
            _tie_key(candidate),
        )
    )
    return selected
