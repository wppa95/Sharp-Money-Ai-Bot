import asyncio
from datetime import datetime

import pytest

from propline_api import _handle
from providers.propline import PropLineEndpointResult
from propline_analytics import PropLineAnalytics


class _Client:
    async def fetch_sports(self):
        return PropLineEndpointResult({"sports": []}, datetime.utcnow(), succeeded=True)

    async def fetch_sport_odds(self, *args, **kwargs):
        return PropLineEndpointResult({"odds": []}, datetime.utcnow(), succeeded=True)


def test_facade_dispatches_allowlisted_capability_to_long_lived_client():
    analytics = PropLineAnalytics(_Client())
    response = asyncio.run(_handle(analytics, {
        "kind": "capability", "capability": "sports", "params": {},
    }))
    assert response["ok"] is True
    assert response["data"]["payload"] == {"sports": []}


def test_facade_rejects_unknown_capability():
    analytics = PropLineAnalytics(_Client())
    with pytest.raises(ValueError, match="Unsupported PropLine capability"):
        asyncio.run(_handle(analytics, {
            "kind": "capability", "capability": "webhooks", "params": {},
        }))


def test_facade_validates_capability_parameters():
    analytics = PropLineAnalytics(_Client())
    with pytest.raises(ValueError, match="Unsupported capability parameter"):
        asyncio.run(_handle(analytics, {
            "kind": "capability", "capability": "odds",
            "params": {"sport_key": "nba", "unknown": "nope"},
        }))