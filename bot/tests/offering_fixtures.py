"""Synthetic current-offering setup for isolated Telegram delivery tests."""

from __future__ import annotations

from datetime import datetime


def seed_current_offering(
    *,
    provider: str,
    source_id: str,
    event_id: str,
    player_name: str,
    sport: str,
    team: str,
    stat_type: str,
    line_value: float,
    direction: str,
    game_time: datetime,
    line_variant: str = "",
    fetched_at: datetime | None = None,
    replace_scope: bool = True,
):
    """Seed one exact, fresh source offer; never use this in live-provider tests."""
    from engine.offering_coverage import coverage
    from engine.player_props_pipeline import (
        CanonicalPlayerProp,
        canonicalize_player_props,
    )

    if replace_scope:
        coverage.clear_scope(provider, sport)
    prop = CanonicalPlayerProp(
        provider=provider,
        external_id=source_id,
        game_id=event_id,
        player_name=player_name,
        team=team,
        sport=sport,
        stat_type=stat_type,
        line_value=float(line_value),
        line_variant=str(line_variant or ""),
        game_time=game_time,
        provider_status="scheduled",
        fetched_at=fetched_at or datetime.utcnow(),
        raw=None,
        available_directions=(str(direction).upper(),),
    )
    canonical = canonicalize_player_props(provider, [prop])[0]
    if str(provider).strip().casefold() == "underdog":
        coverage.mark_scope_ready(provider)
    else:
        coverage.mark_scope_ready(provider, sport)
    return canonical
