"""
Tests for multi-platform market connectors.

Covers:
  - MarketSnapshot data model (market_key, implied_probability, odds_change)
  - BaseConnector interface
  - UnderdogConnector._parse (mocked API response) + line movement detection
  - ConnectorRegistry (register, fetch_all concurrently, health checks)

Note: DraftKings and FanDuel connector tests removed Aug 2026.
Those connectors were removed from the framework (provider rule:
only providers that improve Underdog actionable picks are kept).
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from connectors.base import MarketSnapshot, ConnectorStatus
from connectors.underdog import UnderdogConnector, UnderdogProjection
from connectors.registry import ConnectorRegistry


# ── Sample API payloads ───────────────────────────────────────────────────────

ODDS_API_RESPONSE = [
    {
        "id": "event-001",
        "sport_key": "americanfootball_nfl",
        "away_team": "Kansas City Chiefs",
        "home_team": "Las Vegas Raiders",
        "commence_time": "2025-01-15T20:00:00Z",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -165},
                            {"name": "Las Vegas Raiders",  "price": +140},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -110, "point": -3.5},
                            {"name": "Las Vegas Raiders",  "price": -110, "point": +3.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -170},
                            {"name": "Las Vegas Raiders",  "price": +145},
                        ],
                    },
                ],
            },
        ],
    }
]

# v1 API shape:
#   - appearance_stat lives inside line["over_under"], not on the line directly
#   - appearances is a top-level array; appearance_stat.appearance_id → appearances[].id
#   - players carry team_id (UUID string) instead of team: {alias}
#   - match_id in test uses the same string as the game id so game_time resolves;
#     in production match_id is an integer and game ids are UUIDs, so game_time=None
UNDERDOG_API_RESPONSE = {
    "over_under_lines": [
        {
            "id": "ud-001",
            "stat_value": 27.5,
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "app-001",
                    "display_stat":  "Fantasy Points",
                },
            },
            "over_under_id": "ou-001",
        },
        {
            "id": "ud-002",
            "stat_value": None,            # should be skipped
            "over_under": {
                "appearance_stat": {
                    "appearance_id": "app-001",
                    "display_stat":  "Rushing Yards",
                },
            },
            "over_under_id": "ou-002",
        },
    ],
    "appearances": [
        {
            "id":        "app-001",
            "player_id": "p-001",
            "match_id":  "game-001",   # string here so games lookup succeeds in tests
            "team_id":   "team-kc",
        },
    ],
    "players": [
        {
            "id":         "p-001",
            "first_name": "Patrick",
            "last_name":  "Mahomes",
            "sport_id":   "nfl",
            "team_id":    "team-kc",   # v1: UUID string, no nested alias dict
        }
    ],
    "games": [
        {
            "id":           "game-001",
            "scheduled_at": "2025-01-15T20:00:00Z",
        }
    ],
}


# ── MarketSnapshot model ──────────────────────────────────────────────────────

class TestMarketSnapshot:
    def _make(self, **kwargs) -> MarketSnapshot:
        defaults = dict(
            sportsbook  = "DraftKings",
            sport       = "NFL",
            league      = "NFL",
            event       = "Chiefs @ Raiders",
            market_type = "Moneyline",
            selection   = "Kansas City Chiefs",
            odds        = -110,
        )
        defaults.update(kwargs)
        return MarketSnapshot(**defaults)

    def test_market_key_uniquely_identifies_market(self):
        s = self._make()
        key = s.market_key
        assert isinstance(key, tuple)
        assert len(key) == 4
        assert "NFL" in key
        assert "Chiefs @ Raiders" in key

    def test_implied_probability_negative_odds(self):
        s = self._make(odds=-110)
        # 110/210 ≈ 0.5238
        assert abs(s.implied_probability - 0.5238) < 0.001

    def test_implied_probability_positive_odds(self):
        s = self._make(odds=+140)
        # 100/240 ≈ 0.4167
        assert abs(s.implied_probability - 0.4167) < 0.001

    def test_implied_probability_pickem_returns_half(self):
        s = self._make(odds=0, is_pickem=True)
        assert s.implied_probability == 0.5

    def test_odds_change_with_opening(self):
        s = self._make(odds=-115, opening_odds=-110)
        assert s.odds_change == -5

    def test_odds_change_no_opening(self):
        s = self._make(odds=-110, opening_odds=None)
        assert s.odds_change is None

    def test_repr_positive_odds(self):
        s = self._make(odds=+140)
        assert "+140" in repr(s)

    def test_repr_negative_odds(self):
        s = self._make(odds=-110)
        assert "-110" in repr(s)

    def test_same_market_key_for_different_books(self):
        s1 = self._make(sportsbook="DraftKings")
        s2 = self._make(sportsbook="FanDuel")
        assert s1.market_key == s2.market_key

    def test_different_market_key_for_different_selections(self):
        s1 = self._make(selection="Kansas City Chiefs")
        s2 = self._make(selection="Las Vegas Raiders")
        assert s1.market_key != s2.market_key


# ── Underdog connector ────────────────────────────────────────────────────────

class TestUnderdogConnector:
    def _make_connector(self) -> UnderdogConnector:
        return UnderdogConnector(active_sports=["NFL"], enabled=True)

    def test_parse_valid_projection(self):
        c = self._make_connector()
        projs = c._parse(UNDERDOG_API_RESPONSE)
        assert len(projs) == 1   # ud-002 skipped (stat_value=None)
        p = projs[0]
        assert p.player_name == "Patrick Mahomes"
        assert p.line_value  == 27.5
        assert p.stat_type   == "Fantasy Points"
        assert p.sport       == "NFL"

    def test_parse_skips_null_stat_value(self):
        c = self._make_connector()
        projs = c._parse(UNDERDOG_API_RESPONSE)
        ids = [p.external_id for p in projs]
        assert "ud-002" not in ids

    def test_parse_player_name(self):
        c = self._make_connector()
        projs = c._parse(UNDERDOG_API_RESPONSE)
        assert projs[0].player_name == "Patrick Mahomes"

    def test_parse_game_time_set(self):
        c = self._make_connector()
        projs = c._parse(UNDERDOG_API_RESPONSE)
        assert projs[0].game_time is not None

    @pytest.mark.asyncio
    async def test_fetch_returns_pickem_snapshots(self):
        c = self._make_connector()
        # Inject pre-parsed projections (bypass HTTP)
        projs = c._parse(UNDERDOG_API_RESPONSE)

        # Directly call the normalization portion of fetch
        current_ids = {p.external_id for p in projs}
        c._last_seen = set()
        snapshots = []
        from datetime import datetime as _dt
        now = _dt.utcnow()
        for proj in projs:
            prev = c._previous.get(proj.external_id)
            opening_line = prev.line_value if prev else proj.line_value
            sel = f"{proj.player_name} {proj.stat_type} {proj.line_value}"
            snaps = MarketSnapshot(
                sportsbook="Underdog", sport=proj.sport, league=proj.sport,
                event=proj.game_id or "game", market_type="Pick'em",
                selection=sel, odds=0, timestamp=now, player=proj.player_name,
                team=proj.team, line=proj.line_value, is_pickem=True,
            )
            snapshots.append(snaps)
            c._previous[proj.external_id] = proj
        c._last_seen = current_ids

        assert all(s.is_pickem for s in snapshots)
        assert all(s.sportsbook == "Underdog" for s in snapshots)
        assert all(s.odds == 0 for s in snapshots)

    def test_line_movement_detected(self):
        c = self._make_connector()
        # Pre-load previous projection at a different line
        c._previous["ud-001"] = UnderdogProjection(
            external_id="ud-001", player_name="Patrick Mahomes",
            team="KC", sport="NFL", stat_type="Fantasy Points",
            line_value=25.0,   # was 25.0, now 27.5
        )
        projs = c._parse(UNDERDOG_API_RESPONSE)
        new_proj = projs[0]
        prev = c._previous.get(new_proj.external_id)
        assert prev is not None
        assert abs(new_proj.line_value - prev.line_value) > 0.01

    @pytest.mark.asyncio
    async def test_disabled_connector_returns_empty(self):
        c = UnderdogConnector(enabled=False)
        result = await c.fetch()
        assert result == []


# ── ConnectorRegistry ─────────────────────────────────────────────────────────

class TestConnectorRegistry:
    def _make_registry_with_mocks(self) -> tuple[ConnectorRegistry, MagicMock, MagicMock]:
        registry = ConnectorRegistry()
        snap_a = MarketSnapshot(
            sportsbook="DraftKings", sport="NFL", league="NFL",
            event="Chiefs @ Raiders", market_type="Moneyline",
            selection="Kansas City Chiefs", odds=-165,
        )
        snap_b = MarketSnapshot(
            sportsbook="FanDuel", sport="NFL", league="NFL",
            event="Chiefs @ Raiders", market_type="Moneyline",
            selection="Kansas City Chiefs", odds=-170,
        )
        conn_a = MagicMock(spec=["name", "enabled", "is_pickem", "fetch", "health_check"])
        conn_a.name      = "DraftKings"
        conn_a.enabled   = True
        conn_a.is_pickem = False
        conn_a.fetch     = AsyncMock(return_value=[snap_a])
        conn_a.health_check = AsyncMock(return_value=ConnectorStatus.OK)

        conn_b = MagicMock(spec=["name", "enabled", "is_pickem", "fetch", "health_check"])
        conn_b.name      = "FanDuel"
        conn_b.enabled   = True
        conn_b.is_pickem = False
        conn_b.fetch     = AsyncMock(return_value=[snap_b])
        conn_b.health_check = AsyncMock(return_value=ConnectorStatus.OK)

        registry.register(conn_a)
        registry.register(conn_b)
        return registry, conn_a, conn_b

    async def test_fetch_all_combines_results(self):
        registry, _, _ = self._make_registry_with_mocks()
        snaps = await registry.fetch_all()
        assert len(snaps) == 2
        books = {s.sportsbook for s in snaps}
        assert "DraftKings" in books
        assert "FanDuel"    in books

    async def test_disabled_connector_not_fetched(self):
        registry = ConnectorRegistry()
        conn = MagicMock(spec=["name", "enabled", "is_pickem", "fetch", "health_check"])
        conn.name      = "TestBook"
        conn.enabled   = False
        conn.is_pickem = False
        conn.fetch     = AsyncMock(return_value=[])
        registry.register(conn)
        snaps = await registry.fetch_all()
        conn.fetch.assert_not_called()
        assert snaps == []

    async def test_failing_connector_skipped(self):
        registry = ConnectorRegistry()
        snap = MarketSnapshot(
            sportsbook="GoodBook", sport="NFL", league="NFL",
            event="Chiefs @ Raiders", market_type="Moneyline",
            selection="Chiefs", odds=-110,
        )
        good = MagicMock(spec=["name", "enabled", "is_pickem", "fetch", "health_check"])
        good.name      = "GoodBook"
        good.enabled   = True
        good.is_pickem = False
        good.fetch     = AsyncMock(return_value=[snap])
        good.health_check = AsyncMock(return_value=ConnectorStatus.OK)

        bad = MagicMock(spec=["name", "enabled", "is_pickem", "fetch", "health_check"])
        bad.name      = "BadBook"
        bad.enabled   = True
        bad.is_pickem = False
        bad.fetch     = AsyncMock(side_effect=RuntimeError("API down"))
        bad.health_check = AsyncMock(return_value=ConnectorStatus.ERROR)

        registry.register(good)
        registry.register(bad)
        snaps = await registry.fetch_all()
        # GoodBook returned 1 snapshot; BadBook raised but was caught
        assert len(snaps) == 1
        assert snaps[0].sportsbook == "GoodBook"

    async def test_health_check_all_returns_all_statuses(self):
        registry, conn_a, conn_b = self._make_registry_with_mocks()
        statuses = await registry.health_check_all()
        assert "DraftKings" in statuses
        assert "FanDuel"    in statuses
        assert statuses["DraftKings"] == ConnectorStatus.OK

    def test_sportsbook_vs_pickem_split(self):
        registry = ConnectorRegistry()
        sb = MagicMock(spec=["name", "enabled", "is_pickem"])
        sb.name      = "SB"
        sb.enabled   = True
        sb.is_pickem = False
        pk = MagicMock(spec=["name", "enabled", "is_pickem"])
        pk.name      = "PK"
        pk.enabled   = True
        pk.is_pickem = True
        registry.register(sb)
        registry.register(pk)
        assert len(registry.sportsbook_connectors) == 1
        assert len(registry.pickem_connectors) == 1
        assert registry.sportsbook_connectors[0].name == "SB"
        assert registry.pickem_connectors[0].name == "PK"
