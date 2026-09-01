"""
PropLine-backed PrizePicks provider.

PropLine is an authorized, third-party source for PrizePicks projections.  This
module deliberately knows nothing about Underdog or sportsbook markets.  It
normalizes only PrizePicks player props into the shared ``PlayerProp`` model.

The official PropLine API base URL is used by default.  ``PROPLINE_API_URL``
may override it only when PropLine documents a different authorized endpoint.
The client never invents an API path or sends credentials to another host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

import aiohttp

from engine.sport_policy import is_football_related_sport
from providers.prop_provider import PlayerProp, PropProviderBase

logger = logging.getLogger(__name__)

PROPLINE_PROVIDER_NAME = "PrizePicks"
DEFAULT_PROPLINE_API_URL = "https://api.prop-line.com"

_STAT_NORMALIZATION = {
    "pts": "points",
    "points": "points",
    "reb": "rebounds",
    "rebs": "rebounds",
    "rebounds": "rebounds",
    "ast": "assists",
    "asts": "assists",
    "assists": "assists",
    "3pm": "3-pointers made",
    "3-pointers made": "3-pointers made",
    "threes": "3-pointers made",
    "blk": "blocks",
    "blocks": "blocks",
    "stl": "steals",
    "steals": "steals",
    "pts+reb+ast": "pts+reb+ast",
    "pra": "pts+reb+ast",
    "points_assists": "points+assists",
    "points_rebounds": "points+rebounds",
    "rebounds_assists": "rebounds+assists",
    "points_rebounds_assists": "pts+reb+ast",
    "fantasy points": "fantasy points",
    "hits": "hits",
    "hr": "home runs",
    "home runs": "home runs",
    "rbis": "rbis",
    "strikeouts": "strikeouts",
    "pitcher strikeouts": "strikeouts",
    "walks": "walks",
    "total bases": "total bases",
    "rushing yards": "rushing yards",
    "receiving yards": "receiving yards",
    "receptions": "receptions",
    "passing yards": "passing yards",
    "passing tds": "passing tds",
    "rush+rec yards": "rush+rec yards",
    "shots": "shots on goal",
    "shots on goal": "shots on goal",
    "goals+assists": "points",
    "games won": "games won",
    "sets won": "sets won",
    "kills": "kills",
    "deaths": "deaths",
    "maps won": "maps won",
}

_SPORT_NORMALIZATION = {
    "BASEBALL": "MLB",
    "BASEBALL MLB": "MLB",
    "BASKETBALL": "NBA",
    "BASKETBALL NBA": "NBA",
    "FOOTBALL": "NFL",
    "FOOTBALL NFL": "NFL",
    "AMERICAN FOOTBALL": "NFL",
    "HOCKEY": "NHL",
    "HOCKEY NHL": "NHL",
    "WOMENS BASKETBALL": "WNBA",
    "WOMEN'S BASKETBALL": "WNBA",
    "BASKETBALL WNBA": "WNBA",
}

_OVER_UNDER = {"OVER", "UNDER"}
_EXPLICIT_NON_PLAYER_MARKETS = (
    "moneyline",
    "money line",
    "h2h",
    "spread",
    "game total",
    "team total",
    "total points",
    "winner",
    "match winner",
    "first basket",
)


class PropLineError(RuntimeError):
    """Base error for a failed authorized PropLine request."""


class PropLineRateLimitError(PropLineError):
    """PropLine rejected a request because of rate limiting or quota."""

    def __init__(self, message: str, retry_after: float = 300.0) -> None:
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after))


class PropLineNotFoundError(PropLineError):
    """PropLine returned a 404 for an event that may have rotated."""


class PropLineRequestBudget:
    """Process-wide-safe daily request counter for PropLine calls.

    The counter is deliberately synchronous: each client reserves a request
    while holding its own async lock, and the short synchronous mutation keeps
    separate clients from racing past the shared daily limit.
    """

    def __init__(self, limit: int = 5_000, *, day_factory: Callable[[], Any] | None = None) -> None:
        self.limit = max(1, int(limit))
        self._day_factory = day_factory or (lambda: datetime.utcnow().date())
        self._day = self._day_factory()
        self._count = 0

    @property
    def count(self) -> int:
        self._reset_if_needed()
        return self._count

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.count)

    def _reset_if_needed(self) -> None:
        today = self._day_factory()
        if today != self._day:
            self._day = today
            self._count = 0

    def reserve(self) -> bool:
        self._reset_if_needed()
        if self._count >= self.limit:
            return False
        self._count += 1
        return True

    def reset(self) -> None:
        self._day = self._day_factory()
        self._count = 0


PROPLINE_DAILY_REQUEST_LIMIT = 5_000
_DEFAULT_PROPLINE_REQUEST_BUDGET = PropLineRequestBudget(PROPLINE_DAILY_REQUEST_LIMIT)


@dataclass(frozen=True)
class PropLineFetchResult:
    """Fetch result with provider metadata kept separate from normalized props."""

    props: list[PlayerProp]
    fetched_at: datetime
    from_cache: bool = False
    # An empty, successful response is valid (for example, no currently
    # available projections).  Keep it distinct from a locally blocked,
    # quota-skipped, or failed request so health telemetry is not optimistic.
    succeeded: bool = False
    error: str = ""
    failure_kind: str = ""
    # A result is publishable only when the provider returned the full
    # requested snapshot.  This is separate from ``succeeded`` so a valid
    # empty snapshot can be represented without treating a partial response as
    # current truth.
    complete: bool = False
    discovered_event_count: int = 0
    fetched_event_count: int = 0
    failed_event_ids: tuple[str, ...] = ()
    retained_last_success: bool = False
    partial_props: tuple[PlayerProp, ...] = ()

    @property
    def snapshot_complete(self) -> bool:
        """Compatibility/readability alias for callers tracking completeness."""
        return self.complete

    @property
    def retained(self) -> bool:
        return self.retained_last_success


@dataclass(frozen=True)
class PropLineEvent:
    """Stable event metadata returned by PropLine event discovery."""

    sport_key: str
    event_id: str
    home_team: str = ""
    away_team: str = ""
    start_time: Optional[datetime] = None
    label: str = ""

    @property
    def identity(self) -> str:
        return propline_event_identity(self)


@dataclass(frozen=True)
class PropLineSnapshotState:
    """Last fetch state for one PropLine request scope."""

    complete: bool
    discovered_event_count: int = 0
    fetched_event_count: int = 0
    failed_event_ids: tuple[str, ...] = ()
    last_successful_at: Optional[datetime] = None

    @property
    def snapshot_complete(self) -> bool:
        return self.complete


@dataclass(frozen=True)
class PropLineSport:
    """An active sport advertised by the official PropLine sports endpoint."""

    official_key: str
    canonical_key: str
    title: str = ""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _nested_value(raw: dict[str, Any], *keys: str) -> Any:
    value = _first(raw, *keys)
    if isinstance(value, dict):
        return _first(value, "id", "name", "display_name", "full_name")
    return value


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif value is None:
        return None
    else:
        text = _clean(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _identity_part(value: Any) -> str:
    """Create a stable, case-insensitive identity component."""
    return re.sub(r"\s+", " ", _clean(value)).strip().lower()


def propline_event_identity(
    event: Any,
    *,
    sport_key: str = "",
) -> str:
    """Return a stable event identity without relying on Python's hash()."""
    if isinstance(event, PropLineEvent):
        if event.event_id:
            return f"event:{_identity_part(event.event_id)}"
        home, away, start = event.home_team, event.away_team, event.start_time
        sport_key = event.sport_key or sport_key
    elif isinstance(event, dict):
        event_id = _clean(_first(event, "id", "event_id", "game_id", "match_id"))
        if event_id:
            return f"event:{_identity_part(event_id)}"
        home = _first(event, "home_team", "home", "home_name")
        away = _first(event, "away_team", "away", "away_name")
        start = _first(event, "commence_time", "start_time", "event_start", "game_time")
        sport_key = _first(event, "sport_key", "sport", "league") or sport_key
    else:
        return f"event:{_identity_part(event)}"
    start_time = _parse_datetime(start)
    start_value = start_time.isoformat() if start_time else _identity_part(start)
    return ":".join(
        (
            "event",
            _identity_part(sport_key),
            _identity_part(home),
            _identity_part(away),
            start_value,
        )
    )


def propline_player_identity(player: Any, *, player_id: str = "") -> str:
    """Return a stable player identity, preferring the provider identifier."""
    if isinstance(player, dict):
        player_id = _clean(_first(player, "id", "player_id", "athlete_id")) or player_id
        player = _first(player, "name", "display_name", "full_name")
    return f"player:{_identity_part(player_id or player)}"


def propline_market_identity(
    market: Any,
    *,
    stat_type: str = "",
    direction: str = "",
) -> str:
    """Return a stable player-market identity; line movement is not identity."""
    if isinstance(market, dict):
        market = _first(market, "key", "market_key", "market", "name", "stat", "stat_type")
    market_text = _clean(market or stat_type).lower()
    if market_text.startswith("player_"):
        market_text = market_text.removeprefix("player_")
    normalized_market = _normalize_stat(market_text)
    normalized_direction = _normalize_direction(direction)
    return ":".join(
        (
            "market",
            _identity_part(normalized_market),
            _identity_part(normalized_direction),
        )
    )


def propline_prop_identity(
    *,
    sport_key: str,
    event: Any,
    player: Any,
    market: Any,
    player_id: str = "",
    direction: str = "",
) -> str:
    """Return a stable event/player/market key for deduplication and history."""
    return "|".join(
        (
            propline_event_identity(event, sport_key=sport_key),
            propline_player_identity(player, player_id=player_id),
            propline_market_identity(market, direction=direction),
        )
    )


def _normalize_sport(value: Any) -> str:
    sport = _clean(value).upper().replace("_", " ")
    return _SPORT_NORMALIZATION.get(sport, sport)


def _normalize_stat(value: Any) -> str:
    raw = re.sub(r"\s+", " ", _clean(value).lower())
    return _STAT_NORMALIZATION.get(raw, raw)


def _normalize_direction(value: Any) -> str:
    text = _clean(value).upper()
    if text in _OVER_UNDER:
        return text
    match = re.search(r"\b(OVER|UNDER)\b", text)
    return match.group(1) if match else ""


def _is_prizepicks_player_prop(raw: dict[str, Any]) -> bool:
    """Strictly reject records outside PropLine's PrizePicks prop scope."""
    provider = _clean(
        _first(raw, "provider", "source", "platform", "book", "bookmaker")
    ).lower()
    if provider and provider.replace("_", "").replace("-", "").replace(" ", "") not in {
        "prizepicks",
        "prizepick",
    }:
        return False

    market = _clean(
        _first(raw, "market_type", "market", "market_key", "category", "type")
    ).lower().replace("_", " ")
    if any(term in market for term in _EXPLICIT_NON_PLAYER_MARKETS):
        return False
    if market and not (
        "player" in market
        or "prop" in market
        or market in {"projection", "projections", "line", "lines"}
    ):
        return False
    return True


def normalize_propline_prizepicks_record(
    raw_record: dict[str, Any],
    *,
    fetched_at: Optional[datetime] = None,
) -> Optional[PlayerProp]:
    """
    Normalize one PropLine record, returning ``None`` for unsafe records.

    The endpoint is expected to be PrizePicks-specific, but provider and market
    fields are still checked on every row so a mixed response can never leak
    Underdog, sportsbook, moneyline, or game-total data into this pipeline.
    """
    if not isinstance(raw_record, dict) or not _is_prizepicks_player_prop(raw_record):
        return None

    player = _first(raw_record, "player_name", "player", "athlete", "name")
    if isinstance(player, dict):
        player_id = _clean(_first(player, "id", "player_id", "athlete_id"))
        player_name = _clean(_first(player, "name", "display_name", "full_name"))
    else:
        player_id = _clean(_first(raw_record, "player_id", "athlete_id"))
        player_name = _clean(player)
    if not player_name:
        return None

    sport = _normalize_sport(
        _first(raw_record, "sport", "league", "sport_key", "league_name")
    )
    stat_type = _normalize_stat(
        _first(raw_record, "stat_type", "stat", "market_name", "prop_type", "category")
    )
    if not sport or not stat_type or is_football_related_sport(sport):
        return None

    raw_line = _first(raw_record, "line_value", "line", "point", "threshold", "value")
    try:
        line_value = float(raw_line)
    except (TypeError, ValueError):
        return None

    event = _first(raw_record, "event", "game", "match", "game_description")
    if isinstance(event, dict):
        event_label = _clean(_first(event, "name", "display_name", "description"))
        event_id = _clean(_first(event, "id", "event_id", "game_id"))
        event_time = _parse_datetime(
            _first(event, "game_time", "start_time", "event_start", "commence_time")
        )
        event_status = _clean(
            _first(event, "status", "state", "event_status", "game_status")
        )
    else:
        event_label = _clean(event)
        event_id = _clean(_first(raw_record, "event_id", "game_id", "match_id"))
        event_time = None
        event_status = ""

    raw_direction = _first(raw_record, "direction", "side", "selection", "over_under")
    direction = _normalize_direction(raw_direction)
    stable_event = propline_event_identity(
        event if isinstance(event, dict) else {
            "id": event_id,
            "name": event_label,
            "start_time": _first(
                raw_record, "game_time", "start_time", "event_start", "commence_time"
            ),
            "sport_key": sport,
        },
        sport_key=sport,
    )
    stable_player = propline_player_identity(
        player, player_id=player_id
    )
    stable_market = propline_market_identity(
        _first(raw_record, "market_type", "market", "market_key", "category", "type"),
        stat_type=stat_type,
        direction=direction,
    )
    source_id = _clean(
        _first(raw_record, "external_id", "source_id", "prop_id", "projection_id", "id")
    ) or "|".join((stable_event, stable_player, stable_market))
    provider_timestamp = _parse_datetime(
        _first(
            raw_record,
            "provider_timestamp",
            "observed_at",
            "timestamp",
            "updated_at",
            "last_updated",
            "fetched_at",
        )
    )
    observed_at = fetched_at or datetime.utcnow()
    team = _clean(_nested_value(raw_record, "team", "team_name"))
    provider_status = event_status or _clean(
        _first(raw_record, "status", "availability", "projection_status")
    )

    return PlayerProp(
        provider=PROPLINE_PROVIDER_NAME,
        sport=sport,
        player_name=player_name,
        team=team,
        stat_type=stat_type,
        line_value=line_value,
        game_time=_parse_datetime(
            _first(raw_record, "game_time", "start_time", "event_start", "commence_time")
        ) or event_time,
        external_id=source_id,
        game_id=event_id or stable_event,
        fetched_at=observed_at,
        direction=direction,
        player_id=player_id,
        event_id=event_id,
        event=event_label,
        provider_status=provider_status,
        source_timestamp=provider_timestamp,
    )


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "props", "lines", "projections", "results", "items", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_records(value)
            if nested:
                return nested
    return []


def _pagination_details(payload: Any) -> tuple[Optional[str], Optional[bool], Optional[int]]:
    """Read common cursor/page metadata without assuming one API envelope."""
    if not isinstance(payload, dict):
        return None, None, None
    metadata: dict[str, Any] = dict(payload)
    for key in ("pagination", "paging", "meta"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            metadata.update(nested)

    next_cursor = _first(
        metadata,
        "next_cursor",
        "nextCursor",
        "next_page_token",
        "nextPageToken",
        "continuation",
        "continuation_token",
    )
    links = metadata.get("links")
    if not next_cursor and isinstance(links, dict):
        next_cursor = _first(links, "next", "next_cursor", "next_url")
    if isinstance(next_cursor, dict):
        next_cursor = _first(next_cursor, "cursor", "token", "url")
    next_cursor = _clean(next_cursor) or None

    has_more = _first(metadata, "has_more", "hasMore", "more")
    if has_more is not None:
        has_more = bool(has_more)
    total_pages = _first(metadata, "total_pages", "totalPages", "page_count")
    try:
        total_pages = int(total_pages) if total_pages is not None else None
    except (TypeError, ValueError):
        total_pages = None
    return next_cursor, has_more, total_pages


def _page_fingerprint(payload: Any) -> str:
    """Detect a provider repeating a page, even when it changes cursors."""
    try:
        encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = repr(payload)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _official_event_records(payload: Any) -> list[dict[str, Any]]:
    """Flatten event discovery envelopes while retaining event metadata."""
    return _extract_records(payload)


def _official_events(payload: Any, sport_key: str) -> list[PropLineEvent]:
    events: list[PropLineEvent] = []
    seen: set[str] = set()
    for raw in _official_event_records(payload):
        event_id = _clean(_first(raw, "id", "event_id", "game_id", "match_id"))
        home = _clean(_first(raw, "home_team", "home", "home_name"))
        away = _clean(_first(raw, "away_team", "away", "away_name"))
        start = _parse_datetime(
            _first(raw, "commence_time", "start_time", "event_start", "game_time")
        )
        label = _clean(_first(raw, "name", "display_name", "description")) or (
            f"{away} at {home}" if away and home else away or home or event_id
        )
        identity = propline_event_identity(
            {
                "id": event_id,
                "home_team": home,
                "away_team": away,
                "start_time": start,
                "sport_key": sport_key,
            },
            sport_key=sport_key,
        )
        if identity in seen:
            continue
        seen.add(identity)
        events.append(
            PropLineEvent(
                sport_key=sport_key,
                event_id=event_id or identity.removeprefix("event:"),
                home_team=home,
                away_team=away,
                start_time=start,
                label=label,
            )
        )
    return events


_OFFICIAL_MARKETS = (
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_blocks",
    "player_steals",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
    "player_points_rebounds_assists",
    "player_hits",
    "player_home_runs",
    "player_rbis",
    "player_total_bases",
    "player_strikeouts",
    "player_walks",
    "player_rushing_yards",
    "player_receiving_yards",
    "player_receptions",
    "player_passing_yards",
    "player_passing_tds",
    "player_rush_rec_yards",
    "player_shots_on_goal",
    "player_games_won",
    "player_sets_won",
    "player_kills",
    "player_deaths",
    "player_maps_won",
)

_CANONICAL_TO_OFFICIAL_SPORT = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NFL": "football_nfl",
    "FOOTBALL": "football_nfl",
    "NCAAF": "football_ncaaf",
    "CFB": "football_ncaaf",
    "NCAAB": "basketball_ncaab",
    "NHL": "hockey_nhl",
    "WNBA": "basketball_wnba",
    "UFC": "mma_ufc",
    "MMA": "mma_ufc",
    "TENNIS": "tennis",
    "GOLF": "golf",
    "PGA": "golf",
    "EPL": "soccer_epl",
    "SOCCER": "soccer_epl",
    "LALIGA": "soccer_la_liga",
    "SERIEA": "soccer_serie_a",
    "BUNDESLIGA": "soccer_bundesliga",
    "LIGUE1": "soccer_ligue_1",
    "MLS": "soccer_mls",
    "UCL": "soccer_uefa_champions_league",
    "FIFA": "soccer_fifa_world_cup",
    "CFL": "football_cfl",
    "AFL": "aussie_rules_afl",
}

_OFFICIAL_TO_CANONICAL_SPORT = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "football_nfl": "NFL",
    "americanfootball_nfl": "NFL",
    "hockey_nhl": "NHL",
    "icehockey_nhl": "NHL",
    "basketball_wnba": "WNBA",
    "basketball_ncaab": "NCAAB",
    "football_ncaaf": "NCAAF",
    "americanfootball_ncaaf": "NCAAF",
    "mma_ufc": "UFC",
    "mma_mixed_martial_arts": "UFC",
    "tennis": "TENNIS",
    "golf": "GOLF",
    "soccer_epl": "EPL",
    "soccer_la_liga": "LALIGA",
    "soccer_serie_a": "SERIEA",
    "soccer_bundesliga": "BUNDESLIGA",
    "soccer_ligue_1": "LIGUE1",
    "soccer_mls": "MLS",
    "soccer_uefa_champions_league": "UCL",
    "soccer_fifa_world_cup": "FIFA",
    "football_cfl": "CFL",
    "aussie_rules_afl": "AFL",
}


def _canonical_discovered_sport(official_key: str, title: Any = "") -> str:
    """Map provider sport identifiers into the bot's existing sport namespace."""
    key = _clean(official_key).lower()
    if key in _OFFICIAL_TO_CANONICAL_SPORT:
        return _OFFICIAL_TO_CANONICAL_SPORT[key]

    normalized_title = _clean(title).upper().replace("_", " ")
    if normalized_title in {"AMERICAN FOOTBALL", "FOOTBALL"}:
        return "FOOTBALL"
    if normalized_title in {"COLLEGE FOOTBALL", "NCAAF", "CFB"}:
        return "NCAAF"
    if normalized_title in {"GOLF", "PGA"}:
        return "GOLF"
    if normalized_title in {"TENNIS", "MMA", "UFC"}:
        return normalized_title
    if normalized_title.startswith("FIFA"):
        return "FIFA"
    if normalized_title in {"ESPORTS", "CS", "CS2", "COUNTER STRIKE"}:
        return "CS" if normalized_title != "ESPORTS" else "ESPORTS"
    return _normalize_sport(title or official_key)


def _official_sports_records(payload: Any) -> list[PropLineSport]:
    """Parse active entries from the documented ``GET /v1/sports`` response."""
    if isinstance(payload, dict):
        for key in ("data", "sports", "results", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []

    records: list[PropLineSport] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        official_key = _clean(_first(item, "key", "sport_key", "id"))
        if not official_key:
            continue
        available = item.get("active", item.get("available", True))
        if available is False:
            continue
        canonical = _canonical_discovered_sport(
            official_key, _first(item, "title", "name", "sport_name")
        )
        if not canonical or official_key.lower() in seen:
            continue
        seen.add(official_key.lower())
        records.append(
            PropLineSport(
                official_key=official_key,
                canonical_key=canonical,
                title=_clean(_first(item, "title", "name", "sport_name")),
            )
        )
    return records



def _official_odds_records(payload: Any) -> list[dict[str, Any]]:
    """Flatten the documented PropLine odds response into normalizer records."""
    if isinstance(payload, dict) and payload.get("bookmakers") is not None:
        payload = [payload]
    elif isinstance(payload, dict):
        nested = _extract_records(payload)
        payload = nested
    if not isinstance(payload, list):
        return []

    records: list[dict[str, Any]] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        sport_key = _clean(event.get("sport_key"))
        event_id = _clean(event.get("id"))
        home = _clean(event.get("home_team"))
        away = _clean(event.get("away_team"))
        event_label = (
            f"{away} at {home}"
            if away and home
            else away or home or event_id
        )
        event_data = {
            "id": event_id,
            "name": event_label,
            "start_time": event.get("commence_time"),
        }
        for bookmaker in event.get("bookmakers") or []:
            if not isinstance(bookmaker, dict):
                continue
            bookmaker_key = _clean(bookmaker.get("key")).lower().replace("_", "")
            if bookmaker_key not in {"prizepicks", "prizepick"}:
                continue
            for market in bookmaker.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                market_key = _clean(market.get("key")).lower()
                if not market_key.startswith("player_"):
                    continue
                stat_key = market_key.removeprefix("player_")
                for outcome in market.get("outcomes") or []:
                    if not isinstance(outcome, dict):
                        continue
                    odds_type = _clean(outcome.get("dfs_odds_type")).lower()
                    if odds_type and odds_type != "standard":
                        continue
                    records.append(
                        {
                            "provider": "PrizePicks",
                            "market_type": market_key,
                            "sport": _canonical_discovered_sport(sport_key),
                            "player": outcome.get("description"),
                            "player_id": outcome.get("player_id"),
                            "stat": stat_key,
                            "line": outcome.get("point"),
                            "direction": outcome.get("name"),
                            "event": event_data,
                            "external_id": outcome.get("book_outcome_id")
                            or f"{event_id}:{market_key}:{outcome.get('description', '')}:{odds_type or 'standard'}",
                            # The odds endpoint can return a currently
                            # available line whose last-change timestamp is
                            # older than the fetch.  The client fetch time is
                            # the authoritative freshness marker for this
                            # current-line response.
                            "status": "available",
                        }
                    )
    return records


class PropLineClient:
    """Small, cached, rate-limited HTTP client for the authorized feed."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        session: Any = None,
        min_request_interval: float = 120.0,
        cache_ttl: float = 240.0,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        request_budget: Optional[PropLineRequestBudget] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("PROPLINE_API_KEY", "")
        self.api_url = (
            api_url
            if api_url is not None
            else os.getenv("PROPLINE_API_URL", DEFAULT_PROPLINE_API_URL)
        ).strip()
        self._session = session
        self._owns_session = session is None
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_request_at: Optional[float] = None
        self._backoff_until = 0.0
        self._cache: Optional[PropLineFetchResult] = None
        self._cache_clock_at: Optional[float] = None
        self._cache_scope = ""
        self._cache_by_scope: dict[str, PropLineFetchResult] = {}
        self._cache_clock_by_scope: dict[str, float] = {}
        self._sports_cache: tuple[PropLineSport, ...] = ()
        self._sports_cache_clock_at: Optional[float] = None
        self.request_budget = request_budget or _DEFAULT_PROPLINE_REQUEST_BUDGET
        self._request_day = datetime.utcnow().date()
        self._request_count = 0
        self._last_success_by_scope: dict[str, PropLineFetchResult] = {}
        self._snapshot_state_by_scope: dict[str, PropLineSnapshotState] = {}
        self._events_cache_by_sport: dict[str, tuple[PropLineEvent, ...]] = {}
        self._events_cache_clock_by_sport: dict[str, float] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_url)

    async def __aenter__(self) -> "PropLineClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _cache_is_fresh(self, now: float, scope: str) -> bool:
        cached = self._cache_by_scope.get(scope)
        cached_at = self._cache_clock_by_scope.get(scope)
        return bool(
            cached
            and self.cache_ttl > 0
            and cached_at is not None
            and (now - cached_at) < self.cache_ttl
        )

    def _reset_daily_budget_if_needed(self) -> None:
        self.request_budget._reset_if_needed()
        today = datetime.utcnow().date()
        if today != self._request_day:
            self._request_day = today
            self._request_count = 0

    @property
    def total_request_count(self) -> int:
        """Requests reserved by the shared daily PropLine budget."""
        return self.request_budget.count

    @property
    def remaining_request_budget(self) -> int:
        return self.request_budget.remaining

    @property
    def daily_request_limit(self) -> int:
        """Compatibility view of the centralized daily request limit."""
        return self.request_budget.limit

    @daily_request_limit.setter
    def daily_request_limit(self, value: int) -> None:
        self.request_budget.limit = max(1, int(value))

    def _reserve_request(self) -> bool:
        self._reset_daily_budget_if_needed()
        if not self.request_budget.reserve():
            return False
        self._request_count += 1
        return True

    async def _request_json(self, url: str, params: dict[str, str]) -> Any:
        """Perform one authorized request and account for it centrally."""
        now = self._clock()
        if now < self._backoff_until:
            raise PropLineRateLimitError(
                "PropLine rate-limit backoff active",
                retry_after=max(1.0, self._backoff_until - now),
            )
        if self._last_request_at is not None:
            delay = self.min_request_interval - (now - self._last_request_at)
            if delay > 0:
                await self._sleep(delay)
        self._last_request_at = self._clock()
        if not self._reserve_request():
            raise PropLineError("PropLine daily request budget exhausted")

        session = self._session
        owns_local_session = session is None
        if session is None:
            session = aiohttp.ClientSession()
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "X-API-Key": self.api_key,
                "Accept": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with session.get(
                url, headers=headers, params=params, timeout=timeout
            ) as response:
                if response.status == 404:
                    raise PropLineNotFoundError(f"PropLine resource not found (HTTP {response.status})")
                if response.status == 429:
                    retry_after = _retry_after_seconds(response)
                    self._backoff_until = self._clock() + retry_after
                    raise PropLineRateLimitError(
                        f"PropLine rate limited (HTTP {response.status})",
                        retry_after=retry_after,
                    )
                if response.status in {408, 425, 502, 503, 504}:
                    retry_after = min(900.0, max(30.0, 2.0 * self.min_request_interval))
                    self._backoff_until = self._clock() + retry_after
                    raise PropLineError(
                        f"PropLine temporarily unavailable (HTTP {response.status})"
                    )
                if response.status >= 400:
                    raise PropLineError(f"PropLine request failed (HTTP {response.status})")
                return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise
        finally:
            if owns_local_session:
                await session.close()

    async def _paginate_json(
        self,
        url: str,
        params: dict[str, str],
        *,
        extractor: Callable[[Any], list[dict[str, Any]]],
    ) -> tuple[list[dict[str, Any]], bool, str]:
        """Follow provider pagination until exhaustion, with loop protection.

        There is intentionally no arbitrary page-count ceiling.  A repeated
        cursor/request or repeated response page is the only stop condition
        besides normal provider exhaustion.
        """
        base_params = dict(params)
        current_params = dict(params)
        page_number = 1
        seen_requests: set[str] = set()
        seen_pages: set[str] = set()
        records: list[dict[str, Any]] = []

        while True:
            request_signature = json.dumps(
                [url, sorted(current_params.items())], separators=(",", ":")
            )
            if request_signature in seen_requests:
                return records, False, "PropLine pagination loop detected"
            seen_requests.add(request_signature)

            payload = await self._request_json(url, current_params)
            page_records = extractor(payload)
            page_signature = _page_fingerprint(page_records)
            if page_signature in seen_pages:
                return records, False, "PropLine pagination repeated a response page"
            seen_pages.add(page_signature)
            records.extend(page_records)

            next_cursor, has_more, total_pages = _pagination_details(payload)
            if next_cursor:
                current_params = dict(base_params)
                current_params["cursor"] = next_cursor
                continue
            if total_pages is not None and page_number < total_pages:
                page_number += 1
                current_params = dict(base_params)
                current_params["page"] = str(page_number)
                continue
            if has_more:
                page_number += 1
                current_params = dict(base_params)
                current_params["page"] = str(page_number)
                continue
            return records, True, ""

    def _official_request(
            self,
            sport_key: Optional[str],
            bookmaker: str = "prizepicks",
    ) -> tuple[str, dict[str, str], str]:
        base_url = self.api_url.rstrip("/")
        if base_url == DEFAULT_PROPLINE_API_URL:
            official_sport = _clean(sport_key)
            if not official_sport:
                raise PropLineError("PropLine sport key is required for the official odds route")
            return (
                f"{base_url}/v1/sports/{official_sport}/odds",
                {
                    "markets": ",".join(_OFFICIAL_MARKETS),
                    "bookmakers": bookmaker,
                },
                f"{official_sport}:{bookmaker.lower()}",
            )
        return (
            self.api_url,
            {"provider": "prizepicks", "market": "player_props"},
            f"{sport_key or '__custom__'}:{bookmaker.lower()}",
        )

    def _official_sports_request(self) -> tuple[str, dict[str, str]]:
        return f"{self.api_url.rstrip('/')}/v1/sports", {}

    def _official_events_request(self, sport_key: str) -> tuple[str, dict[str, str]]:
        official_sport = _clean(sport_key)
        if not official_sport:
            raise PropLineError("PropLine sport key is required for event discovery")
        return f"{self.api_url.rstrip('/')}/v1/sports/{quote(official_sport, safe='')}/events", {}

    def _official_event_odds_request(
        self,
        sport_key: str,
        event_id: str,
        bookmaker: str = "prizepicks",
    ) -> tuple[str, dict[str, str], str]:
        official_sport = _clean(sport_key)
        official_event = _clean(event_id)
        if not official_sport or not official_event:
            raise PropLineError("PropLine sport and event keys are required for event odds")
        return (
            f"{self.api_url.rstrip('/')}/v1/sports/{quote(official_sport, safe='')}"
            f"/events/{quote(official_event, safe='')}/odds",
            {
                "markets": ",".join(_OFFICIAL_MARKETS),
                "bookmakers": bookmaker,
            },
            f"{official_sport}:event:{official_event}:{bookmaker.lower()}",
        )

    async def _discover_events_locked(
        self,
        sport_key: str,
        *,
        refresh: bool = False,
    ) -> tuple[list[PropLineEvent], bool, str]:
        canonical_scope = _clean(sport_key).lower()
        now = self._clock()
        if (
            not refresh
            and self.cache_ttl > 0
            and canonical_scope in self._events_cache_by_sport
            and (now - self._events_cache_clock_by_sport.get(canonical_scope, 0)) < self.cache_ttl
        ):
            return list(self._events_cache_by_sport[canonical_scope]), True, ""
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return [], False, "PropLine event discovery requires the official endpoint"
        try:
            url, params = self._official_events_request(sport_key)
            raw_events, complete, error = await self._paginate_json(
                url, params, extractor=_official_event_records
            )
        except PropLineRateLimitError as exc:
            return [], False, str(exc)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return [], False, f"PropLine event discovery failed: {type(exc).__name__}"
        except PropLineError as exc:
            return [], False, str(exc)
        if not complete:
            return [], False, error or "PropLine event discovery incomplete"
        events = _official_events(raw_events, sport_key)
        self._events_cache_by_sport[canonical_scope] = tuple(events)
        self._events_cache_clock_by_sport[canonical_scope] = self._clock()
        return events, True, ""

    async def discover_events(
        self,
        sport_key: str,
        *,
        refresh: bool = False,
    ) -> list[PropLineEvent]:
        """Discover every event for a sport, following cursors/pages safely."""
        if not self.is_configured:
            return []
        async with self._lock:
            events, complete, error = await self._discover_events_locked(
                sport_key, refresh=refresh
            )
            if not complete:
                logger.warning("PropLine event discovery incomplete: %s", error)
                return list(self._events_cache_by_sport.get(_clean(sport_key).lower(), ()))
            return events

    @staticmethod
    def _event_can_be_matched(target: Any, candidate: PropLineEvent) -> bool:
        if isinstance(target, PropLineEvent):
            target_id = target.event_id
            target_home, target_away, target_start = (
                target.home_team, target.away_team, target.start_time
            )
        elif isinstance(target, dict):
            target_id = _clean(_first(target, "id", "event_id", "game_id", "match_id"))
            target_home = _clean(_first(target, "home_team", "home", "home_name"))
            target_away = _clean(_first(target, "away_team", "away", "away_name"))
            target_start = _parse_datetime(
                _first(target, "commence_time", "start_time", "event_start", "game_time")
            )
        else:
            return False
        if target_id and target_id == candidate.event_id:
            return True
        if not (target_home and target_away and target_start and candidate.start_time):
            return False
        return (
            _identity_part(target_home) == _identity_part(candidate.home_team)
            and _identity_part(target_away) == _identity_part(candidate.away_team)
            and target_start == candidate.start_time
        )

    async def fetch_event_props(
        self,
        sport_key: str,
        event: Any,
        *,
        bookmaker: str = "prizepicks",
    ) -> PropLineFetchResult:
        """Fetch one event's player props; rediscover once if its id is stale."""
        if not self.is_configured:
            return PropLineFetchResult(
                [], datetime.utcnow(), error="PropLine is not configured",
                failure_kind="disabled",
            )
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return PropLineFetchResult(
                [], datetime.utcnow(),
                error="PropLine event odds require the official endpoint",
                failure_kind="configuration",
            )
        if isinstance(event, PropLineEvent):
            event_id = event.event_id
        elif isinstance(event, dict):
            event_id = _clean(_first(event, "id", "event_id", "game_id", "match_id"))
        else:
            event_id = _clean(event)
        if not event_id:
            return PropLineFetchResult(
                [], datetime.utcnow(), error="PropLine event id is required",
                failure_kind="configuration",
            )

        async with self._lock:
            result = await self._fetch_event_props_locked(
                sport_key, event_id, bookmaker=bookmaker
            )
            if result.failure_kind != "not_found":
                return result

            # Event ids can rotate between discovery and odds retrieval.  Only
            # retry after matching immutable event fields, never by guessing a
            # new id or retrying an arbitrary event.
            events, complete, _ = await self._discover_events_locked(
                sport_key, refresh=True
            )
            if not complete:
                return result
            replacement = next(
                (candidate for candidate in events if self._event_can_be_matched(event, candidate)),
                None,
            )
            if replacement is None or replacement.event_id == event_id:
                return result
            return await self._fetch_event_props_locked(
                sport_key, replacement.event_id, bookmaker=bookmaker
            )

    async def _fetch_event_props_locked(
        self,
        sport_key: str,
        event_id: str,
        *,
        bookmaker: str,
    ) -> PropLineFetchResult:
        url, params, scope = self._official_event_odds_request(
            sport_key, event_id, bookmaker=bookmaker
        )
        try:
            raw_records, complete, error = await self._paginate_json(
                url, params, extractor=_official_odds_records
            )
        except PropLineNotFoundError as exc:
            return self._failed_result(scope, str(exc), "not_found")
        except PropLineRateLimitError as exc:
            return self._failed_result(scope, str(exc), "quota")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return self._failed_result(scope, f"PropLine request failed: {type(exc).__name__}", "http")
        except PropLineError as exc:
            return self._failed_result(
                scope, str(exc), "quota" if "budget" in str(exc).lower() else "http"
            )
        fetched_at = datetime.utcnow()
        props = [
            prop for record in raw_records
            if (prop := normalize_propline_prizepicks_record(record, fetched_at=fetched_at))
            is not None
        ]
        if not complete:
            return self._failed_result(
                scope, error or "PropLine event snapshot incomplete", "incomplete",
                partial_props=props, discovered_event_count=1,
                fetched_event_count=1,
            )
        result = PropLineFetchResult(
            props, fetched_at, succeeded=True, complete=True,
            discovered_event_count=1, fetched_event_count=1,
        )
        self._last_success_by_scope[scope] = result
        self._snapshot_state_by_scope[scope] = PropLineSnapshotState(
            complete=True, discovered_event_count=1, fetched_event_count=1,
            last_successful_at=fetched_at,
        )
        return result

    async def fetch_event_odds(
        self,
        sport_key: str,
        event: Any,
        *,
        bookmaker: str = "prizepicks",
    ) -> PropLineFetchResult:
        """Explicit alias for callers that name the endpoint rather than props."""
        return await self.fetch_event_props(
            sport_key, event, bookmaker=bookmaker
        )

    async def fetch_event_scoped_props(
        self,
        *,
        sport_key: str,
        bookmaker: str = "prizepicks",
    ) -> PropLineFetchResult:
        """Build one complete snapshot from paginated discovery + event odds."""
        if not self.is_configured:
            return PropLineFetchResult(
                [], datetime.utcnow(), error="PropLine is not configured",
                failure_kind="disabled",
            )
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return PropLineFetchResult(
                [], datetime.utcnow(),
                error="PropLine event-scoped requests require the official endpoint",
                failure_kind="configuration",
            )
        scope = f"{_clean(sport_key)}:{bookmaker.lower()}"
        async with self._lock:
            events, discovery_complete, discovery_error = await self._discover_events_locked(
                sport_key
            )
        if not discovery_complete:
            return self._failed_result(
                scope,
                discovery_error or "PropLine event discovery incomplete",
                "incomplete",
            )
        # An empty, complete catalog is a valid empty snapshot.
        if not events:
            result = PropLineFetchResult(
                [], datetime.utcnow(), succeeded=True, complete=True,
            )
            self._last_success_by_scope[scope] = result
            self._snapshot_state_by_scope[scope] = PropLineSnapshotState(
                complete=True, last_successful_at=result.fetched_at,
            )
            return result

        all_props: list[PlayerProp] = []
        failed_ids: list[str] = []
        fetched_count = 0
        for event in events:
            result = await self.fetch_event_props(
                sport_key, event, bookmaker=bookmaker
            )
            if result.succeeded and result.complete:
                all_props.extend(result.props)
                fetched_count += 1
            else:
                failed_ids.append(event.event_id)
        if failed_ids:
            return self._failed_result(
                scope,
                "PropLine event-scoped snapshot incomplete",
                "incomplete",
                partial_props=all_props,
                discovered_event_count=len(events),
                fetched_event_count=fetched_count,
                failed_event_ids=tuple(failed_ids),
            )
        fetched_at = datetime.utcnow()
        result = PropLineFetchResult(
            all_props, fetched_at, succeeded=True, complete=True,
            discovered_event_count=len(events),
            fetched_event_count=fetched_count,
        )
        self._last_success_by_scope[scope] = result
        self._snapshot_state_by_scope[scope] = PropLineSnapshotState(
            complete=True, discovered_event_count=len(events),
            fetched_event_count=fetched_count, last_successful_at=fetched_at,
        )
        return result
    async def discover_sports(self) -> list[PropLineSport]:
        """Return active sports from PropLine without creating a polling loop."""
        if not self.is_configured:
            logger.info("PropLine PrizePicks provider disabled: credentials or endpoint missing")
            return []
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            # Custom endpoints retain the pre-discovery adapter contract.  An
            # unauthorized host must never receive a guessed /v1/sports path.
            return []

        async with self._lock:
            now = self._clock()
            if (
                self.cache_ttl > 0
                and self._sports_cache_clock_at is not None
                and (now - self._sports_cache_clock_at) < self.cache_ttl
            ):
                return list(self._sports_cache)
            try:
                url, params = self._official_sports_request()
                raw_sports, complete, pagination_error = await self._paginate_json(
                    url, params, extractor=_extract_records
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("PropLine sports discovery failed: %s", type(exc).__name__)
                return []
            except PropLineError as exc:
                logger.warning("PropLine sports discovery error: %s", exc)
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("PropLine sports discovery response error: %s", type(exc).__name__)
                return []

            if not complete:
                logger.warning("PropLine sports discovery incomplete: %s", pagination_error)
                return list(self._sports_cache)
            sports = _official_sports_records(raw_sports)
            self._sports_cache = tuple(sports)
            self._sports_cache_clock_at = self._clock()
            return list(sports)

    def official_sport_for(self, canonical_sport: str) -> Optional[str]:
        """Resolve a canonical sport, preferring the last official discovery."""
        canonical = _normalize_sport(canonical_sport)
        for sport in self._sports_cache:
            if sport.canonical_key == canonical:
                return sport.official_key
        return _CANONICAL_TO_OFFICIAL_SPORT.get(canonical)

    async def fetch_prizepicks_props(
            self,
            *,
            sport_key: Optional[str] = None,
            bookmaker: str = "prizepicks",
            event_scoped: bool = False,
        ) -> PropLineFetchResult:
        if not self.is_configured:
            logger.info("PropLine PrizePicks provider disabled: credentials or endpoint missing")
            return PropLineFetchResult(
                [], datetime.utcnow(), from_cache=False,
                error="PropLine is not configured", failure_kind="disabled",
            )
        if (
            self.api_url.rstrip("/") == DEFAULT_PROPLINE_API_URL
            and not _clean(sport_key)
        ):
            logger.warning("PropLine official odds request skipped: sport key is required")
            return PropLineFetchResult(
                [], datetime.utcnow(), from_cache=False,
                error="PropLine sport key is required", failure_kind="configuration",
            )

        if event_scoped:
            return await self.fetch_event_scoped_props(
                sport_key=_clean(sport_key),
                bookmaker=bookmaker,
            )

        async with self._lock:
            url, params, scope = self._official_request(
                sport_key,
                bookmaker=bookmaker,
            )
            now = self._clock()
            if self._cache_is_fresh(now, scope):
                cached = self._cache_by_scope[scope]
                return PropLineFetchResult(
                    list(cached.props),
                    cached.fetched_at,
                    from_cache=True,
                    succeeded=True,
                    complete=cached.complete,
                    discovered_event_count=cached.discovered_event_count,
                    fetched_event_count=cached.fetched_event_count,
                )
            try:
                raw_records, complete, pagination_error = await self._paginate_json(
                    url,
                    params,
                    extractor=(
                        _official_odds_records
                        if url.startswith(f"{DEFAULT_PROPLINE_API_URL}/v1/")
                        else _extract_records
                    ),
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("PropLine PrizePicks request failed: %s", type(exc).__name__)
                return self._failed_result(
                    scope,
                    f"PropLine request failed: {type(exc).__name__}",
                    "timeout" if isinstance(exc, asyncio.TimeoutError) else "http",
                )
            except PropLineRateLimitError as exc:
                logger.warning("PropLine PrizePicks rate limited; backing off for %.0fs", exc.retry_after)
                return self._failed_result(scope, str(exc), "quota")
            except PropLineError as exc:
                logger.warning("PropLine PrizePicks provider error: %s", exc)
                kind = "quota" if "budget" in str(exc).lower() else "http"
                return self._failed_result(scope, str(exc), kind)
            except Exception as exc:  # noqa: BLE001
                logger.warning("PropLine PrizePicks unexpected response error: %s", type(exc).__name__)
                return self._failed_result(
                    scope,
                    f"PropLine response error: {type(exc).__name__}",
                    "unknown",
                )

            fetched_at = datetime.utcnow()
            props = [
                prop
                for record in raw_records
                if (prop := normalize_propline_prizepicks_record(record, fetched_at=fetched_at))
                is not None
            ]
            if not complete:
                logger.warning("PropLine snapshot incomplete: %s", pagination_error)
                return self._failed_result(
                    scope,
                    pagination_error or "PropLine snapshot incomplete",
                    "incomplete",
                    partial_props=props,
                )
            self._cache = PropLineFetchResult(
                props, fetched_at, from_cache=False, succeeded=True, complete=True
            )
            self._cache_clock_at = self._clock()
            self._cache_scope = scope
            self._cache_by_scope[scope] = self._cache
            self._cache_clock_by_scope[scope] = self._cache_clock_at
            result = PropLineFetchResult(
                list(props), fetched_at, from_cache=False,
                succeeded=True, complete=True,
            )
            self._last_success_by_scope[scope] = result
            self._snapshot_state_by_scope[scope] = PropLineSnapshotState(
                complete=True,
                last_successful_at=fetched_at,
            )
            return result

    def _failed_result(
        self,
        scope: str,
        error: str,
        failure_kind: str,
        *,
        partial_props: Optional[list[PlayerProp]] = None,
        discovered_event_count: int = 0,
        fetched_event_count: int = 0,
        failed_event_ids: tuple[str, ...] = (),
    ) -> PropLineFetchResult:
        """Return retained truth, never a partial snapshot, after a failure."""
        previous = self._last_success_by_scope.get(scope)
        retained_props = list(previous.props) if previous is not None else []
        result = PropLineFetchResult(
            retained_props,
            datetime.utcnow(),
            from_cache=False,
            succeeded=False,
            error=error,
            failure_kind=failure_kind,
            complete=False,
            discovered_event_count=discovered_event_count,
            fetched_event_count=fetched_event_count,
            failed_event_ids=failed_event_ids,
            retained_last_success=previous is not None,
            partial_props=tuple(partial_props or ()),
        )
        self._snapshot_state_by_scope[scope] = PropLineSnapshotState(
            complete=False,
            discovered_event_count=discovered_event_count,
            fetched_event_count=fetched_event_count,
            failed_event_ids=failed_event_ids,
            last_successful_at=previous.fetched_at if previous else None,
        )
        return result

    def snapshot_state(self, scope: str = "") -> Optional[PropLineSnapshotState]:
        """Return completeness/retention metadata for a prior request scope."""
        if scope:
            return self._snapshot_state_by_scope.get(scope)
        if not self._snapshot_state_by_scope:
            return None
        return next(reversed(self._snapshot_state_by_scope.values()))


def _retry_after_seconds(response: Any) -> float:
    value = response.headers.get("Retry-After", "")
    try:
        return min(3600.0, max(1.0, float(value)))
    except (TypeError, ValueError):
        return 300.0


class PropLinePrizePicksProvider(PropProviderBase):
    """PropProviderBase adapter for PropLine's PrizePicks-only feed."""

    def __init__(
            self,
            *,
            sport_filter: Optional[list[str]] = None,
            client: Optional[PropLineClient] = None,
            bookmaker: str = "prizepicks",
        ) -> None:
            self._sport_filter = {s.upper() for s in sport_filter} if sport_filter else None
            self.bookmaker = bookmaker.strip().lower()
            self.client = client or PropLineClient()

    @property
    def provider_name(self) -> str:
        return PROPLINE_PROVIDER_NAME

    @property
    def sport_keys(self) -> list[str]:
        if self._sport_filter is not None:
            return sorted(self._sport_filter)
        # This is only the synchronous ABC hint.  The official bulk path uses
        # discover_sports() and never treats this fallback as provider data.
        return sorted(set(_CANONICAL_TO_OFFICIAL_SPORT))

    def is_available(self) -> bool:
        return self.client.is_configured

    async def discover_sports(self) -> Optional[list[PropLineSport]]:
        """Return active PropLine sports for the official endpoint.

        A custom authorized endpoint may not expose the documented catalog;
        ``None`` tells the caller to use its configured compatibility scope.
        An empty list from the official catalog is meaningful and means there
        is no currently advertised PrizePicks slate.
        """
        if not self.is_available():
            return []
        if self.client.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return None
        discovered = await self.client.discover_sports()
        if self._sport_filter is not None:
            discovered = [
                sport
                for sport in discovered
                if sport.canonical_key in self._sport_filter
                and not is_football_related_sport(sport.canonical_key)
            ]
        else:
            discovered = [
                sport
                for sport in discovered
                if not is_football_related_sport(sport.canonical_key)
            ]
        return discovered

    async def fetch_props(self) -> list[PlayerProp]:
        if not self.is_available():
            logger.info("PropLine PrizePicks provider is disabled")
            return []
        if self.client.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            # Preserve the custom authorized-endpoint contract.  Discovery is
            # only valid on the official host; never guess a path elsewhere.
            result = await self.client.fetch_prizepicks_props(bookmaker=self.bookmaker)
            return [
                prop for prop in result.props
                if not is_football_related_sport(prop.sport)
                and (self._sport_filter is None or prop.sport in self._sport_filter)
            ]

        discovered = await self.client.discover_sports()
        if self._sport_filter is not None:
            discovered = [
                sport for sport in discovered
                if sport.canonical_key in self._sport_filter
                and not is_football_related_sport(sport.canonical_key)
            ]
        else:
            discovered = [
                sport
                for sport in discovered
                if not is_football_related_sport(sport.canonical_key)
            ]

        props: list[PlayerProp] = []
        for sport in discovered:
            result = await self.client.fetch_prizepicks_props(
                sport_key=sport.official_key,
                bookmaker=self.bookmaker,
            )
            props.extend(result.props)
        return props

    async def fetch_props_for_sport(self, sport: str) -> list[PlayerProp]:
        """Fetch only one canonical sport for confirmation freshness and quota safety."""
        result = await self.fetch_props_for_sport_result(sport)
        return result.props

    async def fetch_props_for_sport_result(
            self,
            sport: str,
            bookmaker: Optional[str] = None,
            event_scoped: bool = False,
        ) -> PropLineFetchResult:
        """Fetch one sport and retain whether PropLine itself accepted the request."""
        if not self.is_available():
            logger.info("PropLine PrizePicks provider is disabled")
            return PropLineFetchResult(
                [], datetime.utcnow(),
                error="PropLine is not configured", failure_kind="disabled",
            )
        canonical_sport = _normalize_sport(sport)
        if is_football_related_sport(canonical_sport):
            return PropLineFetchResult(
                [], datetime.utcnow(),
                error=f"PropLine sport is outside football-free scope: {canonical_sport}",
                failure_kind="configuration",
            )
        if self._sport_filter is not None and canonical_sport not in self._sport_filter:
            return PropLineFetchResult(
                [], datetime.utcnow(),
                error=f"PropLine sport is outside configured scope: {canonical_sport}",
                failure_kind="configuration",
            )
        official_sport = self.client.official_sport_for(canonical_sport)
        if not official_sport:
            logger.debug(
                "PropLine sport unavailable or unsupported; skipping %s",
                canonical_sport,
            )
            return PropLineFetchResult(
                [], datetime.utcnow(),
                error=f"PropLine sport is unsupported: {canonical_sport}",
                failure_kind="configuration",
            )
        result = await self.client.fetch_prizepicks_props(
            sport_key=official_sport,
            bookmaker=bookmaker or self.bookmaker,
            event_scoped=event_scoped,
        )
        # The same official endpoint can have multiple aliases in the bot
        # namespace (for example CFB/NCAAF and SOCCER/EPL).  The requested
        # canonical identity is authoritative for confirmation matching.
        return PropLineFetchResult(
            [replace(prop, sport=canonical_sport) for prop in result.props],
            result.fetched_at,
            from_cache=result.from_cache,
            succeeded=result.succeeded,
            error=result.error,
            failure_kind=result.failure_kind,
            complete=result.complete,
            discovered_event_count=result.discovered_event_count,
            fetched_event_count=result.fetched_event_count,
            failed_event_ids=result.failed_event_ids,
            retained_last_success=result.retained_last_success,
            partial_props=result.partial_props,
        )

    async def fetch_props_for_sport_event_scoped(
        self,
        sport: str,
        *,
        bookmaker: Optional[str] = None,
    ) -> PropLineFetchResult:
        """Opt into complete event-discovery/event-odds snapshots explicitly."""
        return await self.fetch_props_for_sport_result(
            sport,
            bookmaker=bookmaker or self.bookmaker,
            event_scoped=True,
        )

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)