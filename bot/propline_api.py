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
from propline_analytics import endpoint_result_payload, results_payload


_CAPABILITIES = {
    "sports", "events", "odds", "event-markets", "event-results",
    "event-odds",
    "event-stats", "event-context", "history", "closing", "movement",
    "ev", "calculate-ev", "projections", "best-line", "futures", "scores",
    "player-history", "player-games", "player-trends", "dfs-payouts",
    "hit-rates", "freshness", "resolution-summary", "grand-salami",
    "daily-goals", "sgp", "clv-grade", "resolved-props-export",
    "exports-sample",
}
_MAX_EXPORT_BYTES = 100_000_000


def _params(request: dict[str, Any]) -> dict[str, Any]:
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise ValueError("Capability params must be an object")
    return params


def _text(params: dict[str, Any], name: str, *, required: bool = False,
          default: str = "") -> str:
    value = params.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > 512:
        raise ValueError(f"{name} is too long")
    return value


def _integer(params: dict[str, Any], name: str, *, default: int,
            maximum: int = 100) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _boolean(params: dict[str, Any], name: str, *, default: bool = False) -> bool:
    value = params.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ValueError(f"{name} must be a boolean")


def _only(params: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"Unsupported capability parameter: {sorted(unknown)[0]}")


async def _capability(analytics: PropLineAnalytics, name: str,
                      params: dict[str, Any]) -> Any:
    """Dispatch the closed API capability set to the long-lived client."""
    if name not in _CAPABILITIES:
        raise ValueError("Unsupported PropLine capability")
    c = analytics.client
    if name == "sports":
        _only(params, set()); result = await c.fetch_sports()
    elif name == "events":
        _only(params, {"sport_key"})
        result = await c.fetch_events(_text(params, "sport_key", required=True))
    elif name in {"odds", "event-odds"}:
        _only(params, {"sport_key", "markets", "bookmakers", "period",
                       "event_id",
                       "include_links", "include_book_ids"})
        if name == "odds":
            result = await c.fetch_sport_odds(
                _text(params, "sport_key", required=True),
                markets=_text(params, "markets"), bookmakers=_text(params, "bookmakers"),
                period=_text(params, "period"),
                include_links=_boolean(params, "include_links"),
                include_book_ids=_boolean(params, "include_book_ids"))
        else:
            result = await c.fetch_event_odds_endpoint(
                _text(params, "sport_key", required=True),
                _text(params, "event_id", required=True),
                markets=_text(params, "markets"), bookmakers=_text(params, "bookmakers"),
                period=_text(params, "period"),
                include_links=_boolean(params, "include_links"),
                include_book_ids=_boolean(params, "include_book_ids"))
    elif name == "event-markets":
        _only(params, {"sport_key", "event_id", "period"})
        result = await c.fetch_event_markets(
            _text(params, "sport_key", required=True),
            _text(params, "event_id", required=True),
            period=_text(params, "period"))
    elif name == "event-results":
        _only(params, {"sport_key", "event_id", "markets", "bookmaker"})
        result = await c.fetch_event_results(
            _text(params, "sport_key", required=True),
            _text(params, "event_id", required=True),
            markets=_text(params, "markets"),
            bookmaker=_text(params, "bookmaker", default="all"))
        return results_payload(result)
    elif name in {"event-stats", "event-context"}:
        _only(params, {"sport_key", "event_id", "stat_type"})
        fn = c.fetch_event_stats if name == "event-stats" else c.fetch_event_context
        result = await fn(_text(params, "sport_key", required=True),
                          _text(params, "event_id", required=True),
                          stat_type=_text(params, "stat_type")) if name == "event-stats" else await fn(
                              _text(params, "sport_key", required=True),
                              _text(params, "event_id", required=True))
    elif name in {"history", "closing", "movement", "ev", "best-line", "projections"}:
        allowed = {"sport_key", "event_id", "markets", "bookmakers", "period",
                   "from_time", "to_time", "relative_from", "relative_to",
                   "interval", "changes_only"}
        _only(params, allowed)
        sport, event = (_text(params, "sport_key", required=True),
                        _text(params, "event_id", required=True))
        markets, books, period = (_text(params, "markets"), _text(params, "bookmakers"),
                                  _text(params, "period"))
        if name == "history":
            result = await c.fetch_odds_history(
                sport, event, markets=markets, from_time=_text(params, "from_time"),
                to_time=_text(params, "to_time"), relative_from=_text(params, "relative_from"),
                relative_to=_text(params, "relative_to"), interval=_text(params, "interval"),
                changes_only=(_boolean(params, "changes_only")
                              if "changes_only" in params else None),
                period=period)
        else:
            fn = {"closing": c.fetch_closing_lines, "movement": c.fetch_movement,
                  "ev": c.fetch_ev, "best-line": c.fetch_best_line,
                  "projections": c.fetch_projections}[name]
            kwargs = {"markets": markets}
            if name in {"closing", "movement"}: kwargs.update(bookmakers=books, period=period)
            elif name in {"ev", "best-line"}: kwargs["bookmakers"] = books
            result = await fn(sport, event, **kwargs)
    elif name == "calculate-ev":
        _only(params, {"sport_key", "event_id", "market", "name", "price", "point", "description"})
        price = params.get("price")
        if isinstance(price, bool) or not isinstance(price, int):
            raise ValueError("price must be an integer")
        result = await c.calculate_ev(
            _text(params, "sport_key", required=True), _text(params, "event_id", required=True),
            market=_text(params, "market", required=True), name=_text(params, "name", required=True),
            price=price, point=params.get("point"), description=_text(params, "description"))
    elif name == "futures":
        _only(params, {"sport_key", "bookmakers"})
        result = await c.fetch_futures(_text(params, "sport_key", required=True),
                                       bookmakers=_text(params, "bookmakers"))
    elif name == "scores":
        _only(params, {"sport_key", "days_from"})
        result = await c.fetch_scores(_text(params, "sport_key", required=True),
                                      days_from=_integer(params, "days_from", default=1, maximum=3))
    elif name in {"player-history", "player-games", "player-trends"}:
        _only(params, {"sport_key", "player_name", "market", "bookmaker", "limit",
                       "stat_type", "opponent", "dfs_odds_type"})
        sport, player = (_text(params, "sport_key", required=True),
                         _text(params, "player_name", required=True))
        limit = _integer(params, "limit", default=20)
        if name == "player-history":
            result = await c.fetch_player_history(sport, player, market=_text(params, "market", required=True),
                bookmaker=_text(params, "bookmaker", default="all"), limit=limit)
        elif name == "player-games":
            result = await c.fetch_player_games(sport, player, limit=limit,
                stat_type=_text(params, "stat_type"), opponent=_text(params, "opponent"))
        else:
            result = await c.fetch_player_trends(sport, player, market=_text(params, "market"),
                dfs_odds_type=_text(params, "dfs_odds_type"))
    elif name == "dfs-payouts":
        _only(params, {"platform", "leg_win_prob"})
        result = await c.fetch_dfs_payouts(platform=_text(params, "platform", default="prizepicks"),
                                           leg_win_prob=params.get("leg_win_prob"))
    elif name == "hit-rates":
        _only(params, {"days", "bookmaker"})
        result = await c.fetch_market_hit_rates(days=_integer(params, "days", default=28, maximum=60),
                                                bookmaker=_text(params, "bookmaker", default="bovada"))
    elif name == "freshness":
        _only(params, set()); result = await c.fetch_freshness()
    elif name == "resolution-summary":
        _only(params, {"days"}); result = await c.fetch_resolution_summary(days=_integer(params, "days", default=30, maximum=90))
    elif name in {"grand-salami", "daily-goals"}:
        _only(params, {"date"})
        fn = c.fetch_grand_salami if name == "grand-salami" else c.fetch_daily_goals_total
        result = await fn(date=_text(params, "date"))
    elif name == "sgp":
        _only(params, {"sport_key", "event_id", "bookmaker", "legs"})
        legs = params.get("legs")
        if not isinstance(legs, list) or not 2 <= len(legs) <= 10:
            raise ValueError("legs must contain between 2 and 10 items")
        result = await c.fetch_sgp(_text(params, "sport_key", required=True),
            _text(params, "event_id", required=True), bookmaker=_text(params, "bookmaker", default="all"), legs=legs)
    elif name == "clv-grade":
        _only(params, {"bets"})
        if not isinstance(params.get("bets"), list): raise ValueError("bets must be an array")
        result = await c.grade_clv(params["bets"])
    elif name == "resolved-props-export":
        _only(params, {"sport", "market", "bookmaker", "since", "until"})
        result = await c.fetch_resolved_props_export(
            sport=_text(params, "sport", required=True), market=_text(params, "market"),
            bookmaker=_text(params, "bookmaker"), since=_text(params, "since"), until=_text(params, "until"))
        payload = result.payload if isinstance(result.payload, str) else ""
        if len(payload.encode()) > _MAX_EXPORT_BYTES:
            return {
                "source": "PropLine",
                "payload": None,
                "fetched_at": result.fetched_at.isoformat(),
                "endpoint": result.endpoint,
                "succeeded": False,
                "complete": False,
                "error": "PropLine export exceeds the 100MB bridge limit",
                "failure_kind": "size",
                "headers": {},
            }
    elif name == "exports-sample":
        _only(params, set())
        result = await c.fetch_exports_sample()
    else:
        raise ValueError("Unsupported PropLine capability")
    return endpoint_result_payload(result)


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
    elif kind == "capability":
        capability = request.get("capability")
        if not isinstance(capability, str):
            raise ValueError("Capability name is required")
        data = await _capability(analytics, capability, _params(request))
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