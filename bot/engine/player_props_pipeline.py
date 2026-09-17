"""Canonical internal boundary for the Player Props product.

Provider fetchers remain responsible for their own authorization, pacing, and
raw-feed adapters.  Once an observation reaches this module, both feeds use
the same normalized shape and source-aware identity before persistence,
evaluation, and reporting handoffs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import inspect
from types import SimpleNamespace
from typing import Any, Iterable


@dataclass
class CanonicalPlayerProp:
    """Provider-neutral observation with source provenance retained."""

    provider: str
    sport: str
    player_name: str
    team: str
    stat_type: str
    line_value: float
    game_time: datetime | None
    external_id: str
    game_id: str
    line_variant: str
    provider_status: str | None
    fetched_at: datetime
    raw: Any
    # Evaluation metadata is copied through when a provider has already
    # enriched its source object.  This lets the same persistence boundary
    # refresh qualification metadata without importing provider models.
    score_total: float | None = None
    score_tier: str | None = None
    bet_recommendation: str | None = None
    bet_confidence: int | None = None
    market_quality_score: int | None = None
    removed: bool = False

    @property
    def event_id(self) -> str:
        return self.game_id

    @property
    def source_id(self) -> str:
        """Stable provider-owned source identity for one alternate line."""
        return self.external_id or self.game_id


def canonicalize_player_prop(provider: str, prop: Any) -> CanonicalPlayerProp | None:
    """Adapt one provider object into the canonical Player Props shape."""
    player = str(getattr(prop, "player_name", "") or "").strip()
    sport = str(getattr(prop, "sport", "") or "").strip().upper()
    stat = str(getattr(prop, "stat_type", "") or "").strip()
    if not player or not sport or not stat:
        return None
    try:
        line = float(getattr(prop, "line_value", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if line < 0:
        return None

    external_id = str(getattr(prop, "external_id", "") or "").strip()
    game_id = str(
        getattr(prop, "event_id", "")
        or getattr(prop, "game_id", "")
        or ""
    ).strip()
    observed_at = getattr(prop, "fetched_at", None)
    if not isinstance(observed_at, datetime):
        observed_at = datetime.utcnow()

    return CanonicalPlayerProp(
        provider=str(provider or getattr(prop, "provider", "") or "").strip()
        or "Unknown",
        sport=sport,
        player_name=player,
        team=str(getattr(prop, "team", "") or ""),
        stat_type=stat,
        line_value=line,
        game_time=getattr(prop, "game_time", None),
        external_id=external_id,
        game_id=game_id,
        line_variant=str(getattr(prop, "line_variant", "") or "").strip().lower(),
        provider_status=(
            getattr(prop, "provider_status", None)
            or getattr(prop, "game_status", None)
            or None
        ),
        fetched_at=observed_at,
        raw=prop,
        score_total=getattr(prop, "score_total", None),
        score_tier=getattr(prop, "score_tier", None),
        bet_recommendation=getattr(prop, "bet_recommendation", None),
        bet_confidence=getattr(prop, "bet_confidence", None),
        market_quality_score=getattr(prop, "market_quality_score", None),
        removed=bool(getattr(prop, "removed", False)),
    )


def canonicalize_player_props(
    provider: str,
    props: Iterable[Any],
) -> list[CanonicalPlayerProp]:
    """Normalize a provider batch and keep the newest row per source identity.

    Provider, event/source ID, and line variant are deliberately part of the
    key.  This means standard, goblin, and demon lines remain independently
    addressable even when they belong to the same player/stat/event.
    """
    unique: dict[tuple[str, str, str, str, str, str, float], CanonicalPlayerProp] = {}
    for prop in props or ():
        canonical = canonicalize_player_prop(provider, prop)
        if canonical is None:
            continue
        key = (
            canonical.provider,
            canonical.player_name,
            canonical.sport,
            canonical.stat_type,
            canonical.source_id,
            canonical.line_variant,
            canonical.line_value,
        )
        unique[key] = canonical
    return list(unique.values())


def as_history_objects(
    props: Iterable[CanonicalPlayerProp],
) -> list[SimpleNamespace]:
    """Expose canonical rows through the existing DB adapter contract."""
    rows: list[SimpleNamespace] = []
    for prop in props:
        rows.append(
            SimpleNamespace(
                provider=prop.provider,
                sport=prop.sport,
                player_name=prop.player_name,
                team=prop.team,
                stat_type=prop.stat_type,
                line_value=prop.line_value,
                game_time=prop.game_time,
                provider_status=prop.provider_status,
                external_id=prop.external_id,
                game_id=prop.game_id,
                event_id=prop.game_id,
                line_variant=prop.line_variant,
                fetched_at=prop.fetched_at,
                score_total=prop.score_total,
                score_tier=prop.score_tier,
                bet_recommendation=prop.bet_recommendation,
                bet_confidence=prop.bet_confidence,
                market_quality_score=prop.market_quality_score,
                removed=prop.removed,
            )
        )
    return rows


async def persist_canonical_player_props(
    db: Any,
    provider: str,
    props: Iterable[Any],
    *,
    append_snapshot: bool = False,
    refresh_metadata: bool = False,
    append_observations: bool = False,
    track: bool = False,
) -> int:
    """Persist one provider batch through the shared Player Props ledger.

    ``append_observations`` and ``track`` are opt-in for compatibility with
    existing callers that use this function only for current-state refreshes.
    Provider ingestion jobs should enable both so the same batch enters the
    immutable observation ledger and the neutral settlement-tracking ledger.
    """
    canonical = canonicalize_player_props(provider, props)
    if db is None or not canonical:
        return 0
    rows = as_history_objects(canonical)
    if append_observations:
        observation_writer = getattr(
            db, "save_prop_line_observations_batch", None
        )
        if callable(observation_writer):
            observed = observation_writer(provider, rows)
            if inspect.isawaitable(observed):
                observed = await observed
            if int(observed or 0) != len(rows):
                raise RuntimeError(
                    f"incomplete {provider} observation-ledger write "
                    f"({observed}/{len(rows)})"
                )
    options = {}
    if append_snapshot:
        options["append_snapshot"] = True
    if refresh_metadata:
        options["refresh_metadata"] = True
    try:
        operation = db.upsert_prop_line_history_batch(
            provider,
            rows,
            **options,
        )
        if inspect.isawaitable(operation):
            await operation
    except TypeError:
        # Lightweight test doubles and older adapters may not expose the
        # optional lifecycle flags; they still receive the same canonical rows.
        operation = db.upsert_prop_line_history_batch(provider, rows)
        if inspect.isawaitable(operation):
            await operation
    if track:
        tracker = getattr(db, "track_prop_records", None)
        if callable(tracker):
            tracking_records = []
            for prop in canonical:
                external_id = prop.external_id or (
                    f"{prop.player_name}:{prop.sport}:{prop.stat_type}:{prop.game_id}"
                )
                tracking_records.append({
                    "provider": prop.provider,
                    "product": "Player Props",
                    "external_id": external_id,
                    "player_name": prop.player_name,
                    "team": prop.team,
                    "sport": prop.sport,
                    "stat_type": prop.stat_type,
                    "line_value": prop.line_value,
                    "game_time": prop.game_time,
                    "game_id": prop.game_id,
                    "line_variant": prop.line_variant,
                })
            tracked = tracker(tracking_records)
            if inspect.isawaitable(tracked):
                tracked = await tracked
            if tracked is not None and int(tracked) < len(tracking_records):
                raise RuntimeError(
                    f"incomplete {provider} tracking write "
                    f"({tracked}/{len(tracking_records)})"
                )
    return len(canonical)
