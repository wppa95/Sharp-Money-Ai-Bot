"""Line-oriented bridge for the read-only PropLine analytics facade.

The API artifact runs in a Node process, while the authorized PropLine client
and its request safety controls live in Python.  Keeping this worker alive
means the facade's cache, request budget, and rolling limiter are reused for
the lifetime of the API server instead of being recreated for every request.
The protocol is intentionally private to the API artifact: one JSON request
per line in, one JSON response per line out.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from propline_analytics import PropLineAnalytics


async def _handle(analytics: PropLineAnalytics, request: dict[str, Any]) -> dict[str, Any]:
    kind = request.get("kind")
    if kind == "event":
        data = await analytics.event_bundle(
            str(request.get("sport_key", "")),
            str(request.get("event_id", "")),
            markets=str(request.get("markets", "")),
            bookmaker=str(request.get("bookmaker", "prizepicks")),
            period=str(request.get("period", "")),
        )
    elif kind == "player":
        data = await analytics.player_bundle(
            str(request.get("sport_key", "")),
            str(request.get("player_name", "")),
            str(request.get("market", "")),
            bookmaker=str(request.get("bookmaker", "prizepicks")),
            limit=int(request.get("limit", 20)),
            stat_type=str(request.get("stat_type", "")),
        )
    else:
        raise ValueError("Unsupported PropLine analytics request")
    return {"ok": True, "data": data}


async def _serve() -> None:
    analytics = PropLineAnalytics.from_environment()
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            response = await _handle(analytics, request)
        except Exception:
            # Do not echo provider URLs, request parameters, or credentials to
            # the API client.  The worker's stderr remains available for local
            # diagnostics when the process is run directly.
            response = {
                "ok": False,
                "error": "PropLine analytics request failed",
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_serve())