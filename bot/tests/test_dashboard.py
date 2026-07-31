"""
Tests for bot/engine/dashboard.py

Covers:
  - DashboardReport dataclass (total_all_alerts, to_telegram rendering)
  - TierPerf (tier_emoji)
  - SportPerf, MarketPerf, DailyTrend
  - DashboardEngine.gather() with a real in-memory database
  - to_telegram() key sections: header, totals, EV, CLV, sport, market, trend
  - Edge cases: empty DB, partial data, no CLV records
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timedelta

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.dashboard import (
    DashboardReport,
    DashboardEngine,
    TierPerf,
    SportPerf,
    MarketPerf,
    DailyTrend,
)


# ── Shared event loop (matches pattern used by other async test files) ─────────

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def empty_db():
    """Provide an initialised in-memory Database for each test."""
    from database import Database
    db = Database("sqlite+aiosqlite:///:memory:")
    _run(db.init())
    yield db
    _run(db.close())


# ── TierPerf tests ────────────────────────────────────────────────────────────

class TestTierPerf:
    def test_tier_emoji_s(self):
        assert TierPerf(tier="S", count=5).tier_emoji == "🔥"

    def test_tier_emoji_a(self):
        assert TierPerf(tier="A", count=3).tier_emoji == "🟢"

    def test_tier_emoji_b(self):
        assert TierPerf(tier="B", count=2).tier_emoji == "🟡"

    def test_tier_emoji_pass(self):
        assert TierPerf(tier="PASS", count=0).tier_emoji == "⚪"

    def test_tier_emoji_unknown(self):
        assert TierPerf(tier="UNKNOWN", count=1).tier_emoji == "⚪"

    def test_optional_fields_default_none(self):
        t = TierPerf(tier="A", count=2)
        assert t.avg_edge is None
        assert t.avg_clv is None
        assert t.hit_rate is None


# ── SportPerf tests ────────────────────────────────────────────────────────────

class TestSportPerf:
    def test_total_sums_all(self):
        sp = SportPerf(sport="MLB", ev_count=3, ud_count=5, pp_count=2)
        assert sp.total == 10

    def test_total_zero_by_default(self):
        sp = SportPerf(sport="NFL")
        assert sp.total == 0

    def test_avg_ev_optional(self):
        sp = SportPerf(sport="NBA")
        assert sp.avg_ev is None


# ── DailyTrend tests ──────────────────────────────────────────────────────────

class TestDailyTrend:
    def test_total_sum(self):
        dt = DailyTrend(date_str="07/31", ev_count=2, ud_count=5, steam_count=1, pp_count=3)
        assert dt.total == 11

    def test_total_zero_by_default(self):
        dt = DailyTrend(date_str="07/31")
        assert dt.total == 0


# ── DashboardReport tests ─────────────────────────────────────────────────────

class TestDashboardReport:
    def _make_report(self, **kwargs) -> DashboardReport:
        return DashboardReport(**kwargs)

    def test_total_all_alerts_sums_types(self):
        r = DashboardReport(
            total_ev_alerts=10,
            total_steam_alerts=5,
            total_ud_alerts=20,
            total_pp_alerts=15,
        )
        assert r.total_all_alerts == 50

    def test_total_all_alerts_zero_default(self):
        r = DashboardReport()
        assert r.total_all_alerts == 0

    def test_generated_at_is_recent(self):
        before = datetime.utcnow()
        r = DashboardReport()
        # generated_at uses timezone-aware datetime; compare naive
        gen_naive = r.generated_at.replace(tzinfo=None)
        assert gen_naive >= before

    def test_to_telegram_returns_string(self):
        r = DashboardReport()
        msg = r.to_telegram()
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_to_telegram_has_header(self):
        r = DashboardReport()
        msg = r.to_telegram()
        assert "Performance Dashboard" in msg

    def test_to_telegram_has_alerts_section(self):
        r = DashboardReport(total_ev_alerts=7, total_ud_alerts=3)
        msg = r.to_telegram()
        assert "All-Time Alerts" in msg
        assert "7" in msg

    def test_to_telegram_ev_section_with_data(self):
        r = DashboardReport(avg_ev_pct=4.5)
        msg = r.to_telegram()
        assert "EV Alert Performance" in msg
        assert "4.5" in msg

    def test_to_telegram_ev_win_rate(self):
        r = DashboardReport(ev_win_rate=0.62, ev_wins=10, ev_losses=6, ev_pushes=1)
        msg = r.to_telegram()
        assert "62.0%" in msg
        assert "10/6/1" in msg

    def test_to_telegram_no_win_rate_awaiting(self):
        r = DashboardReport(ev_win_rate=None)
        msg = r.to_telegram()
        assert "awaiting resolved results" in msg

    def test_to_telegram_clv_section_no_records(self):
        r = DashboardReport(total_clv_records=0)
        msg = r.to_telegram()
        assert "CLV" in msg
        assert "No CLV records yet" in msg

    def test_to_telegram_clv_section_with_records(self):
        r = DashboardReport(
            total_clv_records=12,
            avg_clv_pct=2.34,
            clv_beat_close_rate=0.75,
        )
        msg = r.to_telegram()
        assert "2.34" in msg
        assert "75%" in msg

    def test_to_telegram_pending_seeds(self):
        r = DashboardReport(clv_seeds_pending=5)
        msg = r.to_telegram()
        assert "5" in msg
        assert "seeds" in msg.lower() or "Pending" in msg

    def test_to_telegram_ud_breakdown_shown(self):
        r = DashboardReport(
            total_ud_alerts=20,
            ud_tier_breakdown={"S": 3, "A": 10, "B": 7},
        )
        msg = r.to_telegram()
        assert "Underdog" in msg

    def test_to_telegram_by_sport(self):
        r = DashboardReport(
            by_sport=[
                SportPerf(sport="MLB", ev_count=10, ud_count=5),
                SportPerf(sport="NBA", ev_count=3),
            ]
        )
        msg = r.to_telegram()
        assert "By Sport" in msg
        assert "MLB" in msg

    def test_to_telegram_by_market(self):
        r = DashboardReport(
            by_market=[
                MarketPerf(market="Player Prop", count=15, avg_ev=3.2),
            ]
        )
        msg = r.to_telegram()
        assert "By Market" in msg
        assert "Player Prop" in msg

    def test_to_telegram_daily_trend(self):
        r = DashboardReport(
            daily_trend=[
                DailyTrend(date_str="07/25", ev_count=2, ud_count=3),
                DailyTrend(date_str="07/26", ev_count=0, ud_count=0),
            ]
        )
        msg = r.to_telegram()
        # Only non-zero days should show
        assert "07/25" in msg

    def test_to_telegram_best_sport_shown(self):
        r = DashboardReport(best_sport="MLB")
        r.by_sport = [SportPerf(sport="MLB", ev_count=5)]
        msg = r.to_telegram()
        assert "MLB" in msg

    def test_to_telegram_positive_clv_has_plus(self):
        r = DashboardReport(total_clv_records=5, avg_clv_pct=1.50)
        msg = r.to_telegram()
        assert "+1.50" in msg

    def test_to_telegram_negative_clv_has_minus(self):
        r = DashboardReport(total_clv_records=5, avg_clv_pct=-0.80)
        msg = r.to_telegram()
        assert "-0.80" in msg

    def test_to_telegram_no_html_unclosed_tags(self):
        """Basic check that opening and closing HTML tags are balanced."""
        r = DashboardReport(
            total_ev_alerts=5,
            total_ud_alerts=10,
            avg_ev_pct=2.1,
            total_clv_records=3,
            avg_clv_pct=1.5,
            clv_beat_close_rate=0.67,
            by_sport=[SportPerf(sport="MLB", ev_count=5)],
            daily_trend=[DailyTrend(date_str="07/31", ev_count=2)],
        )
        msg = r.to_telegram()
        assert msg.count("<b>") == msg.count("</b>")
        assert msg.count("<i>") == msg.count("</i>")
        assert msg.count("<code>") == msg.count("</code>")


# ── DashboardEngine integration tests ─────────────────────────────────────────

class TestDashboardEngineEmpty:
    """DashboardEngine against a completely empty database."""

    def test_gather_returns_report(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert isinstance(report, DashboardReport)

    def test_gather_all_totals_zero(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert report.total_ev_alerts    == 0
        assert report.total_steam_alerts == 0
        assert report.total_ud_alerts    == 0
        assert report.total_pp_alerts    == 0
        assert report.total_clv_records  == 0

    def test_gather_no_clv(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert report.avg_clv_pct is None
        assert report.clv_beat_close_rate is None

    def test_gather_no_ev_performance(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert report.avg_ev_pct is None
        assert report.ev_win_rate is None

    def test_gather_empty_breakdowns(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert report.by_sport   == []
        assert report.by_market  == []
        assert report.ud_tier_breakdown == {}

    def test_gather_daily_trend_seven_days(self, empty_db):
        """Daily trend always produces 7 DailyTrend entries (all zeros on empty DB)."""
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert len(report.daily_trend) == 7

    def test_gather_no_best_worst(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        assert report.best_sport  is None
        assert report.worst_sport is None
        assert report.best_market is None

    def test_to_telegram_safe_on_empty(self, empty_db):
        report = _run(
            DashboardEngine.gather(empty_db)
        )
        msg = report.to_telegram()
        assert "Performance Dashboard" in msg
        assert len(msg) > 50


class TestDashboardEngineWithData:
    """DashboardEngine against a DB with some records."""

    @pytest.fixture()
    def db_with_ev(self, empty_db):
        """DB seeded with a few EV records (alerted=True)."""
        from database import EVRecord
        records = []
        for i, (sport, market, ev, result) in enumerate([
            ("MLB", "Player Prop", 4.5,  "WIN"),
            ("MLB", "Player Prop", 3.2,  "LOSS"),
            ("NBA", "Moneyline",   6.0,  "WIN"),
            ("NBA", "Moneyline",   2.1,  "PENDING"),
            ("NFL", "Spread",      5.5,  None),
        ]):
            r = EVRecord(
                sport=sport,
                market_type=market,
                event=f"Event {i}",
                player=None,
                selection=f"Selection {i}",
                line=None,
                best_odds=-110,
                best_book="DraftKings",
                fair_probability=0.52,
                expected_value=ev,
                steam_score=50,
                ai_confidence=80,
                recommendation="Bet",
                stars=3,
                reason_codes="",
                result=result,
                clv=None,
                alert_sent=True,
                detected_at=datetime.utcnow(),
            )
            records.append(r)
        _run(
            _save_all(empty_db, records)
        )
        return empty_db

    def test_ev_count_matches_saved(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        assert report.total_ev_alerts == 5

    def test_avg_ev_pct_computed(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        # avg of 4.5, 3.2, 6.0, 2.1, 5.5 = 21.3/5 = 4.26
        assert report.avg_ev_pct is not None
        assert abs(report.avg_ev_pct - 4.26) < 0.1

    def test_win_rate_from_resolved(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        # Only WIN (2 total), LOSS (1): actually wins=2 (MLB+NBA), losses=1 (MLB)
        # WIN count: MLB-WIN + NBA-WIN = 2; LOSS count: MLB-LOSS = 1
        # But wait: 1 WIN for MLB and 1 WIN for NBA = 2 wins, 1 loss
        # win_rate = 2/(2+1) = 0.666... but only counted if denom >= 5
        # Since denom = 3 < 5, win_rate should be None
        assert report.ev_win_rate is None  # denom < 5

    def test_by_sport_has_mlb_and_nba(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        sports = {sp.sport for sp in report.by_sport}
        assert "MLB" in sports
        assert "NBA" in sports

    def test_by_market_has_player_prop(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        markets = {m.market for m in report.by_market}
        assert "Player Prop" in markets

    def test_daily_trend_has_today(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        # Today should have 5 EV records
        today = report.daily_trend[-1]
        assert today.ev_count == 5

    def test_to_telegram_includes_sport_data(self, db_with_ev):
        report = _run(
            DashboardEngine.gather(db_with_ev)
        )
        msg = report.to_telegram()
        assert "MLB" in msg or "NBA" in msg


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _save_all(db, records):
    for r in records:
        await db.save_ev(r)
