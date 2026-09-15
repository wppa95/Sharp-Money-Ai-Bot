"""Evidence checks for serialized delivery metadata.

Serialized decision and validation payloads are untrusted historical metadata.
They must identify both an allowed result source and a sufficient sample before
they can support a user-facing directional alert.
"""

from __future__ import annotations

import json
from typing import Any


_MIN_DELIVERY_EVIDENCE_SAMPLES = 1
_SOURCE_KEYS = ("data_source", "evidence_source", "source", "src")
_DIRECTIONAL_HISTORY_WINDOWS = ("l5", "l10", "l15", "l20", "l30", "season")
_NESTED_KEYS = (
    "evidence",
    "bet_evidence",
    "intelligence_trace",
    "historical",
    "validation_json",
    "bet_evidence_json",
)

_HISTORY_OPTIONAL_SPORTS = frozenset({
    # Esports identifiers used by the configured feeds.
    "ESPORTS",
    "CS",
    "CS2",
    "LOL",
    "LEAGUE OF LEGENDS",
    "VAL",
    "VALORANT",
    "DOTA",
    "DOTA2",
    # Tennis and soccer identifiers used by the configured feeds.
    "TENNIS",
    "SOCCER",
    "FIFA",
    "EPL",
    "LALIGA",
    "LA LIGA",
    "SERIEA",
    "SERIE A",
    "BUNDESLIGA",
    "LIGUE1",
    "LIGUE 1",
    "MLS",
    "UCL",
    "NWSL",
})


def is_history_optional_sport(sport: Any) -> bool:
    """Return whether direction alone may qualify delivery for *sport*.

    Esports, tennis, and soccer feeds may lack completed player-game history
    at alert time. Every other sport must provide qualifying history.
    """
    normalized = " ".join(
        str(sport or "").strip().upper().replace("_", " ").replace("-", " ").split()
    )
    return normalized in _HISTORY_OPTIONAL_SPORTS


def is_historical_average_source(value: Any) -> bool:
    """Return whether a source label identifies line-average fallback evidence."""
    if not isinstance(value, str):
        return False
    source = " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split())
    if source in {
        "historical average",
        "historical avg",
        "historical line",
        "historical line average",
        "historical average fallback",
        "average fallback",
        "line average fallback",
    }:
        return True
    return (
        "historical" in source
        and any(token in source for token in ("average", "avg", "line", "fallback"))
    )


def has_strict_directional_history(value: Any) -> bool:
    """Require every displayed history window to support the direction.

    Hit rates are stored as OVER rates.  Therefore an OVER needs at least 70%
    in every window, while an UNDER needs at most 40% OVER in every window.
    Missing windows are not treated as passing evidence.
    """
    if value is None:
        return False

    if isinstance(value, dict):
        recommendation = str(value.get("recommendation") or value.get("rec") or "").upper()
    else:
        recommendation = str(getattr(value, "recommendation", "") or "").upper()
    if recommendation not in {"OVER", "UNDER"}:
        return False

    for prefix in _DIRECTIONAL_HISTORY_WINDOWS:
        if isinstance(value, dict):
            window = value.get(prefix) or {}
            games = window.get("games", window.get("g"))
            over_count = window.get("over", window.get("o"))
            under_count = window.get("under", window.get("u"))
        else:
            games = getattr(value, f"{prefix}_games", None)
            over_count = getattr(value, f"{prefix}_over", None)
            under_count = getattr(value, f"{prefix}_under", None)

        if not isinstance(games, (int, float)) or isinstance(games, bool) or games < 5:
            return False
        if not isinstance(over_count, (int, float)) or isinstance(over_count, bool):
            if isinstance(under_count, (int, float)) and not isinstance(under_count, bool):
                over_count = games - under_count
            else:
                return False

        over_rate = float(over_count) / float(games)
        if recommendation == "OVER" and over_rate < 0.70:
            return False
        if recommendation == "UNDER" and over_rate > 0.40:
            return False

    return True


def has_strong_directional_history(
    value: Any,
    direction: Any = None,
) -> bool:
    """Return whether *value* contains complete, directionally strong history.

    ``UDBetDecision`` stores window fields as ``l5_games``/``l5_over`` while
    ``PlayerHitRates`` stores ``WindowStats`` objects under ``l5``.  The
    selection layer needs to apply the same rule to both shapes, so normalize
    them here instead of maintaining two subtly different quality gates.
    """
    if value is None:
        return False

    recommendation = str(
        direction
        or (
            value.get("recommendation") or value.get("rec")
            if isinstance(value, dict)
            else getattr(value, "recommendation", "")
        )
        or ""
    ).upper()
    if recommendation not in {"OVER", "UNDER"}:
        return False

    payload: dict[str, Any] = {"recommendation": recommendation}
    for prefix in _DIRECTIONAL_HISTORY_WINDOWS:
        if isinstance(value, dict):
            window = value.get(prefix)
            if isinstance(window, dict):
                games = window.get("games", window.get("g"))
                over_count = window.get("over", window.get("o"))
                under_count = window.get("under", window.get("u"))
            else:
                games = value.get(f"{prefix}_games")
                over_count = value.get(f"{prefix}_over")
                under_count = value.get(f"{prefix}_under")
        else:
            window = getattr(value, prefix, None)
            if window is not None and not isinstance(window, (str, bytes)):
                games = getattr(window, "games", None)
                over_count = getattr(window, "over_count", None)
                under_count = getattr(window, "under_count", None)
            else:
                games = getattr(value, f"{prefix}_games", None)
                over_count = getattr(value, f"{prefix}_over", None)
                under_count = getattr(value, f"{prefix}_under", None)

        if over_count is None and isinstance(games, (int, float)):
            if isinstance(under_count, (int, float)):
                over_count = games - under_count
        payload[prefix] = {"g": games, "o": over_count}

    return has_strict_directional_history(payload)


def has_directional_history_fields(value: Any) -> bool:
    """Return whether *value* carries the six-window decision shape."""
    if value is None or getattr(type(value), "__module__", "") == "unittest.mock":
        return False
    if isinstance(value, dict):
        return any(prefix in value for prefix in _DIRECTIONAL_HISTORY_WINDOWS)
    return any(
        hasattr(value, f"{prefix}_games")
        for prefix in _DIRECTIONAL_HISTORY_WINDOWS
    )


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _first_count(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[int | None, bool]:
    """Return the first usable count and whether a count field was present."""
    present = False
    for key in keys:
        if key in payload:
            present = True
            count = _nonnegative_int(payload.get(key))
            if count is not None:
                return count, True
    return None, present


def _total_count(payload: dict[str, Any]) -> tuple[int | None, bool]:
    count, present = _first_count(
        payload,
        ("total_games", "sample_count", "sample", "count", "games", "n"),
    )
    if count is not None:
        return count, True

    # Some compact decision payloads store the available window size under g.
    # Use the largest declared window rather than summing overlapping windows.
    window_counts = []
    for key in ("l5", "l10", "l20", "l30", "season", "sea", "h2h"):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        window_count, window_present = _first_count(window, ("games", "g"))
        present = present or window_present
        if window_count is not None:
            window_counts.append(window_count)
    return (max(window_counts) if window_counts else None), present


def _payload_has_sufficient_evidence(
    payload: dict[str, Any],
    *,
    allow_historical_average: bool = True,
) -> bool:
    source_value = next(
        (
            payload.get(key)
            for key in _SOURCE_KEYS
            if isinstance(payload.get(key), str) and payload.get(key).strip()
        ),
        "",
    )
    source = str(source_value).strip().lower()
    if is_historical_average_source(source):
        # Historical-average fallback is the evidence itself. It intentionally
        # does not require settled last-5/last-10 windows or a game count.
        return allow_historical_average
    if source not in {
        "real",
        "game_results",
        "player_game_results",
        "propline",
        "real+propline",
        "primary+propline",
    }:
        return False

    n_real, has_real_count = _first_count(payload, ("n_real_games", "n_real"))
    n_supplemental, has_supplemental_count = _first_count(
        payload,
        ("n_supplemental_games", "n_supp"),
    )
    total, has_total_count = _total_count(payload)

    if source == "propline":
        # When the provenance is explicitly supplemental, prefer its dedicated
        # count. A compact payload may only expose total n, which is safe because
        # every sample in a propline-only payload has that provenance.
        count = n_supplemental if has_supplemental_count else total
        return count is not None and count >= _MIN_DELIVERY_EVIDENCE_SAMPLES

    if source in {"real+propline", "primary+propline"}:
        if has_real_count or has_supplemental_count:
            return (
                (n_real or 0) + (n_supplemental or 0)
                >= _MIN_DELIVERY_EVIDENCE_SAMPLES
            )
        return total is not None and total >= _MIN_DELIVERY_EVIDENCE_SAMPLES

    # For primary result aliases, n_real is authoritative when present; older
    # serialized formats may only contain total n/games.
    count = n_real if has_real_count else total
    return count is not None and count >= _MIN_DELIVERY_EVIDENCE_SAMPLES


def serialized_evidence_supports_delivery(
    value: Any,
    *,
    _depth: int = 0,
    allow_historical_average: bool = True,
) -> bool:
    """Return whether a serialized evidence tree carries sufficient provenance.

    The helper accepts parsed dictionaries, JSON strings, and nested evidence
    containers used by persisted snapshots and downstream intelligence blobs.
    It intentionally returns false for a source label without a usable sample
    count.
    """
    if _depth > 6 or value is None:
        return False

    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "[{":
            return False
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return False

    if isinstance(value, dict):
        if _payload_has_sufficient_evidence(
            value,
            allow_historical_average=allow_historical_average,
        ):
            return True
        return any(
            serialized_evidence_supports_delivery(
                child,
                _depth=_depth + 1,
                allow_historical_average=allow_historical_average,
            )
            for key in _NESTED_KEYS
            if (child := value.get(key)) is not None
        )

    if isinstance(value, (list, tuple)):
        return any(
            serialized_evidence_supports_delivery(
                child,
                _depth=_depth + 1,
                allow_historical_average=allow_historical_average,
            )
            for child in value
        )

    return False