"""
Underdog provider — concrete PropProviderBase implementation.

Maps Underdog Fantasy pick'em projection data into the shared normalized
PlayerProp model.  This lets Underdog data flow into the same PropLineHistory
table and PropComparisonEngine as PrizePicks data.

Role in the overall system:
    PrizePicks (primary, daily use)  → PlayerProp → PropLineHistory
    Underdog   (secondary/reference) → PlayerProp → PropLineHistory  ← this file

Source priority rule:
    PrizePicks remains primary when available.
    Underdog is reference/fallback only.
    Do not assume PP and UD lines are identical.
    Every prop record stores its source via the `provider` field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from providers.prop_provider import PlayerProp, PropProviderBase


# ── Stat type normalization ───────────────────────────────────────────────────

_STAT_NORM: dict[str, str] = {
    # Basketball
    "pts":             "points",
    "points":          "points",
    "reb":             "rebounds",
    "rebounds":        "rebounds",
    "ast":             "assists",
    "assists":         "assists",
    "3pm":             "3-pointers made",
    "blk":             "blocks",
    "stl":             "steals",
    # Baseball
    "hits":            "hits",
    "hr":              "home runs",
    "home runs":       "home runs",
    "rbis":            "rbis",
    "strikeouts":      "strikeouts",
    "walks":           "walks",
    "total bases":     "total bases",
    # American football
    "rushing yards":   "rushing yards",
    "receiving yards": "receiving yards",
    "receptions":      "receptions",
    "passing yards":   "passing yards",
    "passing tds":     "passing tds",
    "rush+rec yards":  "rush+rec yards",
    # Hockey
    "shots":           "shots on goal",
    "shots on goal":   "shots on goal",
    "goals+assists":   "points",
    # Soccer
    "goals":           "goals",
    "key passes":      "key passes",
    "shots on target": "shots on target",
    # Tennis
    "games won":       "games won",
    "sets won":        "sets won",
    # Esports
    "kills":           "kills",
    "deaths":          "deaths",
    "maps won":        "maps won",
    "cs":              "cs per min",
}


def _normalize_stat(raw: str) -> str:
    """Return canonical stat name for an Underdog stat label."""
    return _STAT_NORM.get(raw.lower().strip(), raw.lower().strip())


def ud_snapshot_to_player_prop(snap: Any) -> PlayerProp:
    """
    Convert a UnderdogSnapshotRecord ORM row → normalized PlayerProp.

    `snap` is a UnderdogSnapshotRecord instance from database.py.
    This is the only place where Underdog-specific field names are known.
    """
    return PlayerProp(
        provider    = "Underdog",
        sport       = snap.sport        or "",
        player_name = snap.player_name  or "",
        team        = snap.team         or "",
        stat_type   = _normalize_stat(snap.stat_type or ""),
        line_value  = float(snap.line_value) if snap.line_value is not None else 0.0,
        game_time   = snap.game_time,
        external_id = snap.external_id  or "",
        game_id     = snap.game_id      or "",
        fetched_at  = snap.fetched_at   or datetime.utcnow(),
    )


# ── UnderdogProvider ─────────────────────────────────────────────────────────

class UnderdogProvider(PropProviderBase):
    """
    Underdog concrete provider.

    Serves Underdog pick'em projections as normalized PlayerProp objects.

    In production the live data is fetched by UnderdogConnector and stored
    as UnderdogSnapshotRecord rows.  This class wraps those rows so they
    feed into the shared normalized pipeline alongside PrizePicks data.

    Two typical usage patterns:

    Pattern A — wrap a batch of snapshot rows from the DB:

        rows = await db.get_recent_underdog_snapshots(limit=200)
        provider = UnderdogProvider(snapshots=rows)
        props = await provider.fetch_props()

    Pattern B — wrap snapshot rows and write them to PropLineHistory:

        rows = await db.get_recent_underdog_snapshots(limit=200)
        provider = UnderdogProvider(snapshots=rows)
        props = await provider.fetch_props()
        from database import PropLineHistory
        records = [PropLineHistory(...) for p in props]
        await db.save_prop_line_history_bulk(records)

    Removed props (snap.removed=True) are automatically skipped.
    """

    def __init__(
        self,
        *,
        snapshots:     Optional[list[Any]] = None,    # list[UnderdogSnapshotRecord]
        sport_filter:  Optional[list[str]] = None,
    ) -> None:
        self._snapshots    = snapshots    or []
        self._sport_filter = sport_filter

    # ── PropProviderBase interface ────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "Underdog"

    @property
    def sport_keys(self) -> list[str]:
        return self._sport_filter or [
            "MLB", "NBA", "NFL", "NHL", "WNBA",
            "SOCCER", "TENNIS", "CS", "DOTA", "LOL",
        ]

    def is_available(self) -> bool:
        return bool(self._snapshots)

    async def fetch_props(self) -> list[PlayerProp]:
        """
        Convert all non-removed snapshots into normalized PlayerProp objects.

        Removed props (snap.removed=True) are always skipped because a removal
        event means the line is no longer available for betting.
        """
        props: list[PlayerProp] = []
        for snap in self._snapshots:
            # Skip props that have been removed from Underdog
            if getattr(snap, "removed", False):
                continue
            prop = ud_snapshot_to_player_prop(snap)
            if self._sport_filter is None or prop.sport in self._sport_filter:
                props.append(prop)
        return props

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)

    def __len__(self) -> int:
        return len(self._snapshots)

    def __repr__(self) -> str:
        n_active  = sum(1 for s in self._snapshots if not getattr(s, "removed", False))
        n_removed = len(self._snapshots) - n_active
        return (
            f"<UnderdogProvider snapshots={len(self._snapshots)} "
            f"active={n_active} removed={n_removed} sports={self._sport_filter}>"
        )
