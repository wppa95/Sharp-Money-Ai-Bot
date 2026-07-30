"""
Tests for the player-prop Odds API integration.

Covers:
  - AnalysisEngine.fetch_player_prop_odds() parsing and filtering
  - _SPORT_PLAYER_PROP_MARKETS coverage
  - PlayerPropLine dataclass
  - infer_call_priority HIGH detection for player prop market strings
  - Config: player_prop_sports property and PLAYER_PROP_POLL_INTERVAL
  - _player_props_job writing OddsRecord rows with correct market_type
"""
from __future__ import annotations

import sys
import os
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.analysis import (
    AnalysisEngine,
    PlayerPropLine,
    _SPORT_PLAYER_PROP_MARKETS,
    _SPORT_TO_ODDS_API_KEY,
)
from models import Sport
from providers.usage_tracker import CallPriority, infer_call_priority
from config import Config


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_event(
    player: str = "Test Player",
    market_key: str = "player_points",
    price: int = -115,
    point: float = 25.5,
    description: str = "Over",
    bookmaker_title: str = "DraftKings",
) -> dict:
    """Minimal Odds API event dict with one prop outcome."""
    return {
        "id": "evt_1",
        "sport_key": "basketball_nba",
        "away_team": "Team A",
        "home_team": "Team B",
        "commence_time": "2026-11-01T20:00:00Z",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": bookmaker_title,
                "markets": [
                    {
                        "key": market_key,
                        "outcomes": [
                            {
                                "name": player,
                                "description": description,
                                "price": price,
                                "point": point,
                            }
                        ],
                    }
                ],
            }
        ],
    }


# ── _SPORT_PLAYER_PROP_MARKETS coverage ───────────────────────────────────────

def test_nba_markets_in_map():
    markets = _SPORT_PLAYER_PROP_MARKETS[Sport.NBA]
    for m in ["player_points", "player_rebounds", "player_assists",
              "player_threes", "player_steals", "player_blocks"]:
        assert m in markets, f"Expected {m} in NBA player-prop markets"


def test_mlb_markets_in_map():
    markets = _SPORT_PLAYER_PROP_MARKETS[Sport.MLB]
    for m in ["player_hits", "player_pitcher_strikeouts", "player_total_bases"]:
        assert m in markets, f"Expected {m} in MLB player-prop markets"


def test_soccer_leagues_in_map():
    soccer_sports = [Sport.EPL, Sport.MLS, Sport.LA_LIGA,
                     Sport.SERIE_A, Sport.BUNDESLIGA, Sport.LIGUE_1, Sport.UCL]
    for sp in soccer_sports:
        assert sp in _SPORT_PLAYER_PROP_MARKETS, f"{sp} missing from player-prop market map"
        markets = _SPORT_PLAYER_PROP_MARKETS[sp]
        assert "player_shots_on_target" in markets or "player_goal_scorer" in markets


def test_nfl_not_in_default_map():
    """NFL is excluded by design — confirm it has no default entry."""
    assert Sport.NFL not in _SPORT_PLAYER_PROP_MARKETS


def test_all_prop_sports_have_odds_api_key():
    """Every sport in _SPORT_PLAYER_PROP_MARKETS must have an Odds API key."""
    for sport in _SPORT_PLAYER_PROP_MARKETS:
        assert sport in _SPORT_TO_ODDS_API_KEY, (
            f"{sport} is in _SPORT_PLAYER_PROP_MARKETS but has no _SPORT_TO_ODDS_API_KEY entry"
        )


# ── infer_call_priority HIGH detection ────────────────────────────────────────

@pytest.mark.parametrize("markets", [
    "player_points,player_rebounds",
    "player_hits,player_pitcher_strikeouts",
    "player_props",
    "player_shots_on_target,player_goal_scorer_anytime",
    "player_threes,player_steals,player_blocks",
])
def test_player_prop_markets_are_high_priority(markets):
    priority = infer_call_priority("basketball_nba", markets)
    assert priority == CallPriority.HIGH, (
        f"Expected HIGH for markets={markets!r}, got {priority}"
    )


def test_game_line_markets_not_high():
    assert infer_call_priority("basketball_nba", "h2h,spreads,totals") == CallPriority.MEDIUM


def test_low_sport_game_lines_are_low():
    assert infer_call_priority("soccer_epl", "h2h,spreads,totals") == CallPriority.LOW


# ── Config ────────────────────────────────────────────────────────────────────

def test_default_player_prop_sports():
    cfg = Config()
    sports = cfg.player_prop_sports
    assert "NBA" in sports
    assert "MLB" in sports
    assert "NFL" not in sports


def test_player_prop_poll_interval_default():
    cfg = Config()
    assert cfg.PLAYER_PROP_POLL_INTERVAL == 600


def test_player_prop_sports_raw_override():
    """PLAYER_PROP_SPORTS_RAW can be set directly to extend the sport list."""
    cfg = Config()
    cfg.PLAYER_PROP_SPORTS_RAW = "NBA,MLB,EPL,MLS"
    sports = cfg.player_prop_sports
    assert set(sports) == {"NBA", "MLB", "EPL", "MLS"}


def test_prizepicks_leagues_default_is_nba_mlb():
    cfg = Config()
    leagues = cfg.prizepicks_leagues
    assert "NBA" in leagues
    assert "MLB" in leagues
    assert "NFL" not in leagues


# ── PlayerPropLine dataclass ──────────────────────────────────────────────────

def test_player_prop_line_fields():
    pl = PlayerPropLine(
        sportsbook="FanDuel",
        sport=Sport.NBA,
        market_key="player_points",
        event="Team A @ Team B",
        player_name="LeBron James",
        description="Over",
        american_odds=-115,
        line=25.5,
        event_start=datetime(2026, 11, 1, 20, 0),
    )
    assert pl.sportsbook == "FanDuel"
    assert pl.market_key == "player_points"
    assert pl.player_name == "LeBron James"
    assert pl.description == "Over"
    assert pl.line == 25.5


# ── AnalysisEngine.fetch_player_prop_odds ─────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_player_prop_odds_parses_event():
    engine = AnalysisEngine()
    event = _make_event(player="LeBron James", market_key="player_points",
                        price=-115, point=25.5, description="Over")

    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(return_value=[event])

    with patch("engine.analysis.config") as mock_cfg, \
         patch("providers.odds_cache.get_odds_cache", return_value=mock_cache):
        mock_cfg.ODDS_API_KEY = "test_key"
        lines = await engine.fetch_player_prop_odds(Sport.NBA)

    assert len(lines) == 1
    pl = lines[0]
    assert pl.player_name == "LeBron James"
    assert pl.market_key == "player_points"
    assert pl.american_odds == -115
    assert pl.line == 25.5
    assert pl.description == "Over"
    assert pl.sportsbook == "DraftKings"
    assert pl.sport == Sport.NBA
    assert pl.event == "Team A @ Team B"


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_filters_non_player_markets():
    """Markets that don't start with 'player_' should be silently skipped."""
    engine = AnalysisEngine()
    event = _make_event(market_key="totals", price=-110, point=220.5, description="Over")

    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(return_value=[event])

    with patch("engine.analysis.config") as mock_cfg, \
         patch("providers.odds_cache.get_odds_cache", return_value=mock_cache):
        mock_cfg.ODDS_API_KEY = "test_key"
        lines = await engine.fetch_player_prop_odds(Sport.NBA)

    assert lines == []


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_unsupported_sport_returns_empty():
    """Sports not in _SPORT_PLAYER_PROP_MARKETS should return empty immediately."""
    engine = AnalysisEngine()
    with patch("engine.analysis.config") as mock_cfg:
        mock_cfg.ODDS_API_KEY = "test_key"
        lines = await engine.fetch_player_prop_odds(Sport.NFL)
    assert lines == []


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_no_api_key_returns_empty():
    engine = AnalysisEngine()
    with patch("engine.analysis.config") as mock_cfg:
        mock_cfg.ODDS_API_KEY = ""
        lines = await engine.fetch_player_prop_odds(Sport.NBA)
    assert lines == []


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_cache_none_returns_empty():
    engine = AnalysisEngine()
    with patch("engine.analysis.config") as mock_cfg, \
         patch("providers.odds_cache.get_odds_cache", return_value=None):
        mock_cfg.ODDS_API_KEY = "test_key"
        lines = await engine.fetch_player_prop_odds(Sport.NBA)
    assert lines == []


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_mlb_uses_mlb_markets():
    """MLB fetch should use player_hits/strikeouts/bases markets, not NBA markets."""
    engine = AnalysisEngine()
    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(return_value=[])

    with patch("engine.analysis.config") as mock_cfg, \
         patch("providers.odds_cache.get_odds_cache", return_value=mock_cache):
        mock_cfg.ODDS_API_KEY = "test_key"
        await engine.fetch_player_prop_odds(Sport.MLB)

    call_kwargs = mock_cache.get_or_fetch.call_args
    markets_used = call_kwargs.kwargs.get("markets") or call_kwargs.args[2]
    assert "player_hits" in markets_used
    assert "player_pitcher_strikeouts" in markets_used
    assert "player_points" not in markets_used


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_api_error_returns_empty():
    from providers.odds_cache import OddsApiError
    from providers.base import FailureType
    engine = AnalysisEngine()
    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(
        side_effect=OddsApiError(429, "quota_exceeded", "quota exceeded", FailureType.QUOTA)
    )

    with patch("engine.analysis.config") as mock_cfg, \
         patch("providers.odds_cache.get_odds_cache", return_value=mock_cache):
        mock_cfg.ODDS_API_KEY = "test_key"
        lines = await engine.fetch_player_prop_odds(Sport.NBA)

    assert lines == []


@pytest.mark.asyncio
async def test_fetch_player_prop_odds_malformed_outcome_skipped():
    """An outcome missing 'price' should be skipped without crashing."""
    engine = AnalysisEngine()
    event = {
        "id": "evt_bad",
        "away_team": "A",
        "home_team": "B",
        "commence_time": "2026-11-01T20:00:00Z",
        "bookmakers": [{
            "title": "FanDuel",
            "markets": [{
                "key": "player_points",
                "outcomes": [
                    {"name": "Good Player", "description": "Over", "price": -110, "point": 20.5},
                    {"name": "Bad Player"},   # missing price — should be skipped
                ],
            }],
        }],
    }
    mock_cache = MagicMock()
    mock_cache.get_or_fetch = AsyncMock(return_value=[event])

    with patch("engine.analysis.config") as mock_cfg, \
         patch("providers.odds_cache.get_odds_cache", return_value=mock_cache):
        mock_cfg.ODDS_API_KEY = "test_key"
        lines = await engine.fetch_player_prop_odds(Sport.NBA)

    assert len(lines) == 1
    assert lines[0].player_name == "Good Player"


# ── _player_props_job ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_player_props_job_writes_odds_records():
    """Job should write one OddsRecord per PlayerPropLine with correct fields."""
    import main as main_mod

    pl = PlayerPropLine(
        sportsbook="FanDuel",
        sport=Sport.NBA,
        market_key="player_points",
        event="Team A @ Team B",
        player_name="LeBron James",
        description="Over",
        american_odds=-115,
        line=25.5,
        event_start=datetime(2026, 11, 1, 20, 0),
    )

    mock_db = MagicMock()
    mock_db.save_odds = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.fetch_player_prop_odds = AsyncMock(return_value=[pl])

    saved_records = []
    mock_db.save_odds.side_effect = lambda r: saved_records.append(r) or None

    mock_cfg = MagicMock()
    mock_cfg.player_prop_sports = ["NBA"]
    mock_cfg.ODDS_API_KEY = "test_key"

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", None), \
         patch.object(main_mod, "config", mock_cfg):
        await main_mod._player_props_job(MagicMock())

    assert mock_engine.fetch_player_prop_odds.called
    assert len(saved_records) == 1
    rec = saved_records[0]
    assert rec.market_type == "player_points"
    assert "LeBron James" in rec.selection
    assert "Over" in rec.selection
    assert rec.sportsbook == "FanDuel"
    assert rec.line == 25.5


@pytest.mark.asyncio
async def test_player_props_job_skips_out_of_season():
    """Job must not call the engine for sports the season-checker marks inactive."""
    import main as main_mod
    from engine.analysis import _SPORT_TO_ODDS_API_KEY

    mock_db = MagicMock()
    mock_db.save_odds = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.fetch_player_prop_odds = AsyncMock(return_value=[])

    mock_season = MagicMock()
    mock_season.is_sport_active = MagicMock(return_value=False)

    mock_cfg = MagicMock()
    mock_cfg.player_prop_sports = ["NBA"]

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", mock_season), \
         patch.object(main_mod, "config", mock_cfg):
        await main_mod._player_props_job(MagicMock())

    mock_engine.fetch_player_prop_odds.assert_not_called()


@pytest.mark.asyncio
async def test_player_props_job_skips_when_db_not_ready():
    import main as main_mod
    mock_engine = MagicMock()
    mock_engine.fetch_player_prop_odds = AsyncMock()

    with patch.object(main_mod, "_db", None), \
         patch.object(main_mod, "_engine", mock_engine):
        await main_mod._player_props_job(MagicMock())

    mock_engine.fetch_player_prop_odds.assert_not_called()


@pytest.mark.asyncio
async def test_player_props_job_unknown_sport_skipped():
    """Invalid sport strings in config should be warned and skipped."""
    import main as main_mod

    mock_db = MagicMock()
    mock_db.save_odds = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.fetch_player_prop_odds = AsyncMock(return_value=[])

    mock_cfg = MagicMock()
    mock_cfg.player_prop_sports = ["NOTASPORT"]

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", None), \
         patch.object(main_mod, "config", mock_cfg):
        await main_mod._player_props_job(MagicMock())

    mock_engine.fetch_player_prop_odds.assert_not_called()
