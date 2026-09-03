"""Evidence checks for serialized delivery metadata.

Serialized decision and validation payloads are untrusted historical metadata.
They must identify both an allowed result source and a sufficient sample before
they can support a user-facing directional alert.
"""

from __future__ import annotations

import json
from typing import Any


_MIN_DELIVERY_EVIDENCE_SAMPLES = 5
_SOURCE_KEYS = ("data_source", "source", "src")
_NESTED_KEYS = (
    "evidence",
    "bet_evidence",
    "intelligence_trace",
    "historical",
    "validation_json",
    "bet_evidence_json",
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


def _payload_has_sufficient_evidence(payload: dict[str, Any]) -> bool:
    source_value = next(
        (
            payload.get(key)
            for key in _SOURCE_KEYS
            if isinstance(payload.get(key), str) and payload.get(key).strip()
        ),
        "",
    )
    source = str(source_value).strip().lower()
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


def serialized_evidence_supports_delivery(value: Any, *, _depth: int = 0) -> bool:
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
        if _payload_has_sufficient_evidence(value):
            return True
        return any(
            serialized_evidence_supports_delivery(child, _depth=_depth + 1)
            for key in _NESTED_KEYS
            if (child := value.get(key)) is not None
        )

    if isinstance(value, (list, tuple)):
        return any(
            serialized_evidence_supports_delivery(child, _depth=_depth + 1)
            for child in value
        )

    return False