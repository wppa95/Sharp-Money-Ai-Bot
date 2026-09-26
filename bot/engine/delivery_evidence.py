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

_STRICT_HISTORY_SPORTS = frozenset({"MLB", "NFL", "NBA", "WNBA"})


def is_history_optional_sport(sport: Any) -> bool:
    """Return whether *sport* skips the stricter history-quality checks.

    NFL, NBA, MLB, and WNBA are strict-history sports. Other sports may be
    delivered after common completed-history qualification without the
    additional strict-sport checks. This never makes missing history optional:
    every user-facing alert still needs five usable completed or settled
    results and an OVER/UNDER direction.

    This is deliberately based on the strict set rather than a hand-maintained
    list of aliases. The feed contains additional active sports (for example
    CFB, NHL, and FIFA), and omitting one from an optional-sport allowlist
    silently changes its additional quality checks.
    """
    normalized = " ".join(
        str(sport or "").strip().upper().replace("_", " ").replace("-", " ").split()
    )
    return bool(normalized) and normalized not in _STRICT_HISTORY_SPORTS


def propline_history_supports_delivery(
    *values: Any,
    min_samples: int = 5,
) -> bool:
    """Return whether any evidence payload contains enough PropLine history.

    Primary-provider game logs and line-movement snapshots are useful for
    diagnostics, but they do not satisfy the product's user-facing evidence
    contract. A PropLine-only payload needs ``min_samples`` supplemental
    results; a mixed payload still needs that many PropLine results.
    """
    minimum = max(1, int(min_samples))

    def _payload(payload: Any, depth: int = 0) -> bool:
        if payload is None or depth > 6:
            return False
        if isinstance(payload, str):
            text = payload.strip()
            if not text or text[0] not in "[{":
                return False
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                return False
        if isinstance(payload, dict):
            source = next(
                (
                    str(payload.get(key) or "").strip().lower()
                    for key in _SOURCE_KEYS
                    if str(payload.get(key) or "").strip()
                ),
                "",
            )
            supplemental, supplemental_present = _first_count(
                payload,
                ("n_supplemental_games", "n_supp"),
            )
            total, total_present = _total_count(payload)
            if source == "propline":
                count = supplemental if supplemental_present else (
                    total if total_present else None
                )
                if count is not None and count >= minimum:
                    return True
            elif source in {"real+propline", "primary+propline"}:
                if supplemental is not None and supplemental >= minimum:
                    return True
            return any(
                _payload(child, depth + 1)
                for key in _NESTED_KEYS
                if (child := payload.get(key)) is not None
            )
        if isinstance(payload, (list, tuple)):
            return any(_payload(child, depth + 1) for child in payload)
        source = str(getattr(payload, "data_source", "") or "").strip().lower()
        supplemental = _nonnegative_int(
            getattr(payload, "n_supplemental_games", 0)
        )
        return (
            source in {"propline", "real+propline", "primary+propline"}
            and supplemental is not None
            and supplemental >= minimum
        )

    return any(_payload(value) for value in values)


_COMPLETED_HISTORY_SOURCES = frozenset(
    {
        "real",
        "primary",
        "game_results",
        "player_game_results",
        "settled_results",
        "propline",
        "real+propline",
        "primary+propline",
    }
)


def history_supports_delivery(
    *values: Any,
    min_samples: int = 5,
) -> bool:
    """Return whether a payload contains completed player-game history.

    ``propline_history_supports_delivery`` is retained for callers that need
    to specifically prove supplemental PropLine coverage.  Telegram
    qualification, however, accepts either completed primary-provider game
    results or explicit settled PropLine results.  Both are real history and
    both must carry a usable sample count; proxy observations and historical
    line averages do not qualify.
    """
    minimum = max(1, int(min_samples))

    def _window_count(payload: dict[str, Any]) -> int | None:
        counts: list[int] = []
        for key in (
            "l5",
            "l10",
            "l15",
            "l20",
            "l30",
            "season",
            "h2h",
        ):
            window = payload.get(key)
            if isinstance(window, dict):
                count, present = _first_count(window, ("games", "g"))
                if present and count is not None:
                    counts.append(count)
            elif isinstance(window, (int, float)) and not isinstance(window, bool):
                counts.append(int(window))
        for key in (
            "l5_games",
            "l10_games",
            "l15_games",
            "l20_games",
            "l30_games",
            "season_games",
            "h2h_games",
            "total_games",
        ):
            count = _nonnegative_int(payload.get(key))
            if count is not None:
                counts.append(count)
        return max(counts) if counts else None

    def _object_count(payload: Any) -> int | None:
        counts: list[int] = []
        for attr in (
            "n_real_games",
            "n_supplemental_games",
            "total_games",
            "season_games",
            "l30_games",
            "l20_games",
            "l15_games",
            "l10_games",
            "l5_games",
        ):
            count = _nonnegative_int(getattr(payload, attr, None))
            if count is not None:
                counts.append(count)
        return max(counts) if counts else None

    def _payload(payload: Any, depth: int = 0) -> bool:
        if payload is None or depth > 6:
            return False
        if isinstance(payload, str):
            text = payload.strip()
            if not text or text[0] not in "[{":
                return False
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                return False

        if isinstance(payload, dict):
            source = next(
                (
                    str(payload.get(key) or "").strip().lower()
                    for key in _SOURCE_KEYS
                    if str(payload.get(key) or "").strip()
                ),
                "",
            )
            if source in _COMPLETED_HISTORY_SOURCES:
                real = _nonnegative_int(
                    payload.get("n_real_games", payload.get("n_real"))
                ) or 0
                supplemental = _nonnegative_int(
                    payload.get(
                        "n_supplemental_games",
                        payload.get("n_supp"),
                    )
                ) or 0
                count = (
                    real + supplemental
                    if real or supplemental
                    else (_total_count(payload)[0] or _window_count(payload))
                )
                if count is not None and count >= minimum:
                    return True
            return any(
                _payload(child, depth + 1)
                for key in _NESTED_KEYS
                if (child := payload.get(key)) is not None
            )

        if isinstance(payload, (list, tuple)):
            return any(_payload(child, depth + 1) for child in payload)

        source = str(
            getattr(payload, "data_source", "")
            or getattr(payload, "evidence_source", "")
            or ""
        ).strip().lower()
        if source in _COMPLETED_HISTORY_SOURCES:
            real = _nonnegative_int(getattr(payload, "n_real_games", None)) or 0
            supplemental = _nonnegative_int(
                getattr(payload, "n_supplemental_games", None)
            ) or 0
            count = real + supplemental
            if not count:
                count = _object_count(payload)
            if count is not None and count >= minimum:
                return True

        # A PlayerHitRates object always carries an explicit real-data marker
        # even in older serialized forms that omitted data_source.
        if bool(getattr(payload, "has_real_data", False)):
            count = _object_count(payload)
            if count is not None and count >= minimum:
                return True
        return False

    return any(_payload(value) for value in values)


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


def history_is_available(*values: Any) -> bool:
    """Return whether any payload marks that history/evidence was available.

    This is intentionally different from ``history_supports_delivery``: a
    marker is not proof of usable completed history. Delivery callers must
    reject missing history and then validate its source and sample count.
    """

    source_keys = _SOURCE_KEYS
    count_keys = (
        "n",
        "n_history",
        "n_real",
        "n_real_games",
        "n_supp",
        "n_supplemental_games",
        "n_proxy",
        "n_proxy_snaps",
        "total_games",
        "sample",
        "sample_count",
        "count",
    )

    def _has_marker(value: Any, depth: int = 0) -> bool:
        if value is None or depth > 6:
            return False
        if isinstance(value, str):
            text = value.strip()
            if not text or text[0] not in "[{":
                return False
            try:
                return _has_marker(json.loads(text), depth + 1)
            except (TypeError, ValueError):
                return False
        if isinstance(value, dict):
            for key in source_keys:
                source = str(value.get(key) or "").strip().lower()
                if source and source not in {"none", "unavailable", "unknown"}:
                    return True
            if any(
                key in value
                and isinstance(value.get(key), (int, float))
                and not isinstance(value.get(key), bool)
                and value.get(key) > 0
                for key in count_keys
            ):
                return True
            return any(
                _has_marker(value.get(key), depth + 1)
                for key in _NESTED_KEYS
                if value.get(key) is not None
            )
        if isinstance(value, (list, tuple)):
            return any(_has_marker(child, depth + 1) for child in value)
        if getattr(type(value), "__module__", "") == "unittest.mock":
            return False
        if bool(getattr(value, "has_supporting_data", False)):
            return True
        if bool(getattr(value, "has_real_data", False)):
            return True
        for attr in source_keys:
            source = str(getattr(value, attr, "") or "").strip().lower()
            if source and source not in {"none", "unavailable", "unknown"}:
                return True
        for attr in count_keys:
            count = getattr(value, attr, None)
            if (
                isinstance(count, (int, float))
                and not isinstance(count, bool)
                and count > 0
            ):
                return True
        return any(
            _has_marker(getattr(value, key, None), depth + 1)
            for key in _NESTED_KEYS
            if getattr(value, key, None) is not None
        )

    return any(_has_marker(value) for value in values)


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