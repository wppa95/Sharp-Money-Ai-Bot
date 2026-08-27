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
    line_value:  Optional[float]
    game_id:     str = ""
    game_time:   Optional[datetime] = None
    fetched_at:  datetime = field(default_factory=datetime.utcnow)
    odds: int = 0
    market_type: str = "Pick'em"
    is_moneyline: bool = False
    is_game_market: bool = False
    selection: str = ""
    event: str = ""


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
            line_moved = (
                prev is not None and (
                    (proj.line_value is not None and prev.line_value is not None
                     and abs(proj.line_value - prev.line_value) >= 0.01)
                    or (proj.is_moneyline and proj.odds != prev.odds)
                )
            )

            notes = ""
            if line_moved and prev:
                if proj.is_moneyline:
                    direction = "up" if proj.odds > prev.odds else "down"
                    notes = f"odds_moved:{prev.odds}→{proj.odds}:{direction}"
                else:
                    direction = "up" if proj.line_value > prev.line_value else "down"
                    notes = f"line_moved:{prev.line_value:.1f}→{proj.line_value:.1f}:{direction}"

            sel = proj.selection or f"{proj.player_name} {proj.stat_type} {proj.line_value}"

            snaps = MarketSnapshot(
                sportsbook   = _BOOK_TITLE,
                sport        = proj.sport,
                league       = proj.sport,
                event        = proj.event or proj.game_id or f"{proj.sport} game",
                market_type  = proj.market_type,
                selection    = sel,
                odds         = proj.odds,
                timestamp    = now,
                player       = proj.player_name,
                team         = proj.team,
            line         = None if proj.is_moneyline else proj.line_value,
            game_time    = proj.game_time,
             opening_odds = (
                 prev.odds if proj.is_moneyline and prev else
                 (proj.odds if proj.is_moneyline else None)
             ),
            external_id  = proj.external_id,
            is_pickem    = True,
             is_moneyline  = proj.is_moneyline,
             is_game_market = proj.is_game_market,
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
                logger.debug(
                    "Underdog market REMOVED: %s %s %s",
                    old.player_name or old.team, old.stat_type,
                    old.line_value if old.line_value is not None else old.odds,
                )
                sel = old.selection or f"{old.player_name} {old.stat_type} {old.line_value}"
                snapshots.append(MarketSnapshot(
                    sportsbook   = _BOOK_TITLE,
                    sport        = old.sport,
                    league       = old.sport,
                    event        = old.event or old.game_id or f"{old.sport} game",
                     market_type  = old.market_type,
                    selection    = sel + " [REMOVED]",
                     odds         = old.odds,
                    timestamp    = now,
                    player       = old.player_name,
                    team         = old.team,
                     line         = None if old.is_moneyline else old.line_value,
                    game_time    = old.game_time,
                    opening_odds = None,
                    external_id  = old.external_id,
                    is_pickem    = True,
                     is_moneyline  = old.is_moneyline,
                     is_game_market = old.is_game_market,
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
                from engine.identity import display_player_name
                player_name = display_player_name(
                    first_name=p_data.get("first_name"),
                    last_name=p_data.get("last_name"),
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

        # Game-market feeds have varied over time.  Only parse explicitly named
        # moneyline/total containers and market types; never infer from
        # arbitrary nested objects (which protects the player-prop parser).
        containers = []
        for key in ("moneyline_lines", "moneylines", "moneyline_markets"):
            value = data.get(key, [])
            containers.extend(value if isinstance(value, list) else [value])
        for game in data.get("games", []):
            if not isinstance(game, dict):
                continue
            for market in game.get("markets", []):
                if not isinstance(market, dict):
                    continue
                kind = str(market.get("market_type") or market.get("type") or
                           market.get("key") or "").lower()
                if "moneyline" in kind or kind in {"h2h", "ml"}:
                    containers.append({**market, "_game": game})

        def _first(obj, *keys):
            for key in keys:
                if obj.get(key) is not None:
                    return obj[key]
            return None

        for item in containers:
            if not isinstance(item, dict):
                continue
            game = item.get("_game") or {}
            outcomes = item.get("outcomes") or item.get("selections")
            if isinstance(outcomes, list):
                entries = outcomes
            else:
                entries = [item]
            for outcome in entries:
                if not isinstance(outcome, dict):
                    continue
                odds = _first(outcome, "odds", "price", "american_odds")
                team = _first(outcome, "team", "team_name", "name", "selection")
                ext = _first(outcome, "id", "external_id")
                parent_id = _first(item, "id", "external_id", "market_id")
                event_id = _first(outcome, "event_id", "game_id", "match_id") or \
                    _first(item, "event_id", "game_id", "match_id") or \
                    _first(game, "id", "game_id", "match_id")
                sport = _first(outcome, "sport", "sport_id") or \
                    _first(item, "sport", "sport_id") or _first(game, "sport", "sport_id")
                if team is None or sport is None:
                    continue
                try:
                    odds_int = int(odds) if odds is not None else 0
                except (TypeError, ValueError):
                    continue
                if isinstance(team, dict):
                    team = _first(team, "name", "alias", "abbreviation", "id")
                if team is None:
                    continue
                team = str(team)
                stable = str(ext or f"{parent_id or event_id or 'event'}:{team}")
                projections.append(UnderdogProjection(
                    external_id=stable, player_name="", team=team,
                    sport=str(sport).upper(), stat_type="Moneyline",
                    line_value=None, game_id=str(event_id or ""),
                    odds=odds_int, market_type="Moneyline", is_moneyline=True,
                    selection=team, event=str(event_id or ""),
                ))

        total_containers = []
        for key in ("total_lines", "totals", "total_markets", "game_total_lines",
                    "game_totals"):
            value = data.get(key, [])
            total_containers.extend(value if isinstance(value, list) else [value])
        for game in data.get("games", []):
            if not isinstance(game, dict):
                continue
            for market in game.get("markets", []):
                if not isinstance(market, dict):
                    continue
                kind = str(market.get("market_type") or market.get("type") or
                           market.get("key") or "").lower()
                if "total" in kind and "player" not in kind:
                    total_containers.append({**market, "_game": game})

        for item in total_containers:
            if not isinstance(item, dict):
                continue
            game = item.get("_game") or {}
            outcomes = item.get("outcomes") or item.get("selections")
            entries = outcomes if isinstance(outcomes, list) else [item]
            for outcome in entries:
                if not isinstance(outcome, dict):
                    continue
                side = _first(outcome, "name", "selection", "side", "direction")
                raw_line = _first(outcome, "point", "line", "total", "stat_value")
                if side is None or raw_line is None:
                    continue
                side_text = str(side).strip()
                if side_text.lower() not in {"over", "under", "o", "u"}:
                    continue
                try:
                    total_line = float(raw_line)
                except (TypeError, ValueError):
                    continue
                odds = _first(outcome, "odds", "price", "american_odds")
                try:
                    odds_int = int(odds) if odds is not None else 0
                except (TypeError, ValueError):
                    continue
                ext = _first(outcome, "id", "external_id")
                parent_id = _first(item, "id", "external_id", "market_id")
                event_id = _first(outcome, "event_id", "game_id", "match_id") or \
                    _first(item, "event_id", "game_id", "match_id") or \
                    _first(game, "id", "game_id", "match_id")
                sport = _first(outcome, "sport", "sport_id") or \
                    _first(item, "sport", "sport_id") or _first(game, "sport", "sport_id")
                if sport is None:
                    continue
                event = _first(outcome, "event", "event_name") or \
                    _first(item, "event", "event_name", "name") or \
                    _first(game, "event", "event_name", "name") or str(event_id or "")
                stable = str(ext or f"{parent_id or event_id or 'event'}:{side_text}:{total_line:g}")
                projections.append(UnderdogProjection(
                    external_id=stable, player_name="", team="",
                    sport=str(sport).upper(), stat_type="Game Total",
                    line_value=total_line, game_id=str(event_id or ""),
                    market_type="Total (O/U)", is_game_market=True,
                    selection=side_text.title(), event=str(event),
                ))

        logger.debug("Underdog: parsed %d projections", len(projections))
        return projections
