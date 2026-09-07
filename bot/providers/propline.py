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
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional
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
    "regular season points": "points",
    "season points": "points",
    "player points": "points",
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
    "fantasy_points_in_game_1": "fantasy points",
    "fantasy_points_in_game_2": "fantasy points",
    "fantasy_points_in_games_1+2": "fantasy points",
    "fantasy_points_in_games_1+2+3": "fantasy points",
    "fantasy_points_on_maps_1+2": "fantasy points",
    "fantasy points in game 1": "fantasy points",
    "fantasy points in game 2": "fantasy points",
    "fantasy points in games 1+2": "fantasy points",
    "fantasy points in games 1+2+3": "fantasy points",
    "fantasy points on maps 1+2": "fantasy points",
    "kills_in_games_1+2": "kills",
    "kills_in_games_1+2+3": "kills",
    "kills_on_maps_1+2": "kills",
    "kills_on_maps_1+2+3": "kills",
    "assists_on_maps_1+2": "assists",
    "assists_on_maps_1+2+3": "assists",
    "headshots_on_maps_1+2": "headshots",
    "headshots_on_maps_1+2+3": "headshots",
    "kills in games 1+2": "kills",
    "kills in games 1+2+3": "kills",
    "kills on maps 1+2": "kills",
    "kills on maps 1+2+3": "kills",
    "assists on maps 1+2": "assists",
    "assists on maps 1+2+3": "assists",
    "headshots on maps 1+2": "headshots",
    "headshots on maps 1+2+3": "headshots",
    "points+rebounds": "points+rebounds",
    "points+assists": "points+assists",
    "rebounds+assists": "rebounds+assists",
    "points+rebounds+assists": "pts+reb+ast",
    "points+reb+assists": "pts+reb+ast",
    "pts + reb + ast": "pts+reb+ast",
    "pts + reb": "pts+reb",
    "pts + ast": "pts+ast",
    "reb + ast": "reb+ast",
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
    "goals": "goals",
    "goal": "goals",
    "1h goals": "goals",
    "2h goals": "goals",
    "goals scored": "goals",
    "headshots": "headshots",
    "headshots on maps 1+2": "headshots",
    "headshots on maps 1+2+3": "headshots",
    "saves": "saves",
    "goals+assists": "points",
    "games won": "games won",
    "sets won": "sets won",
    "games played": "games played",
    "sets played": "sets played",
    "1st set games won": "1st set games won",
    "1st set games played": "1st set games played",
    "tiebreakers played": "tiebreakers played",
    "tie breakers played": "tiebreakers played",
    "shots on target": "shots on target",
    "shots attempted": "shots attempted",
    "goals + assists": "points",
    "hits + runs + rbis": "hits+runs+rbis",
    "significant strikes": "significant strikes",
    "fight time (mins)": "fight time (mins)",
    "takedowns": "takedowns",
    "finishing position": "finishing position",
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


PROPLINE_DAILY_REQUEST_LIMIT = 5_000
PROPLINE_ENDPOINT_DAILY_REQUEST_LIMIT = 650


class PropLineRequestBudget:
    """Process-wide-safe daily request counter for PropLine calls.

    The counter is deliberately synchronous: each client reserves a request
    while holding its own async lock, and the short synchronous mutation keeps
    separate clients from racing past the shared daily limit.
    """

    def __init__(
        self,
        limit: int = PROPLINE_DAILY_REQUEST_LIMIT,
        *,
        day_factory: Callable[[], Any] | None = None,
    ) -> None:
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


# PropLine's hard provider allowance.  The limiter below is the only request
# admission point, so separate client instances cannot burst independently.
PROPLINE_RECOVERY_REQUESTS_PER_HOUR = 160
PROPLINE_DEFAULT_REQUESTS_PER_HOUR = 180
PROPLINE_MAX_REQUESTS_PER_HOUR = 190
PROPLINE_ENDPOINT_REQUESTS_PER_HOUR = 27
PROPLINE_RECOVERY_MIN_REQUEST_INTERVAL = (
    3600.0 / PROPLINE_RECOVERY_REQUESTS_PER_HOUR
)
PROPLINE_MIN_REQUEST_INTERVAL = 3600.0 / PROPLINE_DEFAULT_REQUESTS_PER_HOUR
_DEFAULT_PROPLINE_REQUEST_BUDGET = PropLineRequestBudget(PROPLINE_DAILY_REQUEST_LIMIT)
_DEFAULT_PROPLINE_ENDPOINT_REQUEST_BUDGET = PropLineRequestBudget(
    PROPLINE_ENDPOINT_DAILY_REQUEST_LIMIT
)


def _propline_request_kind(url: str) -> str:
    """Classify a request for quota telemetry without exposing its URL."""
    path = str(url or "").lower()
    if "/odds" in path and "/sports/" in path:
        return "bulk"
    if "/events/" in path:
        return "event"
    if "/results" in path:
        return "results"
    if "/history" in path:
        return "candidate"
    if any(
        marker in path
        for marker in ("/trends", "/movement", "/best-line", "/ev", "/projections")
    ):
        return "candidate"
    if any(
        marker in path
        for marker in (
            "/futures",
            "/payouts",
            "/hit-rates",
            "/freshness",
            "/grand-salami",
            "/daily-goals",
            "/clv",
            "/sgp",
        )
    ):
        return "reporting"
    return "other"


class PropLineRequestLimiter:
    """Shared rolling-hour limiter with continuous, non-bursting pacing."""

    def __init__(
        self,
        limit: int = PROPLINE_DEFAULT_REQUESTS_PER_HOUR,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.configured_limit = min(
            PROPLINE_MAX_REQUESTS_PER_HOUR,
            max(1, int(limit)),
        )
        self.provider_limit = PROPLINE_MAX_REQUESTS_PER_HOUR
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._phase = "baseline"
        self._recovery_clean_since: Optional[float] = None
        self._attempted_count = 0
        self._successful_count = 0
        self._rate_limited_count = 0

    @property
    def effective_limit(self) -> int:
        self._advance_phase()
        phase_limit = (
            PROPLINE_RECOVERY_REQUESTS_PER_HOUR
            if self._phase == "recovery"
            else PROPLINE_MAX_REQUESTS_PER_HOUR
        )
        return min(self.configured_limit, self.provider_limit, phase_limit)

    @property
    def min_interval(self) -> float:
        return 3600.0 / self.effective_limit

    @property
    def phase(self) -> str:
        self._advance_phase()
        return self._phase

    @property
    def backoff_remaining(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    @property
    def attempted_count(self) -> int:
        return self._attempted_count

    @property
    def successful_count(self) -> int:
        return self._successful_count

    @property
    def rate_limited_count(self) -> int:
        return self._rate_limited_count

    def _advance_phase(self) -> None:
        if (
            self._phase == "recovery"
            and self._recovery_clean_since is not None
            and self._clock() - self._recovery_clean_since >= 30 * 60
        ):
            self._phase = "baseline"
            self._recovery_clean_since = None

    def record_attempt(self) -> None:
        self._attempted_count += 1

    def record_success(self) -> None:
        self._successful_count += 1
        if self._phase == "recovery" and self.backoff_remaining <= 0:
            if self._recovery_clean_since is None:
                self._recovery_clean_since = self._clock()
            self._advance_phase()

    def record_rate_limit(self) -> None:
        self._rate_limited_count += 1

    def configure_min_interval(self, interval: float) -> None:
        """Honor a caller's slower configured pace without raising the cap."""
        interval = max(0.0, float(interval))
        if interval <= PROPLINE_MIN_REQUEST_INTERVAL:
            return
        configured_limit = max(1, int(3600.0 // interval))
        self.configured_limit = min(self.configured_limit, configured_limit)

    def observe_provider_limit(self, headers: Any) -> None:
        """Adopt a lower limit advertised by the provider, never a higher one."""
        if not headers:
            return
        normalized = {
            str(name).lower(): value
            for name, value in getattr(headers, "items", lambda: ())()
        }
        for name in (
            "x-ratelimit-limit",
            "x-rate-limit-limit",
            "ratelimit-limit",
        ):
            raw = normalized.get(name)
            if raw is not None:
                match = re.search(r"\d+", str(raw))
                if match:
                    reported = int(match.group())
                    if 0 < reported < self.provider_limit:
                        self.provider_limit = reported
                break

    async def acquire(self) -> None:
        """Wait for one slot; reservations are spaced across the hour."""
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    raise PropLineRateLimitError(
                        "PropLine rate-limit backoff active",
                        retry_after=self._blocked_until - now,
                    )
                cutoff = now - 3600.0
                while self._request_times and self._request_times[0] <= cutoff:
                    self._request_times.popleft()

                delay = max(0.0, self._blocked_until - now)
                delay = max(delay, self._next_request_at - now)
                if len(self._request_times) >= self.effective_limit:
                    delay = max(
                        delay,
                        self._request_times[0] + 3600.0 - now,
                    )
                if delay > 0:
                    await self._sleep(delay)
                    continue

                reserved_at = self._clock()
                self._request_times.append(reserved_at)
                self._next_request_at = reserved_at + self.min_interval
                return

    def defer(self, retry_after: float) -> None:
        """Share a provider cooldown across every client instance."""
        self._phase = "recovery"
        self._recovery_clean_since = None
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + max(1.0, float(retry_after)),
        )
        self.record_rate_limit()

    def reset(self) -> None:
        self._request_times.clear()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._phase = "baseline"
        self._recovery_clean_since = None
        self._attempted_count = 0
        self._successful_count = 0
        self._rate_limited_count = 0


_DEFAULT_PROPLINE_REQUEST_LIMITER = PropLineRequestLimiter()
_DEFAULT_PROPLINE_ENDPOINT_REQUEST_LIMITER = PropLineRequestLimiter(
    PROPLINE_ENDPOINT_REQUESTS_PER_HOUR
)


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
    raw_record_count: int = 0
    normalized_prop_count: int = 0
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
class PropLineMovementPoint:
    """One timestamped PropLine observation in a movement timeline."""

    observed_at: datetime
    line_value: float
    delta_from_previous: Optional[float] = None
    delta_from_opening: Optional[float] = None
    external_id: str = ""
    event_id: str = ""
    provider: str = PROPLINE_PROVIDER_NAME
    supplemental_source: str = ""


@dataclass(frozen=True)
class PropLineOpeningLineEvidence:
    """The first observed line for one PropLine source identity."""

    observed_at: datetime
    line_value: float
    provider: str
    player_name: str
    sport: str
    stat_type: str
    external_id: str = ""
    event_id: str = ""
    supplemental_source: str = ""


PROPLINE_RESULT_WON = "WON"
PROPLINE_RESULT_LOST = "LOST"
PROPLINE_RESULT_PUSH = "PUSH"
PROPLINE_RESULT_VOID = "VOID"
PROPLINE_RESULT_UNGRADED = "UNGRADED"
PROPLINE_RESULT_UNRESOLVED = "UNRESOLVED"
PROPLINE_RESULT_INCOMPLETE = "INCOMPLETE"
_PROPLINE_SETTLED_RESULTS = {
    PROPLINE_RESULT_WON,
    PROPLINE_RESULT_LOST,
    PROPLINE_RESULT_PUSH,
    PROPLINE_RESULT_VOID,
}
_PROPLINE_KNOWN_RESULT_STATES = _PROPLINE_SETTLED_RESULTS | {
    PROPLINE_RESULT_UNGRADED,
    PROPLINE_RESULT_UNRESOLVED,
    PROPLINE_RESULT_INCOMPLETE,
}


def normalize_propline_result_state(
    value: Any,
    *,
    response_complete: bool = True,
) -> str:
    """Normalize a documented result without manufacturing a settlement.

    A null/blank ``resolution`` means that PropLine has not supplied a
    resolution yet.  ``response_complete`` describes the HTTP payload rather
    than the market itself, so an incomplete payload always remains distinct
    from an unresolved result.  Unknown non-empty provider labels are retained
    in uppercase and are never considered settled.
    """
    if not response_complete:
        return PROPLINE_RESULT_INCOMPLETE
    raw = _clean(value)
    if not raw:
        return PROPLINE_RESULT_UNRESOLVED
    normalized = re.sub(r"[^a-z]+", "_", raw.lower()).strip("_").upper()
    if normalized in _PROPLINE_KNOWN_RESULT_STATES:
        return normalized
    return normalized


@dataclass(frozen=True)
class PropLineResultEvidence:
    """One read-only PrizePicks result returned by PropLine."""

    observed_at: datetime
    resolution_state: str
    source_resolution: str
    actual_value: Optional[float]
    resolved_at: Optional[datetime]
    provider: str
    player_name: str
    sport: str
    stat_type: str
    line_value: Optional[float] = None
    direction: str = ""
    event_id: str = ""
    player_id: str = ""
    market_key: str = ""
    market_description: str = ""
    bookmaker_key: str = ""
    bookmaker_title: str = ""
    provider_status: str = ""
    redacted: bool = False
    dfs_odds_type: str = ""
    supplemental_source: str = "PropLine"
    response_complete: bool = True

    @property
    def is_settled(self) -> bool:
        """Only explicit won/lost/push/void labels count as settled."""
        return self.resolution_state in _PROPLINE_SETTLED_RESULTS


@dataclass(frozen=True)
class PropLineResultsFetchResult:
    """Result of an explicit event-results request, separate from polling."""

    results: tuple[PropLineResultEvidence, ...]
    fetched_at: datetime
    event_id: str = ""
    succeeded: bool = False
    complete: bool = False
    error: str = ""
    failure_kind: str = ""


@dataclass(frozen=True)
class PropLineEndpointResult:
    """Result envelope for a documented, read-only PropLine endpoint.

    The payload is intentionally not projected into a betting model here.
    Each endpoint has a different shape and callers decide whether it is
    suitable for evidence, reporting, or corroboration.  Keeping the raw
    response in this envelope also preserves redacted/free-tier responses.
    """

    payload: Any
    fetched_at: datetime
    endpoint: str = ""
    from_cache: bool = False
    succeeded: bool = False
    complete: bool = False
    redacted: bool = False
    upgrade_url: str = ""
    error: str = ""
    failure_kind: str = ""

    @property
    def data(self) -> Any:
        """Compatibility/readability alias for the response payload."""
        return self.payload


@dataclass(frozen=True)
class PropLinePlayerHistoryEntry:
    """One official, supplemental PropLine player-prop history entry."""

    observed_at: datetime
    player_name: str
    sport: str
    market: str
    event_id: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmaker: str
    bookmaker_title: str
    line: Optional[float]
    over_price: Optional[int]
    under_price: Optional[int]
    actual_value: Optional[float]
    over_result: Optional[str]
    under_result: Optional[str]
    resolved_at: Optional[datetime]
    provider: str = PROPLINE_PROVIDER_NAME
    supplemental_source: str = "PropLine"
    redacted: bool = False
    response_complete: bool = True

    @property
    def over_resolution_state(self) -> str:
        return normalize_propline_result_state(self.over_result)

    @property
    def under_resolution_state(self) -> str:
        return normalize_propline_result_state(self.under_result)

    @property
    def over_is_settled(self) -> bool:
        return self.over_resolution_state in _PROPLINE_SETTLED_RESULTS

    @property
    def under_is_settled(self) -> bool:
        return self.under_resolution_state in _PROPLINE_SETTLED_RESULTS


PropLinePlayerHistoryEvidence = PropLinePlayerHistoryEntry


@dataclass(frozen=True)
class PropLinePlayerHistoryFetchResult:
    """One official player-history response, kept separate from prop odds."""

    entries: tuple[PropLinePlayerHistoryEntry, ...]
    fetched_at: datetime
    player_name: str = ""
    sport_key: str = ""
    market: str = ""
    from_cache: bool = False
    succeeded: bool = False
    complete: bool = False
    redacted: bool = False
    upgrade_url: str = ""
    error: str = ""
    failure_kind: str = ""

    @property
    def history(self) -> tuple[PropLinePlayerHistoryEntry, ...]:
        return self.entries


@dataclass(frozen=True)
class PropLineHitRateSplit:
    window: int
    games: int
    over: int
    under: int
    push: int
    over_pct: Optional[float]


@dataclass(frozen=True)
class PropLineTrendStreak:
    result: str
    length: int


@dataclass(frozen=True)
class PropLineTrendLastGame:
    event_id: str
    commence_time: datetime
    line: Optional[float]
    actual_value: Optional[float]
    result: Optional[str]

    @property
    def resolution_state(self) -> str:
        return normalize_propline_result_state(self.result)


@dataclass(frozen=True)
class PropLinePlayerMarketTrend:
    """Aggregated, source-provided hit-rate data for one player market."""

    market: str
    games_graded: int
    reference_bookmaker: Optional[str]
    reference_bookmaker_title: Optional[str]
    recent_line: Optional[float]
    avg_actual: Optional[float]
    last_5: Optional[PropLineHitRateSplit]
    last_10: Optional[PropLineHitRateSplit]
    last_20: Optional[PropLineHitRateSplit]
    last_50: Optional[PropLineHitRateSplit]
    current_streak: Optional[PropLineTrendStreak]
    last_game: Optional[PropLineTrendLastGame]
    provider: str = PROPLINE_PROVIDER_NAME
    supplemental_source: str = "PropLine"
    redacted: bool = False
    response_complete: bool = True


@dataclass(frozen=True)
class PropLinePlayerTrendsFetchResult:
    """One official player-trends response, kept separate from prop odds."""

    markets: tuple[PropLinePlayerMarketTrend, ...]
    fetched_at: datetime
    player_name: str = ""
    sport_key: str = ""
    dfs_odds_type: Optional[str] = None
    from_cache: bool = False
    succeeded: bool = False
    complete: bool = False
    redacted: bool = False
    upgrade_url: str = ""
    error: str = ""
    failure_kind: str = ""

    @property
    def trends(self) -> tuple[PropLinePlayerMarketTrend, ...]:
        return self.markets


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected a numeric value")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("expected a numeric value")


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected an integer value")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("expected an integer value")
    if isinstance(value, float) and parsed != value:
        raise ValueError("expected an integer value")
    return parsed


def _documented_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer value")
    return value


def build_propline_player_history_entry(
    record: dict[str, Any],
    *,
    player_name: str = "",
    sport_key: str = "",
    market: str = "",
    observed_at: Optional[datetime] = None,
    provider: str = PROPLINE_PROVIDER_NAME,
) -> Optional[PropLinePlayerHistoryEntry]:
    """Normalize one documented history entry without inferring outcomes."""
    if not isinstance(record, dict) or _clean(provider) != PROPLINE_PROVIDER_NAME:
        return None
    name = _clean(record.get("player_name")) or _clean(player_name)
    raw_sport = _clean(record.get("sport_key")) or _clean(sport_key)
    normalized_sport = _normalize_sport(raw_sport)
    market_name = _clean(record.get("market")) or _clean(market)
    event_id = _clean(record.get("event_id"))
    commence_time = _parse_datetime(record.get("commence_time"))
    observed = observed_at or _parse_datetime(record.get("_observed_at"))
    if not name or not normalized_sport or not market_name or not event_id:
        return None
    if commence_time is None or not isinstance(observed, datetime):
        return None
    resolved_at = _parse_datetime(record.get("resolved_at"))
    if record.get("resolved_at") not in (None, "") and resolved_at is None:
        return None
    try:
        line = _optional_float(record.get("line"))
        over_price = _optional_int(record.get("over_price"))
        under_price = _optional_int(record.get("under_price"))
        actual_value = _optional_float(record.get("actual_value"))
    except ValueError:
        return None
    over_result = record.get("over_result")
    under_result = record.get("under_result")
    if over_result is not None and not isinstance(over_result, str):
        return None
    if under_result is not None and not isinstance(under_result, str):
        return None
    bookmaker = _clean(record.get("bookmaker"))
    bookmaker_title = _clean(record.get("bookmaker_title"))
    normalized_bookmaker = bookmaker.lower().replace("_", "").replace("-", "")
    entry_provider = (
        PROPLINE_PROVIDER_NAME
        if normalized_bookmaker in {"prizepicks", "prizepick"}
        else bookmaker_title or bookmaker or PROPLINE_PROVIDER_NAME
    )
    return PropLinePlayerHistoryEntry(
        observed_at=observed,
        player_name=name,
        sport=normalized_sport,
        market=market_name,
        event_id=event_id,
        commence_time=commence_time,
        home_team=_clean(record.get("home_team")),
        away_team=_clean(record.get("away_team")),
        bookmaker=bookmaker,
        bookmaker_title=bookmaker_title,
        line=line,
        over_price=over_price,
        under_price=under_price,
        actual_value=actual_value,
        over_result=over_result,
        under_result=under_result,
        resolved_at=resolved_at,
        provider=entry_provider,
        supplemental_source=_clean(record.get("supplemental_source")) or "PropLine",
        redacted=bool(record.get("redacted", False)),
    )


def _build_propline_hit_rate_split(value: Any) -> Optional[PropLineHitRateSplit]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("trend split is incomplete")
    required = {"window", "games", "over", "under", "push", "over_pct"}
    if not required.issubset(value):
        raise ValueError("trend split is incomplete")
    try:
        values = [
            _documented_int(value[field])
            for field in ("window", "games", "over", "under", "push")
        ]
        over_pct = _optional_float(value["over_pct"])
    except (TypeError, ValueError):
        raise ValueError("trend split contains an invalid value")
    if min(values) < 0:
        raise ValueError("trend split contains a negative value")
    return PropLineHitRateSplit(*values, over_pct)


def _build_propline_trend_last_game(value: Any) -> Optional[PropLineTrendLastGame]:
    if value is None:
        return None
    required = {"event_id", "commence_time", "line", "actual_value", "result"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("trend last game is incomplete")
    commence_time = _parse_datetime(value["commence_time"])
    if not _clean(value["event_id"]) or commence_time is None:
        raise ValueError("trend last game identity is incomplete")
    result = value["result"]
    if result is not None and not isinstance(result, str):
        raise ValueError("trend last game result is invalid")
    try:
        line = _optional_float(value["line"])
        actual_value = _optional_float(value["actual_value"])
    except ValueError:
        raise ValueError("trend last game contains an invalid value")
    return PropLineTrendLastGame(
        _clean(value["event_id"]), commence_time, line, actual_value, result
    )


def _build_propline_player_market_trend(
    record: Any,
    *,
    redacted: bool = False,
) -> PropLinePlayerMarketTrend:
    required = {
        "market", "games_graded", "reference_bookmaker",
        "reference_bookmaker_title", "recent_line", "avg_actual",
        "last_5", "last_10", "last_20", "last_50",
        "current_streak", "last_game",
    }
    if not isinstance(record, dict) or not required.issubset(record):
        raise ValueError("player trend is incomplete")
    if not _clean(record["market"]):
        raise ValueError("player trend identity is incomplete")
    try:
        games_graded = _documented_int(record["games_graded"])
        if games_graded < 0:
            raise ValueError("negative games graded")
        splits = [
            _build_propline_hit_rate_split(record[field])
            for field in ("last_5", "last_10", "last_20", "last_50")
        ]
        streak_value = record["current_streak"]
        if streak_value is None:
            streak = None
        elif (
            isinstance(streak_value, dict)
            and {"result", "length"}.issubset(streak_value)
            and isinstance(streak_value["result"], str)
        ):
            streak = PropLineTrendStreak(
                streak_value["result"], _documented_int(streak_value["length"])
            )
        else:
            raise ValueError("trend streak is incomplete")
        last_game = _build_propline_trend_last_game(record["last_game"])
        recent_line = _optional_float(record["recent_line"])
        avg_actual = _optional_float(record["avg_actual"])
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc))
    reference_bookmaker = (
        _clean(record["reference_bookmaker"])
        if record["reference_bookmaker"] is not None else ""
    )
    reference_title = (
        _clean(record["reference_bookmaker_title"])
        if record["reference_bookmaker_title"] is not None else ""
    )
    normalized_bookmaker = reference_bookmaker.lower().replace("_", "").replace("-", "")
    trend_provider = (
        PROPLINE_PROVIDER_NAME
        if normalized_bookmaker in {"prizepicks", "prizepick"}
        else reference_title or reference_bookmaker or PROPLINE_PROVIDER_NAME
    )
    return PropLinePlayerMarketTrend(
        market=_clean(record["market"]),
        games_graded=games_graded,
        reference_bookmaker=reference_bookmaker or None,
        reference_bookmaker_title=reference_title or None,
        recent_line=recent_line,
        avg_actual=avg_actual,
        last_5=splits[0],
        last_10=splits[1],
        last_20=splits[2],
        last_50=splits[3],
        current_streak=streak,
        last_game=last_game,
        provider=trend_provider,
        redacted=redacted or bool(record.get("redacted", False)),
    )


def build_propline_result_evidence(
    record: dict[str, Any],
    *,
    observed_at: Optional[datetime] = None,
    provider: str = PROPLINE_PROVIDER_NAME,
    response_complete: bool = True,
) -> Optional[PropLineResultEvidence]:
    """Build result evidence from the documented results response fields.

    This helper only copies a provider-supplied ``resolution``.  A missing
    resolution becomes ``UNRESOLVED``; it is never derived from event status,
    actual value, line, or the last observed projection.
    """
    if not isinstance(record, dict):
        return None
    expected_provider = _clean(provider)
    if expected_provider != PROPLINE_PROVIDER_NAME:
        return None
    if _clean(record.get("provider")) != expected_provider:
        return None
    if _clean(record.get("bookmaker_key")).replace("_", "").lower() not in {
        "prizepicks",
        "prizepick",
    }:
        return None

    event_id = _clean(record.get("event_id"))
    player_name = _clean(record.get("player_name"))
    market_key = _clean(record.get("market_key")).lower()
    if not event_id or not player_name or not market_key.startswith("player_"):
        return None
    stat_type = _normalize_stat(market_key.removeprefix("player_"))
    sport = _normalize_sport(record.get("sport_key"))
    if not sport or not stat_type or is_football_related_sport(sport):
        return None

    observed = observed_at or _parse_datetime(record.get("_observed_at"))
    if not isinstance(observed, datetime):
        return None

    line_value: Optional[float]
    raw_line = record.get("line")
    if raw_line is None:
        line_value = None
    else:
        try:
            line_value = float(raw_line)
        except (TypeError, ValueError):
            return None

    actual_value: Optional[float]
    raw_actual = record.get("actual_value")
    if raw_actual is None:
        actual_value = None
    else:
        try:
            actual_value = float(raw_actual)
        except (TypeError, ValueError):
            return None

    resolved_at = _parse_datetime(record.get("resolved_at"))
    if record.get("resolved_at") not in (None, "") and resolved_at is None:
        return None
    source_resolution = _clean(record.get("resolution"))
    return PropLineResultEvidence(
        observed_at=observed,
        resolution_state=normalize_propline_result_state(
            source_resolution,
            response_complete=response_complete,
        ),
        source_resolution=source_resolution,
        actual_value=actual_value,
        resolved_at=resolved_at,
        provider=expected_provider,
        player_name=player_name,
        sport=sport,
        stat_type=stat_type,
        line_value=line_value,
        direction=_normalize_direction(record.get("direction")),
        event_id=event_id,
        player_id=_clean(record.get("player_id")),
        market_key=market_key,
        market_description=_clean(record.get("market_description")),
        bookmaker_key=_clean(record.get("bookmaker_key")),
        bookmaker_title=_clean(record.get("bookmaker_title")),
        provider_status=_clean(record.get("event_status")),
        redacted=bool(record.get("redacted", False)),
        dfs_odds_type=_clean(record.get("dfs_odds_type")),
        supplemental_source=_clean(record.get("supplemental_source")) or "PropLine",
        response_complete=response_complete,
    )


PROPLINE_VERIFIED_CLOSING_LABEL = "VERIFIED_CLOSING_LINE"
PROPLINE_TERMINAL_OBSERVED_LABEL = "TERMINAL_LAST_OBSERVED"
PROPLINE_VERIFIED_CLOSING_BASIS = "provider_closed_state"
PROPLINE_TERMINAL_OBSERVED_BASIS = "last_persisted_observation"
_PROPLINE_CLOSED_STATUSES = {
    "closed",
    "complete",
    "completed",
    "final",
    "resolved",
    "settled",
    "settlement",
}


@dataclass(frozen=True)
class PropLineClosingLineEvidence:
    """Last line evidence, explicitly distinguished from a verified close."""

    observed_at: datetime
    line_value: float
    label: str
    evidence_basis: str
    is_verified_closing: bool
    provider_status: str
    provider: str
    player_name: str
    sport: str
    stat_type: str
    external_id: str = ""
    event_id: str = ""
    supplemental_source: str = ""


@dataclass(frozen=True)
class PropLineMovementHistory:
    """Read-only line movement evidence for one PrizePicks source identity."""

    provider: str
    player_name: str
    sport: str
    stat_type: str
    opening_line: float
    current_line: float
    total_delta: float
    change_count: int
    points: tuple[PropLineMovementPoint, ...]
    supplemental_source: str = ""
    opening_evidence: Optional[PropLineOpeningLineEvidence] = None

    @property
    def direction(self) -> str:
        """Return the observed movement direction without making a bet decision."""
        if self.total_delta > 0:
            return "UP"
        if self.total_delta < 0:
            return "DOWN"
        return "FLAT"


def build_propline_opening_line_evidence(
    row: Any,
    *,
    provider: str = PROPLINE_PROVIDER_NAME,
) -> Optional[PropLineOpeningLineEvidence]:
    """Convert one persisted first-observation row into opening evidence."""
    expected_provider = _clean(provider)
    if _clean(getattr(row, "provider", "")) != expected_provider:
        return None
    observed_at = getattr(row, "fetched_at", None)
    if not isinstance(observed_at, datetime):
        return None
    try:
        line_value = float(getattr(row, "line_value"))
    except (TypeError, ValueError):
        return None
    return PropLineOpeningLineEvidence(
        observed_at=observed_at,
        line_value=line_value,
        provider=expected_provider,
        player_name=_clean(getattr(row, "player_name", "")),
        sport=_clean(getattr(row, "sport", "")),
        stat_type=_clean(getattr(row, "stat_type", "")),
        external_id=_clean(getattr(row, "external_id", "")),
        event_id=_clean(
            getattr(row, "event_id", "")
            or getattr(row, "game_id", "")
        ),
        supplemental_source=_clean(getattr(row, "supplemental_source", "")),
    )


def _propline_status_is_closed(status: Any) -> bool:
    """Accept only explicit provider states that establish market completion."""
    normalized = re.sub(r"[^a-z]+", "_", _clean(status).lower()).strip("_")
    return normalized in _PROPLINE_CLOSED_STATUSES


def build_propline_closing_line_evidence(
    rows: Iterable[Any],
    *,
    provider: str = PROPLINE_PROVIDER_NAME,
    event_status: str = "",
) -> Optional[PropLineClosingLineEvidence]:
    """Build close evidence without upgrading a last observation to a close.

    The latest source-scoped persisted observation is always available as
    terminal evidence.  It becomes a verified closing line only when the
    caller or latest source row supplies an explicit closed/final state.
    """
    expected_provider = _clean(provider)
    candidates = [
        row for row in rows
        if _clean(getattr(row, "provider", "")) == expected_provider
    ]
    if not candidates:
        return None

    source_ids = {
        _clean(getattr(row, "external_id", ""))
        or _clean(getattr(row, "game_id", ""))
        for row in candidates
    }
    source_ids.discard("")
    if len(source_ids) != 1:
        return None

    dated_rows = [
        row for row in candidates
        if isinstance(getattr(row, "fetched_at", None), datetime)
    ]
    if not dated_rows:
        return None
    latest = max(
        dated_rows,
        key=lambda row: (
            getattr(row, "fetched_at"),
            int(getattr(row, "id", 0) or 0),
        ),
    )
    movement = build_propline_line_movement(
        candidates,
        provider=expected_provider,
    )
    if movement is None or not movement.points:
        return None
    latest_point = movement.points[-1]
    source_status = _clean(
        event_status or getattr(latest, "provider_status", "")
    )
    verified = _propline_status_is_closed(source_status)
    return PropLineClosingLineEvidence(
        observed_at=latest_point.observed_at,
        line_value=latest_point.line_value,
        label=(
            PROPLINE_VERIFIED_CLOSING_LABEL
            if verified
            else PROPLINE_TERMINAL_OBSERVED_LABEL
        ),
        evidence_basis=(
            PROPLINE_VERIFIED_CLOSING_BASIS
            if verified
            else PROPLINE_TERMINAL_OBSERVED_BASIS
        ),
        is_verified_closing=verified,
        provider_status=source_status,
        provider=expected_provider,
        player_name=movement.player_name,
        sport=movement.sport,
        stat_type=movement.stat_type,
        external_id=latest_point.external_id,
        event_id=latest_point.event_id,
        supplemental_source=latest_point.supplemental_source,
    )


def build_propline_line_movement(
    rows: Iterable[Any],
    *,
    provider: str = PROPLINE_PROVIDER_NAME,
) -> Optional[PropLineMovementHistory]:
    """Build a source-scoped movement timeline from persisted observations.

    This is intentionally a pure evidence helper.  It never calls PropLine,
    changes a provider line, or produces a recommendation.  Rows from another
    provider are ignored so PrizePicks and Underdog history cannot be mixed.
    Callers should pass rows selected for one source identity (external ID or
    event ID); mixed source identities are rejected rather than combined.
    """
    expected_provider = _clean(provider)
    candidates = [
        row for row in rows
        if _clean(getattr(row, "provider", "")) == expected_provider
    ]
    if not candidates:
        return None

    source_ids = {
        _clean(getattr(row, "external_id", ""))
        or _clean(getattr(row, "game_id", ""))
        for row in candidates
    }
    source_ids.discard("")
    if len(source_ids) > 1:
        return None

    normalized: list[tuple[datetime, Any, float]] = []
    for row in candidates:
        observed_at = getattr(row, "fetched_at", None)
        if not isinstance(observed_at, datetime):
            continue
        try:
            line_value = float(getattr(row, "line_value"))
        except (TypeError, ValueError):
            continue
        normalized.append((observed_at, row, line_value))
    if not normalized:
        return None

    normalized.sort(key=lambda item: item[0])
    opening_line = normalized[0][2]
    previous_line: Optional[float] = None
    points: list[PropLineMovementPoint] = []
    change_count = 0
    supplemental_sources: set[str] = set()

    for observed_at, row, line_value in normalized:
        delta_from_previous = (
            line_value - previous_line if previous_line is not None else None
        )
        if delta_from_previous is not None and abs(delta_from_previous) >= 0.01:
            change_count += 1
        supplemental_source = _clean(getattr(row, "supplemental_source", ""))
        if supplemental_source:
            supplemental_sources.add(supplemental_source)
        points.append(
            PropLineMovementPoint(
                observed_at=observed_at,
                line_value=line_value,
                delta_from_previous=delta_from_previous,
                delta_from_opening=line_value - opening_line,
                external_id=_clean(getattr(row, "external_id", "")),
                event_id=_clean(
                    getattr(row, "event_id", "")
                    or getattr(row, "game_id", "")
                ),
                provider=expected_provider,
                supplemental_source=supplemental_source,
            )
        )
        previous_line = line_value

    current_line = points[-1].line_value
    opening_evidence = build_propline_opening_line_evidence(
        normalized[0][1],
        provider=expected_provider,
    )
    return PropLineMovementHistory(
        provider=expected_provider,
        player_name=_clean(getattr(normalized[0][1], "player_name", "")),
        sport=_clean(getattr(normalized[0][1], "sport", "")),
        stat_type=_clean(getattr(normalized[0][1], "stat_type", "")),
        opening_line=opening_line,
        current_line=current_line,
        total_delta=current_line - opening_line,
        change_count=change_count,
        points=tuple(points),
        supplemental_source=(
            "PropLine" if "PropLine" in supplemental_sources
            else (sorted(supplemental_sources)[0] if supplemental_sources else "")
        ),
        opening_evidence=opening_evidence,
    )


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


def _safe_player_name_match(first: Any, second: Any) -> bool:
    """Accept only an exact normalized player-name match."""
    return _identity_part(first) == _identity_part(second) and bool(
        _identity_part(first)
    )


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
    compact = re.sub(r"\s*\+\s*", "+", raw)
    return _STAT_NORMALIZATION.get(
        raw,
        _STAT_NORMALIZATION.get(compact, compact),
    )


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
        line_variant=_clean(
            _first(raw_record, "dfs_odds_type", "line_variant", "odds_type")
        ).lower(),
        supplemental_source="PropLine",
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
    "player_hits_runs_rbis",
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
    "player_shots_on_target",
    "player_goals",
    "player_headshots",
    "player_saves",
    "player_games_won",
    "player_sets_won",
    "player_games_played",
    "player_sets_played",
    "player_tiebreakers_played",
    "player_kills",
    "player_deaths",
    "player_maps_won",
    "player_fantasy_points",
)

# Keep the odds request conservative: PropLine's catalog can expose a valid
# player-history market before that market is accepted by every sport's bulk
# odds route. History resolution may still use the expanded market mapping
# above without risking a whole-sport odds request.
_OFFICIAL_ODDS_MARKETS = tuple(
    market
    for market in _OFFICIAL_MARKETS
    if market not in {"player_goals", "player_headshots", "player_saves"}
)

_STAT_TO_OFFICIAL_MARKET = {
    "points": "player_points",
    "goals": "player_goals",
    "headshots": "player_headshots",
    "saves": "player_saves",
    "rebounds": "player_rebounds",
    "assists": "player_assists",
    "3-pointers made": "player_threes",
    "blocks": "player_blocks",
    "steals": "player_steals",
    "pts+reb": "player_points_rebounds",
    "points+rebounds": "player_points_rebounds",
    "pts+ast": "player_points_assists",
    "points+assists": "player_points_assists",
    "reb+ast": "player_rebounds_assists",
    "rebounds+assists": "player_rebounds_assists",
    "pts+reb+ast": "player_points_rebounds_assists",
    "points+rebounds+assists": "player_points_rebounds_assists",
    "fantasy points": "player_fantasy_points",
    "hits": "player_hits",
    "home runs": "player_home_runs",
    "rbis": "player_rbis",
    "total bases": "player_total_bases",
    "strikeouts": "player_strikeouts",
    "walks": "player_walks",
    "walks allowed": "player_walks",
    "hits+runs+rbis": "player_hits_runs_rbis",
    "rushing yards": "player_rushing_yards",
    "receiving yards": "player_receiving_yards",
    "receptions": "player_receptions",
    "passing yards": "player_passing_yards",
    "passing tds": "player_passing_tds",
    "rush+rec yards": "player_rush_rec_yards",
    "shots on goal": "player_shots_on_goal",
    "shots on target": "player_shots_on_target",
    "games won": "player_games_won",
    "sets won": "player_sets_won",
    "games played": "player_games_played",
    "sets played": "player_sets_played",
    "tiebreakers played": "player_tiebreakers_played",
    "tie breakers played": "player_tiebreakers_played",
    "kills": "player_kills",
    "deaths": "player_deaths",
    "maps won": "player_maps_won",
}


def official_player_market_for(stat_type: Any) -> Optional[str]:
    """Return the documented player market key for a normalized stat.

    The history endpoint takes official market keys, not the bot's display
    labels.  Returning ``None`` for an unknown composite is intentional:
    asking PropLine for a guessed market can return a valid-looking but
    unrelated response.
    """
    candidate = _clean(stat_type).lower()
    if candidate.startswith("player_") and candidate in _OFFICIAL_MARKETS:
        return candidate
    normalized = _normalize_stat(stat_type)
    return _STAT_TO_OFFICIAL_MARKET.get(normalized)

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
    "BOXING": "boxing",
    "TT": "table_tennis",
    "TABLE TENNIS": "table_tennis",
    "BADMINTON": "badminton",
    # PropLine currently exposes one shared esports catalog entry. Keep the
    # bot's sport identity in the request/response metadata while routing the
    # provider call through that documented namespace.
    "CS": "esports",
    "DOTA": "esports",
    "LOL": "esports",
    "VAL": "esports",
    "VALORANT": "esports",
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
    "boxing": "BOXING",
    "table_tennis": "TT",
    "badminton": "BADMINTON",
    "esports": "ESPORTS",
}

_ESPORTS_SPORT_ALIASES = frozenset({"CS", "DOTA", "LOL", "VAL", "VALORANT", "ESPORTS"})


def _sports_match(left: Any, right: Any) -> bool:
    """Match canonical sport names, including PropLine's shared esports key."""
    left_key = _normalize_sport(left)
    right_key = _normalize_sport(right)
    if left_key == right_key:
        return True
    return left_key in _ESPORTS_SPORT_ALIASES and right_key in _ESPORTS_SPORT_ALIASES


def _canonical_discovered_sport(official_key: str, title: Any = "") -> str:
    """Map provider sport identifiers into the bot's existing sport namespace."""
    key = _clean(official_key).lower()
    if key in _OFFICIAL_TO_CANONICAL_SPORT:
        return _OFFICIAL_TO_CANONICAL_SPORT[key]

    normalized_title = _clean(title).upper().replace("_", " ")
    normalized_key = re.sub(r"[^A-Z0-9]+", " ", key.upper()).strip()
    sport_text = f"{normalized_key} {normalized_title}".strip()
    if (
        "LEAGUE OF LEGENDS" in sport_text
        or normalized_key in {"LOL", "ESPORTS LOL"}
    ):
        return "LOL"
    if "VALORANT" in sport_text or normalized_key in {"VAL", "ESPORTS VAL"}:
        return "VALORANT"
    if (
        "TABLE TENNIS" in sport_text
        or normalized_key in {"TT", "TABLE TENNIS", "TABLETENNIS"}
    ):
        return "TT"
    if "BADMINTON" in sport_text:
        return "BADMINTON"
    if "DOTA" in sport_text or normalized_key in {"DOTA", "DOTA 2"}:
        return "DOTA"
    if (
        "COUNTER STRIKE" in sport_text
        or "CS2" in sport_text
        or normalized_key in {"CS", "ESPORTS CS"}
    ):
        return "CS"
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
                    odds_type = (
                        _clean(outcome.get("dfs_odds_type")).lower()
                        or "standard"
                    )
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
                            "dfs_odds_type": odds_type,
                            # The odds endpoint can return a currently
                            # available line whose last-change timestamp is
                            # older than the fetch.  The client fetch time is
                            # the authoritative freshness marker for this
                            # current-line response.
                            "status": "available",
                        }
                    )
    return records


def _official_result_records(
    payload: Any,
    *,
    bookmaker: str = "prizepicks",
) -> tuple[list[dict[str, Any]], bool, str]:
    """Flatten the documented event-results response for one bookmaker."""
    if not isinstance(payload, dict):
        return [], False, "PropLine results response is not an object"
    required_event_fields = {
        "id",
        "sport_key",
        "home_team",
        "away_team",
        "commence_time",
        "status",
        "home_score",
        "away_score",
        "bookmakers",
    }
    if not required_event_fields.issubset(payload):
        return [], False, "PropLine results response is incomplete"
    if not isinstance(payload.get("bookmakers"), list):
        return [], False, "PropLine results bookmakers are incomplete"

    requested_bookmaker = _clean(bookmaker).lower().replace("_", "")
    records: list[dict[str, Any]] = []
    for bookmaker_record in payload["bookmakers"]:
        if not isinstance(bookmaker_record, dict):
            return [], False, "PropLine results bookmaker is incomplete"
        if not {"key", "title", "markets"}.issubset(bookmaker_record):
            return [], False, "PropLine results bookmaker is incomplete"
        if not isinstance(bookmaker_record["markets"], list):
            return [], False, "PropLine results markets are incomplete"

        bookmaker_key = _clean(bookmaker_record.get("key"))
        if bookmaker_key.lower().replace("_", "") != requested_bookmaker:
            continue
        for market in bookmaker_record["markets"]:
            if not isinstance(market, dict):
                return [], False, "PropLine results market is incomplete"
            if not {"key", "description", "outcomes"}.issubset(market):
                return [], False, "PropLine results market is incomplete"
            if not isinstance(market["outcomes"], list):
                return [], False, "PropLine results outcomes are incomplete"
            market_key = _clean(market.get("key")).lower()
            if not market_key.startswith("player_"):
                continue
            for outcome in market["outcomes"]:
                if not isinstance(outcome, dict):
                    return [], False, "PropLine results outcome is incomplete"
                required_outcome_fields = {
                    "name",
                    "description",
                    "price",
                    "point",
                    "resolution",
                    "actual_value",
                    "resolved_at",
                }
                if not required_outcome_fields.issubset(outcome):
                    return [], False, "PropLine results outcome is incomplete"
                records.append(
                    {
                        "provider": PROPLINE_PROVIDER_NAME,
                        "event_id": _clean(payload.get("id")),
                        "sport_key": _clean(payload.get("sport_key")),
                        "event_status": _clean(payload.get("status")),
                        "commence_time": payload.get("commence_time"),
                        "bookmaker_key": bookmaker_key,
                        "bookmaker_title": _clean(bookmaker_record.get("title")),
                        "market_key": market_key,
                        "market_description": _clean(market.get("description")),
                        "player_name": _clean(outcome.get("description")),
                        "player_id": _clean(outcome.get("player_id")),
                        "direction": _clean(outcome.get("name")),
                        "line": outcome.get("point"),
                        "price": outcome.get("price"),
                        "resolution": outcome.get("resolution"),
                        "actual_value": outcome.get("actual_value"),
                        "resolved_at": outcome.get("resolved_at"),
                        "redacted": bool(outcome.get("redacted", False)),
                        "dfs_odds_type": _clean(outcome.get("dfs_odds_type")),
                        "supplemental_source": "PropLine",
                    }
                )
    return records, True, ""


_PLAYER_HISTORY_REQUIRED_FIELDS = {
    "event_id", "commence_time", "home_team", "away_team", "bookmaker",
    "bookmaker_title", "line", "over_price", "under_price", "actual_value",
    "over_result", "under_result", "resolved_at",
}
_PLAYER_TRENDS_REQUIRED_FIELDS = {
    "market", "games_graded", "reference_bookmaker",
    "reference_bookmaker_title", "recent_line", "avg_actual", "last_5",
    "last_10", "last_20", "last_50", "current_streak", "last_game",
}


def _official_player_history_records(
    payload: Any,
    *,
    requested_player: str,
    requested_sport: str,
    requested_market: str,
) -> tuple[list[dict[str, Any]], bool, str, dict[str, Any]]:
    """Validate the documented PlayerHistoryOut response."""
    if not isinstance(payload, dict):
        return [], False, "PropLine player history response is not an object", {}
    if not {"player_name", "sport_key", "market", "entries"}.issubset(payload):
        return [], False, "PropLine player history response is incomplete", {}
    player_name = _clean(payload["player_name"])
    sport_key = _clean(payload["sport_key"])
    market = _clean(payload["market"])
    if (
        not player_name
        or not sport_key
        or not market
        or not _safe_player_name_match(player_name, requested_player)
        or not _sports_match(sport_key, requested_sport)
        or _normalize_stat(market) != _normalize_stat(requested_market)
    ):
        return [], False, "PropLine player history identity does not match request", {}
    entries = payload["entries"]
    if not isinstance(entries, list):
        return [], False, "PropLine player history entries are incomplete", {}
    records: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not _PLAYER_HISTORY_REQUIRED_FIELDS.issubset(entry):
            return [], False, "PropLine player history entry is incomplete", {}
        records.append({
            **entry,
            "player_name": player_name,
            "sport_key": sport_key,
            "market": market,
            "supplemental_source": "PropLine",
        })
    return records, True, "", {
        "player_name": player_name,
        "sport_key": sport_key,
        "market": market,
        "redacted": bool(payload.get("redacted", False)),
        "upgrade_url": _clean(payload.get("upgrade_url")),
    }


def _official_player_trends_payload(
    payload: Any,
    *,
    requested_player: str,
    requested_sport: str,
    requested_dfs_odds_type: str,
) -> tuple[dict[str, Any], bool, str]:
    """Validate the documented PlayerTrendsOut response."""
    if not isinstance(payload, dict):
        return {}, False, "PropLine player trends response is not an object"
    if not {"player_name", "sport_key", "markets"}.issubset(payload):
        return {}, False, "PropLine player trends response is incomplete"
    player_name = _clean(payload["player_name"])
    sport_key = _clean(payload["sport_key"])
    if (
        not player_name
        or not sport_key
        or not _safe_player_name_match(player_name, requested_player)
        or _normalize_sport(sport_key) != _normalize_sport(requested_sport)
    ):
        return {}, False, "PropLine player trends identity does not match request"
    returned_flavor = _clean(payload.get("dfs_odds_type")).lower()
    if requested_dfs_odds_type and returned_flavor != requested_dfs_odds_type:
        return {}, False, "PropLine player trends flavor does not match request"
    markets = payload["markets"]
    if not isinstance(markets, list):
        return {}, False, "PropLine player trends markets are incomplete"
    if any(
        not isinstance(market, dict)
        or not _PLAYER_TRENDS_REQUIRED_FIELDS.issubset(market)
        for market in markets
    ):
        return {}, False, "PropLine player trend is incomplete"
    return {
        "player_name": player_name,
        "sport_key": sport_key,
        "dfs_odds_type": returned_flavor or None,
        "markets": markets,
        "redacted": bool(payload.get("redacted", False)),
        "upgrade_url": _clean(payload.get("upgrade_url")),
    }, True, ""


class PropLineClient:
    """Small, cached, rate-limited HTTP client for the authorized feed."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        session: Any = None,
        min_request_interval: float = PROPLINE_MIN_REQUEST_INTERVAL,
        cache_ttl: float = 240.0,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        request_budget: Optional[PropLineRequestBudget] = None,
        request_limiter: Optional[PropLineRequestLimiter] = None,
        endpoint_request_budget: Optional[PropLineRequestBudget] = None,
        endpoint_request_limiter: Optional[PropLineRequestLimiter] = None,
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
        self._sports_discovery_failed = False
        self.request_budget = request_budget or _DEFAULT_PROPLINE_REQUEST_BUDGET
        self.endpoint_request_budget = (
            endpoint_request_budget
            or _DEFAULT_PROPLINE_ENDPOINT_REQUEST_BUDGET
        )
        self._request_day = datetime.utcnow().date()
        self._request_count = 0
        self._last_success_by_scope: dict[str, PropLineFetchResult] = {}
        self._snapshot_state_by_scope: dict[str, PropLineSnapshotState] = {}
        self._events_cache_by_sport: dict[str, tuple[PropLineEvent, ...]] = {}
        self._events_cache_clock_by_sport: dict[str, float] = {}
        self._player_history_cache: dict[str, PropLinePlayerHistoryFetchResult] = {}
        self._player_history_cache_clock: dict[str, float] = {}
        self._player_trends_cache: dict[str, PropLinePlayerTrendsFetchResult] = {}
        self._player_trends_cache_clock: dict[str, float] = {}
        self._endpoint_cache: dict[str, PropLineEndpointResult] = {}
        self._endpoint_cache_clock: dict[str, float] = {}
        self.request_limiter = request_limiter or _DEFAULT_PROPLINE_REQUEST_LIMITER
        self.request_limiter.configure_min_interval(self.min_request_interval)
        self.endpoint_request_limiter = (
            endpoint_request_limiter
            or _DEFAULT_PROPLINE_ENDPOINT_REQUEST_LIMITER
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_url)

    @property
    def sports_discovery_failed(self) -> bool:
        """Whether the most recent catalog request failed or was incomplete."""
        return self._sports_discovery_failed

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

    @property
    def request_phase(self) -> str:
        return self.request_limiter.phase

    @property
    def request_rate_per_hour(self) -> int:
        return self.request_limiter.effective_limit

    @property
    def backoff_remaining(self) -> float:
        return max(
            self.request_limiter.backoff_remaining,
            self.endpoint_request_limiter.backoff_remaining,
            max(0.0, self._backoff_until - self._clock()),
        )

    @property
    def request_stats(self) -> dict[str, int]:
        return {
            "attempted": self.request_limiter.attempted_count,
            "successful": self.request_limiter.successful_count,
            "rate_limited": self.request_limiter.rate_limited_count,
        }

    @daily_request_limit.setter
    def daily_request_limit(self, value: int) -> None:
        self.request_budget.limit = max(1, int(value))

    def _reserve_request(self, *, endpoint: bool = False) -> bool:
        self._reset_daily_budget_if_needed()
        if endpoint and self.endpoint_request_budget.remaining <= 0:
            return False
        if self.request_budget.remaining <= 0:
            return False
        if endpoint and not self.endpoint_request_budget.reserve():
            return False
        if not self.request_budget.reserve():
            return False
        self._request_count += 1
        return True

    async def _request_json(
        self,
        url: str,
        params: dict[str, str],
        *,
        method: str = "GET",
        json_body: Any = None,
    ) -> Any:
        """Perform one authorized request and account for it centrally."""
        if not self.api_key:
            raise PropLineError("PropLine API key is missing")
        request_kind = _propline_request_kind(url)
        is_endpoint_request = request_kind != "bulk"
        request_limiter = (
            self.endpoint_request_limiter
            if is_endpoint_request
            else self.request_limiter
        )
        now = self._clock()
        shared_backoff = self.backoff_remaining
        if now < self._backoff_until or shared_backoff > 0:
            retry_after = max(
                max(1.0, self._backoff_until - now),
                shared_backoff,
            )
            raise PropLineRateLimitError(
                "PropLine rate-limit backoff active",
                retry_after=retry_after,
            )
        if self.request_budget.remaining <= 0:
            raise PropLineError("PropLine daily request budget exhausted")
        if is_endpoint_request and self.endpoint_request_budget.remaining <= 0:
            raise PropLineError("PropLine endpoint daily request budget exhausted")
        await request_limiter.acquire()
        # Another client can receive a provider 429 while this request waits
        # for its rolling-hour slot. Never send during that shared cooldown.
        shared_backoff = self.backoff_remaining
        if shared_backoff > 0:
            raise PropLineRateLimitError(
                "PropLine rate-limit backoff active",
                retry_after=shared_backoff,
            )
        # Reserve immediately before network I/O. Waiting for a local rolling
        # slot or a shared cooldown must not consume the daily budget.
        if not self._reserve_request(endpoint=is_endpoint_request):
            raise PropLineError("PropLine daily request budget exhausted")
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
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            request_method = getattr(session, method.lower(), None)
            if request_method is None:
                raise PropLineError(f"PropLine HTTP method is unsupported: {method}")
            request_kwargs = {
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
            if json_body is not None:
                request_kwargs["json"] = json_body
            request_limiter.record_attempt()
            async with request_method(url, **request_kwargs) as response:
                request_limiter.observe_provider_limit(response.headers)
                if response.status == 404:
                    raise PropLineNotFoundError(f"PropLine resource not found (HTTP {response.status})")
                if response.status == 429:
                    retry_after = _retry_after_seconds(response)
                    self._backoff_until = self._clock() + retry_after
                    self.request_limiter.defer(retry_after)
                    self.endpoint_request_limiter.defer(retry_after)
                    stats = {
                        "attempted": request_limiter.attempted_count,
                        "successful": request_limiter.successful_count,
                        "rate_limited": request_limiter.rate_limited_count,
                    }
                    logger.warning(
                        "PropLine 429: phase=%s rate=%d/hour kind=%s "
                        "attempted=%d successful=%d 429s=%d Retry-After=%.0fs",
                        request_limiter.phase,
                        request_limiter.effective_limit,
                        request_kind,
                        stats["attempted"],
                        stats["successful"],
                        stats["rate_limited"],
                        retry_after,
                    )
                    raise PropLineRateLimitError(
                        f"PropLine rate limited (HTTP {response.status})",
                        retry_after=retry_after,
                    )
                if response.status in {408, 425, 502, 503, 504}:
                    retry_after = min(
                        900.0,
                        max(30.0, 2.0 * request_limiter.min_interval),
                    )
                    self._backoff_until = self._clock() + retry_after
                    self.request_limiter.defer(retry_after)
                    self.endpoint_request_limiter.defer(retry_after)
                    raise PropLineError(
                        f"PropLine temporarily unavailable (HTTP {response.status})"
                    )
                if response.status >= 400:
                    raise PropLineError(f"PropLine request failed (HTTP {response.status})")
                payload = await response.json(content_type=None)
                request_limiter.record_success()
                stats = {
                    "attempted": request_limiter.attempted_count,
                    "successful": request_limiter.successful_count,
                    "rate_limited": request_limiter.rate_limited_count,
                }
                logger.info(
                    "PropLine request success: phase=%s rate=%d/hour kind=%s "
                    "attempted=%d successful=%d 429s=%d backoff=idle",
                    request_limiter.phase,
                    request_limiter.effective_limit,
                    request_kind,
                    stats["attempted"],
                    stats["successful"],
                    stats["rate_limited"],
                )
                return payload
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
                    "markets": ",".join(_OFFICIAL_ODDS_MARKETS),
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

    def _official_sport_odds_request(
        self,
        sport_key: str,
        *,
        markets: Any = "",
        bookmakers: Any = "",
        period: str = "",
        include_links: bool = False,
        include_book_ids: bool = False,
    ) -> tuple[str, dict[str, str]]:
        official_sport = _clean(sport_key)
        if not official_sport:
            raise PropLineError("PropLine sport key is required for sport odds")
        params: dict[str, str] = {}
        for name, value in (
            ("markets", markets),
            ("bookmakers", bookmakers),
            ("period", period),
        ):
            cleaned = self._csv_param(value)
            if cleaned:
                params[name] = cleaned
        if include_links:
            params["includeLinks"] = "true"
        if include_book_ids:
            params["includeBookIds"] = "true"
        return (
            f"{self.api_url.rstrip('/')}/v1/sports/"
            f"{quote(official_sport, safe='')}/odds",
            params,
        )

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
                "markets": ",".join(_OFFICIAL_ODDS_MARKETS),
                "bookmakers": bookmaker,
            },
            f"{official_sport}:event:{official_event}:{bookmaker.lower()}",
        )

    def _official_event_results_request(
        self,
        sport_key: str,
        event_id: str,
        *,
        markets: str = "",
    ) -> tuple[str, dict[str, str]]:
        official_sport = _clean(sport_key)
        official_event = _clean(event_id)
        if not official_sport or not official_event:
            raise PropLineError(
                "PropLine sport and event keys are required for event results"
            )
        params: dict[str, str] = {}
        if _clean(markets):
            params["markets"] = _clean(markets)
        return (
            f"{self.api_url.rstrip('/')}/v1/sports/{quote(official_sport, safe='')}"
            f"/events/{quote(official_event, safe='')}/results",
            params,
        )

    def _official_read_path(self, *parts: str) -> str:
        """Build a path only for the documented official PropLine host."""
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            raise PropLineError(
                "This PropLine capability requires the official endpoint"
            )
        if not parts or any(not _clean(part) for part in parts):
            raise PropLineError("PropLine endpoint path is incomplete")
        return f"{self.api_url.rstrip('/')}/v1/" + "/".join(
            quote(_clean(part), safe="") for part in parts
        )

    @staticmethod
    def _csv_param(value: Any) -> str:
        if isinstance(value, (list, tuple, set, frozenset)):
            return ",".join(_clean(item) for item in value if _clean(item))
        return _clean(value)

    @staticmethod
    def _event_id(event: Any) -> str:
        if isinstance(event, PropLineEvent):
            return event.event_id
        if isinstance(event, dict):
            return _clean(_first(event, "id", "event_id", "game_id", "match_id"))
        return _clean(event)

    async def _fetch_read_only(
        self,
        endpoint: str,
        params: Optional[dict[str, str]] = None,
        *,
        cache_scope: str = "",
    ) -> PropLineEndpointResult:
        """Fetch one documented GET endpoint with common safety guarantees."""
        fetched_at = datetime.utcnow()
        scope = cache_scope or endpoint
        if not self.is_configured:
            return PropLineEndpointResult(
                None, fetched_at, endpoint=endpoint,
                error="PropLine is not configured", failure_kind="disabled",
            )
        async with self._lock:
            cached = self._endpoint_cache.get(scope)
            cached_at = self._endpoint_cache_clock.get(scope)
            if (
                cached is not None
                and cached_at is not None
                and self.cache_ttl > 0
                and self._clock() - cached_at < self.cache_ttl
            ):
                return replace(cached, from_cache=True)
            try:
                payload = await self._request_json(endpoint, params or {})
            except PropLineNotFoundError as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=str(exc), failure_kind="not_found",
                )
            except PropLineRateLimitError as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=str(exc), failure_kind="quota",
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=f"PropLine request failed: {type(exc).__name__}",
                    failure_kind="http",
                )
            except PropLineError as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=str(exc),
                    failure_kind="quota" if "budget" in str(exc).lower() else "http",
                )
            redacted = bool(payload.get("redacted", False)) if isinstance(payload, dict) else False
            upgrade_url = _clean(payload.get("upgrade_url")) if isinstance(payload, dict) else ""
            result = PropLineEndpointResult(
                payload, fetched_at, endpoint=endpoint,
                succeeded=True, complete=True, redacted=redacted,
                upgrade_url=upgrade_url,
            )
            self._endpoint_cache[scope] = result
            self._endpoint_cache_clock[scope] = self._clock()
            return result

    async def _post_endpoint(
        self,
        endpoint: str,
        body: dict[str, Any],
    ) -> PropLineEndpointResult:
        """POST one documented Hobby action without entering live polling."""
        fetched_at = datetime.utcnow()
        if not self.is_configured:
            return PropLineEndpointResult(
                None, fetched_at, endpoint=endpoint,
                error="PropLine is not configured", failure_kind="disabled",
            )
        async with self._lock:
            try:
                payload = await self._request_json(
                    endpoint, {}, method="POST", json_body=body
                )
            except PropLineNotFoundError as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=str(exc), failure_kind="not_found",
                )
            except PropLineRateLimitError as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=str(exc), failure_kind="quota",
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=f"PropLine request failed: {type(exc).__name__}",
                    failure_kind="http",
                )
            except PropLineError as exc:
                return PropLineEndpointResult(
                    None, fetched_at, endpoint=endpoint,
                    error=str(exc),
                    failure_kind="quota" if "budget" in str(exc).lower() else "http",
                )
            redacted = bool(payload.get("redacted", False)) if isinstance(payload, dict) else False
            upgrade_url = _clean(payload.get("upgrade_url")) if isinstance(payload, dict) else ""
            return PropLineEndpointResult(
                payload, fetched_at, endpoint=endpoint,
                succeeded=True, complete=True, redacted=redacted,
                upgrade_url=upgrade_url,
            )

    async def fetch_sports(self) -> PropLineEndpointResult:
        """Fetch the canonical PropLine sport catalog."""
        try:
            endpoint = self._official_read_path("sports")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(endpoint, {}, cache_scope="sports")

    async def fetch_events(
        self, sport_key: str, *, refresh: bool = False
    ) -> PropLineEndpointResult:
        """Fetch the raw event catalog; event ids remain the join key."""
        try:
            endpoint = self._official_read_path("sports", sport_key, "events")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        if refresh:
            self._endpoint_cache.pop(f"events:{_clean(sport_key).lower()}", None)
            self._endpoint_cache_clock.pop(f"events:{_clean(sport_key).lower()}", None)
        return await self._fetch_read_only(
            endpoint, {}, cache_scope=f"events:{_clean(sport_key).lower()}"
        )

    async def fetch_sport_odds(
        self,
        sport_key: str,
        *,
        markets: Any = "",
        bookmakers: Any = "",
        period: str = "",
        include_links: bool = False,
        include_book_ids: bool = False,
    ) -> PropLineEndpointResult:
        """Fetch bulk game lines for a sport, including optional player props."""
        try:
            endpoint, params = self._official_sport_odds_request(
                sport_key,
                markets=markets,
                bookmakers=bookmakers,
                period=period,
                include_links=include_links,
                include_book_ids=include_book_ids,
            )
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(
            endpoint, params,
            cache_scope="sport-odds:" + json.dumps(
                [endpoint, sorted(params.items())], separators=(",", ":")
            ),
        )

    async def fetch_grand_salami(
        self, *, date: str = ""
    ) -> PropLineEndpointResult:
        """Fetch the optional MLB all-games runs comparison."""
        try:
            endpoint = self._official_read_path(
                "sports", "baseball_mlb", "grand-salami"
            )
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        params = {"date": _clean(date)} if _clean(date) else {}
        return await self._fetch_read_only(
            endpoint, params,
            cache_scope="grand-salami:" + json.dumps(sorted(params.items())),
        )

    async def fetch_daily_goals_total(
        self, *, date: str = ""
    ) -> PropLineEndpointResult:
        """Fetch the optional NHL daily goals comparison."""
        try:
            endpoint = self._official_read_path(
                "sports", "hockey_nhl", "daily-goals-total"
            )
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        params = {"date": _clean(date)} if _clean(date) else {}
        return await self._fetch_read_only(
            endpoint, params,
            cache_scope="daily-goals-total:" + json.dumps(sorted(params.items())),
        )

    async def fetch_exports_sample(self) -> PropLineEndpointResult:
        """Fetch PropLine's small public export-shape sample."""
        try:
            endpoint = self._official_read_path("exports", "sample")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(endpoint, {}, cache_scope="exports-sample")

    async def fetch_sgp(
        self,
        sport_key: str,
        event: Any,
        *,
        bookmaker: str,
        legs: list[dict[str, Any]],
    ) -> PropLineEndpointResult:
        """Request one book-native same-game parlay quote.

        SGP is an explicit caller-driven analysis request. It is never part of
        the live polling or pick qualification path.
        """
        event_id = self._event_id(event)
        if not _clean(bookmaker):
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine SGP bookmaker is required",
                failure_kind="configuration",
            )
        if not isinstance(legs, list) or not 2 <= len(legs) <= 10:
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine SGP requires between 2 and 10 legs",
                failure_kind="configuration",
            )
        try:
            endpoint = self._official_read_path(
                "sports", sport_key, "events", event_id, "sgp"
            )
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._post_endpoint(
            endpoint,
            {"bookmaker": _clean(bookmaker).lower(), "legs": legs},
        )

    async def grade_clv(
        self, bets: list[dict[str, Any]]
    ) -> PropLineEndpointResult:
        """Batch-grade already placed bets against the captured close."""
        if not isinstance(bets, list) or not 1 <= len(bets) <= 500:
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine CLV grading requires between 1 and 500 bets",
                failure_kind="configuration",
            )
        try:
            endpoint = self._official_read_path("clv", "grade")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._post_endpoint(endpoint, {"bets": bets})

    async def fetch_player_games(
        self,
        sport_key: str,
        player_name: str,
        *,
        limit: int = 20,
        opponent: str = "",
        stat_type: str = "",
    ) -> PropLineEndpointResult:
        """Fetch raw, book-agnostic player game logs."""
        try:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise PropLineError("PropLine player games limit must be between 1 and 100")
            sport = _clean(sport_key)
            player = _clean(player_name)
            if not sport or not player:
                raise PropLineError("PropLine sport and player are required for player games")
            params = {"limit": str(limit)}
            if _clean(opponent):
                params["opponent"] = _clean(opponent)
            if _clean(stat_type):
                params["stat_type"] = self._csv_param(stat_type)
            endpoint = self._official_read_path(
                "sports", sport, "players", player, "games"
            )
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        scope = "games:" + json.dumps([endpoint, sorted(params.items())], separators=(",", ":"))
        return await self._fetch_read_only(endpoint, params, cache_scope=scope)

    async def fetch_event_stats(
        self, sport_key: str, event: Any, *, stat_type: str = ""
    ) -> PropLineEndpointResult:
        """Fetch raw player/team box-score stats for one event."""
        return await self._fetch_event_endpoint(
            sport_key, event, "stats",
            {"stat_type": self._csv_param(stat_type)} if _clean(stat_type) else {},
        )

    async def fetch_event_context(
        self, sport_key: str, event: Any
    ) -> PropLineEndpointResult:
        """Fetch venue, lineup, pitcher, and weather context."""
        return await self._fetch_event_endpoint(sport_key, event, "context")

    async def fetch_event_markets(
        self, sport_key: str, event: Any, *, period: str = ""
    ) -> PropLineEndpointResult:
        """Discover the markets available on one event before pulling odds."""
        params = {"period": _clean(period)} if _clean(period) else {}
        return await self._fetch_event_endpoint(sport_key, event, "markets", params)

    async def fetch_scores(
        self, sport_key: str, *, days_from: int = 3
    ) -> PropLineEndpointResult:
        """Fetch recent scores and game status for a sport."""
        if isinstance(days_from, bool) or not isinstance(days_from, int) or not 0 <= days_from <= 30:
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine days_from must be between 0 and 30",
                failure_kind="configuration",
            )
        try:
            endpoint = self._official_read_path("sports", _clean(sport_key), "scores")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(
            endpoint, {"days_from": str(days_from)},
            cache_scope=f"scores:{_clean(sport_key).lower()}:{days_from}",
        )

    async def fetch_futures(
        self, sport_key: str, *, bookmakers: Any = ""
    ) -> PropLineEndpointResult:
        """Fetch season-long futures markets without entering prop scoring."""
        try:
            endpoint = self._official_read_path("sports", _clean(sport_key), "futures")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        params = {}
        if self._csv_param(bookmakers):
            params["bookmakers"] = self._csv_param(bookmakers)
        return await self._fetch_read_only(
            endpoint, params,
            cache_scope="futures:" + json.dumps([endpoint, sorted(params.items())]),
        )

    async def fetch_odds_history(
        self,
        sport_key: str,
        event: Any,
        *,
        markets: Any = "",
        from_time: str = "",
        to_time: str = "",
        relative_from: str = "",
        relative_to: str = "",
        interval: str = "",
        changes_only: Optional[bool] = None,
        period: str = "",
    ) -> PropLineEndpointResult:
        """Fetch historical odds ticks; never interpret movement as a pick."""
        params = self._history_params(
            markets=markets, from_time=from_time, to_time=to_time,
            relative_from=relative_from, relative_to=relative_to,
            interval=interval, changes_only=changes_only, period=period,
        )
        return await self._fetch_event_endpoint(
            sport_key, event, "odds/history", params
        )

    async def fetch_closing_lines(
        self, sport_key: str, event: Any, *, markets: Any = ""
    ) -> PropLineEndpointResult:
        """Fetch provider-defined opening and closing line evidence."""
        params = {"markets": self._csv_param(markets)} if self._csv_param(markets) else {}
        return await self._fetch_event_endpoint(
            sport_key, event, "odds/closing", params
        )

    async def fetch_movement(
        self, sport_key: str, event: Any, *, markets: Any = "", period: str = ""
    ) -> PropLineEndpointResult:
        """Fetch read-only line movement and cross-book steam context."""
        params = {}
        if self._csv_param(markets):
            params["markets"] = self._csv_param(markets)
        if _clean(period):
            params["period"] = _clean(period)
        return await self._fetch_event_endpoint(sport_key, event, "movement", params)

    async def fetch_best_line(
        self, sport_key: str, event: Any, *, markets: Any = "", bookmakers: Any = ""
    ) -> PropLineEndpointResult:
        """Fetch best comparable sportsbook prices for a documented event."""
        params = {}
        if self._csv_param(markets):
            params["markets"] = self._csv_param(markets)
        if self._csv_param(bookmakers):
            params["bookmakers"] = self._csv_param(bookmakers)
        return await self._fetch_event_endpoint(sport_key, event, "best-line", params)

    async def fetch_ev(
        self, sport_key: str, event: Any, *, markets: Any = "", bookmakers: Any = ""
    ) -> PropLineEndpointResult:
        """Fetch cross-book no-vig +EV calculations as informational data."""
        params = {}
        if self._csv_param(markets):
            params["markets"] = self._csv_param(markets)
        if self._csv_param(bookmakers):
            params["bookmakers"] = self._csv_param(bookmakers)
        return await self._fetch_event_endpoint(sport_key, event, "ev", params)

    async def calculate_ev(
        self,
        sport_key: str,
        event: Any,
        *,
        market: str,
        name: str,
        price: int,
        point: Any = None,
        description: str = "",
    ) -> PropLineEndpointResult:
        """Score a caller-supplied price against PropLine's fair line."""
        if isinstance(price, bool) or not isinstance(price, int):
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine EV price must be an integer American price",
                failure_kind="configuration",
            )
        params = {"market": _clean(market), "name": _clean(name), "price": str(price)}
        if point is not None:
            params["point"] = str(point)
        if _clean(description):
            params["description"] = _clean(description)
        return await self._fetch_event_endpoint(sport_key, event, "ev/calc", params)

    async def fetch_projections(
        self, sport_key: str, event: Any, *, markets: Any = ""
    ) -> PropLineEndpointResult:
        """Fetch market-implied values as validation context, not forecasts."""
        params = {"markets": self._csv_param(markets)} if self._csv_param(markets) else {}
        return await self._fetch_event_endpoint(
            sport_key, event, "projections", params
        )

    async def fetch_dfs_payouts(
        self, *, platform: str = "prizepicks", leg_win_prob: Any = None
    ) -> PropLineEndpointResult:
        """Fetch published DFS payout and breakeven reference math."""
        params = {}
        if _clean(platform):
            params["platform"] = _clean(platform).lower()
        if leg_win_prob is not None:
            try:
                probability = float(leg_win_prob)
            except (TypeError, ValueError):
                return PropLineEndpointResult(
                    None, datetime.utcnow(),
                    error="PropLine leg_win_prob must be numeric",
                    failure_kind="configuration",
                )
            if not 0.0 <= probability <= 1.0:
                return PropLineEndpointResult(
                    None, datetime.utcnow(),
                    error="PropLine leg_win_prob must be between 0 and 1",
                    failure_kind="configuration",
                )
            params["leg_win_prob"] = str(leg_win_prob)
        try:
            endpoint = self._official_read_path("dfs", "payouts")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(
            endpoint, params,
            cache_scope="dfs-payouts:" + json.dumps(sorted(params.items())),
        )

    async def fetch_market_hit_rates(
        self, *, days: int = 28, bookmaker: str = "bovada"
    ) -> PropLineEndpointResult:
        """Fetch aggregate market hit-rate calibration counts."""
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 60:
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine hit-rate days must be between 1 and 60",
                failure_kind="configuration",
            )
        try:
            endpoint = self._official_read_path("markets", "hit-rates")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        params = {"days": str(days)}
        if _clean(bookmaker):
            params["bookmaker"] = _clean(bookmaker).lower()
        return await self._fetch_read_only(
            endpoint, params,
            cache_scope="hit-rates:" + json.dumps(sorted(params.items())),
        )

    async def fetch_resolution_summary(self, *, days: int = 30) -> PropLineEndpointResult:
        """Fetch aggregate resolution coverage, with no individual pick claims."""
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine resolution-summary days must be between 1 and 90",
                failure_kind="configuration",
            )
        try:
            endpoint = self._official_read_path("markets", "resolution-summary")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(
            endpoint, {"days": str(days)},
            cache_scope=f"resolution-summary:{days}",
        )

    async def fetch_freshness(self) -> PropLineEndpointResult:
        """Fetch per-book game-line and prop freshness."""
        try:
            endpoint = self._official_read_path("freshness")
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        return await self._fetch_read_only(endpoint, {}, cache_scope="freshness")

    # Explicit event-prefixed aliases keep the public client vocabulary close
    # to the documentation and make capability discovery straightforward.
    async def fetch_event_odds_history(self, sport_key: str, event: Any, **kwargs):
        return await self.fetch_odds_history(sport_key, event, **kwargs)

    async def fetch_event_closing_lines(self, sport_key: str, event: Any, **kwargs):
        return await self.fetch_closing_lines(sport_key, event, **kwargs)

    async def fetch_event_movement(self, sport_key: str, event: Any, **kwargs):
        return await self.fetch_movement(sport_key, event, **kwargs)

    async def fetch_event_best_line(self, sport_key: str, event: Any, **kwargs):
        return await self.fetch_best_line(sport_key, event, **kwargs)

    async def fetch_event_ev(self, sport_key: str, event: Any, **kwargs):
        return await self.fetch_ev(sport_key, event, **kwargs)

    async def fetch_event_projections(self, sport_key: str, event: Any, **kwargs):
        return await self.fetch_projections(sport_key, event, **kwargs)

    async def fetch_player_game_log(self, sport_key: str, player_name: str, **kwargs):
        return await self.fetch_player_games(sport_key, player_name, **kwargs)

    async def _fetch_event_endpoint(
        self,
        sport_key: str,
        event: Any,
        suffix: str,
        params: Optional[dict[str, str]] = None,
    ) -> PropLineEndpointResult:
        sport = _clean(sport_key)
        event_id = self._event_id(event)
        if not sport or not event_id:
            return PropLineEndpointResult(
                None, datetime.utcnow(),
                error="PropLine sport and event id are required",
                failure_kind="configuration",
            )
        try:
            endpoint = self._official_read_path(
                "sports", sport, "events", event_id, *suffix.split("/")
            )
        except PropLineError as exc:
            return PropLineEndpointResult(
                None, datetime.utcnow(), error=str(exc), failure_kind="configuration"
            )
        request_params = params or {}
        scope = "event:" + json.dumps(
            [endpoint, sorted(request_params.items())], separators=(",", ":")
        )
        return await self._fetch_read_only(endpoint, request_params, cache_scope=scope)

    @staticmethod
    def _history_params(
        *,
        markets: Any,
        from_time: str,
        to_time: str,
        relative_from: str,
        relative_to: str,
        interval: str,
        changes_only: Optional[bool],
        period: str,
    ) -> dict[str, str]:
        params = {}
        values = (
            ("markets", PropLineClient._csv_param(markets)),
            ("from", _clean(from_time)),
            ("to", _clean(to_time)),
            ("relative_from", _clean(relative_from)),
            ("relative_to", _clean(relative_to)),
            ("interval", _clean(interval)),
            ("period", _clean(period)),
        )
        for key, value in values:
            if value:
                params[key] = value
        if changes_only is not None:
            params["changes_only"] = "true" if changes_only else "false"
        return params

    def _official_player_history_request(
        self,
        sport_key: str,
        player_name: str,
        market: str,
        *,
        bookmaker: str = "",
        limit: int = 100,
    ) -> tuple[str, dict[str, str], str]:
        official_sport = _clean(sport_key)
        official_player = _clean(player_name)
        requested_market = _clean(market)
        if not official_sport or not official_player or not requested_market:
            raise PropLineError(
                "PropLine sport, player, and market are required for player history"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise PropLineError("PropLine player history limit must be between 1 and 100")
        params = {"market": requested_market, "limit": str(limit)}
        if _clean(bookmaker):
            params["bookmaker"] = _clean(bookmaker)
        return (
            f"{self.api_url.rstrip('/')}/v1/sports/"
            f"{quote(official_sport, safe='')}/players/"
            f"{quote(official_player, safe='')}/history",
            params,
            f"{official_sport}:player:{_identity_part(official_player)}:history:"
            f"{_identity_part(requested_market)}:{_identity_part(bookmaker)}:{limit}",
        )

    def _official_player_trends_request(
        self,
        sport_key: str,
        player_name: str,
        *,
        market: str = "",
        dfs_odds_type: str = "",
    ) -> tuple[str, dict[str, str], str]:
        official_sport = _clean(sport_key)
        official_player = _clean(player_name)
        if not official_sport or not official_player:
            raise PropLineError("PropLine sport and player are required for player trends")
        flavor = _clean(dfs_odds_type).lower()
        if flavor and flavor not in {"standard", "goblin", "demon"}:
            raise PropLineError(
                "PropLine DFS odds type must be standard, goblin, or demon"
            )
        params: dict[str, str] = {}
        if _clean(market):
            params["market"] = _clean(market)
        if flavor:
            params["dfs_odds_type"] = flavor
        return (
            f"{self.api_url.rstrip('/')}/v1/sports/{quote(official_sport, safe='')}"
            f"/players/{quote(official_player, safe='')}/trends",
            params,
            f"{official_sport}:player:{_identity_part(official_player)}:trends:"
            f"{_identity_part(market)}:{flavor}",
        )

    async def fetch_player_history(
        self,
        sport_key: str,
        player_name: str,
        market: str,
        *,
        bookmaker: str = "",
        limit: int = 20,
    ) -> PropLinePlayerHistoryFetchResult:
        """Fetch official, read-only player history from PropLine.

        This is intentionally limited to the documented official host.  The
        caller may use the returned entries as supplemental evidence, but this
        method never turns them into a prop line or a provider identity.
        """
        fetched_at = datetime.utcnow()
        if not self.is_configured:
            return PropLinePlayerHistoryFetchResult(
                (), fetched_at, player_name=_clean(player_name),
                sport_key=_clean(sport_key), market=_clean(market),
                error="PropLine is not configured", failure_kind="disabled",
            )
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return PropLinePlayerHistoryFetchResult(
                (), fetched_at, player_name=_clean(player_name),
                sport_key=_clean(sport_key), market=_clean(market),
                error="PropLine player history requires the official endpoint",
                failure_kind="configuration",
            )

        try:
            url, params, scope = self._official_player_history_request(
                sport_key,
                player_name,
                market,
                bookmaker=bookmaker,
                limit=limit,
            )
        except PropLineError as exc:
            return PropLinePlayerHistoryFetchResult(
                (), fetched_at, player_name=_clean(player_name),
                sport_key=_clean(sport_key), market=_clean(market),
                error=str(exc), failure_kind="configuration",
            )

        async with self._lock:
            cached = self._player_history_cache.get(scope)
            cached_at = self._player_history_cache_clock.get(scope)
            if (
                cached is not None
                and cached_at is not None
                and self.cache_ttl > 0
                and self._clock() - cached_at < self.cache_ttl
            ):
                return replace(cached, from_cache=True)
            try:
                payload = await self._request_json(url, params)
            except PropLineNotFoundError as exc:
                return PropLinePlayerHistoryFetchResult(
                    (), fetched_at, player_name=_clean(player_name),
                    sport_key=_clean(sport_key), market=_clean(market),
                    error=str(exc), failure_kind="not_found",
                )
            except PropLineRateLimitError as exc:
                return PropLinePlayerHistoryFetchResult(
                    (), fetched_at, player_name=_clean(player_name),
                    sport_key=_clean(sport_key), market=_clean(market),
                    error=str(exc), failure_kind="quota",
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return PropLinePlayerHistoryFetchResult(
                    (), fetched_at, player_name=_clean(player_name),
                    sport_key=_clean(sport_key), market=_clean(market),
                    error=f"PropLine player history request failed: {type(exc).__name__}",
                    failure_kind="http",
                )
            except PropLineError as exc:
                return PropLinePlayerHistoryFetchResult(
                    (), fetched_at, player_name=_clean(player_name),
                    sport_key=_clean(sport_key), market=_clean(market),
                    error=str(exc),
                    failure_kind="quota" if "budget" in str(exc).lower() else "http",
                )

        raw_records, complete, error, metadata = _official_player_history_records(
            payload,
            requested_player=player_name,
            requested_sport=sport_key,
            requested_market=market,
        )
        if not complete:
            return PropLinePlayerHistoryFetchResult(
                (), fetched_at, player_name=_clean(player_name),
                sport_key=_clean(sport_key), market=_clean(market),
                error=error,
                failure_kind="incomplete",
                redacted=bool(payload.get("redacted", False))
                if isinstance(payload, dict) else False,
                upgrade_url=_clean(payload.get("upgrade_url"))
                if isinstance(payload, dict) else "",
            )
        entries: list[PropLinePlayerHistoryEntry] = []
        for record in raw_records:
            entry = build_propline_player_history_entry(
                record, observed_at=fetched_at
            )
            if entry is None:
                return PropLinePlayerHistoryFetchResult(
                    (), fetched_at, player_name=_clean(player_name),
                    sport_key=_clean(sport_key), market=_clean(market),
                    error="PropLine player history entry is invalid",
                    failure_kind="incomplete",
                )
            entries.append(entry)
        result = PropLinePlayerHistoryFetchResult(
            tuple(entries),
            fetched_at,
            player_name=metadata["player_name"],
            sport_key=metadata["sport_key"],
            market=metadata["market"],
            redacted=metadata["redacted"],
            upgrade_url=metadata["upgrade_url"],
            succeeded=True,
            complete=True,
        )
        self._player_history_cache[scope] = result
        self._player_history_cache_clock[scope] = self._clock()
        return result

    async def fetch_player_trends(
        self,
        sport_key: str,
        player_name: str,
        *,
        market: str = "",
        dfs_odds_type: str = "",
    ) -> PropLinePlayerTrendsFetchResult:
        """Fetch official, read-only player trends from PropLine."""
        fetched_at = datetime.utcnow()
        player = _clean(player_name)
        sport = _clean(sport_key)
        if not self.is_configured:
            return PropLinePlayerTrendsFetchResult(
                (), fetched_at, player_name=player, sport_key=sport,
                error="PropLine is not configured", failure_kind="disabled",
            )
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return PropLinePlayerTrendsFetchResult(
                (), fetched_at, player_name=player, sport_key=sport,
                error="PropLine player trends require the official endpoint",
                failure_kind="configuration",
            )
        try:
            url, params, scope = self._official_player_trends_request(
                sport, player, market=market, dfs_odds_type=dfs_odds_type
            )
        except PropLineError as exc:
            return PropLinePlayerTrendsFetchResult(
                (), fetched_at, player_name=player, sport_key=sport,
                error=str(exc), failure_kind="configuration",
            )
        async with self._lock:
            cached = self._player_trends_cache.get(scope)
            cached_at = self._player_trends_cache_clock.get(scope)
            if (
                cached is not None
                and cached_at is not None
                and self.cache_ttl > 0
                and self._clock() - cached_at < self.cache_ttl
            ):
                return replace(cached, from_cache=True)
            try:
                payload = await self._request_json(url, params)
            except PropLineNotFoundError as exc:
                return PropLinePlayerTrendsFetchResult(
                    (), fetched_at, player_name=player, sport_key=sport,
                    error=str(exc), failure_kind="not_found",
                )
            except PropLineRateLimitError as exc:
                return PropLinePlayerTrendsFetchResult(
                    (), fetched_at, player_name=player, sport_key=sport,
                    error=str(exc), failure_kind="quota",
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return PropLinePlayerTrendsFetchResult(
                    (), fetched_at, player_name=player, sport_key=sport,
                    error=f"PropLine player trends request failed: {type(exc).__name__}",
                    failure_kind="http",
                )
            except PropLineError as exc:
                return PropLinePlayerTrendsFetchResult(
                    (), fetched_at, player_name=player, sport_key=sport,
                    error=str(exc),
                    failure_kind="quota" if "budget" in str(exc).lower() else "http",
                )
        normalized, complete, error = _official_player_trends_payload(
            payload,
            requested_player=player,
            requested_sport=sport,
            requested_dfs_odds_type=_clean(dfs_odds_type).lower(),
        )
        if not complete:
            return PropLinePlayerTrendsFetchResult(
                (), fetched_at, player_name=player, sport_key=sport,
                error=error, failure_kind="incomplete",
            )
        try:
            markets = tuple(
                _build_propline_player_market_trend(
                    record, redacted=normalized["redacted"]
                )
                for record in normalized["markets"]
            )
        except ValueError as exc:
            return PropLinePlayerTrendsFetchResult(
                (), fetched_at, player_name=player, sport_key=sport,
                error=str(exc), failure_kind="incomplete",
            )
        result = PropLinePlayerTrendsFetchResult(
            markets,
            fetched_at,
            player_name=normalized["player_name"],
            sport_key=normalized["sport_key"],
            dfs_odds_type=normalized["dfs_odds_type"],
            redacted=normalized["redacted"],
            upgrade_url=normalized["upgrade_url"],
            succeeded=True,
            complete=True,
        )
        self._player_trends_cache[scope] = result
        self._player_trends_cache_clock[scope] = self._clock()
        return result

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

    async def fetch_event_results(
        self,
        sport_key: str,
        event: Any,
        *,
        markets: str = "",
        bookmaker: str = "prizepicks",
    ) -> PropLineResultsFetchResult:
        """Fetch official, informational PropLine result evidence.

        This is an explicit caller-driven request, not part of the live
        projection polling path.  Results are never inferred from event
        status, line state, or actual stats returned by another endpoint.
        """
        fetched_at = datetime.utcnow()
        if not self.is_configured:
            return PropLineResultsFetchResult(
                (), fetched_at, succeeded=False, complete=False,
                error="PropLine is not configured", failure_kind="disabled",
            )
        if self.api_url.rstrip("/") != DEFAULT_PROPLINE_API_URL:
            return PropLineResultsFetchResult(
                (), fetched_at, succeeded=False, complete=False,
                error="PropLine event results require the official endpoint",
                failure_kind="configuration",
            )
        if isinstance(event, PropLineEvent):
            event_id = event.event_id
        elif isinstance(event, dict):
            event_id = _clean(_first(event, "id", "event_id", "game_id", "match_id"))
        else:
            event_id = _clean(event)
        if not event_id:
            return PropLineResultsFetchResult(
                (), fetched_at, succeeded=False, complete=False,
                error="PropLine event id is required", failure_kind="configuration",
            )

        async with self._lock:
            try:
                url, params = self._official_event_results_request(
                    sport_key, event_id, markets=markets
                )
                payload = await self._request_json(url, params)
            except PropLineNotFoundError as exc:
                return PropLineResultsFetchResult(
                    (), fetched_at, event_id=event_id, error=str(exc),
                    failure_kind="not_found",
                )
            except PropLineRateLimitError as exc:
                return PropLineResultsFetchResult(
                    (), fetched_at, event_id=event_id, error=str(exc),
                    failure_kind="quota",
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return PropLineResultsFetchResult(
                    (), fetched_at, event_id=event_id,
                    error=f"PropLine results request failed: {type(exc).__name__}",
                    failure_kind="http",
                )
            except PropLineError as exc:
                return PropLineResultsFetchResult(
                    (), fetched_at, event_id=event_id, error=str(exc),
                    failure_kind="quota" if "budget" in str(exc).lower() else "http",
                )

            raw_records, complete, error = _official_result_records(
                payload, bookmaker=bookmaker
            )
            results = tuple(
                evidence
                for record in raw_records
                if (
                    evidence := build_propline_result_evidence(
                        record,
                        observed_at=fetched_at,
                        response_complete=complete,
                    )
                )
                is not None
            )
            if not complete:
                return PropLineResultsFetchResult(
                    results,
                    fetched_at,
                    event_id=event_id,
                    succeeded=False,
                    complete=False,
                    error=error or "PropLine results response incomplete",
                    failure_kind="incomplete",
                )
            return PropLineResultsFetchResult(
                results,
                fetched_at,
                event_id=event_id,
                succeeded=True,
                complete=True,
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
                self._sports_discovery_failed = True
                logger.warning("PropLine sports discovery failed: %s", type(exc).__name__)
                return []
            except PropLineError as exc:
                self._sports_discovery_failed = True
                logger.warning("PropLine sports discovery error: %s", exc)
                return []
            except Exception as exc:  # noqa: BLE001
                self._sports_discovery_failed = True
                logger.warning("PropLine sports discovery response error: %s", type(exc).__name__)
                return []

            if not complete:
                self._sports_discovery_failed = True
                logger.warning("PropLine sports discovery incomplete: %s", pagination_error)
                return list(self._sports_cache)
            sports = _official_sports_records(raw_sports)
            self._sports_discovery_failed = False
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
                    raw_record_count=cached.raw_record_count,
                    normalized_prop_count=cached.normalized_prop_count,
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
            snapshot_complete = complete and bool(props)
            logger.info(
                "PropLine PrizePicks snapshot: sport=%s raw_records=%d "
                "normalized_props=%d complete=%s",
                sport_key or "__custom__",
                len(raw_records),
                len(props),
                snapshot_complete,
            )
            if not complete:
                logger.warning("PropLine snapshot incomplete: %s", pagination_error)
                return self._failed_result(
                    scope,
                    pagination_error or "PropLine snapshot incomplete",
                    "incomplete",
                    partial_props=props,
                    raw_record_count=len(raw_records),
                    normalized_prop_count=len(props),
                )
            self._cache = PropLineFetchResult(
                props, fetched_at, from_cache=False, succeeded=True,
                complete=snapshot_complete,
                raw_record_count=len(raw_records),
                normalized_prop_count=len(props),
            )
            self._cache_clock_at = self._clock()
            self._cache_scope = scope
            self._cache_by_scope[scope] = self._cache
            self._cache_clock_by_scope[scope] = self._cache_clock_at
            result = PropLineFetchResult(
                list(props), fetched_at, from_cache=False,
                succeeded=True, complete=snapshot_complete,
                raw_record_count=len(raw_records),
                normalized_prop_count=len(props),
            )
            self._last_success_by_scope[scope] = result
            self._snapshot_state_by_scope[scope] = PropLineSnapshotState(
                complete=snapshot_complete,
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
        raw_record_count: int = 0,
        normalized_prop_count: int = 0,
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
            raw_record_count=raw_record_count,
            normalized_prop_count=normalized_prop_count,
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
        if getattr(self.client, "sports_discovery_failed", False):
            # Preserve a failed catalog as distinguishable from a valid empty
            # slate so the caller can use its complete non-football fallback.
            return None
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
            raw_record_count=result.raw_record_count,
            normalized_prop_count=result.normalized_prop_count,
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

    async def fetch_player_history(
        self,
        sport: str,
        player_name: str,
        *,
        market: str,
        bookmaker: Optional[str] = None,
        limit: int = 20,
    ) -> PropLinePlayerHistoryFetchResult:
        """Fetch supplemental history without entering the live feed."""
        canonical_sport = _normalize_sport(sport)
        if not self.is_available():
            return PropLinePlayerHistoryFetchResult(
                (), datetime.utcnow(), player_name=_clean(player_name),
                sport_key=canonical_sport, market=_clean(market),
                error="PropLine is not configured", failure_kind="disabled",
            )
        if (
            is_football_related_sport(canonical_sport)
            or (self._sport_filter is not None and canonical_sport not in self._sport_filter)
        ):
            return PropLinePlayerHistoryFetchResult(
                (), datetime.utcnow(), player_name=_clean(player_name),
                sport_key=canonical_sport, market=_clean(market),
                error=f"PropLine sport is outside configured scope: {canonical_sport}",
                failure_kind="configuration",
            )
        official_sport = self.client.official_sport_for(canonical_sport)
        if not official_sport:
            return PropLinePlayerHistoryFetchResult(
                (), datetime.utcnow(), player_name=_clean(player_name),
                sport_key=canonical_sport, market=_clean(market),
                error=f"PropLine sport is unsupported: {canonical_sport}",
                failure_kind="configuration",
            )
        result = await self.client.fetch_player_history(
            official_sport, player_name, market=market,
            bookmaker=bookmaker or self.bookmaker, limit=limit,
        )
        return replace(
            result,
            sport_key=canonical_sport,
            entries=tuple(
                replace(entry, sport=canonical_sport) for entry in result.entries
            ),
        )

    async def fetch_player_trends(
        self,
        sport: str,
        player_name: str,
        *,
        market: str = "",
        dfs_odds_type: str = "",
    ) -> PropLinePlayerTrendsFetchResult:
        """Fetch supplemental trends without changing picks or alerts."""
        canonical_sport = _normalize_sport(sport)
        if not self.is_available():
            return PropLinePlayerTrendsFetchResult(
                (), datetime.utcnow(), player_name=_clean(player_name),
                sport_key=canonical_sport,
                error="PropLine is not configured", failure_kind="disabled",
            )
        if (
            is_football_related_sport(canonical_sport)
            or (self._sport_filter is not None and canonical_sport not in self._sport_filter)
        ):
            return PropLinePlayerTrendsFetchResult(
                (), datetime.utcnow(), player_name=_clean(player_name),
                sport_key=canonical_sport,
                error=f"PropLine sport is outside configured scope: {canonical_sport}",
                failure_kind="configuration",
            )
        official_sport = self.client.official_sport_for(canonical_sport)
        if not official_sport:
            return PropLinePlayerTrendsFetchResult(
                (), datetime.utcnow(), player_name=_clean(player_name),
                sport_key=canonical_sport,
                error=f"PropLine sport is unsupported: {canonical_sport}",
                failure_kind="configuration",
            )
        result = await self.client.fetch_player_trends(
            official_sport, player_name, market=market,
            dfs_odds_type=dfs_odds_type,
        )
        return replace(result, sport_key=canonical_sport)

    def normalize_stat(self, raw: str) -> str:
        return _normalize_stat(raw)