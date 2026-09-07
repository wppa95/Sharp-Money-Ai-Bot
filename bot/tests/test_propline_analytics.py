from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from propline_analytics import PropLineAnalytics
from providers.propline import (
    PropLineEndpointResult,
    PropLinePlayerHistoryFetchResult,
    PropLineResultEvidence,
    PropLineResultsFetchResult,
)


def _endpoint(payload):
    return PropLineEndpointResult(
        payload=payload,
        fetched_at=datetime(2026, 9, 7, 12, 0),
        endpoint="https://api.prop-line.com/v1/example",
        succeeded=True,
        complete=True,
    )


@pytest.mark.asyncio
async def test_event_bundle_composes_results_stats_and_line_movement():
    result = PropLineResultsFetchResult(
        results=(
            PropLineResultEvidence(
                observed_at=datetime(2026, 9, 7, 12, 0),
                resolution_state="WON",
                source_resolution="won",
                actual_value=31.0,
                resolved_at=datetime(2026, 9, 7, 11, 0),
                provider="PrizePicks",
                player_name="Alex Example",
                sport="NBA",
                stat_type="points",
                line_value=24.5,
                direction="OVER",
                event_id="event-1",
            ),
        ),
        fetched_at=datetime(2026, 9, 7, 12, 0),
        event_id="event-1",
        succeeded=True,
        complete=True,
    )
    client = SimpleNamespace(
        fetch_event_results=AsyncMock(return_value=result),
        fetch_event_stats=AsyncMock(return_value=_endpoint({"players": []})),
        fetch_odds_history=AsyncMock(return_value=_endpoint({"history": []})),
    )

    payload = await PropLineAnalytics(client).event_bundle(
        "baseball_mlb",
        "event-1",
        markets="player_hits",
    )

    assert payload["kind"] == "event_bundle"
    assert payload["resolved_props"]["results"][0]["resolution_state"] == "WON"
    assert payload["actual_player_stats"]["payload"] == {"players": []}
    assert payload["line_movement"]["payload"] == {"history": []}
    client.fetch_event_results.assert_awaited_once_with(
        "baseball_mlb",
        "event-1",
        markets="player_hits",
        bookmaker="prizepicks",
    )
    client.fetch_event_stats.assert_awaited_once_with(
        "baseball_mlb",
        "event-1",
        stat_type="player_hits",
    )
    client.fetch_odds_history.assert_awaited_once_with(
        "baseball_mlb",
        "event-1",
        markets="player_hits",
        period="",
    )


@pytest.mark.asyncio
async def test_player_bundle_composes_resolved_history_games_and_trends():
    client = SimpleNamespace(
        fetch_player_history=AsyncMock(
            return_value=PropLinePlayerHistoryFetchResult(
                entries=(),
                fetched_at=datetime(2026, 9, 7, 12, 0),
                player_name="Alex Example",
                sport_key="basketball_nba",
                market="player_points",
                succeeded=True,
                complete=True,
            )
        ),
        fetch_player_games=AsyncMock(
            return_value=_endpoint({"games": [{"points": 31}]})
        ),
        fetch_player_trends=AsyncMock(
            return_value=_endpoint({"markets": [{"last_5": 0.8}]})
        ),
    )

    payload = await PropLineAnalytics(client).player_bundle(
        "basketball_nba",
        "Alex Example",
        "player_points",
        limit=10,
        stat_type="points",
    )

    assert payload["kind"] == "player_bundle"
    assert payload["actual_player_games"]["payload"]["games"][0]["points"] == 31
    assert payload["player_trends"]["payload"]["markets"][0]["last_5"] == 0.8
    client.fetch_player_history.assert_awaited_once_with(
        "basketball_nba",
        "Alex Example",
        "player_points",
        bookmaker="prizepicks",
        limit=10,
    )
    client.fetch_player_games.assert_awaited_once_with(
        "basketball_nba",
        "Alex Example",
        limit=10,
        stat_type="points",
    )
    client.fetch_player_trends.assert_awaited_once_with(
        "basketball_nba",
        "Alex Example",
        market="player_points",
    )