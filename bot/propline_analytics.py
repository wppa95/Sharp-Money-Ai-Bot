"""Programmatic PropLine analytics access for the Sharp Money framework.

This module is deliberately read-only.  It composes the existing authorized
PropLine client into two stable payloads that can be consumed by trackers,
dashboards, and model jobs without creating another polling or alert path.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from typing import Any, Optional

from providers.propline import (
    PropLineClient,
    PropLineEndpointResult,
    PropLineResultsFetchResult,
)


def _jsonable(value: Any) -> Any:
    """Convert provider dataclasses and timestamps into JSON-safe values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def endpoint_result_payload(result: PropLineEndpointResult) -> dict[str, Any]:
    """Return a stable machine-readable envelope for any read-only endpoint."""
    return {
        "source": "PropLine",
        "payload": _jsonable(result.payload),
        "fetched_at": result.fetched_at.isoformat(),
        "endpoint": result.endpoint,
        "from_cache": result.from_cache,
        "succeeded": result.succeeded,
        "complete": result.complete,
        "redacted": result.redacted,
        "upgrade_url": result.upgrade_url,
        "error": result.error,
        "failure_kind": result.failure_kind,
    }


def results_payload(result: PropLineResultsFetchResult) -> dict[str, Any]:
    """Return resolved prop evidence without inventing settlement states."""
    return {
        "source": "PropLine",
        "results": _jsonable(result.results),
        "fetched_at": result.fetched_at.isoformat(),
        "event_id": result.event_id,
        "succeeded": result.succeeded,
        "complete": result.complete,
        "error": result.error,
        "failure_kind": result.failure_kind,
    }


@dataclass(frozen=True)
class PropLineAnalytics:
    """Read-only facade over the official PropLine API."""

    client: PropLineClient

    @classmethod
    def from_environment(cls) -> "PropLineAnalytics":
        """Build the configured client without exposing the API key to callers."""
        return cls(PropLineClient())

    async def event_bundle(
        self,
        sport_key: str,
        event_id: str,
        *,
        markets: str = "",
        bookmaker: str = "prizepicks",
        period: str = "",
    ) -> dict[str, Any]:
        """Fetch resolved props, event stats, and historical line movement."""
        results, stats, movement = await asyncio.gather(
            self.client.fetch_event_results(
                sport_key,
                event_id,
                markets=markets,
                bookmaker=bookmaker,
            ),
            self.client.fetch_event_stats(
                sport_key,
                event_id,
                stat_type=markets,
            ),
            self.client.fetch_odds_history(
                sport_key,
                event_id,
                markets=markets,
                period=period,
            ),
        )
        return {
            "kind": "event_bundle",
            "source": "PropLine",
            "provider": "PropLine",
            "sport": sport_key,
            "event_id": event_id,
            "bookmaker": bookmaker,
            "resolved_props": results_payload(results),
            "actual_player_stats": endpoint_result_payload(stats),
            "line_movement": endpoint_result_payload(movement),
        }

    async def player_bundle(
        self,
        sport_key: str,
        player_name: str,
        market: str,
        *,
        bookmaker: str = "prizepicks",
        limit: int = 20,
        stat_type: str = "",
    ) -> dict[str, Any]:
        """Fetch resolved prop history, actual player games, and trends."""
        history, games, trends = await asyncio.gather(
            self.client.fetch_player_history(
                sport_key,
                player_name,
                market,
                bookmaker=bookmaker,
                limit=limit,
            ),
            self.client.fetch_player_games(
                sport_key,
                player_name,
                limit=limit,
                stat_type=stat_type,
            ),
            self.client.fetch_player_trends(
                sport_key,
                player_name,
                market=market,
            ),
        )
        return {
            "kind": "player_bundle",
            "source": "PropLine",
            "provider": "PropLine",
            "sport": sport_key,
            "player_name": player_name,
            "market": market,
            "bookmaker": bookmaker,
            "resolved_prop_history": _jsonable(history),
            "actual_player_games": endpoint_result_payload(games),
            "player_trends": endpoint_result_payload(trends),
        }
