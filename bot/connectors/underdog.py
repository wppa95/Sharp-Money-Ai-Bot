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
import math
import re
from engine.offering_coverage import coverage, directions_from
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


_EVENT_ID_KEYS = (
    "event_id",
    "game_id",
    "match_id",
    "fixture_id",
    "id",
)
_EVENT_TIME_KEYS = (
    "scheduled_at",
    "scheduled_start",
    "start_time",
    "start_at",
    "starts_at",
    "commence_time",
    "game_time",
    "start",
)
_EVENT_STATUS_KEYS = (
    "status",
    "state",
    "event_status",
    "game_status",
    "match_status",
    "game_state",
)
_EVENT_REFERENCE_KEYS = (
    "event_id",
    "game_id",
    "match_id",
    "fixture_id",
)


def _nonempty(value) -> bool:
    return value is not None and str(value).strip() != ""


def _first_present(record: object, *keys: str):
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if _nonempty(value):
            return value
    return None


def _records(value) -> list[dict]:
    """Return provider records from either list or keyed-object envelopes."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    if any(_nonempty(value.get(key)) for key in _EVENT_ID_KEYS):
        return [value]
    return [item for item in value.values() if isinstance(item, dict)]


def _parse_event_time(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _event_id(record: object) -> str:
    # Prefer the record's own stable ID over a join-only match reference.
    value = _first_present(
        record, "id", "event_id", "game_id", "match_id", "fixture_id"
    )
    return str(value).strip() if _nonempty(value) else ""


def _event_reference_id(record: object) -> str:
    value = _first_present(record, *_EVENT_REFERENCE_KEYS)
    return str(value).strip() if _nonempty(value) else ""


def _event_status(record: object) -> str:
    value = _first_present(record, *_EVENT_STATUS_KEYS)
    if isinstance(value, dict):
        value = _first_present(value, "name", "value", "status", "state")
    return str(value).strip() if _nonempty(value) else ""


def _event_time(record: object) -> Optional[datetime]:
    return _parse_event_time(_first_present(record, *_EVENT_TIME_KEYS))


def _event_label(record: object) -> str:
    value = _first_present(
        record, "event", "event_name", "match_name", "fixture_name", "label", "title"
    )
    return str(value).strip() if _nonempty(value) else ""


_TEAM_UUID_RE = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _is_team_uuid(value: object) -> bool:
    return bool(_TEAM_UUID_RE.fullmatch(str(value or "").strip()))


def _team_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    result = _first_present(value, "id", "team_id", "uuid")
    return str(result).strip() if _nonempty(result) else ""


def _team_label(value: object, *, id_hint: object = "") -> str:
    if isinstance(value, dict):
        result = _first_present(
            value,
            "name",
            "display_name",
            "full_name",
            "team_name",
            "short_name",
            "abbreviation",
            "alias",
        )
        return " ".join(str(result or "").strip().split())
    label = " ".join(str(value or "").strip().split())
    if (
        not label
        or label == str(id_hint or "").strip()
        or _is_team_uuid(label)
    ):
        return ""
    return label


def _team_reference(
    value: object,
    *,
    id_hint: object = "",
    name_hint: object = "",
    names_by_id: Optional[dict[str, set[str]]] = None,
) -> tuple[str, str]:
    """Extract an explicit provider team identity and display label."""
    team_id = _team_id(value) or str(id_hint or "").strip()
    if not team_id and _is_team_uuid(value):
        team_id = str(value).strip()
    label = _team_label(value, id_hint=team_id) or _team_label(
        name_hint, id_hint=team_id
    )
    if not label and team_id and names_by_id:
        labels = names_by_id.get(team_id, set())
        if len(labels) == 1:
            label = next(iter(labels))
    return team_id, label


def _event_team_references(
    record: object,
    names_by_id: Optional[dict[str, set[str]]] = None,
) -> tuple[tuple[str, str], tuple[str, str]]:
    if not isinstance(record, dict):
        return ("", ""), ("", "")
    home_value = (
        record.get("home_team")
        or record.get("home")
        or record.get("home_team_name")
    )
    away_value = (
        record.get("away_team")
        or record.get("away")
        or record.get("away_team_name")
    )
    home = _team_reference(
        home_value,
        id_hint=_first_present(record, "home_team_id", "home_id"),
        name_hint=_first_present(record, "home_team_name", "home_name"),
        names_by_id=names_by_id,
    )
    away = _team_reference(
        away_value,
        id_hint=_first_present(record, "away_team_id", "away_id"),
        name_hint=_first_present(record, "away_team_name", "away_name"),
        names_by_id=names_by_id,
    )
    return home, away


def _build_team_name_index(
    data: dict,
    event_lookup: dict[str, dict],
) -> dict[str, set[str]]:
    """Collect only explicit team ID/name pairs from the provider payload."""
    names_by_id: dict[str, set[str]] = {}

    def add(team_id: object, label: object) -> None:
        key = str(team_id or "").strip()
        name = _team_label(label, id_hint=key)
        if key and name:
            names_by_id.setdefault(key, set()).add(name)

    for record in _records(data.get("teams", [])):
        if not isinstance(record, dict):
            continue
        team_id = _first_present(record, "id", "team_id", "uuid")
        add(team_id, _team_label(record, id_hint=team_id))

    for player in _records(data.get("players", [])):
        if not isinstance(player, dict):
            continue
        team_value = player.get("team")
        team_id = (
            _first_present(player, "team_id", "team_uuid")
            or _team_id(team_value)
        )
        label = _team_label(team_value, id_hint=team_id) or _first_present(
            player,
            "team_name",
            "team_display_name",
            "team_alias",
        )
        add(team_id, label)

    for record in event_lookup.values():
        for team_id, label in _event_team_references(record):
            add(team_id, label)

    return names_by_id


def _build_event_lookup(data: dict) -> dict[str, dict]:
    """Index explicit provider event records without guessing from teams."""
    lookup: dict[str, dict] = {}
    for key in ("games", "matches", "events", "fixtures", "solo_games"):
        for record in _records(data.get(key, [])):
            for raw_id in _EVENT_ID_KEYS:
                value = record.get(raw_id)
                if _nonempty(value):
                    lookup[str(value).strip()] = record
    return lookup


def _resolve_event(
    sources: list[object],
    event_lookup: dict[str, dict],
) -> tuple[str, dict]:
    """Resolve a projection's explicit event identity and metadata."""
    candidate_ids: list[str] = []
    nested_sources: list[object] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        raw_id = _event_reference_id(source)
        if raw_id and raw_id not in candidate_ids:
            candidate_ids.append(raw_id)
        for key in ("event", "game", "match", "fixture"):
            nested = source.get(key)
            if isinstance(nested, dict):
                nested_sources.append(nested)
                nested_id = _event_id(nested)
                if nested_id and nested_id not in candidate_ids:
                    candidate_ids.append(nested_id)

    for candidate_id in candidate_ids:
        metadata = event_lookup.get(candidate_id)
        if metadata is not None:
            canonical_id = _event_id(metadata) or candidate_id
            return canonical_id, metadata
    for source in nested_sources:
        if _event_time(source) is not None or _event_status(source) or _event_label(source):
            return _event_id(source) or (candidate_ids[0] if candidate_ids else ""), (
                source if isinstance(source, dict) else {}
            )
    # A projection's own status commonly describes line availability
    # ("active"), not event state. Only accept a direct schedule timestamp
    # from projection sources when it is explicitly present; never promote the
    # projection's line ID into a game ID because of that availability status.
    for source in sources:
        if _event_time(source) is not None:
            return candidate_ids[0] if candidate_ids else "", (
                source if isinstance(source, dict) else {}
            )
    return (candidate_ids[0] if candidate_ids else ""), {}


def _opponent_for_player(team, home, away) -> str:
    """Return the opposing display label using explicit IDs or exact labels."""
    player_id = str(team or "").strip()
    home_id = _team_id(home) or (
        str(home).strip() if _is_team_uuid(home) else ""
    )
    away_id = _team_id(away) or (
        str(away).strip() if _is_team_uuid(away) else ""
    )
    home_label = _team_label(home, id_hint=home_id)
    away_label = _team_label(away, id_hint=away_id)

    if player_id and home_id and player_id == home_id:
        return away_label
    if player_id and away_id and player_id == away_id:
        return home_label

    player_label = _team_label(team)
    if (
        player_label
        and home_label
        and player_label.casefold() == home_label.casefold()
    ):
        return away_label
    if (
        player_label
        and away_label
        and player_label.casefold() == away_label.casefold()
    ):
        return home_label
    return ""


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
    provider_status: str = ""
    fetched_at:  datetime = field(default_factory=datetime.utcnow)
    odds: int = 0
    market_type: str = "Pick'em"
    is_moneyline: bool = False
    is_game_market: bool = False
    selection: str = ""
    event: str = ""
    opponent: str = ""
    # Explicit event participant ID/name pairs from Underdog's event payload.
    # IDs remain separate from labels so downstream identity keys stay stable.
    event_teams: tuple[tuple[str, str], ...] = ()
    available_directions: tuple[str, ...] = ()
    line_variant: str = ""
    market_id: str = ""


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
    poll_interval = 120  # 2 minutes — refresh the complete current feed

    def __init__(
        self,
        active_sports: Optional[list[str]] = None,
        enabled: bool = True,
    ) -> None:
        self._active_sports = {
            str(sport or "").strip().upper()
            for sport in (active_sports or [])
            if str(sport or "").strip()
        }
        self.enabled        = enabled
        # external_id → previous projection (for movement detection)
        self._previous: dict[str, UnderdogProjection] = {}
        # Set of external_ids present in last fetch (for removal detection)
        self._last_seen: set[str] = set()
        # Explicit fetch outcome for the registry/owning job.  A legitimate
        # empty response leaves this as None; transport/HTTP failures set it.
        self.last_fetch_error: Optional[str] = None

    # ── Public interface ──────────────────────────────────────────────────────

    async def fetch(self) -> list[MarketSnapshot]:
        if not self.enabled:
            return []

        self.last_fetch_error = None
        projections = await self._fetch_projections()
        if projections is None:
            return []

        current_ids = {p.external_id for p in projections}
        removed_ids = self._last_seen - current_ids

        snapshots: list[MarketSnapshot] = []
        now = datetime.utcnow()

        for proj in projections:
            if (
                self._active_sports
                and str(proj.sport or "").strip().upper()
                not in self._active_sports
            ):
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
            provider_status = proj.provider_status,
             is_moneyline  = proj.is_moneyline,
             is_game_market = proj.is_game_market,
             opponent    = getattr(proj, "opponent", None) or None,
            event_teams = proj.event_teams,
            available_directions = proj.available_directions,
            line_variant = proj.line_variant,
            game_id = proj.game_id,
            original_stat_type = proj.stat_type,
            market_id = proj.market_id,
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
                    provider_status = old.provider_status,
                     is_moneyline  = old.is_moneyline,
                     is_game_market = old.is_game_market,
                     event_teams = old.event_teams,
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
                        self.last_fetch_error = f"HTTP {resp.status}"
                        return None

                    raw = await resp.json(content_type=None)
                        
                    projections = self._parse(raw)

                    logger.info(
                        "Underdog parsed %d projections",
                                                            len(projections),
                     )

                    return projections
        except aiohttp.ClientError as exc:
            self.last_fetch_error = str(exc)[:200] or type(exc).__name__
            logger.warning("Underdog request error: %s", exc)
            return None

        except Exception as exc:  # noqa: BLE001
            self.last_fetch_error = str(exc)[:200] or type(exc).__name__
            logger.warning("Underdog unexpected error: %s", exc)
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
          - Event metadata has appeared under ``games``, ``matches``, ``events``,
            ``fixtures``, and ``solo_games`` with several equivalent
            ID/time/status field names. Join only on explicit provider IDs;
            never infer an event from teams.
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
        for app in _records(data.get("appearances", [])):
            aid = app.get("id", "")
            if aid:
                appearances[aid] = app

        event_lookup = _build_event_lookup(data)
        team_names_by_id = _build_team_name_index(data, event_lookup)

        for line in _records(data.get("over_under_lines", [])):
            raw_stat = (line.get("over_under") or {}).get("appearance_stat") or {}
            raw_app = appearances.get(raw_stat.get("appearance_id", ""), {})
            raw_player = players.get(raw_app.get("player_id", ""), {})
            raw_sport = str(raw_player.get("sport_id", "")).upper()
            raw_market = str(raw_stat.get("display_stat", "Unknown"))
            coverage.raw("Underdog", raw_sport, raw_market, str(line.get("id", "")))
            try:
                stat_value = line.get("stat_value")
                if stat_value is None:
                    coverage.failures[("Underdog", "missing line")] += 1
                    coverage.raw_rejections[("Underdog", raw_sport, raw_market, str(line.get("id", "")))] = "missing line"
                    continue
                if not math.isfinite(float(stat_value)) or float(stat_value) < 0:
                    raise ValueError("non-finite or negative line")

                # v1: appearance_stat is nested inside the embedded over_under object
                over_under      = line.get("over_under") or {}
                appearance_stat = over_under.get("appearance_stat") or {}

                appearance_id = appearance_stat.get("appearance_id", "")
                display_stat  = appearance_stat.get("display_stat", "Unknown")

                # Resolve player and event through the top-level appearances table.
                app       = appearances.get(appearance_id, {})
                player_id = app.get("player_id", "")
                event_id, event_data = _resolve_event(
                    [line, over_under, appearance_stat, app],
                    event_lookup,
                )

                p_data      = players.get(player_id, {})
                from engine.identity import display_player_name
                player_name = display_player_name(
                    first_name=p_data.get("first_name"),
                    last_name=p_data.get("last_name"),
                )
                # v1 players carry team_id (UUID string) — no nested team dict
                team  = p_data.get("team_id", "")
                home_team, away_team = _event_team_references(
                    event_data,
                    names_by_id=team_names_by_id,
                )
                event_teams = tuple(
                    reference
                    for reference in (home_team, away_team)
                    if reference[0] or reference[1]
                )
                sport = p_data.get("sport_id", "Unknown").upper()

                game_time = _event_time(event_data)
                provider_status = _event_status(event_data)
                if game_time is None:
                    for source in (line, over_under, appearance_stat, app):
                        game_time = _event_time(source)
                        if game_time is not None:
                            break
                if not provider_status:
                    for source in (line, over_under, appearance_stat, app):
                        provider_status = _event_status(source)
                        if provider_status:
                            break

                projections.append(UnderdogProjection(
                    external_id = line.get("id", ""),
                    player_name = player_name,
                    team        = team,
                    sport       = sport,
                    stat_type   = display_stat,
                    line_value  = float(stat_value),
                    game_id     = event_id,
                    game_time   = game_time,
                    provider_status = provider_status,
                    fetched_at  = datetime.utcnow(),
                    event       = _event_label(event_data),
                    opponent    = _opponent_for_player(
                        team,
                        {"id": home_team[0], "name": home_team[1]},
                        {"id": away_team[0], "name": away_team[1]},
                    ),
                    event_teams = event_teams,
                    available_directions = directions_from(line.get("options") or line.get("available_directions") or ()),
                    line_variant = str(line.get("line_variant") or line.get("odds_type") or ""),
                    market_id = str(appearance_stat.get("id") or ""),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                coverage.failures[("Underdog", "invalid provider record")] += 1
                coverage.raw_rejections[("Underdog", raw_sport, raw_market, str(line.get("id", "")))] = str(exc)
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
