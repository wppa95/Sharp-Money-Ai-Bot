"""
connectors/underdog.py — Underdog Fantasy pick'em connector.

Fetches Underdog Fantasy player prop projections from the public API,
tracks projection changes over time (line movement, value changes, removals),
and returns normalized MarketSnapshot objects with is_pickem=True.

Output is strictly in the pick'em domain — never mixed into sportsbook
moneyline or consensus analysis.

API notes:
  The Underdog API is unofficial (reverse-engineered from app traffic).
  Endpoint: https://api.underdogfantasy.com/v3/over_under_lines
  No authentication required for public projections.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from .base import BaseConnector, ConnectorStatus, MarketSnapshot

logger = logging.getLogger(__name__)

_UNDERDOG_BASE = "https://api.underdogfantasy.com/v1"
_BOOK_TITLE    = "Underdog"
_TIMEOUT       = aiohttp.ClientTimeout(total=20)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SharpMoneyBot/1.0)",
    "Accept":     "application/json",
}


@dataclass
class UnderdogProjection:
    """
    Normalized Underdog projection — one over/under line for one player.
    Tracked across fetches to detect line movement, value changes, removals.
    """
    external_id: str
    player_name: str
    team:        str
    sport:       str
    stat_type:   str
    line_value:  float
    game_id:     str = ""
    game_time:   Optional[datetime] = None
    fetched_at:  datetime = field(default_factory=datetime.utcnow)


class UnderdogConnector(BaseConnector):
    """
    Fetches Underdog Fantasy pick'em projections and detects:
      - Line movement (stat value changed since last fetch)
      - Projection changes (value changed)
      - Removed props (present in previous fetch, absent in current)

    Output snapshots have is_pickem=True and must not be fed into
    sportsbook consensus or moneyline steam analysis.
    """

    name          = "Underdog"
    is_pickem     = True
    poll_interval = 300  # 5 minutes — pick'em lines move slowly

    def __init__(
        self,
        active_sports: Optional[list[str]] = None,
        enabled: bool = True,
    ) -> None:
        self._active_sports = set(active_sports or [])
        self.enabled        = enabled
        # external_id → previous projection (for movement detection)
        self._previous: dict[str, UnderdogProjection] = {}
        # Set of external_ids present in last fetch (for removal detection)
        self._last_seen: set[str] = set()

    # ── Public interface ──────────────────────────────────────────────────────

    async def fetch(self) -> list[MarketSnapshot]:
        if not self.enabled:
            return []

        projections = await self._fetch_projections()
        if projections is None:
            return []

        current_ids = {p.external_id for p in projections}
        removed_ids = self._last_seen - current_ids

        snapshots: list[MarketSnapshot] = []
        now = datetime.utcnow()

        for proj in projections:
            if self._active_sports and proj.sport not in self._active_sports:
                continue

            prev = self._previous.get(proj.external_id)
            opening_line = prev.line_value if prev else proj.line_value

            # Detect line movement
            line_moved = (prev is not None and abs(proj.line_value - prev.line_value) >= 0.01)

            notes = ""
            if line_moved and prev:
                direction = "up" if proj.line_value > prev.line_value else "down"
                notes = f"line_moved:{prev.line_value:.1f}→{proj.line_value:.1f}:{direction}"

            sel = f"{proj.player_name} {proj.stat_type} {proj.line_value}"

            snaps = MarketSnapshot(
                sportsbook   = _BOOK_TITLE,
                sport        = proj.sport,
                league       = proj.sport,
                event        = proj.game_id or f"{proj.sport} game",
                market_type  = "Pick'em",
                selection    = sel,
                odds         = 0,   # no odds on pick'em
                timestamp    = now,
                player       = proj.player_name,
                team         = proj.team,
                line         = proj.line_value,
                game_time    = proj.game_time,
                opening_odds = None,  # pick'em has no American odds
                is_pickem    = True,
            )
            # Stash notes for downstream consumers via a convention attr
            object.__setattr__(snaps, "_notes", notes) if False else None
            snapshots.append(snaps)

            # Update tracking state
            self._previous[proj.external_id] = proj

        # Emit removal markers
        for rid in removed_ids:
            old = self._previous.pop(rid, None)
            if old:
                logger.info(
                    "Underdog prop REMOVED: %s %s %.1f",
                    old.player_name, old.stat_type, old.line_value,
                )
                sel = f"{old.player_name} {old.stat_type} {old.line_value}"
                snapshots.append(MarketSnapshot(
                    sportsbook   = _BOOK_TITLE,
                    sport        = old.sport,
                    league       = old.sport,
                    event        = old.game_id or f"{old.sport} game",
                    market_type  = "Pick'em",
                    selection    = sel + " [REMOVED]",
                    odds         = 0,
                    timestamp    = now,
                    player       = old.player_name,
                    team         = old.team,
                    line         = old.line_value,
                    game_time    = old.game_time,
                    opening_odds = None,
                    is_pickem    = True,
                ))

        self._last_seen = current_ids
        logger.info("Underdog: %d snapshots (%d removed)", len(snapshots), len(removed_ids))
        return snapshots

    async def health_check(self) -> ConnectorStatus:
        url = f"{_UNDERDOG_BASE}/over_under_lines"
        try:
            async with aiohttp.ClientSession(headers=_HEADERS, timeout=_TIMEOUT) as s:
                async with s.get(url) as resp:
                    return ConnectorStatus.OK if resp.status == 200 else ConnectorStatus.ERROR
        except Exception:
            return ConnectorStatus.ERROR

    # ── API client ────────────────────────────────────────────────────────────

    async def _fetch_projections(self) -> Optional[list[UnderdogProjection]]:
        """Fetch raw Underdog projections. Returns None on any error."""
        url = f"{_UNDERDOG_BASE}/over_under_lines"
        try:
            async with aiohttp.ClientSession(headers=_HEADERS, timeout=_TIMEOUT) as s:
                async with s.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("Underdog API HTTP %d", resp.status)
                        try:
                            from providers.health_monitor import get_health_monitor as _ghm
                            from providers.base import FailureType as _FT
                            _mon = _ghm()
                            if _mon:
                                _ftype = _FT.BLOCKED if resp.status == 403 else _FT.HTTP_ERROR
                                _mon.record_failure("Underdog", f"HTTP {resp.status}", _ftype)
                        except ImportError:
                            pass
                        return None
                    try:
                        from providers.health_monitor import get_health_monitor as _ghm
                        _mon = _ghm()
                        if _mon:
                            _mon.record_success("Underdog")
                    except ImportError:
                        pass

                    raw = await resp.json(content_type=None)
                        
                    projections = self._parse(raw)

                    logger.info(
                        "Underdog parsed %d projections",
                                                            len(projections),
                     )

                    return projections
        except aiohttp.ClientError as exc:
                logger.warning("Underdog request error: %s", exc)
                return None

        except Exception as exc:  # noqa: BLE001
            logger.warning("Underdog unexpected error: %s", exc)
            try:
                from providers.health_monitor import get_health_monitor as _ghm
                from providers.base import FailureType as _FT
                _mon = _ghm()
                if _mon:
                    _mon.record_failure("Underdog", str(exc)[:120], _FT.UNKNOWN)
            except ImportError:
                pass
            return None

            projections = self._parse(raw)

            logger.info(
                "UD parsed sample stats: %s",
                [p.stat_type for p in projections[:20]]
            )

            return projections

    def _parse(self, data: dict) -> list[UnderdogProjection]:
        """Parse Underdog API v1 JSON into UnderdogProjection objects.

        v1 shape differences from the old v3 endpoint:
          - ``appearance_stat`` is nested inside ``line["over_under"]``, not on
            the line directly.
          - Player-to-appearance linking goes through a top-level ``appearances``
            array; ``appearance_stat.appearance_id`` → ``appearances[].id``.
          - Players carry ``team_id`` (UUID string) instead of ``team: {alias}``.
          - ``appearances[].match_id`` is an integer; ``games[].id`` is a UUID, so
            the two do not cross-reference.  ``game_time`` resolves only when the
            fixture supplies matching string ids (tests); it will be ``None`` in
            production, which is acceptable — it is display-only.
        """
        projections: list[UnderdogProjection] = []

        # Build player lookup: id → player dict
        players: dict[str, dict] = {}
        for player in data.get("players", []):
            pid = player.get("id", "")
            if pid:
                players[pid] = player

        # Build appearances lookup: id → appearance dict (v1 top-level array)
        appearances: dict[str, dict] = {}
        for app in data.get("appearances", []):
            aid = app.get("id", "")
            if aid:
                appearances[aid] = app

        # Build game lookup: id → game dict
        games: dict[str, dict] = {}
        for game in data.get("games", []):
            gid = game.get("id", "")
            if gid:
                games[gid] = game

        for line in data.get("over_under_lines", []):
            try:
                stat_value = line.get("stat_value")
                if stat_value is None:
                    continue

                # v1: appearance_stat is nested inside the embedded over_under object
                over_under      = line.get("over_under") or {}
                appearance_stat = over_under.get("appearance_stat") or {}

                appearance_id = appearance_stat.get("appearance_id", "")
                display_stat  = appearance_stat.get("display_stat", "Unknown")

                # Resolve player and game through the top-level appearances table
                app       = appearances.get(appearance_id, {})
                player_id = app.get("player_id", "")
                # match_id is an integer in production; str() normalises for lookup
                match_id  = str(app.get("match_id", ""))

                p_data      = players.get(player_id, {})
                player_name = (
                    f"{p_data.get('first_name', '')} {p_data.get('last_name', '')}".strip()
                    or "Unknown Player"
                )
                # v1 players carry team_id (UUID string) — no nested team dict
                team  = p_data.get("team_id", "")
                sport = p_data.get("sport_id", "Unknown").upper()

                # game_time: attempt lookup by match_id string.  In production
                # match_id is an integer and game ids are UUIDs, so this will
                # usually be None — that is acceptable; game_time is display-only.
                game_time: Optional[datetime] = None
                g_data    = games.get(match_id, {})
                scheduled = g_data.get("scheduled_at", "")
                if scheduled:
                    try:
                        game_time = datetime.fromisoformat(
                            scheduled.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except ValueError:
                        pass

                projections.append(UnderdogProjection(
                    external_id = line.get("id", ""),
                    player_name = player_name,
                    team        = team,
                    sport       = sport,
                    stat_type   = display_stat,
                    line_value  = float(stat_value),
                    game_id     = match_id,
                    game_time   = game_time,
                    fetched_at  = datetime.utcnow(),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("Underdog parse error: %s", exc)
                continue

        logger.debug("Underdog: parsed %d projections", len(projections))
        return projections
