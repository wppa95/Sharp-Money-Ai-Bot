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


def evidence_profile(candidate: dict) -> dict:
    """Return a truthful, presentation-safe evidence description.

    This intentionally does not turn confidence or model scores into evidence.
    """
    validation = candidate.get("validation")
    decision = candidate.get("decision")

    def _value(obj, name, default=None):
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    sources = (
        _value(validation, "data_source"),
        candidate.get("evidence_source"),
        _value(decision, "evidence_source"),
    )
    source = next(
        (
            str(value).strip().lower()
            for value in sources
            if value is not None
            and str(value).strip().lower()
            not in {
                "", "none", "unavailable", "proxy", "model_proxy",
                "no_data", "no history", "missing",
            }
        ),
        next(
            (
                str(value).strip().lower()
                for value in sources
                if value is not None and str(value).strip()
            ),
            "unavailable",
        ),
    )
    real = int(_number(_value(validation, "n_real_games", 0)))
    supplemental = int(_number(_value(validation, "n_supplemental_games", 0)))
    if (
        source in {"composite", "primary+supplemental", "real+propline", "primary+propline"}
        or ("propline" in source and ("real" in source or "primary" in source))
    ):
        label, source_label = "composite evidence", "composite"
    elif source in {
        "primary", "player_game_results", "completed_results",
        "completed_result", "settled_results", "real",
    }:
        label, source_label = "completed primary game results", "primary"
    elif source in {"propline", "prop_line", "supplemental", "propline_history"}:
        label, source_label = "PropLine supplemental/settled evidence", "propline"
    elif source in {
        "historical_average", "average", "historical-average",
        "historical average",
    }:
        label, source_label = "historical-average fallback", "historical_average"
    elif source in {"proxy", "model_proxy"}:
        label, source_label = "proxy", "proxy"
    else:
        label, source_label = "unavailable", "unavailable"
    direction = _direction(candidate)
    history = candidate.get("hit_rates") or decision
    trusted_sample_count = (
        supplemental
        if source_label == "propline"
        else (
            real
            if source_label == "primary"
            else real + supplemental
            if source_label == "composite"
            else 0
        )
    )
    strong = bool(
        direction in {"OVER", "UNDER"}
        and not is_history_optional_sport(_sport(candidate))
        and has_strong_directional_history(
            history, direction
        )
        and trusted_sample_count >= 5
        and source_label in {"primary", "propline", "composite"}
    )
    return {
        "class": "STRONG" if strong else ("HISTORICAL-AVERAGE FALLBACK" if source_label == "historical_average" else "QUALIFIED"),
        "source": source_label,
        "source_label": label,
        "real_samples": real,
        "supplemental_samples": supplemental,
        "sample_count": trusted_sample_count,
        "direction": direction,
    }


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


def season_directional_hit_rate_strength(candidate: dict) -> float | None:
    """Return season hit-rate strength oriented to the candidate's direction.

    Hit rates are measured from the OVER perspective, so a 70% season rate
    gives an OVER strength of 0.70 and a 30% season rate gives an UNDER
    strength of 0.70. At least five season games are required for this ordering
    signal; existing delivery gates remain responsible for eligibility.
    """
    decision = candidate.get("decision")
    validation = candidate.get("validation")
    hit_rates = candidate.get("hit_rates") or getattr(decision, "hit_rates", None)

    def _value(source, key):
        if isinstance(source, dict):
            return source.get(key)
        return getattr(source, key, None) if source is not None else None

    season = _value(hit_rates, "season")
    games = (
        _value(season, "games")
        or _value(hit_rates, "season_games")
        or _value(decision, "season_games")
        or _value(validation, "season_games")
        or candidate.get("season_games")
    )
    rate = (
        _value(season, "hit_rate")
        if _value(season, "hit_rate") is not None
        else (
            _value(hit_rates, "season_hit_rate")
            if _value(hit_rates, "season_hit_rate") is not None
            else (
                _value(decision, "season_hit_rate")
                if _value(decision, "season_hit_rate") is not None
                else (
                    _value(validation, "season_hit_rate")
                    if _value(validation, "season_hit_rate") is not None
                    else candidate.get("season_hit_rate")
                )
            )
        )
    )
    try:
        games_value = float(games)
        rate_value = float(rate)
    except (TypeError, ValueError):
        return None
    if games_value < 5:
        return None
    if 1.0 < rate_value <= 100.0:
        rate_value /= 100.0
    if not 0.0 <= rate_value <= 1.0:
        return None

    direction = _direction(candidate)
    if direction == "OVER":
        return rate_value
    if direction == "UNDER":
        return 1.0 - rate_value
    return None


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


def rank_qualified_actionable_picks(candidates: Iterable[dict]) -> list[dict]:
    """Rank every already-qualified candidate without filtering or capping.

    Strong, source-backed directional history leads. Existing input order is
    the final tie-break so callers can preserve product-level sport priority.
    """
    indexed = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if isinstance(candidate, dict)
    ]

    def _rank(item: tuple[int, dict]):
        index, candidate = item
        profile = evidence_profile(candidate)
        direction_strength = directional_hit_rate_strength(candidate)
        source_rank = {
            "primary": 0,
            "composite": 1,
            "propline": 2,
            "historical_average": 3,
            "proxy": 4,
            "unavailable": 5,
        }.get(profile["source"], 6)
        return (
            0 if profile["class"] == "STRONG" else 1,
            -(direction_strength if direction_strength is not None else -1.0),
            -int(profile["sample_count"]),
            source_rank,
            index,
        )

    indexed.sort(key=_rank)
    return [candidate for _index, candidate in indexed]

