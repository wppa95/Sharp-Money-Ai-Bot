"""Safe supplemental access to documented The Odds API v4 endpoints.

This provider supplies event metadata, current market quotes, and team scores.
It is deliberately not a player-stat, player-history, settlement, pick-
qualification, or offering-validation provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

import aiohttp

from providers.base import FailureType, ProviderStatus
from providers.odds_api_budget import (
    ODDS_API_MONTHLY_HARD_LIMIT,
    ODDS_API_MONTHLY_TARGET,
    OddsApiCreditBudget,
)

logger = logging.getLogger(__name__)
UTC = timezone.utc
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_API_AUTH_BACKOFF_SECONDS = 60 * 60

# Snapshot of the provider's published Scores & Results coverage on
# 2026-10-17. Runtime requests are still restricted to keys returned by the
# live, quota-free /sports endpoint, so new or inactive keys are not guessed.
SCORE_SUPPORTED_SPORT_KEYS = frozenset(
    {
        "americanfootball_cfl",
        "americanfootball_ncaaf",
        "americanfootball_ncaaf_fcs",
        "americanfootball_nfl",
        "americanfootball_nfl_preseason",
        "americanfootball_ufl",
        "aussierules_afl",
        "baseball_mlb",
        "baseball_mlb_preseason",
        "basketball_euroleague",
        "basketball_nba",
        "basketball_nba_preseason",
        "basketball_nba_summer_league",
        "basketball_wnba",
        "basketball_ncaab",
        "basketball_wncaab",
        "basketball_nbl",
        "handball_germany_bundesliga",
        "icehockey_nhl",
        "icehockey_nhl_preseason",
        "icehockey_sweden_hockey_league",
        "icehockey_sweden_allsvenskan",
        "rugbyleague_nrl",
        "soccer_argentina_primera_division",
        "soccer_australia_aleague",
        "soccer_austria_bundesliga",
        "soccer_belgium_first_div",
        "soccer_brazil_campeonato",
        "soccer_brazil_serie_b",
        "soccer_chile_campeonato",
        "soccer_china_superleague",
        "soccer_denmark_superliga",
        "soccer_efl_champ",
        "soccer_england_efl_cup",
        "soccer_england_league1",
        "soccer_england_league2",
        "soccer_epl",
        "soccer_fa_cup",
        "soccer_fifa_world_cup",
        "soccer_fifa_world_cup_qualifiers_europe",
        "soccer_fifa_world_cup_qualifiers_south_america",
        "soccer_fifa_world_cup_womens",
        "soccer_fifa_club_world_cup",
        "soccer_finland_veikkausliiga",
        "soccer_france_coupe_de_france",
        "soccer_france_ligue_one",
        "soccer_france_ligue_two",
        "soccer_germany_bundesliga",
        "soccer_germany_bundesliga2",
        "soccer_germany_bundesliga_women",
        "soccer_germany_dfb_pokal",
        "soccer_germany_liga3",
        "soccer_greece_super_league",
        "soccer_italy_coppa_italia",
        "soccer_italy_serie_a",
        "soccer_italy_serie_b",
        "soccer_japan_j_league",
        "soccer_korea_kleague1",
        "soccer_league_of_ireland",
        "soccer_mexico_ligamx",
        "soccer_netherlands_eredivisie",
        "soccer_norway_eliteserien",
        "soccer_poland_ekstraklasa",
        "soccer_portugal_primeira_liga",
        "soccer_russia_premier_league",
        "soccer_spain_copa_del_rey",
        "soccer_spain_la_liga",
        "soccer_spain_segunda_division",
        "soccer_saudi_arabia_pro_league",
        "soccer_spl",
        "soccer_sweden_allsvenskan",
        "soccer_sweden_superettan",
        "soccer_switzerland_superleague",
        "soccer_turkey_super_league",
        "soccer_uefa_europa_conference_league",
        "soccer_uefa_champs_league",
        "soccer_uefa_champs_league_qualification",
        "soccer_uefa_champs_league_women",
        "soccer_uefa_europa_league",
        "soccer_uefa_european_championship",
        "soccer_uefa_euro_qualification",
        "soccer_uefa_nations_league",
        "soccer_concacaf_leagues_cup",
        "soccer_conmebol_copa_america",
        "soccer_conmebol_copa_libertadores",
        "soccer_conmebol_copa_sudamericana",
        "soccer_usa_mls",
        "tennis_atp_canadian_open",
        "tennis_atp_china_open",
        "tennis_atp_cincinnati_open",
        "tennis_atp_japan_open",
        "tennis_atp_shanghai_masters",
        "tennis_atp_us_open",
        "tennis_atp_washington_open",
        "tennis_wta_canadian_open",
        "tennis_wta_china_open",
        "tennis_wta_cincinnati_open",
        "tennis_wta_guadalajara_open",
        "tennis_wta_monterrey_open",
        "tennis_wta_singapore_open",
        "tennis_wta_us_open",
        "tennis_wta_washington_open",
        "tennis_wta_wuhan_open",
    }
)


class OddsApiError(RuntimeError):
    """Sanitized provider error. Never includes request URLs or credentials."""


class OddsApiDeferred(OddsApiError):
    """Request was intentionally postponed by budget, pacing, or cooldown."""


@dataclass(frozen=True)
class OddsApiHttpResponse:
    status: int
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True)
class OddsApiSport:
    key: str
    title: str
    group: str = ""
    active: bool = True
    has_outrights: bool = False


@dataclass(frozen=True)
class OddsApiEvent:
    id: str
    sport_key: str
    commence_time: datetime | None
    home_team: str
    away_team: str


@dataclass(frozen=True)
class OddsApiMarket:
    key: str
    description: str = ""
    last_update: str = ""
    bookmaker: str = ""


@dataclass(frozen=True)
class OddsApiQuote:
    market_key: str
    bookmaker: str
    player: str
    outcome: str
    price: float | None
    point: float | None
    last_update: str = ""


@dataclass(frozen=True)
class OddsApiScore:
    event_id: str
    sport_key: str
    commence_time: datetime | None
    home_team: str
    away_team: str
    completed: bool
    scores: tuple[tuple[str, float], ...]
    last_update: datetime | None = None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _quote_identity(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore")
    return re.sub(r"[^a-z0-9]+", "", text.decode("ascii").casefold())


def match_event_exactly(
    source_event: dict[str, Any],
    odds_events: list[OddsApiEvent],
    *,
    time_tolerance_seconds: int = 600,
) -> OddsApiEvent | None:
    """Match on sport, both participants, and a close start time; ambiguity fails closed."""
    sport = str(source_event.get("sport_key") or source_event.get("sport") or "").strip().casefold()
    home = _quote_identity(source_event.get("home_team"))
    away = _quote_identity(source_event.get("away_team"))
    start = _parse_datetime(
        source_event.get("commence_time")
        or source_event.get("start_time")
        or source_event.get("game_time")
    )
    if not sport or not home or not away or start is None:
        return None
    matches = []
    for event in odds_events:
        if event.sport_key.casefold() != sport:
            continue
        participants = {_quote_identity(event.home_team), _quote_identity(event.away_team)}
        if participants != {home, away}:
            continue
        if event.commence_time is None:
            continue
        if abs((event.commence_time - start).total_seconds()) <= time_tolerance_seconds:
            matches.append(event)
    return matches[0] if len(matches) == 1 else None


class OddsApiClient:
    """Rate-limited client for free discovery plus current event context/scores."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        budget: OddsApiCreditBudget | None = None,
        health_monitor: Any | None = None,
        transport: Callable[[str, dict[str, Any]], Any] | None = None,
        base_url: str = ODDS_API_BASE_URL,
    ) -> None:
        self._api_key = str(api_key or os.environ.get("ODDS_API_KEY", "")).strip()
        self.budget = budget or OddsApiCreditBudget()
        self.health_monitor = health_monitor
        self._transport = transport or self._aiohttp_transport
        self._base_url = base_url.rstrip("/")
        self._request_lock = asyncio.Lock()
        self._cache_hits = 0
        self._cache_misses = 0
        self._last_sports: list[OddsApiSport] = []
        self._last_events_by_sport: dict[str, list[OddsApiEvent]] = {}
        self._last_scores_by_sport: dict[str, list[OddsApiScore]] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def _aiohttp_transport(
        self, path: str, params: dict[str, Any]
    ) -> OddsApiHttpResponse:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self._base_url}{path}", params=params
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                    body = None
                return OddsApiHttpResponse(
                    int(response.status),
                    {str(k).casefold(): str(v) for k, v in response.headers.items()},
                    body,
                )

    def _headers_cost(self, headers: dict[str, str]) -> tuple[int | None, int | None]:
        def parse(name: str) -> int | None:
            try:
                return max(0, int(headers.get(name, "")))
            except (TypeError, ValueError):
                return None

        return parse("x-requests-last"), parse("x-requests-used")

    def _retry_after_seconds(self, headers: dict[str, str]) -> float | None:
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _record_failure(self, message: str, failure_type: FailureType) -> None:
        if self.health_monitor is None:
            return
        try:
            self.health_monitor.record_failure("OddsAPI", message, failure_type)
        except Exception:
            logger.debug("Odds API health failure could not be recorded")

    def _record_success(self, headers: dict[str, str]) -> None:
        if self.health_monitor is None:
            return
        last, used = self._headers_cost(headers)
        try:
            remaining = (
                max(0, int(headers["x-requests-remaining"]))
                if "x-requests-remaining" in headers else None
            )
            self.health_monitor.record_success(
                "OddsAPI", quota_remaining=remaining, quota_used=used
            )
        except Exception:
            logger.debug("Odds API health success could not be recorded")

    def _cache_key(self, endpoint: str, params: dict[str, Any]) -> str:
        safe = {
            str(k): v for k, v in sorted(params.items())
            if str(k).casefold() not in {"apikey", "api_key", "key", "token"}
        }
        return endpoint + ":" + json.dumps(safe, sort_keys=True, separators=(",", ":"))

    async def _get_json(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        max_cost: int,
        cache_ttl: int,
        actual_cost: Callable[[Any, dict[str, str]], int | None] | None = None,
    ) -> Any:
        cache_key = self._cache_key(endpoint, params)
        cached = self.budget.cache_get(cache_key)
        if cached is not None:
            self._cache_hits += 1
            return cached
        self._cache_misses += 1
        if not self.is_configured:
            raise OddsApiDeferred("Odds API is not configured")

        async with self._request_lock:
            # Recheck after acquiring the shared in-process request lock.
            cached = self.budget.cache_get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                return cached
            if max_cost == 0:
                can_request, reason = self.budget.can_make_free_request()
                if not can_request:
                    raise OddsApiDeferred(reason)
                reservation_id = None
            else:
                reservation_id, reason = self.budget.reserve(
                    endpoint=endpoint, params=params, max_cost=max_cost
                )
                if reservation_id is None:
                    raise OddsApiDeferred(reason)

            request_params = {**params, "apiKey": self._api_key}
            try:
                response = await self._transport(endpoint, request_params)
            except asyncio.TimeoutError:
                if reservation_id:
                    self.budget.complete(
                        reservation_id, actual_cost=None, outcome="timeout"
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint, params=params, outcome="timeout"
                    )
                self._record_failure("request timed out", FailureType.TIMEOUT)
                raise OddsApiError("request timed out") from None
            except Exception as exc:
                if reservation_id:
                    self.budget.complete(
                        reservation_id, actual_cost=None, outcome="network_error"
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint, params=params, outcome="network_error"
                    )
                failure = (
                    FailureType.TIMEOUT
                    if isinstance(exc, (aiohttp.ServerTimeoutError, TimeoutError))
                    else FailureType.HTTP_ERROR
                )
                self._record_failure(
                    f"network error ({type(exc).__name__})", failure
                )
                raise OddsApiError("network request failed") from None

            headers = {
                str(key).casefold(): str(value)
                for key, value in (response.headers or {}).items()
            }
            last_cost, reported_used = self._headers_cost(headers)
            try:
                reported_remaining = (
                    max(0, int(headers["x-requests-remaining"]))
                    if "x-requests-remaining" in headers else None
                )
            except (TypeError, ValueError):
                reported_remaining = None
            retry_after = self._retry_after_seconds(headers)
            status = int(response.status)
            if status == 429:
                if reservation_id:
                    self.budget.complete(
                        reservation_id,
                        actual_cost=None,
                        outcome="rate_limited",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                        retry_after_seconds=retry_after,
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint,
                        params=params,
                        outcome="rate_limited",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                        retry_after_seconds=retry_after,
                    )
                self._record_failure("HTTP 429; provider cooldown applied", FailureType.HTTP_ERROR)
                raise OddsApiDeferred("HTTP 429; provider cooldown applied")

            if status in (401, 402):
                error_code = (
                    str(response.body.get("error_code") or "").upper()
                    if isinstance(response.body, dict) else ""
                )
                quota_rejected = (
                    status == 402
                    or "OUT_OF_USAGE_CREDITS" in error_code
                    or "QUOTA" in error_code
                )
                auth_backoff = (
                    None if quota_rejected else ODDS_API_AUTH_BACKOFF_SECONDS
                )
                if reservation_id:
                    self.budget.complete(
                        reservation_id,
                        actual_cost=None,
                        outcome=(
                            "quota_rejected" if quota_rejected
                            else "authentication_rejected"
                        ),
                        reported_used=reported_used,
                        reported_remaining=0 if quota_rejected else reported_remaining,
                        retry_after_seconds=auth_backoff,
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint,
                        params=params,
                        outcome=(
                            "quota_rejected" if quota_rejected
                            else "authentication_rejected"
                        ),
                        reported_used=reported_used,
                        reported_remaining=0 if quota_rejected else reported_remaining,
                        retry_after_seconds=auth_backoff,
                    )
                if quota_rejected:
                    self._record_failure("HTTP quota rejection", FailureType.QUOTA)
                else:
                    self._record_failure("HTTP authentication rejection", FailureType.HTTP_ERROR)
                raise OddsApiError("provider rejected the request")

            if status == 403:
                if reservation_id:
                    self.budget.complete(
                        reservation_id,
                        actual_cost=last_cost if last_cost is not None else None,
                        outcome="forbidden",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                        retry_after_seconds=ODDS_API_AUTH_BACKOFF_SECONDS,
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint, params=params, outcome="forbidden",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                        retry_after_seconds=ODDS_API_AUTH_BACKOFF_SECONDS,
                    )
                self._record_failure("HTTP 403", FailureType.BLOCKED)
                raise OddsApiError("provider denied the request")

            if status < 200 or status >= 300:
                if reservation_id:
                    self.budget.complete(
                        reservation_id,
                        actual_cost=last_cost if last_cost is not None else None,
                        outcome=f"http_{status}",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint, params=params, outcome=f"http_{status}",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                    )
                self._record_failure(f"HTTP {status}", FailureType.HTTP_ERROR)
                raise OddsApiError(f"HTTP {status}")

            body = response.body
            if not isinstance(body, (list, dict)):
                if reservation_id:
                    self.budget.complete(
                        reservation_id,
                        actual_cost=last_cost if last_cost is not None else None,
                        outcome="malformed_response",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                    )
                else:
                    self.budget.record_free_request(
                        endpoint=endpoint, params=params, outcome="malformed_response",
                        reported_used=reported_used,
                        reported_remaining=reported_remaining,
                    )
                self._record_failure("malformed JSON response", FailureType.PARSE_ERROR)
                raise OddsApiError("malformed JSON response")

            if reservation_id:
                documented_cost = actual_cost(body, headers) if actual_cost else max_cost
                finalized_cost = (
                    max(documented_cost or 0, last_cost or 0)
                    if documented_cost is not None or last_cost is not None
                    else None
                )
                self.budget.complete(
                    reservation_id,
                    actual_cost=finalized_cost,
                    outcome="success",
                    reported_used=reported_used,
                    reported_remaining=reported_remaining,
                )
            else:
                self.budget.record_free_request(
                    endpoint=endpoint,
                    params=params,
                    outcome="success",
                    reported_used=reported_used,
                    reported_remaining=reported_remaining,
                )
            self._record_success(headers)
            self.budget.cache_set(cache_key, body, ttl_seconds=cache_ttl)
            return body

    @staticmethod
    def _unique_market_cost(body: Any, headers: dict[str, str], regions: int) -> int | None:
        if not isinstance(body, dict):
            return None
        keys: set[str] = set()
        for bookmaker in body.get("bookmakers", []) or []:
            if not isinstance(bookmaker, dict):
                continue
            for market in bookmaker.get("markets", []) or []:
                if isinstance(market, dict) and market.get("key"):
                    keys.add(str(market["key"]))
        return len(keys) * regions

    async def fetch_sports(self, *, all_sports: bool = False) -> list[OddsApiSport]:
        params = {"all": "true"} if all_sports else {}
        body = await self._get_json(
            "/sports", params, max_cost=0, cache_ttl=24 * 60 * 60
        )
        sports = [
            OddsApiSport(
                key=str(row.get("key") or ""),
                title=str(row.get("title") or ""),
                group=str(row.get("group") or ""),
                active=bool(row.get("active", True)),
                has_outrights=bool(row.get("has_outrights", False)),
            )
            for row in body
            if isinstance(row, dict) and str(row.get("key") or "").strip()
        ]
        self._last_sports = sports
        return sports

    async def fetch_events(self, sport_key: str) -> list[OddsApiEvent]:
        key = quote(str(sport_key).strip(), safe="_-")
        if not key:
            raise ValueError("sport_key is required")
        body = await self._get_json(
            f"/sports/{key}/events", {}, max_cost=0, cache_ttl=20 * 60
        )
        events = [
            OddsApiEvent(
                id=str(row.get("id") or ""),
                sport_key=str(row.get("sport_key") or sport_key),
                commence_time=_parse_datetime(row.get("commence_time")),
                home_team=str(row.get("home_team") or ""),
                away_team=str(row.get("away_team") or ""),
            )
            for row in body
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        ]
        self._last_events_by_sport[str(sport_key)] = events
        return events

    async def fetch_event_markets(
        self, sport_key: str, event_id: str, *, regions: tuple[str, ...] = ("us",)
    ) -> list[OddsApiMarket]:
        if len(set(regions)) != 1:
            raise ValueError("market discovery is restricted to one region per request")
        sport = quote(str(sport_key).strip(), safe="_-")
        event = quote(str(event_id).strip(), safe="_-")
        if not sport or not event:
            raise ValueError("sport_key and event_id are required")
        region = ",".join(regions)
        body = await self._get_json(
            f"/sports/{sport}/events/{event}/markets",
            {"regions": region},
            max_cost=1,
            cache_ttl=15 * 60,
        )
        markets: list[OddsApiMarket] = []
        if isinstance(body, dict):
            for bookmaker in body.get("bookmakers", []) or []:
                if not isinstance(bookmaker, dict):
                    continue
                bookmaker_key = str(bookmaker.get("key") or "")
                for market in bookmaker.get("markets", []) or []:
                    if isinstance(market, dict) and market.get("key"):
                        markets.append(
                            OddsApiMarket(
                                key=str(market["key"]),
                                description=str(market.get("description") or ""),
                                last_update=str(market.get("last_update") or ""),
                                bookmaker=bookmaker_key,
                            )
                        )
        return markets

    def event_markets_cached(
        self, sport_key: str, event_id: str, *, region: str = "us"
    ) -> bool:
        sport = quote(str(sport_key).strip(), safe="_-")
        event = quote(str(event_id).strip(), safe="_-")
        params = {"regions": region}
        return self.budget.cache_get(
            self._cache_key(
                f"/sports/{sport}/events/{event}/markets", params
            )
        ) is not None

    async def fetch_event_odds(
        self,
        sport_key: str,
        event_id: str,
        markets: tuple[str, ...] | list[str],
        *,
        regions: tuple[str, ...] = ("us",),
    ) -> list[OddsApiQuote]:
        unique_markets = tuple(sorted({str(m).strip() for m in markets if str(m).strip()}))
        unique_regions = tuple(sorted({str(r).strip() for r in regions if str(r).strip()}))
        if not unique_markets or not unique_regions:
            raise ValueError("at least one market and region are required")
        sport = quote(str(sport_key).strip(), safe="_-")
        event = quote(str(event_id).strip(), safe="_-")
        if not sport or not event:
            raise ValueError("sport_key and event_id are required")
        params = {
            "regions": ",".join(unique_regions),
            "markets": ",".join(unique_markets),
            "oddsFormat": "decimal",
        }
        body = await self._get_json(
            f"/sports/{sport}/events/{event}/odds",
            params,
            max_cost=len(unique_markets) * len(unique_regions),
            cache_ttl=5 * 60,
            actual_cost=lambda value, headers: self._unique_market_cost(
                value, headers, len(unique_regions)
            ),
        )
        quotes: list[OddsApiQuote] = []
        if isinstance(body, dict):
            for bookmaker in body.get("bookmakers", []) or []:
                if not isinstance(bookmaker, dict):
                    continue
                bookmaker_name = str(bookmaker.get("title") or bookmaker.get("key") or "")
                for market in bookmaker.get("markets", []) or []:
                    if not isinstance(market, dict):
                        continue
                    market_key = str(market.get("key") or "")
                    for outcome in market.get("outcomes", []) or []:
                        if not isinstance(outcome, dict):
                            continue
                        quotes.append(
                            OddsApiQuote(
                                market_key=market_key,
                                bookmaker=bookmaker_name,
                                player=str(outcome.get("description") or ""),
                                outcome=str(outcome.get("name") or ""),
                                price=_number(outcome.get("price")),
                                point=_number(outcome.get("point")),
                                last_update=str(market.get("last_update") or ""),
                            )
                        )
        return quotes

    async def fetch_scores(
        self, sport_key: str, *, days_from: int | None = 1
    ) -> list[OddsApiScore]:
        if days_from is not None and not 1 <= int(days_from) <= 3:
            raise ValueError("days_from must be between 1 and 3")
        sport = quote(str(sport_key).strip(), safe="_-")
        if not sport:
            raise ValueError("sport_key is required")
        params = {"daysFrom": int(days_from)} if days_from is not None else {}
        body = await self._get_json(
            f"/sports/{sport}/scores",
            params,
            max_cost=2 if days_from is not None else 1,
            cache_ttl=60,
        )
        scores: list[OddsApiScore] = []
        for row in body:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            event_scores = []
            for score in row.get("scores", []) or []:
                if not isinstance(score, dict):
                    continue
                value = _number(score.get("score"))
                if value is not None:
                    event_scores.append((str(score.get("name") or ""), value))
            scores.append(
                OddsApiScore(
                    event_id=str(row.get("id")),
                    sport_key=str(row.get("sport_key") or sport_key),
                    commence_time=_parse_datetime(row.get("commence_time")),
                    home_team=str(row.get("home_team") or ""),
                    away_team=str(row.get("away_team") or ""),
                    completed=bool(row.get("completed", False)),
                    scores=tuple(event_scores),
                    last_update=_parse_datetime(row.get("last_update")),
                )
            )
        self._last_scores_by_sport[str(sport_key)] = scores
        return scores

    async def market_context_for_qualified_pick(
        self,
        pick: dict[str, Any],
        source_event: dict[str, Any],
        *,
        market_key: str,
    ) -> dict[str, Any] | None:
        """Return supplemental quotes only for a qualified, exact event match.

        This method is not wired into pick qualification, line selection,
        offering validation, grading, or settlement. The source pick identity
        and original line are returned unchanged alongside any quote context.
        """
        if not isinstance(pick, dict) or pick.get("qualified") is not True:
            return None
        provider = str(pick.get("provider") or pick.get("platform") or "").casefold()
        if provider not in {"underdog", "prizepicks"}:
            return None
        required = (
            "source_id", "event_id", "player_name", "sport", "stat_type",
            "line_value", "direction",
        )
        if any(pick.get(key) in (None, "") for key in required):
            return None
        if pick.get("direction") not in {"OVER", "UNDER"}:
            return None
        if not market_key or not source_event.get("home_team") or not source_event.get("away_team"):
            return None
        sport_key = str(source_event.get("sport_key") or "").strip()
        if not sport_key:
            return None
        events = await self.fetch_events(sport_key)
        matched = match_event_exactly(source_event, events)
        if matched is None:
            return None
        quotes = await self.fetch_event_odds(
            matched.sport_key, matched.id, (market_key,)
        )
        player_identity = _quote_identity(pick.get("player_name"))
        quotes = [
            item for item in quotes
            if item.market_key == market_key
            and _quote_identity(item.player) == player_identity
        ]
        if not quotes:
            return None
        return {
            "source_pick": {
                key: pick[key]
                for key in (*required, "line_variant")
                if key in pick
            },
            "odds_api_event_id": matched.id,
            "odds_api_market_key": market_key,
            "quotes": [quote.__dict__ for quote in quotes],
            "is_player_result": False,
            "may_validate_offering": False,
        }

    def diagnostics(self) -> dict[str, Any]:
        data = self.budget.diagnostics()
        active_sports = [
            sport.key for sport in self._last_sports
            if sport.active and not sport.has_outrights
        ]
        cursor = int(data.get("rotation_cursor", 0) or 0)
        score_sports = [
            key for key in active_sports if key in SCORE_SUPPORTED_SPORT_KEYS
        ]
        remaining = int(data.get("credits_remaining_to_target", 0) or 0)
        reset_at = _parse_datetime(data.get("next_reset_utc"))
        seconds_left = (
            max(0.0, (reset_at - datetime.now(UTC)).total_seconds())
            if reset_at else 0.0
        )
        estimated_score_rotation_days = (
            round(len(score_sports) * 2 * seconds_left / remaining / 86400, 1)
            if remaining > 0 and score_sports else None
        )
        next_sports = (
            [
                active_sports[(cursor + offset) % len(active_sports)]
                for offset in range(min(3, len(active_sports)))
            ]
            if active_sports else []
        )
        data.update(
            {
                "configured": self.is_configured,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "discovered_sports": len(self._last_sports),
                "active_sport_count": len(active_sports),
                "score_supported_sports": len(score_sports),
                "no_documented_score_endpoint": sorted(
                    key for key in active_sports
                    if key not in SCORE_SUPPORTED_SPORT_KEYS
                ),
                "next_paid_sports": next_sports,
                "estimated_score_rotation_days": estimated_score_rotation_days,
                "pending_pick_contexts": len(self._pending_pick_contexts)
                if hasattr(self, "_pending_pick_contexts") else 0,
                "retained_pick_contexts": len(self._last_pick_contexts)
                if hasattr(self, "_last_pick_contexts") else 0,
                "discovered_events": sum(
                    len(events) for events in self._last_events_by_sport.values()
                ),
                "cached_score_sports": sorted(self._last_scores_by_sport),
                "historical_endpoints_enabled": False,
                "player_result_support": False,
            }
        )
        return data

    async def health_check(self):
        if not self.is_configured:
            from providers.base import ProviderHealth

            return ProviderHealth(
                name="OddsAPI",
                status=ProviderStatus.DISABLED,
                last_success=None,
                last_failure=None,
                consecutive_failures=0,
                failure_type=None,
                quota_remaining=None,
                quota_used=None,
                error_msg="not configured",
            )
        if self.health_monitor is not None:
            try:
                health = self.health_monitor.get_health("OddsAPI")
                if health is not None:
                    return health
            except Exception:
                pass
        from providers.base import ProviderHealth

        return ProviderHealth(
            name="OddsAPI",
            status=ProviderStatus.DEGRADED,
            last_success=None,
            last_failure=None,
            consecutive_failures=0,
            failure_type=None,
            quota_remaining=None,
            quota_used=None,
            error_msg="configured; first request not completed",
        )


class OddsApiScheduler:
    """Quota-free discovery plus one paced, fair score-refresh turn per tick."""

    def __init__(self, client: OddsApiClient) -> None:
        self.client = client
        self._last_discovery_sport = ""
        self._pending_pick_contexts: dict[str, dict[str, Any]] = {}
        self._market_event_cursors: dict[str, int] = {}
        self._last_pick_contexts: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _market_key_for_pick(sport: str, stat: str) -> str:
        """Only map common, documented player markets; unknown labels are skipped."""
        stat_key = re.sub(r"[^a-z0-9]+", "_", str(stat).casefold()).strip("_")
        sport_key = re.sub(r"[^a-z0-9]+", "_", str(sport).casefold()).strip("_")
        aliases = {
            "points": "player_points",
            "rebounds": "player_rebounds",
            "assists": "player_assists",
            "3_pointers_made": "player_threes",
            "three_pointers_made": "player_threes",
            "threes": "player_threes",
            "steals": "player_steals",
            "blocks": "player_blocks",
            "turnovers": "player_turnovers",
            "points_rebounds_assists": "player_points_rebounds_assists",
            "pts_rebs_asts": "player_points_rebounds_assists",
            "hits": "batter_hits",
            "total_bases": "batter_total_bases",
            "home_runs": "batter_home_runs",
            "rbis": "batter_rbis",
            "runs": "batter_runs",
            "pitcher_strikeouts": "pitcher_strikeouts",
            "strikeouts": "pitcher_strikeouts",
            "passing_yards": "player_pass_yds",
            "passing_tds": "player_pass_tds",
            "passing_touchdowns": "player_pass_tds",
            "rushing_yards": "player_rush_yds",
            "receiving_yards": "player_reception_yds",
            "receptions": "player_receptions",
            "shots_on_goal": "player_shots_on_goal",
            "goals": "player_goals",
        }
        if sport_key in {"mlb", "baseball"}:
            return aliases.get(stat_key, "")
        if sport_key in {"nfl", "cfb", "american_football"}:
            return aliases.get(stat_key, "")
        if sport_key in {"nhl", "ice_hockey"}:
            return aliases.get(stat_key, "")
        if sport_key in {"nba", "wnba", "ncaab", "basketball"}:
            return aliases.get(stat_key, "")
        return aliases.get(stat_key, "")

    def queue_qualified_pick_context(self, queue_item: dict[str, Any]) -> bool:
        """Queue optional quotes only after durable admission of a qualified pick."""
        if not self.client.is_configured or not isinstance(queue_item, dict):
            return False
        try:
            qualification = json.loads(queue_item.get("qualification_json") or "{}")
            evidence = json.loads(queue_item.get("evidence_snapshot_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        provider = str(queue_item.get("provider") or "").casefold()
        if qualification.get("qualified") is not True:
            return False
        if provider not in {"underdog", "prizepicks"}:
            return False
        required = (
            "pick_key", "source_id", "event_id", "player_name", "sport",
            "stat_type", "line_value", "direction",
        )
        if any(queue_item.get(key) in (None, "") for key in required):
            return False
        if str(queue_item.get("direction") or "").upper() not in {"OVER", "UNDER"}:
            return False
        if (
            str(evidence.get("source_id") or "") != str(queue_item["source_id"])
            or str(evidence.get("source_event_id") or "")
            != str(queue_item["event_id"])
            or str(evidence.get("direction") or "").upper()
            != str(queue_item["direction"]).upper()
            or str(evidence.get("line_variant") or "")
            != str(queue_item.get("line_variant") or "")
        ):
            return False
        try:
            if float(evidence.get("line")) != float(queue_item["line_value"]):
                return False
        except (TypeError, ValueError):
            return False
        if provider == "prizepicks" and not str(queue_item.get("line_variant") or "").strip():
            return False
        event_teams = evidence.get("source_event_teams")
        if not isinstance(event_teams, list) or len(event_teams) != 1:
            return False
        pair = event_teams[0]
        if not isinstance(pair, list) or len(pair) != 2 or not all(pair):
            return False
        market_key = self._market_key_for_pick(
            str(queue_item["sport"]), str(queue_item["stat_type"])
        )
        if not market_key:
            return False
        pick = {
            "qualified": True,
            "provider": queue_item["provider"],
            "source_id": queue_item["source_id"],
            "event_id": queue_item["event_id"],
            "player_name": queue_item["player_name"],
            "sport": queue_item["sport"],
            "stat_type": queue_item["stat_type"],
            "line_value": queue_item["line_value"],
            "line_variant": queue_item.get("line_variant", ""),
            "direction": str(queue_item["direction"]).upper(),
        }
        source_event = {
            "sport": str(queue_item["sport"]),
            "home_team": str(pair[0]),
            "away_team": str(pair[1]),
            "commence_time": queue_item.get("game_time"),
        }
        self._pending_pick_contexts[str(queue_item["pick_key"])] = {
            "pick": pick,
            "source_event": source_event,
            "market_key": market_key,
        }
        return True

    def _resolve_api_sport(
        self, source_sport: str, sports: list[OddsApiSport]
    ) -> OddsApiSport | None:
        key = str(source_sport or "").strip().casefold()
        matches = [
            sport for sport in sports
            if sport.active
            and (sport.key.casefold() == key or sport.title.strip().casefold() == key)
        ]
        return matches[0] if len(matches) == 1 else None

    async def discovery_tick(self) -> dict[str, Any]:
        if not self.client.is_configured:
            return {"status": "disabled"}
        sports = await self.client.fetch_sports()
        active_sports = [sport for sport in sports if sport.active and not sport.has_outrights]
        if not active_sports:
            return {"status": "no_active_sports", "sports": 0}
        # One quota-free event listing per tick; round-robin spans every live
        # sports key rather than a curated short list.
        next_index = (active_sports.index(
            next((s for s in active_sports if s.key == self._last_discovery_sport),
                 active_sports[-1])
        ) + 1) % len(active_sports)
        sport = active_sports[next_index]
        events = await self.client.fetch_events(sport.key)
        self._last_discovery_sport = sport.key
        return {"status": "ok", "sport_key": sport.key, "events": len(events)}

    async def paid_rotation_tick(self) -> dict[str, Any]:
        if not self.client.is_configured:
            return {"status": "disabled"}
        snapshot = self.client.budget.diagnostics()
        now = datetime.now(UTC)
        next_at = _parse_datetime(snapshot.get("next_paid_request_at"))
        if next_at is None:
            next_at = self.client.budget.initialize_schedule(now)
        if now < next_at:
            return {
                "status": "paced",
                "next_request_at": next_at.isoformat(),
            }

        sports = await self.client.fetch_sports()
        active_sports = [
            sport for sport in sports
            if sport.active and not sport.has_outrights
        ]
        if not active_sports:
            return {"status": "no_active_sports"}
        index = self.client.budget.advance_rotation_cursor(len(active_sports), now)
        sport = active_sports[index]
        sport_key = sport.key

        # A qualified pick's current quote is considered only after strict
        # primary-source identity and event-team matching. Context requests are
        # per-source pick; shared quotes are served from cache when identical.
        context_item = next(
            (
                (pick_key, item)
                for pick_key, item in self._pending_pick_contexts.items()
                if self._resolve_api_sport(
                    str(item["source_event"]["sport"]), sports
                ) == sport
            ),
            None,
        )
        if context_item is not None:
            pick_key, item = context_item
            source_event = dict(item["source_event"])
            source_event["sport_key"] = sport_key
            start = _parse_datetime(source_event.get("commence_time"))
            if start is None or start <= now:
                self._pending_pick_contexts.pop(pick_key, None)
                return {"status": "pick_context_expired", "sport_key": sport_key}
            try:
                events = await self.client.fetch_events(sport_key)
                matched = match_event_exactly(source_event, events)
                if matched is None:
                    self._pending_pick_contexts.pop(pick_key, None)
                    return {"status": "pick_context_unmatched", "sport_key": sport_key}
                if not item.get("markets_confirmed"):
                    available = await self.client.fetch_event_markets(
                        sport_key, matched.id
                    )
                    if not any(
                        market.key == item["market_key"] for market in available
                    ):
                        self._pending_pick_contexts.pop(pick_key, None)
                        return {
                            "status": "pick_market_unavailable",
                            "sport_key": sport_key,
                        }
                    item["markets_confirmed"] = True
                    item["matched_event_id"] = matched.id
                    return {
                        "status": "pick_market_confirmed",
                        "sport_key": sport_key,
                        "event": matched.id,
                        "quotes": 0,
                    }
                if item.get("matched_event_id") != matched.id:
                    # Event rediscovery must not silently retarget an old pick.
                    self._pending_pick_contexts.pop(pick_key, None)
                    return {"status": "pick_event_changed", "sport_key": sport_key}
            except OddsApiDeferred as exc:
                return {"status": "deferred", "reason": str(exc)}
            except OddsApiError as exc:
                self._pending_pick_contexts.pop(pick_key, None)
                return {"status": "failed", "sport_key": sport_key, "reason": str(exc)}
            try:
                context = await self.client.market_context_for_qualified_pick(
                    item["pick"], source_event, market_key=item["market_key"]
                )
            except OddsApiDeferred as exc:
                return {"status": "deferred", "reason": str(exc)}
            except OddsApiError as exc:
                self._pending_pick_contexts.pop(pick_key, None)
                return {"status": "failed", "sport_key": sport_key, "reason": str(exc)}
            self._pending_pick_contexts.pop(pick_key, None)
            if context:
                self._last_pick_contexts[pick_key] = context
            return {
                "status": "pick_context" if context else "pick_context_unmatched",
                "sport_key": sport_key,
                "quotes": len(context.get("quotes", [])) if context else 0,
                "identity_preserved": bool(context),
                "is_player_result": False,
            }

        # Discover event-specific market availability periodically for planning.
        # Only returned API market keys are cached; they never validate a
        # PrizePicks/Underdog offering. One in four fair sport turns is reserved
        # for this one-credit discovery, and unsupported sports are still
        # represented by their own fair rotation slot.
        discover_markets = index % 4 == 0 or sport_key not in SCORE_SUPPORTED_SPORT_KEYS
        if discover_markets:
            try:
                events = await self.client.fetch_events(sport_key)
                upcoming = [
                    event for event in events
                    if event.commence_time is not None and event.commence_time > now
                ]
                if upcoming:
                    cursor = self._market_event_cursors.get(sport_key, 0) % len(upcoming)
                    self._market_event_cursors[sport_key] = cursor + 1
                    event = upcoming[cursor]
                    if not self.client.event_markets_cached(sport_key, event.id):
                        markets = await self.client.fetch_event_markets(
                            sport_key, event.id
                        )
                        return {
                            "status": "market_discovery",
                            "sport_key": sport_key,
                            "event": event.id,
                            "markets": len(markets),
                            "credits": 1,
                        }
            except OddsApiDeferred as exc:
                return {"status": "deferred", "reason": str(exc)}
            except OddsApiError as exc:
                # Try a supported score turn below; no immediate retry.
                market_error = str(exc)
            else:
                market_error = ""
        else:
            market_error = ""

        if sport_key not in SCORE_SUPPORTED_SPORT_KEYS:
            return {
                "status": "market_unavailable",
                "sport_key": sport_key,
                "reason": market_error or "no current event with market discovery data",
            }
        try:
            scores = await self.client.fetch_scores(sport_key, days_from=1)
        except OddsApiDeferred as exc:
            return {"status": "deferred", "reason": str(exc)}
        except OddsApiError as exc:
            # No immediate retry: the credit reservation and provider cooldown
            # are already persisted by the client.
            return {"status": "failed", "sport_key": sport_key, "reason": str(exc)}
        return {
            "status": "ok",
            "sport_key": sport_key,
            "events": len(scores),
            "completed": sum(1 for score in scores if score.completed),
            "credits_target": ODDS_API_MONTHLY_TARGET,
            "credits_hard_limit": ODDS_API_MONTHLY_HARD_LIMIT,
        }
