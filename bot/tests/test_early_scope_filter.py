"""
Tests for the early scope filter that drops disallowed markets before they
reach the analysis engine or the database.

Covers:
  - is_ev_line_in_scope() predicate
  - _poll_odds_job drops out-of-scope lines before save_odds and analyze_line
  - _steam_check_job skips non-MLB sports before get_odds_window
  - consensus_check_job checks scope before dedup queries and stops writing
    dedup markers for blocked alerts
  - clv_check_job checks scope before dedup queries and stops writing
    dedup markers for blocked alerts
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alert_scope_filter import is_ev_line_in_scope
from models import MarketType, Sport


# ── is_ev_line_in_scope ────────────────────────────────────────────────────────

class TestIsEvLineInScope:
    """Unit tests for the cheap line-level pre-filter predicate."""

    def test_mlb_moneyline_allowed(self):
        assert is_ev_line_in_scope(Sport.MLB, MarketType.MONEYLINE) is True

    def test_mlb_total_allowed(self):
        assert is_ev_line_in_scope(Sport.MLB, MarketType.TOTAL) is True

    def test_mlb_spread_blocked(self):
        assert is_ev_line_in_scope(Sport.MLB, MarketType.SPREAD) is False

    def test_mlb_player_prop_blocked(self):
        assert is_ev_line_in_scope(Sport.MLB, MarketType.PLAYER_PROP) is False

    @pytest.mark.parametrize("sport", [
        # Tier 2 sports — always blocked from Odds API EV pipeline.
        Sport.NBA, Sport.NFL, Sport.MLB,
        # These Sport enum values don't appear in ud_tier1_sports (different key format).
        Sport.NCAAB, Sport.EPL, Sport.MLS, Sport.LA_LIGA,
        Sport.SERIE_A, Sport.BUNDESLIGA, Sport.LIGUE_1, Sport.UCL,
    ])
    def test_all_sports_moneyline_allowed(self, sport):
        assert is_ev_line_in_scope(sport, MarketType.MONEYLINE) is True

    @pytest.mark.parametrize("sport", [
        # Tier-2 sports blocked from Odds API.
        Sport.NBA, Sport.NFL, Sport.MLB,
    ])
    def test_tier2_total_allowed(self, sport):
        assert is_ev_line_in_scope(sport, MarketType.TOTAL) is True

    @pytest.mark.parametrize("sport", [
        # All sports pass the market-only early filter; the poller controls
        # which configured active sports are queried.
        Sport.WNBA, Sport.NHL,
    ])
    def test_tier1_sport_passes_scope(self, sport):
        assert is_ev_line_in_scope(sport, MarketType.MONEYLINE) is True


# ── _poll_odds_job player-prop ingestion ──────────────────────────────────────

def _make_prop_line(sport: Sport, market_key: str = "player_points") -> MagicMock:
    line = MagicMock()
    line.sport = sport
    line.market_key = market_key
    line.sportsbook = "FanDuel"
    line.event = "Team A @ Team B"
    line.event_id = "event-1"
    line.player_name = "Player A"
    line.description = "Over"
    line.american_odds = -110
    line.line = 20.5
    line.event_start = None
    line.observed_at = __import__("datetime").datetime.utcnow()
    return line


@pytest.mark.asyncio
async def test_poll_odds_job_does_not_fetch_player_props_from_odds_api():
    """Player-prop feed ownership stays with PropLine."""
    import main as main_mod

    mock_db = MagicMock()
    mock_engine = MagicMock()
    mock_engine.fetch_player_prop_odds = AsyncMock(
        return_value=[_make_prop_line(Sport.WNBA)]
    )
    mock_cfg = MagicMock()
    mock_cfg.ODDS_API_SPORTS_RAW = "WNBA,MLB"
    mock_cfg.player_prop_sports = ["WNBA"]
    mock_cfg.ODDS_API_ENABLED = True
    mock_cfg.SPORTSBOOK_CONFIRMATION_ENABLED = True
    mock_cfg.DRAFTKINGS_ENABLED = False
    mock_cfg.FANDUEL_ENABLED = True

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", None), \
         patch.object(main_mod, "config", mock_cfg):
        await main_mod._poll_odds_job(MagicMock())

    mock_engine.fetch_player_prop_odds.assert_not_awaited()
    mock_engine.fetch_live_odds.assert_not_called()


@pytest.mark.asyncio
async def test_poll_odds_job_skips_sports_without_player_prop_mapping():
    """Unsupported game markets are not fetched as a fallback."""
    import main as main_mod

    mock_db = MagicMock()
    mock_engine = MagicMock()
    mock_engine.fetch_player_prop_odds = AsyncMock()
    mock_cfg = MagicMock()
    mock_cfg.ODDS_API_SPORTS_RAW = "NCAAB"
    mock_cfg.player_prop_sports = ["NCAAB"]
    mock_cfg.ODDS_API_ENABLED = True
    mock_cfg.SPORTSBOOK_CONFIRMATION_ENABLED = True
    mock_cfg.DRAFTKINGS_ENABLED = True
    mock_cfg.FANDUEL_ENABLED = True

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", None), \
         patch.object(main_mod, "config", mock_cfg):
        await main_mod._poll_odds_job(MagicMock())

    mock_engine.fetch_player_prop_odds.assert_not_awaited()


# ── _steam_check_job early sport filter ───────────────────────────────────────

def test_configured_odds_api_sports_excludes_underdog_only_scope():
    """ACTIVE_SPORTS may contain Underdog identifiers absent from OddsAPI."""
    import main as main_mod

    mock_config = MagicMock()
    mock_config.active_sports = [
        "TENNIS", "WNBA", "CS", "LOL", "VAL", "DOTA", "PGA", "FIFA", "CFB"
    ]
    with patch.object(main_mod, "config", mock_config):
        configured = main_mod._configured_odds_api_sports()

    assert [(raw, sport.value) for raw, sport in configured] == [
        ("WNBA", "WNBA"),
        ("CFB", "NCAAF"),
    ]


def test_default_odds_api_scope_excludes_nfl():
    """NFL is not part of the default OddsAPI polling scope."""
    import main as main_mod

    mock_config = MagicMock()
    mock_config.ODDS_API_SPORTS_RAW = (
        "MLB,NBA,NHL,NCAAF,NCAAB,UFC,WNBA,EPL,LaLiga,SerieA,"
        "Bundesliga,Ligue1,MLS,UCL"
    )
    with patch.object(main_mod, "config", mock_config):
        configured = main_mod._configured_odds_api_sports()

    assert "NFL" not in {sport.value for _, sport in configured}


@pytest.mark.asyncio
async def test_steam_check_job_skips_non_mlb_before_db_read():
    """get_odds_window must not be called for non-MLB sports."""
    import main as main_mod

    mock_db = MagicMock()
    mock_db.get_odds_window = AsyncMock(return_value=[])
    mock_engine = MagicMock()

    mock_cfg = MagicMock()
    mock_cfg.active_sports = ["NHL", "NBA", "NCAAF", "UFC"]
    mock_cfg.allowed_user_ids = []
    mock_cfg.ODDS_POLL_INTERVAL = 60

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", None), \
         patch.object(main_mod, "config", mock_cfg), \
         patch("main.AlertDelivery", return_value=MagicMock()):
        await main_mod._steam_check_job(MagicMock())

    mock_db.get_odds_window.assert_not_called()


@pytest.mark.asyncio
async def test_steam_check_job_reads_mlb_records():
    """MLB should still reach get_odds_window (steam is blocked but data is valid)."""
    import main as main_mod

    mock_db = MagicMock()
    mock_db.get_odds_window = AsyncMock(return_value=[])
    mock_engine = MagicMock()

    mock_cfg = MagicMock()
    mock_cfg.active_sports = ["MLB"]
    mock_cfg.allowed_user_ids = []
    mock_cfg.ODDS_POLL_INTERVAL = 60

    with patch.object(main_mod, "_db", mock_db), \
         patch.object(main_mod, "_engine", mock_engine), \
         patch.object(main_mod, "_season_checker", None), \
         patch.object(main_mod, "config", mock_cfg), \
         patch("main.AlertDelivery", return_value=MagicMock()):
        await main_mod._steam_check_job(MagicMock())

    assert mock_db.get_odds_window.call_count == 1
    assert mock_db.get_odds_window.call_args[0][0] == "MLB"


# ── consensus_check_job — scope before dedup ──────────────────────────────────

@pytest.mark.asyncio
async def test_consensus_check_job_no_dedup_write_when_scope_blocked():
    """
    When an inefficiency alert is out of scope, has_recent_inefficiency_alert
    and save_market_snapshot must NOT be called (no wasted DB round-trips).
    """
    import market_engine as me

    mock_ineff = MagicMock()
    mock_ineff.abs_deviation = 20
    mock_ineff.sport = "NHL"
    mock_ineff.event = "Team A @ Team B"
    mock_ineff.market_type = "Spread"
    mock_ineff.selection = "Team A"
    mock_ineff.sportsbook = "DraftKings"

    mock_db = MagicMock()
    mock_db.has_recent_inefficiency_alert = AsyncMock(return_value=False)
    mock_db.save_market_snapshot = AsyncMock()
    mock_db.has_recent_steam_alert = AsyncMock(return_value=False)
    mock_db.save_steam = AsyncMock()

    me._snapshot_cache = {("key",): [MagicMock()]}

    mock_cfg = MagicMock()
    mock_cfg.allowed_user_ids = []
    mock_cfg.MIN_INEFFICIENCY_DEVIATION = 5
    mock_cfg.INEFFICIENCY_THRESHOLD = 3
    mock_cfg.CONSENSUS_MIN_BOOKS = 2

    # find_inefficiencies returns one out-of-scope inefficiency
    with patch("market_engine.find_inefficiencies", return_value=[mock_ineff]), \
         patch("market_engine.compute_consensus", return_value=[]), \
         patch("market_engine.build_multi_book_steam_inputs", return_value={}), \
         patch("market_engine.config", mock_cfg), \
         patch("market_engine.broadcast_alert", new_callable=AsyncMock):
        ctx = MagicMock()
        ctx.bot_data = {"db": mock_db}
        await me.consensus_check_job(ctx)

    # Scope blocked → no DB dedup queries, no dedup marker writes
    mock_db.has_recent_inefficiency_alert.assert_not_called()
    mock_db.save_market_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_consensus_check_job_no_steam_record_when_scope_blocked():
    """
    Multi-book steam that is out of scope must not write a SteamRecord
    or call has_recent_steam_alert.
    """
    import market_engine as me

    me._snapshot_cache = {}

    mock_db = MagicMock()
    mock_db.has_recent_steam_alert = AsyncMock(return_value=False)
    mock_db.save_steam = AsyncMock()
    mock_db.has_recent_inefficiency_alert = AsyncMock(return_value=False)
    mock_db.save_market_snapshot = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.allowed_user_ids = []
    mock_cfg.MIN_INEFFICIENCY_DEVIATION = 5
    mock_cfg.INEFFICIENCY_THRESHOLD = 3
    mock_cfg.CONSENSUS_MIN_BOOKS = 2

    # One out-of-scope steam entry
    steam_inputs = {("NHL", "Team A @ Team B", "Spread", "Team A"): [
        {"sportsbook": "DraftKings", "open_odds": -110, "current_odds": -120},
        {"sportsbook": "FanDuel",    "open_odds": -110, "current_odds": -120},
    ]}

    with patch("market_engine.find_inefficiencies", return_value=[]), \
         patch("market_engine.compute_consensus", return_value=[]), \
         patch("market_engine.build_multi_book_steam_inputs", return_value=steam_inputs), \
         patch("market_engine.config", mock_cfg), \
         patch("market_engine.broadcast_alert", new_callable=AsyncMock):
        ctx = MagicMock()
        ctx.bot_data = {"db": mock_db}
        me._snapshot_cache = {"key": [MagicMock()]}
        await me.consensus_check_job(ctx)

    mock_db.has_recent_steam_alert.assert_not_called()
    mock_db.save_steam.assert_not_called()


# ── clv_check_job — scope before dedup ────────────────────────────────────────

@pytest.mark.asyncio
async def test_clv_check_job_no_dedup_write_when_scope_blocked():
    """
    A CLV opportunity that is out of scope must not write a dedup marker
    or call has_recent_inefficiency_alert.
    """
    import market_engine as me

    mock_opp = MagicMock()
    mock_opp.is_actionable = True
    mock_opp.sport = "NHL"
    mock_opp.event = "Team A @ Team B"
    mock_opp.selection = "Team A"
    mock_opp.sportsbook = "DraftKings"
    mock_opp.market_type = "Spread"
    mock_opp.clv_lead = 5

    mock_snap = MagicMock()
    mock_snap.odds = -110
    mock_snap.is_pickem = False

    mock_db = MagicMock()
    mock_db.has_recent_inefficiency_alert = AsyncMock(return_value=False)
    mock_db.save_market_snapshot = AsyncMock()

    mock_cfg = MagicMock()
    mock_cfg.allowed_user_ids = []
    mock_cfg.CONSENSUS_MIN_BOOKS = 2
    mock_cfg.MIN_CLV_LEAD = 3
    mock_cfg.CLV_DEDUP_WINDOW = 3600

    me._snapshot_cache = {"key": [mock_snap, mock_snap]}

    with patch("market_engine.build_clv_opportunity", return_value=mock_opp), \
         patch("market_engine.format_clv_opportunity_alert", return_value="msg"), \
         patch("market_engine.config", mock_cfg), \
         patch("market_engine.broadcast_alert", new_callable=AsyncMock):
        ctx = MagicMock()
        ctx.bot_data = {"db": mock_db}
        await me.clv_check_job(ctx)

    mock_db.has_recent_inefficiency_alert.assert_not_called()
    mock_db.save_market_snapshot.assert_not_called()
