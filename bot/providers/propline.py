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
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import aiohttp

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


@dataclass(frozen=True)
class PropLineFetchResult:
    """Fetch result with provider metadata kept separate from normalized props."""

    props: list[PlayerProp]
    fetched_at: datetime
    from_cache: bool = False


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
    if not sport or not stat_type:
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
    else:
        event_label = _clean(event)
        event_id = _clean(_first(raw_record, "event_id", "game_id", "match_id"))

    source_id = _clean(
        _first(raw_record, "external_id", "source_id", "prop_id", "projection_id", "id")
    )
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
    direction = _normalize_direction(
        _first(raw_record, "direction", "side", "selection", "over_under")
    )
    provider_status = _clean(
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
        ),
        external_id=source_id,
        game_id=event_id or event_label,
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
    for key in ("data", "props", "lines", "projections", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_records(value)
            if nested:
                return nested
    return []


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
                            "sport": sport_key,
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
        min_request_interval: float = 300.0,
        cache_ttl: float = 240.0,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
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
        self._request_day = datetime.utcnow().date()
        self._request_count = 0
        # Stay below PropLine's documented 1,000-request daily free limit even
        # if callers invoke this client more often than the normal 5-minute
        # pacing interval.
        self.daily_request_limit = 900

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
        today = datetime.utcnow().date()
        if today != self._request_day:
            self._request_day = today
            self._request_count = 0

    def _official_request(
        self, sport_key: Optional[str],
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
                    "bookmakers": "prizepicks",
                },
                official_sport,
            )
        return (
            self.api_url,
            {"provider": "prizepicks", "market": "player_props"},
            sport_key or "__custom__",
        )

    def _official_sports_request(self) -> tuple[str, dict[str, str]]:
        return f"{self.api_url.rstrip('/')}/v1/sports", {}

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
            if self._request_count >= self.daily_request_limit:
                logger.warning("PropLine daily request budget exhausted; skipping sports discovery")
                return []
            if now < self._backoff_until:
                logger.warning("PropLine rate-limit backoff active; skipping sports discovery")
                return []
            if self._last_request_at is not None:
                delay = self.min_request_interval - (now - self._last_request_at)
                if delay > 0:
                    await self._sleep(delay)
            self._last_request_at = self._clock()

            session = self._session
            owns_local_session = session is None
            if session is None:
                session = aiohttp.ClientSession()
            try:
                self._request_count += 1
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                }
                timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                url, params = self._official_sports_request()
                async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
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
                        raise PropLineError(f"PropLine temporarily unavailable (HTTP {response.status})")
                    if response.status >= 400:
                        raise PropLineError(f"PropLine sports discovery failed (HTTP {response.status})")
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("PropLine sports discovery failed: %s", type(exc).__name__)
                return []
            except PropLineRateLimitError as exc:
                logger.warning("PropLine sports discovery rate limited; backing off for %.0fs", exc.retry_after)
                return []
            except PropLineError as exc:
                logger.warning("PropLine sports discovery error: %s", exc)
                return []
            except Exception as exc:  # noqa: BLE001
                logger.warning("PropLine sports discovery response error: %s", type(exc).__name__)
                return []
            finally:
                if owns_local_session:
                    await session.close()

            sports = _official_sports_records(payload)
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
        self, *, sport_key: Optional[str] = None,
    ) -> PropLineFetchResult:
        if not self.is_configured:
            logger.info("PropLine PrizePicks provider disabled: credentials or endpoint missing")
            return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
        if (
            self.api_url.rstrip("/") == DEFAULT_PROPLINE_API_URL
            and not _clean(sport_key)
        ):
            logger.warning("PropLine official odds request skipped: sport key is required")
            return PropLineFetchResult([], datetime.utcnow(), from_cache=False)

        async with self._lock:
            self._reset_daily_budget_if_needed()
            url, params, scope = self._official_request(sport_key)
            now = self._clock()
            if self._cache_is_fresh(now, scope):
                cached = self._cache_by_scope[scope]
                return PropLineFetchResult(
                    list(cached.props),
                    cached.fetched_at,
                    from_cache=True,
                )
            if self._request_count >= self.daily_request_limit:
                logger.warning("PropLine daily request budget exhausted; skipping request")
                return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
            if now < self._backoff_until:
                logger.warning("PropLine rate-limit backoff active; reusing fresh cache if available")
                cached = self._cache_by_scope.get(scope)
                if cached is not None:
                    return PropLineFetchResult(
                        list(cached.props), cached.fetched_at, from_cache=True
                    )
                return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
            if self._last_request_at is not None:
                delay = self.min_request_interval - (now - self._last_request_at)
                if delay > 0:
                    await self._sleep(delay)
            self._last_request_at = self._clock()

            session = self._session
            owns_local_session = session is None
            if session is None:
                session = aiohttp.ClientSession()
            try:
                self._request_count += 1
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                }
                timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
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
                        raise PropLineError(f"PropLine temporarily unavailable (HTTP {response.status})")
                    if response.status >= 400:
                        raise PropLineError(f"PropLine request failed (HTTP {response.status})")
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("PropLine PrizePicks request failed: %s", type(exc).__name__)
                return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
            except PropLineRateLimitError as exc:
                logger.warning("PropLine PrizePicks rate limited; backing off for %.0fs", exc.retry_after)
                return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
            except PropLineError as exc:
                logger.warning("PropLine PrizePicks provider error: %s", exc)
                return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("PropLine PrizePicks unexpected response error: %s", type(exc).__name__)
                return PropLineFetchResult([], datetime.utcnow(), from_cache=False)
            finally:
                if owns_local_session:
                    await session.close()

            fetched_at = datetime.utcnow()
            records = (
                _official_odds_records(payload)
                if url.startswith(f"{DEFAULT_PROPLINE_API_URL}/v1/")
                else _extract_records(payload)
            )
            props = [
                prop
                for record in records
                if (prop := normalize_propline_prizepicks_record(record, fetched_at=fetched_at))
                is not None
            ]
            self._cache = PropLineFetchResult(props, fetched_at, from_cache=False)
            self._cache_clock_at = self._clock()
            self._cache_scope = scope
            self._cache_by_scope[scope] = self._cache
            self._cache_clock_by_scope[scope] = self._cache_clock_at
            return PropLineFetchResult(list(props), fetched_at, from_cache=False)


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
    ) -> None:
        self._sport_filter = {s.upper() for s in sport_filter} if sport_filter else None
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

    async def fetch_props(self) -> list[PlayerProp]:
        if not self.is_available():
            logger.info("PropLine PrizePicks provider is disabled")
            return []
        if self.client.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            # Preserve the custom authorized-endpoint contract.  Discovery is
            # only valid on the official host; never guess a path elsewhere.
            result = await self.client.fetch_prizepicks_props()
            return [
                prop for prop in result.props
                if self._sport_filter is None or prop.sport in self._sport_filter
            ]

        discovered = await self.client.discover_sports()
        if self._sport_filter is not None:
            discovered = [
                sport for sport in discovered
                if sport.canonical_key in self._sport_filter
            ]

        props: list[PlayerProp] = []
        for sport in discovered:
            result = await self.client.fetch_prizepicks_props(
                sport_key=sport.official_key
            )
            props.extend(result.props)
        return props

    async def fetch_props_for_sport(self, sport: str) -> list[PlayerProp]:
        """Fetch only one canonical sport for confirmation freshness and quota safety."""
        if not self.is_available():
            logger.info("PropLine PrizePicks provider is disabled")
            return []
        canonical_sport = _normalize_sport(sport)
        if self._sport_filter is not None and canonical_sport not in self._sport_filter:
            return []
        official_sport = self.client.official_sport_for(canonical_sport)
        if not official_sport:
            logger.debug(
                "PropLine sport unavailable or unsupported; skipping %s",
                canonical_sport,
            )
            return []
        result = await self.client.fetch_prizepicks_props(sport_key=official_sport)
        return [prop for prop in result.props if prop.sport == canonical_sport]

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)