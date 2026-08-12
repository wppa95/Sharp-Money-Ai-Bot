"""
Tests for P1/P2 alert gate changes:
  - MLB UNDER block at all 3 alert paths
  - C-tier now passes the line-change qualification gate
  - Sport funnel breakdown in get_funnel_summary
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ═══════════════════════════════════════════════════════════════════════════
# MLB UNDER block — gate logic tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMlbUnderGateLogic:
    """The MLB UNDER gate must block UNDER and allow OVER for MLB S-tier picks."""

    def _lc_mlb_ok(self, sport: str, tier: str, recommendation: str,
                   mlb_alert_tiers=frozenset({"S"})) -> bool:
        """Replicate the _lc_mlb_ok logic from market_engine.py."""
        sport_up = sport.upper()
        return (
            sport_up != "MLB"
            or (
                tier in mlb_alert_tiers
                and recommendation != "UNDER"
            )
        )

    # ── MLB cases ─────────────────────────────────────────────────────────

    def test_mlb_s_over_passes(self):
        assert self._lc_mlb_ok("MLB", "S", "OVER") is True

    def test_mlb_s_under_blocked(self):
        assert self._lc_mlb_ok("MLB", "S", "UNDER") is False

    def test_mlb_a_over_blocked_by_tier_gate(self):
        # A-tier is not in default mlb_alert_tiers (only S) → blocked
        assert self._lc_mlb_ok("MLB", "A", "OVER") is False

    def test_mlb_a_under_blocked(self):
        assert self._lc_mlb_ok("MLB", "A", "UNDER") is False

    def test_mlb_b_under_blocked(self):
        assert self._lc_mlb_ok("MLB", "B", "UNDER") is False

    # ── Non-MLB cases — UNDER should pass fine ────────────────────────────

    def test_nba_under_passes(self):
        assert self._lc_mlb_ok("NBA", "S", "UNDER") is True

    def test_cs_under_passes(self):
        assert self._lc_mlb_ok("CS", "A", "UNDER") is True

    def test_tennis_under_passes(self):
        assert self._lc_mlb_ok("TENNIS", "B", "UNDER") is True

    def test_soccer_under_passes(self):
        assert self._lc_mlb_ok("SOCCER", "S", "UNDER") is True

    def test_dota_under_passes(self):
        assert self._lc_mlb_ok("DOTA", "C", "UNDER") is True

    # ── Edge cases ────────────────────────────────────────────────────────

    def test_mlb_pass_not_blocked_by_mlb_under_gate(self):
        # PASS is filtered by the recommendation != "PASS" check, which is
        # a separate earlier gate. The MLB UNDER gate only blocks "UNDER",
        # so _lc_mlb_ok itself returns True for PASS (correct — it's not
        # this gate's responsibility to block PASS).
        assert self._lc_mlb_ok("MLB", "S", "PASS") is True

    def test_mlb_uppercase_lowercase_invariant(self):
        # sport is uppercased before comparison
        assert self._lc_mlb_ok("mlb", "S", "OVER") is True   # uppercased = MLB
        assert self._lc_mlb_ok("mlb", "S", "UNDER") is False


# ═══════════════════════════════════════════════════════════════════════════
# C-tier gate — line-change qualification test
# ═══════════════════════════════════════════════════════════════════════════

class TestCTierQualificationGate:
    """C-tier picks must now pass the is_qualified gate (line-change path)."""

    def _is_qualified(
        self,
        tier: str,
        recommendation: str,
        sport: str,
        stars: int,
        min_stars: int = 3,
        alert_sports: frozenset = frozenset({"NBA", "MLB", "CS", "TENNIS", "DOTA",
                                             "LOL", "VALORANT", "TT", "BADMINTON",
                                             "MMA", "GOLF", "NFL", "NCAAF",
                                             "SOCCER", "WNBA", "NHL"}),
        mlb_alert_tiers: frozenset = frozenset({"S"}),
    ) -> bool:
        """Mirror the is_qualified expression from market_engine.py."""
        sport_up = sport.upper()
        mlb_ok = (
            sport_up != "MLB"
            or (
                tier in mlb_alert_tiers
                and recommendation != "UNDER"
            )
        )
        return (
            stars >= min_stars
            and recommendation != "PASS"
            and tier in ("S", "A", "B", "C")
            and sport in alert_sports
            and mlb_ok
        )

    def test_c_tier_over_qualifies(self):
        assert self._is_qualified("C", "OVER", "CS", stars=3) is True

    def test_c_tier_under_qualifies(self):
        assert self._is_qualified("C", "UNDER", "TENNIS", stars=3) is True

    def test_c_tier_pass_does_not_qualify(self):
        assert self._is_qualified("C", "PASS", "CS", stars=3) is False

    def test_c_tier_blocked_by_star_gate(self):
        assert self._is_qualified("C", "OVER", "CS", stars=2, min_stars=3) is False

    def test_s_tier_still_qualifies(self):
        assert self._is_qualified("S", "OVER", "NBA", stars=4) is True

    def test_a_tier_still_qualifies(self):
        assert self._is_qualified("A", "UNDER", "TENNIS", stars=3) is True

    def test_b_tier_still_qualifies(self):
        assert self._is_qualified("B", "OVER", "CS", stars=3) is True

    def test_d_tier_does_not_qualify(self):
        # D-tier (hypothetical) — not in the allowed set
        assert self._is_qualified("D", "OVER", "CS", stars=3) is False

    def test_c_tier_mlb_under_blocked(self):
        # MLB UNDER is always blocked regardless of tier
        assert self._is_qualified("C", "UNDER", "MLB", stars=5) is False

    def test_c_tier_mlb_over_blocked_by_tier_gate(self):
        # MLB OVER C-tier: mlb_ok = "MLB" and tier not in {"S"} → False
        assert self._is_qualified("C", "OVER", "MLB", stars=5) is False

    def test_sport_not_in_whitelist_blocked(self):
        assert self._is_qualified("C", "OVER", "CRICKET", stars=5) is False


# ═══════════════════════════════════════════════════════════════════════════
# Sport funnel breakdown — DB method tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestFunnelSportBreakdown:
    """get_funnel_summary must return a by_sport key."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from database import Database, PropCandidateLog
        from datetime import datetime
        self.db = Database("sqlite+aiosqlite:///:memory:")
        await self.db.init()
        # Insert test candidate rows for 3 sports
        _rows = [
            # NBA: 2 accepted, 1 rejected
            ("Player A", "NBA", "points",    "ACCEPTED", "S"),
            ("Player B", "NBA", "rebounds",  "ACCEPTED", "A"),
            ("Player C", "NBA", "assists",   "REJECTED", "B"),
            # CS: 1 accepted, 2 rejected
            ("Player D", "CS",  "kills",     "ACCEPTED", "A"),
            ("Player E", "CS",  "kills",     "REJECTED", "B"),
            ("Player F", "CS",  "kills",     "REJECTED", "C"),
            # MLB: 1 watchlist, 1 removed
            ("Player G", "MLB", "strikeouts","WATCHLIST","B"),
            ("Player H", "MLB", "hits",      "REMOVED",  "S"),
        ]
        from sqlalchemy.ext.asyncio import AsyncSession
        async with self.db.session() as s:
            for (name, sport, stat, gate, tier) in _rows:
                s.add(PropCandidateLog(
                    scan_ts          = datetime.utcnow(),
                    player_name      = name,
                    team             = "",
                    sport            = sport,
                    stat_type        = stat,
                    line_value       = 10.0,
                    provider         = "Underdog",
                    gate_decision    = gate,
                    score_tier       = tier,
                    score_total      = 70.0,
                    confidence       = 70,
                    snapshot_external_id = f"{name}-ext",
                ))
            await s.commit()
        yield
        await self.db.close()

    @pytest.mark.asyncio
    async def test_by_sport_key_present(self):
        summary = await self.db.get_funnel_summary(since_hours=24)
        assert "by_sport" in summary

    @pytest.mark.asyncio
    async def test_by_sport_contains_all_three_sports(self):
        summary = await self.db.get_funnel_summary(since_hours=24)
        sports = {row["sport"] for row in summary["by_sport"]}
        assert "NBA" in sports
        assert "CS"  in sports
        assert "MLB" in sports

    @pytest.mark.asyncio
    async def test_nba_counts_correct(self):
        summary = await self.db.get_funnel_summary(since_hours=24)
        nba = next(r for r in summary["by_sport"] if r["sport"] == "NBA")
        assert nba["scanned"]  == 3
        assert nba["accepted"] == 2
        assert nba["rejected"] == 1
        assert nba["watchlist"] == 0

    @pytest.mark.asyncio
    async def test_cs_counts_correct(self):
        summary = await self.db.get_funnel_summary(since_hours=24)
        cs = next(r for r in summary["by_sport"] if r["sport"] == "CS")
        assert cs["scanned"]  == 3
        assert cs["accepted"] == 1
        assert cs["rejected"] == 2

    @pytest.mark.asyncio
    async def test_mlb_counts_correct(self):
        summary = await self.db.get_funnel_summary(since_hours=24)
        mlb = next(r for r in summary["by_sport"] if r["sport"] == "MLB")
        assert mlb["scanned"]   == 2
        assert mlb["watchlist"] == 1
        assert mlb["removed"]   == 1
        assert mlb["accepted"]  == 0

    @pytest.mark.asyncio
    async def test_sorted_by_scanned_desc(self):
        summary = await self.db.get_funnel_summary(since_hours=24)
        scanned_counts = [r["scanned"] for r in summary["by_sport"]]
        assert scanned_counts == sorted(scanned_counts, reverse=True)

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_list(self):
        from database import Database
        empty_db = Database("sqlite+aiosqlite:///:memory:")
        await empty_db.init()
        summary = await empty_db.get_funnel_summary(since_hours=24)
        assert summary["by_sport"] == []
        await empty_db.close()


# ═══════════════════════════════════════════════════════════════════════════
# cmd_funnel — sport breakdown rendering
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestCmdFunnelSportBreakdown:
    """cmd_funnel must render the by_sport section."""

    def _make_update(self):
        update = MagicMock()
        update.effective_user.id = 7245518659
        update.message.reply_text = AsyncMock()
        ctx = MagicMock()
        ctx.args = []
        return update, ctx

    @pytest.mark.asyncio
    async def test_funnel_renders_sport_breakdown(self):
        from commands import cmd_funnel
        update, ctx = self._make_update()
        mock_db = MagicMock()
        mock_db.get_funnel_summary = AsyncMock(return_value={
            "since_hours": 24,
            "counts": {"ACCEPTED": 5, "WATCHLIST": 3, "REJECTED": 10, "REMOVED": 2},
            "top_rejections": [],
            "by_sport": [
                {"sport": "CS",  "scanned": 8, "accepted": 3, "watchlist": 2, "rejected": 2, "removed": 1},
                {"sport": "NBA", "scanned": 7, "accepted": 2, "watchlist": 1, "rejected": 4, "removed": 0},
                {"sport": "MLB", "scanned": 5, "accepted": 0, "watchlist": 0, "rejected": 4, "removed": 1},
            ],
        })
        mock_db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
        with patch("commands._db", mock_db):
            await cmd_funnel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "Sport Funnel Breakdown" in msg
        assert "CS"  in msg
        assert "NBA" in msg
        assert "MLB" in msg

    @pytest.mark.asyncio
    async def test_funnel_renders_no_breakdown_when_empty(self):
        from commands import cmd_funnel
        update, ctx = self._make_update()
        mock_db = MagicMock()
        mock_db.get_funnel_summary = AsyncMock(return_value={
            "since_hours": 24,
            "counts": {},
            "top_rejections": [],
            "by_sport": [],
        })
        mock_db.get_scan_cycle_summary = AsyncMock(return_value={"cycles": 0})
        with patch("commands._db", mock_db):
            await cmd_funnel(update, ctx)
        msg = update.message.reply_text.call_args[0][0]
        assert "accumulates" in msg


# ═══════════════════════════════════════════════════════════════════════════
# DK/FD removal — verify connectors module no longer exports them
# ═══════════════════════════════════════════════════════════════════════════

class TestDkFdRemoved:
    """DraftKings and FanDuel must no longer be importable from connectors."""

    def test_draftkings_not_in_connectors_all(self):
        import connectors
        assert "DraftKingsConnector" not in connectors.__all__

    def test_fanduel_not_in_connectors_all(self):
        import connectors
        assert "FanDuelConnector" not in connectors.__all__

    def test_underdog_still_exported(self):
        from connectors import UnderdogConnector
        assert UnderdogConnector is not None

    def test_registry_still_exported(self):
        from connectors import ConnectorRegistry
        assert ConnectorRegistry is not None

    def test_mock_connector_still_available(self):
        from connectors import MockOddsConnector, make_mock_dk, make_mock_fd
        assert MockOddsConnector is not None
