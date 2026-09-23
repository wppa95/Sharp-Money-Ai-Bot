"""Provider-scoped strongest-actionable-pick selection.

This module deliberately operates only on already-qualified candidates.  It
does not score, qualify, mutate lines, or create opportunities.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from engine.delivery_evidence import (
    has_strong_directional_history,
    history_supports_delivery,
    is_history_optional_sport,
)


_PRIMARY_PROVIDERS = {"prizepicks", "underdog"}


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


def directional_hit_rate_strength(candidate: dict) -> float | None:
    """Return the settled hit-rate strength for the candidate's direction.

    Hit rates are stored from the OVER perspective.  Therefore an OVER pick
    is stronger as its primary-window rate rises, while an UNDER pick is
    stronger as that same rate falls.
    """
    decision = candidate.get("decision")
    hit_rates = candidate.get("hit_rates") or getattr(decision, "hit_rates", None)

    windows = ("l5", "l10", "l15", "l20", "l30", "season")
    for window_name in windows:
        if isinstance(hit_rates, dict):
            window = hit_rates.get(window_name)
            games = (
                window.get("games")
                if isinstance(window, dict)
                else hit_rates.get(f"{window_name}_games")
            )
            rate = (
                window.get("hit_rate")
                if isinstance(window, dict)
                else hit_rates.get(f"{window_name}_hit_rate")
            )
        else:
            window = getattr(hit_rates, window_name, None)
            games = getattr(window, "games", None)
            rate = getattr(window, "hit_rate", None)
            if rate is None:
                rate = getattr(decision, f"{window_name}_hit_rate", None)
            if games is None:
                games = getattr(decision, f"{window_name}_games", None)

        try:
            if games is None:
                games = getattr(decision, f"{window_name}_games", None)
            if rate is None:
                rate = getattr(decision, f"{window_name}_hit_rate", None)
            games_value = float(games)
            rate_value = float(rate)
        except (TypeError, ValueError):
            continue
        if games_value < 5 or not 0.0 <= rate_value <= 1.0:
            continue
        direction = _direction(candidate)
        if direction == "OVER":
            return rate_value
        if direction == "UNDER":
            return 1.0 - rate_value
        return None
    return None


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


def is_strong_actionable_pick(
    candidate: dict,
    *,
    require_strong: bool = True,
) -> bool:
    """Return whether a candidate meets the shared strongest-pick contract.

    Strict-history sports need complete directional history. High-priority
    non-MLB/NFL/NBA sports retain the direction-plus-confidence path used by
    the canonical delivery gate, even when completed player-game history is
    not available yet.
    """
    direction = _direction(candidate)
    if direction not in {"OVER", "UNDER"}:
        return False
    if not require_strong:
        return True

    sport = _sport(candidate)
    history = candidate.get("hit_rates") or candidate.get("decision")
    validation = candidate.get("validation")
    history_values = (
        (validation,)
        if validation is not None
        else (history, candidate.get("decision"))
    )
    if not history_supports_delivery(*history_values):
        return False
    if is_history_optional_sport(sport):
        # Non-goblin sports are actionable as soon as the canonical gate has
        # a direction. Do not turn the legacy confidence score into a second
        # delivery gate.
        return True
    return has_strong_directional_history(history, direction)


def _ranking_key(candidate: dict) -> tuple[float, ...]:
    """Evidence-only ordering for already-qualified candidates."""
    evidence, sample_count = _validation_evidence(candidate)
    hit_rate_strength = directional_hit_rate_strength(candidate)
    return (
        float(hit_rate_strength is not None),
        hit_rate_strength if hit_rate_strength is not None else -1.0,
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
    *,
    require_strong: bool = False,
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
        if require_strong:
            group = [
                candidate
                for candidate in group
                if is_strong_actionable_pick(candidate, require_strong=True)
            ]
            if not group:
                continue
        winner = min(
            group,
            key=lambda candidate: (
                tuple(-value for value in _ranking_key(candidate)),
                _tie_key(candidate),
            ),
        )
        selected.append(winner)

    # The product path needs strongest-first ordering so the alert rate limit
    # and /picks cap preserve the best candidates.  Keep the historical
    # identity ordering for compatibility callers that use this helper only as
    # a per-player deduplicator.
    if require_strong:
        selected.sort(
            key=lambda candidate: (
                -(
                    directional_hit_rate_strength(candidate)
                    if directional_hit_rate_strength(candidate) is not None
                    else -1.0
                ),
                -_validation_evidence(candidate)[1],
                -(_directional_strength(candidate) or 0.0),
                -float(bool(candidate.get("is_meaningful_change"))),
                _provider(candidate),
                _player(candidate),
                _tie_key(candidate),
            )
        )
    else:
        selected.sort(
            key=lambda candidate: (
                _provider(candidate),
                _player(candidate),
                _tie_key(candidate),
            )
        )
    return selected
