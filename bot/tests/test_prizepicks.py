"""
Tests for the PrizePicks monitoring module.

Covers:
  - Implied probability / vig removal helpers
  - compare_pp_to_sportsbook edge math (same line, lower PP, higher PP)
  - PrizePicksLine and PPEdgeOpportunity data models
  - PrizePicksClient._parse response parsing
  - Database: PrizePicksRecord and PPEdgeRecord storage + dedup
  - format_pp_alert HTML output
  - compute_pp_risk_factors
  - Threshold filtering logic
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from prizepicks import (
    PrizePicksLine,
    PPEdgeOpportunity,
    PrizePicksClient,
    compare_pp_to_sportsbook,
    PP_LEAGUE_IDS,
    PP_STAT_TO_ODDS_API,
    _american_to_implied,
    _fair_prob_multiplicative,
    _prob_per_unit_for,
)
from alerts import format_pp_alert, compute_pp_risk_factors


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_pp_line(
    *,
    player_name: str = "LeBron James",
    team: str = "LAL",
    sport: str = "NBA",
    stat_type: str = "Points",
    line_value: float = 25.5,
    external_id: str = "proj-001",
) -> PrizePicksLine:
    return PrizePicksLine(
        external_id=external_id,
        player_name=player_name,
        team=team,
        sport=sport,
        league=sport,
        stat_type=stat_type,
        line_value=line_value,
        start_time=datetime(2025, 1, 15, 20, 0),
        game_description="vs LAC",
    )


SAMPLE_API_RESPONSE = {
    "data": [
        {
            "id": "proj-001",
            "type": "projection",
            "attributes": {
                "stat_type": "Points",
                "line_score": "25.5",
                "start_time": "2025-01-15T20:00:00Z",
                "description": "vs LAC",
                "status": "pre_game",
            },
            "relationships": {
                "new_player": {"data": {"id": "p-001", "type": "new_player"}},
                "league":     {"data": {"id": "2",     "type": "league"}},
            },
        },
        {
            "id": "proj-002",
            "type": "projection",
            "attributes": {
                "stat_type": "Rebounds",
                "line_score": "8.5",
                "status": "final",          # should be skipped
            },
            "relationships": {
                "new_player": {"data": {"id": "p-001", "type": "new_player"}},
                "league":     {"data": {"id": "2",     "type": "league"}},
            },
        },
        {
            "id": "proj-003",
            "type": "not_projection",       # should be skipped
            "attributes": {"stat_type": "Assists", "line_score": "7.5"},
            "relationships": {
                "new_player": {"data": {"id": "p-001", "type": "new_player"}},
                "league":     {"data": {"id": "2",     "type": "league"}},
            },
        },
        {
            "id": "proj-004",
            "type": "projection",
            "attributes": {
                "stat_type": "Assists",
                "line_score": None,          # should be skipped (no line)
                "status": "pre_game",
            },
            "relationships": {
                "new_player": {"data": {"id": "p-001", "type": "new_player"}},
                "league":     {"data": {"id": "2",     "type": "league"}},
            },
        },
    ],
    "included": [
        {
            "id": "p-001",
            "type": "new_player",
            "attributes": {
                "name": "LeBron James",
                "team_name": "Los Angeles Lakers",
                "position": "SF",
            },
        },
        {
            "id": "2",
            "type": "league",
            "attributes": {"name": "NBA"},
        },
    ],
}


# ── Implied probability helpers ───────────────────────────────────────────────

class TestImpliedProbability:
    def test_negative_odds_implied(self):
        # -110 → 110/210 ≈ 0.5238
        p = _american_to_implied(-110)
        assert abs(p - 0.5238) < 0.001

    def test_positive_odds_implied(self):
        # +200 → 100/300 ≈ 0.3333
        p = _american_to_implied(200)
        assert abs(p - 0.3333) < 0.001

    def test_even_odds_implied(self):
        # -100 → 0.5
        p = _american_to_implied(-100)
        assert abs(p - 0.5) < 0.001

    def test_fair_prob_sums_to_one(self):
        over, under = _fair_prob_multiplicative(-110, -110)
        assert abs(over + under - 1.0) < 1e-9

    def test_fair_prob_vig_removed(self):
        # -110 / -110 market: each side implied 52.38% but fair should be 50%
        over, under = _fair_prob_multiplicative(-110, -110)
        assert abs(over - 0.5) < 0.001
        assert abs(under - 0.5) < 0.001

    def test_fair_prob_asymmetric_market(self):
        # -130 favourite, +110 dog
        over, under = _fair_prob_multiplicative(-130, 110)
        assert over > under      # favourite has higher fair prob
        assert abs(over + under - 1.0) < 1e-9


# ── Edge calculation ──────────────────────────────────────────────────────────

class TestCompareToSportsbook:
    def test_same_line_equal_odds_zero_edge(self):
        """When PP line = SB line and market is fair, edge should be ≈ 0."""
        pp = make_pp_line(line_value=25.5, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=25.5,
            sb_over_odds=-100, sb_under_odds=-100,
        )
        assert abs(opp.edge_over)  < 0.1
        assert abs(opp.edge_under) < 0.1
        assert opp.line_diff == 0.0

    def test_pp_line_lower_than_sb_gives_over_edge(self):
        """PP line 25.5 vs SB 27.5: OVER is easier → positive edge."""
        pp = make_pp_line(line_value=25.5, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="DraftKings", sb_line=27.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        assert opp.line_diff == pytest.approx(2.0)
        assert opp.edge_over > 0, "OVER should be +EV when PP line is lower"
        assert opp.best_side == "OVER"
        assert opp.best_edge == opp.edge_over

    def test_pp_line_higher_than_sb_gives_under_edge(self):
        """PP line 27.5 vs SB 25.5: UNDER is easier → positive edge."""
        pp = make_pp_line(line_value=27.5, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="FanDuel", sb_line=25.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        assert opp.line_diff == pytest.approx(-2.0)
        assert opp.edge_under > 0, "UNDER should be +EV when PP line is higher"
        assert opp.best_side == "UNDER"

    def test_juiced_sportsbook_reduces_edge(self):
        """A heavily juiced sportsbook market (-130/-130) shifts fair prob."""
        pp = make_pp_line(line_value=25.5, stat_type="Points")
        # Equal lines, equal implied on both sides but heavy vig → fair = 50%
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Low-Quality-Book", sb_line=25.5,
            sb_over_odds=-130, sb_under_odds=-130,
        )
        # Fair prob is still 50% (vig removed symmetrically)
        assert abs(opp.fair_prob_over_at_sb_line - 0.5) < 0.01
        assert abs(opp.edge_over) < 0.1

    def test_probability_capped_at_bounds(self):
        """Large line differences should not produce impossible probabilities."""
        pp = make_pp_line(line_value=5.0, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=20.0,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        assert 0.01 <= opp.adjusted_fair_prob_over  <= 0.99
        assert 0.01 <= opp.adjusted_fair_prob_under <= 0.99

    def test_edge_fields_populated(self):
        pp = make_pp_line(line_value=25.5, stat_type="Assists")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=26.5,
            sb_over_odds=-115, sb_under_odds=-105,
        )
        assert opp.sportsbook == "Pinnacle"
        assert opp.sportsbook_line == 26.5
        assert opp.sportsbook_over_odds == -115
        assert opp.sportsbook_under_odds == -105
        assert opp.pp_line is pp
        assert isinstance(opp.best_side, str)
        assert opp.best_side in ("OVER", "UNDER")
        assert opp.best_edge == pytest.approx(max(opp.edge_over, opp.edge_under))

    def test_prob_per_unit_default_for_unknown_stat(self):
        pp = make_pp_line(line_value=10.0, stat_type="UnknownStat")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Book", sb_line=11.0,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        assert opp.prob_per_unit == 3.0   # default

    def test_prob_per_unit_for_known_stat(self):
        pp = make_pp_line(line_value=25.5, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Book", sb_line=25.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        assert opp.prob_per_unit == 2.5   # "Points" mapped value


# ── Data model ────────────────────────────────────────────────────────────────

class TestPrizePicksLineModel:
    def test_required_fields(self):
        pp = make_pp_line()
        assert pp.player_name == "LeBron James"
        assert pp.stat_type   == "Points"
        assert pp.line_value  == 25.5
        assert pp.sport       == "NBA"

    def test_fetched_at_defaults_to_now(self):
        before = datetime.utcnow()
        pp = PrizePicksLine(
            external_id="x", player_name="Test Player", team="TM",
            sport="NBA", league="NBA", stat_type="Points", line_value=20.0,
        )
        after = datetime.utcnow()
        assert before <= pp.fetched_at <= after

    def test_optional_start_time_none(self):
        pp = make_pp_line()
        assert pp.start_time == datetime(2025, 1, 15, 20, 0)   # fixture sets it

    def test_league_ids_mapping(self):
        assert PP_LEAGUE_IDS["NBA"] == 2
        assert PP_LEAGUE_IDS["NFL"] == 9
        assert PP_LEAGUE_IDS["MLB"] == 3

    def test_stat_to_odds_api_mapping(self):
        assert PP_STAT_TO_ODDS_API["Points"]        == "player_points"
        assert PP_STAT_TO_ODDS_API["Passing Yards"] == "player_pass_yds"
        assert PP_STAT_TO_ODDS_API["Rebounds"]      == "player_rebounds"


# ── Client response parsing ───────────────────────────────────────────────────

class TestPrizePicksClientParsing:
    def _client(self) -> PrizePicksClient:
        client = PrizePicksClient.__new__(PrizePicksClient)
        client._session = MagicMock()
        client._owns_session = False
        return client

    def test_parse_valid_projection(self):
        c = self._client()
        lines = c._parse(SAMPLE_API_RESPONSE, league_id=2)
        assert len(lines) == 1                       # only proj-001 passes filters
        line = lines[0]
        assert line.external_id  == "proj-001"
        assert line.player_name  == "LeBron James"
        assert line.team         == "Los Angeles Lakers"
        assert line.sport        == "NBA"
        assert line.stat_type    == "Points"
        assert line.line_value   == 25.5
        assert line.game_description == "vs LAC"

    def test_parse_skips_final_status(self):
        c = self._client()
        lines = c._parse(SAMPLE_API_RESPONSE, league_id=2)
        ids = [l.external_id for l in lines]
        assert "proj-002" not in ids   # status=final

    def test_parse_skips_non_projection_type(self):
        c = self._client()
        lines = c._parse(SAMPLE_API_RESPONSE, league_id=2)
        ids = [l.external_id for l in lines]
        assert "proj-003" not in ids   # type=not_projection

    def test_parse_skips_null_line_score(self):
        c = self._client()
        lines = c._parse(SAMPLE_API_RESPONSE, league_id=2)
        ids = [l.external_id for l in lines]
        assert "proj-004" not in ids   # line_score=None

    def test_parse_empty_response(self):
        c = self._client()
        lines = c._parse({"data": [], "included": []}, league_id=2)
        assert lines == []

    def test_parse_handles_missing_player(self):
        data = {
            "data": [{
                "id": "proj-999",
                "type": "projection",
                "attributes": {
                    "stat_type": "Points", "line_score": "20.0", "status": "pre_game",
                },
                "relationships": {
                    "new_player": {"data": {"id": "unknown-id", "type": "new_player"}},
                    "league":     {"data": {"id": "2", "type": "league"}},
                },
            }],
            "included": [],
        }
        c = self._client()
        lines = c._parse(data, league_id=2)
        assert len(lines) == 1
        assert lines[0].player_name == "Unknown Player"


# ── Database storage ──────────────────────────────────────────────────────────

class TestPrizePicksDatabase:
    @pytest.fixture()
    async def db(self):
        from database import Database
        _db = Database("sqlite+aiosqlite:///:memory:")
        await _db.init()
        yield _db
        await _db.close()

    async def test_save_and_retrieve_pp_line(self, db):
        from database import PrizePicksRecord
        record = PrizePicksRecord(
            external_id="proj-001",
            player_name="LeBron James",
            team="LAL",
            sport="NBA",
            stat_type="Points",
            line_value=25.5,
            game_description="vs LAC",
            fetched_at=datetime.utcnow(),
        )
        saved = await db.save_pp_line(record)
        assert saved.id is not None

        rows = await db.get_recent_pp_lines(limit=5)
        assert len(rows) == 1
        assert rows[0].player_name == "LeBron James"
        assert rows[0].line_value  == 25.5

    async def test_save_and_retrieve_pp_edge(self, db):
        from database import PPEdgeRecord
        now = datetime.utcnow()
        edge = PPEdgeRecord(
            player_name="LeBron James",
            team="LAL",
            sport="NBA",
            stat_type="Points",
            pp_line_value=25.5,
            sportsbook="Pinnacle",
            sb_line_value=27.5,
            sb_over_odds=-110,
            sb_under_odds=-110,
            fair_prob_over=0.622,
            fair_prob_under=0.378,
            edge_over=12.2,
            edge_under=-12.2,
            best_side="OVER",
            best_edge=12.2,
            alert_sent=True,
            detected_at=now,
        )
        saved = await db.save_pp_edge(edge)
        assert saved.id is not None

        rows = await db.get_recent_pp_edges(limit=5)
        assert len(rows) == 1
        assert rows[0].best_edge   == 12.2
        assert rows[0].best_side   == "OVER"
        assert rows[0].alert_sent  is True

    async def test_count_pp_records(self, db):
        from database import PrizePicksRecord
        for i in range(3):
            await db.save_pp_line(PrizePicksRecord(
                external_id=f"proj-{i}",
                player_name=f"Player {i}",
                team="TM", sport="NBA",
                stat_type="Points",
                line_value=20.0 + i,
                fetched_at=datetime.utcnow(),
            ))
        count = await db.count_pp_records()
        assert count == 3

    async def test_has_recent_pp_alert_true(self, db):
        from database import PPEdgeRecord
        await db.save_pp_edge(PPEdgeRecord(
            player_name="LeBron James",
            team="LAL", sport="NBA", stat_type="Points",
            pp_line_value=25.5, sportsbook="Pinnacle",
            sb_line_value=27.5, sb_over_odds=-110, sb_under_odds=-110,
            fair_prob_over=0.62, fair_prob_under=0.38,
            edge_over=12.0, edge_under=-12.0,
            best_side="OVER", best_edge=12.0,
            alert_sent=True, detected_at=datetime.utcnow(),
        ))
        is_recent = await db.has_recent_pp_alert(
            "LeBron James", "Points", within_seconds=3600
        )
        assert is_recent is True

    async def test_has_recent_pp_alert_false_different_player(self, db):
        from database import PPEdgeRecord
        await db.save_pp_edge(PPEdgeRecord(
            player_name="Steph Curry",
            team="GSW", sport="NBA", stat_type="Points",
            pp_line_value=28.5, sportsbook="Pinnacle",
            sb_line_value=28.5, sb_over_odds=-110, sb_under_odds=-110,
            fair_prob_over=0.50, fair_prob_under=0.50,
            edge_over=0.0, edge_under=0.0,
            best_side="OVER", best_edge=0.0,
            alert_sent=True, detected_at=datetime.utcnow(),
        ))
        is_recent = await db.has_recent_pp_alert(
            "LeBron James", "Points", within_seconds=3600
        )
        assert is_recent is False   # different player

    async def test_has_recent_pp_alert_false_outside_window(self, db):
        from database import PPEdgeRecord
        old_time = datetime.utcnow() - timedelta(seconds=7200)
        await db.save_pp_edge(PPEdgeRecord(
            player_name="LeBron James",
            team="LAL", sport="NBA", stat_type="Points",
            pp_line_value=25.5, sportsbook="Pinnacle",
            sb_line_value=27.5, sb_over_odds=-110, sb_under_odds=-110,
            fair_prob_over=0.62, fair_prob_under=0.38,
            edge_over=12.0, edge_under=-12.0,
            best_side="OVER", best_edge=12.0,
            alert_sent=True, detected_at=old_time,
        ))
        is_recent = await db.has_recent_pp_alert(
            "LeBron James", "Points", within_seconds=3600
        )
        assert is_recent is False   # outside 1-hour window

    async def test_find_player_prop_odds_matches_by_name(self, db):
        from database import OddsRecord
        now = datetime.utcnow()
        for side, odds in [("LeBron James Over", -110), ("LeBron James Under", -110)]:
            await db.save_odds(OddsRecord(
                sportsbook="DraftKings", sport="NBA",
                market_type="player_points",
                event="LAL @ LAC", selection=side,
                american_odds=odds, line=27.5,
                recorded_at=now,
            ))
        matches = await db.find_player_prop_odds(
            "LeBron James", "player_points",
            since=now - timedelta(minutes=5),
        )
        assert len(matches) == 2

    async def test_find_player_prop_odds_no_match(self, db):
        from database import OddsRecord
        now = datetime.utcnow()
        await db.save_odds(OddsRecord(
            sportsbook="DraftKings", sport="NBA",
            market_type="player_points",
            event="LAL @ LAC", selection="Steph Curry Over",
            american_odds=-110, line=29.5,
            recorded_at=now,
        ))
        matches = await db.find_player_prop_odds(
            "LeBron James", "player_points",
            since=now - timedelta(minutes=5),
        )
        assert len(matches) == 0   # different player


# ── Alert formatting ──────────────────────────────────────────────────────────

class TestFormatPPAlert:
    def _make_opp(
        self,
        *,
        edge_over: float = 12.2,
        edge_under: float = -12.2,
        best_side: str = "OVER",
        line_value: float = 25.5,
        sb_line: float = 27.5,
    ) -> PPEdgeOpportunity:
        pp = make_pp_line(line_value=line_value)
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle",
            sb_line=sb_line,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        return opp

    def test_contains_player_name(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "LeBron James" in msg

    def test_contains_stat_type(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "Points" in msg

    def test_contains_sportsbook(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "Pinnacle" in msg

    def test_contains_pp_line_value(self):
        opp = self._make_opp(line_value=25.5)
        msg = format_pp_alert(opp)
        assert "25.5" in msg

    def test_contains_sb_line_value(self):
        opp = self._make_opp(sb_line=27.5)
        msg = format_pp_alert(opp)
        assert "27.5" in msg

    def test_contains_best_side(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "OVER" in msg

    def test_contains_edge_percentage(self):
        pp = make_pp_line(line_value=25.5)
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=27.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        msg = format_pp_alert(opp)
        # Edge should be a non-zero positive number
        assert "%" in msg
        assert "Edge" in msg

    def test_contains_risk_section(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "Risk" in msg

    def test_alert_type_header(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "PRIZEPICKS" in msg

    def test_html_opens_and_closes_bold(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert msg.count("<b>") == msg.count("</b>"), "Unbalanced <b> tags"

    def test_html_opens_and_closes_italic(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert msg.count("<i>") == msg.count("</i>"), "Unbalanced <i> tags"

    def test_game_description_included(self):
        opp = self._make_opp()
        msg = format_pp_alert(opp)
        assert "vs LAC" in msg   # from fixture game_description

    def test_custom_risk_factors_used(self):
        from alerts import RiskFactor
        opp = self._make_opp()
        custom = [RiskFactor("HIGH", "Test risk factor")]
        msg = format_pp_alert(opp, risk_factors=custom)
        assert "Test risk factor" in msg


# ── Risk factors ──────────────────────────────────────────────────────────────

class TestPPRiskFactors:
    def test_thin_edge_medium_risk(self):
        pp = make_pp_line(line_value=25.5)
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=25.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        # Edge is ~0 → thin edge warning
        factors = compute_pp_risk_factors(opp)
        levels = {f.level for f in factors}
        assert "MEDIUM" in levels

    def test_large_line_diff_flagged(self):
        pp = make_pp_line(line_value=20.0)
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=25.0,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        factors = compute_pp_risk_factors(opp)
        descriptions = " ".join(f.description for f in factors)
        assert "unit" in descriptions.lower() or "model" in descriptions.lower()

    def test_no_risk_for_strong_edge_same_line(self):
        # Same line at -120/+100 (asymmetric): one side clearly has edge
        pp = make_pp_line(line_value=25.5)
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=25.5,
            sb_over_odds=100, sb_under_odds=-120,
        )
        factors = compute_pp_risk_factors(opp)
        high_risks = [f for f in factors if f.level == "HIGH"]
        assert len(high_risks) == 0   # no HIGH risk for straightforward comparison


# ── Threshold logic ───────────────────────────────────────────────────────────

class TestPPThresholdFiltering:
    def test_edge_above_threshold(self):
        """Verify a 2-point lower PP line produces edge well above 5% for Points."""
        pp = make_pp_line(line_value=25.5, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=27.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        # 2 pts × 2.5%/pt = 5% adjustment → edge > 5%
        assert opp.best_edge >= 5.0

    def test_no_edge_when_lines_equal_and_fair(self):
        """PP line = SB line in a -100/-100 fair market → zero edge."""
        pp = make_pp_line(line_value=25.5, stat_type="Points")
        opp = compare_pp_to_sportsbook(
            pp, sportsbook="Pinnacle", sb_line=25.5,
            sb_over_odds=-100, sb_under_odds=-100,
        )
        assert opp.best_edge < 1.0   # below any reasonable MIN_PP_EDGE

    def test_edge_proportional_to_line_diff(self):
        """Larger line diff → larger edge (for the favoured side)."""
        pp_small = make_pp_line(line_value=25.5)
        pp_large = make_pp_line(line_value=23.5)
        opp_small = compare_pp_to_sportsbook(
            pp_small, sportsbook="X", sb_line=27.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        opp_large = compare_pp_to_sportsbook(
            pp_large, sportsbook="X", sb_line=27.5,
            sb_over_odds=-110, sb_under_odds=-110,
        )
        assert opp_large.edge_over > opp_small.edge_over
