"""
PropLine-backed PrizePicks provider.

PropLine is an authorized, third-party source for PrizePicks projections.  This
module deliberately knows nothing about Underdog or sportsbook markets.  It
normalizes only PrizePicks player props into the shared ``PlayerProp`` model.

The endpoint is configurable because PropLine deployments may expose different
API gateways.  ``PROPLINE_API_URL`` may be a full endpoint URL; the default is
the documented PrizePicks player-props route used by the hosted API.
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
DEFAULT_PROPLINE_API_URL = (
    "https://api.propline.com/v1/prizepicks/player-props"
)

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
    "BASKETBALL": "NBA",
    "FOOTBALL": "NFL",
    "AMERICAN FOOTBALL": "NFL",
    "HOCKEY": "NHL",
    "WOMENS BASKETBALL": "WNBA",
    "WOMEN'S BASKETBALL": "WNBA",
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

    def _cache_is_fresh(self, now: float) -> bool:
        return bool(
            self._cache
            and self.cache_ttl > 0
            and self._cache_clock_at is not None
            and (now - self._cache_clock_at) < self.cache_ttl
        )

    async def fetch_prizepicks_props(self) -> PropLineFetchResult:
        if not self.is_configured:
            logger.info("PropLine PrizePicks provider disabled: PROPLINE_API_KEY is missing")
            return PropLineFetchResult([], datetime.utcnow(), from_cache=False)

        async with self._lock:
            now = self._clock()
            if self._cache_is_fresh(now):
                return PropLineFetchResult(
                    list(self._cache.props),
                    self._cache.fetched_at,
                    from_cache=True,
                )
            if now < self._backoff_until:
                logger.warning("PropLine rate-limit backoff active; reusing fresh cache if available")
                if self._cache is not None:
                    return PropLineFetchResult(
                        list(self._cache.props), self._cache.fetched_at, from_cache=True
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
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-API-Key": self.api_key,
                    "Accept": "application/json",
                }
                params = {"provider": "prizepicks", "market": "player_props"}
                timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                async with session.get(self.api_url, headers=headers, params=params, timeout=timeout) as response:
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
            props = [
                prop
                for record in _extract_records(payload)
                if (prop := normalize_propline_prizepicks_record(record, fetched_at=fetched_at))
                is not None
            ]
            self._cache = PropLineFetchResult(props, fetched_at, from_cache=False)
            self._cache_clock_at = self._clock()
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
        return [
            "MLB", "NBA", "NFL", "NHL", "WNBA",
            "SOCCER", "TENNIS", "CS", "DOTA", "LOL",
        ]

    def is_available(self) -> bool:
        return self.client.is_configured

    async def fetch_props(self) -> list[PlayerProp]:
        if not self.is_available():
            logger.info("PropLine PrizePicks provider is disabled")
            return []
        result = await self.client.fetch_prizepicks_props()
        if self._sport_filter is None:
            return result.props
        return [prop for prop in result.props if prop.sport in self._sport_filter]

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)