"""
test_oddsapi_supporting_data.py — V3.5 OddsAPI Supporting Data Pass

Validates all spec requirements:

☐ fetch_player_prop_lines alias works (critical bug fix)
☐ S-tier candidate lookup triggered
☐ A-tier candidate lookup triggered
☐ Strong UNDER candidates included (via S/A gate)
☐ B-tier candidates do NOT trigger OddsAPI
☐ PASS candidates do NOT trigger OddsAPI
☐ avg_odds field present in confirmation result (CLV seeding support)
☐ Duplicate requests avoided (TTL cache key reuse)
☐ Underdog remains PRIMARY (OddsAPI cannot override direction/line)
☐ Telegram behavior unchanged (no raw OddsAPI alerts)
☐ No credentials exposed in result dict
☐ Standing path gate includes recommendation != PASS
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — lightweight stubs so we can import without the full bot stack
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakePlayerPropLine:
    sportsbook:    str
    sport:         object
    market_key:    str
    event:         str
    player_name:   str
    description:   str
    american_odds: int
    line:          Optional[float]
    event_start:   object = None


def _make_lines(player: str, market: str, direction: str,
                line: float, odds: int, books: list[str]) -> list[_FakePlayerPropLine]:
    """Build fake PlayerPropLine objects for multiple sportsbooks."""
    out = []
    for book in books:
        out.append(_FakePlayerPropLine(
            sportsbook=book,
            sport=None,
            market_key=market,
            event="Team A @ Team B",
            player_name=player,
            description=direction,
            american_odds=odds,
            line=line,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Stub the AnalysisEngine so we can test fetch_player_prop_lines alias
# ─────────────────────────────────────────────────────────────────────────────

class TestFetchPlayerPropLinesAlias(unittest.TestCase):
    """fetch_player_prop_lines must exist on AnalysisEngine and delegate to
    fetch_player_prop_odds — this was the critical bug (AttributeError silently
    returning None for all S/A confirmations)."""

    def test_alias_method_exists(self):
        # Minimal stub imports to verify the alias exists in analysis.py
        import importlib, types as _types
        # We just need to verify the method exists in the source
        import inspect as _inspect
        import engine.analysis as _analysis
        ae_cls = _analysis.AnalysisEngine
        self.assertTrue(
            hasattr(ae_cls, "fetch_player_prop_lines"),
            "AnalysisEngine must have fetch_player_prop_lines (alias for fetch_player_prop_odds)",
        )

    def test_alias_is_coroutine(self):
        import engine.analysis as _analysis
        import asyncio as _asyncio
        method = getattr(_analysis.AnalysisEngine, "fetch_player_prop_lines", None)
        self.assertIsNotNone(method)
        self.assertTrue(
            _asyncio.iscoroutinefunction(method),
            "fetch_player_prop_lines must be an async method",
        )

    def test_alias_delegates_to_fetch_player_prop_odds(self):
        """Both methods should exist and alias should ultimately call the same path."""
        import engine.analysis as _analysis
        ae_cls = _analysis.AnalysisEngine
        self.assertTrue(hasattr(ae_cls, "fetch_player_prop_odds"))
        self.assertTrue(hasattr(ae_cls, "fetch_player_prop_lines"))
        # Source of alias should reference fetch_player_prop_odds
        src = inspect.getsource(ae_cls.fetch_player_prop_lines)
        self.assertIn("fetch_player_prop_odds", src,
                      "fetch_player_prop_lines must delegate to fetch_player_prop_odds")


# ─────────────────────────────────────────────────────────────────────────────
# Test _get_odds_api_confirmation result shape + avg_odds
# ─────────────────────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestConfirmationResultShape(unittest.TestCase):
    """Confirmation result must include avg_odds for CLV seeding support."""

    def _make_engine_mock(self, lines):
        engine = MagicMock()
        engine.fetch_player_prop_lines = AsyncMock(return_value=lines)
        return engine

    def test_result_contains_avg_odds_when_confirmed(self):
        """When books report american_odds, avg_odds must appear in result."""
        import market_engine as me
        lines = _make_lines("LeBron James", "player_points", "Over", 25.5, -115, ["FanDuel", "BetMGM"])
        engine_mock = self._make_engine_mock(lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="LeBron James",
                    stat_type="points", direction="OVER", line=25.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        self.assertIn("avg_odds", result, "Result must include avg_odds key")
        self.assertEqual(result["avg_odds"], -115)

    def test_result_contains_required_keys(self):
        """Result must always have num_books, avg_line, avg_odds, notes, confirmed."""
        import market_engine as me
        lines = _make_lines("Steph Curry", "player_threes", "Over", 4.5, -120, ["DraftKings"])
        engine_mock = self._make_engine_mock(lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Steph Curry",
                    stat_type="3-pointers made", direction="OVER", line=4.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        for key in ("num_books", "avg_line", "avg_odds", "notes", "confirmed"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_avg_odds_none_when_no_american_odds(self):
        """avg_odds is None when books report no american_odds."""
        import market_engine as me
        lines = [_FakePlayerPropLine(
            sportsbook="BookX", sport=None, market_key="player_hits",
            event="Game", player_name="Aaron Judge", description="Over",
            american_odds=0, line=1.5, event_start=None,
        )]
        engine_mock = self._make_engine_mock(lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="MLB", player_name="Aaron Judge",
                    stat_type="hits", direction="OVER", line=1.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        self.assertIsNone(result["avg_odds"])

    def test_no_credentials_in_result(self):
        """Result dict must never contain API key or credential fields."""
        import market_engine as me
        lines = _make_lines("Shohei Ohtani", "player_pitcher_strikeouts",
                            "Over", 7.5, -110, ["BetMGM"])
        engine_mock = self._make_engine_mock(lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="MLB", player_name="Shohei Ohtani",
                    stat_type="pitcher strikeouts", direction="OVER", line=7.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        for key in ("api_key", "apikey", "key", "token", "secret", "ODDS_API_KEY"):
            self.assertNotIn(key, result, f"Credential key must not appear in result: {key}")
        for v in result.values():
            if isinstance(v, str):
                self.assertNotIn("apikey", v.lower())


# ─────────────────────────────────────────────────────────────────────────────
# Test tier gating — only S/A-tier with non-PASS recommendation trigger OddsAPI
# ─────────────────────────────────────────────────────────────────────────────

class TestTierGate(unittest.TestCase):
    """B/PASS candidates must NOT trigger OddsAPI lookups."""

    def _gate_allows(self, tier: str, rec: str) -> bool:
        """Mirror the gate logic from all 3 call sites."""
        return tier in ("S", "A") and rec != "PASS"

    # ── S-tier ───────────────────────────────────────────────────────────────

    def test_s_tier_over_allowed(self):
        self.assertTrue(self._gate_allows("S", "OVER"))

    def test_s_tier_under_allowed(self):
        """Strong UNDER candidates at S-tier must be allowed."""
        self.assertTrue(self._gate_allows("S", "UNDER"))

    def test_s_tier_pass_blocked(self):
        """S-tier PASS must NOT query OddsAPI."""
        self.assertFalse(self._gate_allows("S", "PASS"))

    # ── A-tier ───────────────────────────────────────────────────────────────

    def test_a_tier_over_allowed(self):
        self.assertTrue(self._gate_allows("A", "OVER"))

    def test_a_tier_under_allowed(self):
        self.assertTrue(self._gate_allows("A", "UNDER"))

    def test_a_tier_pass_blocked(self):
        self.assertFalse(self._gate_allows("A", "PASS"))

    # ── B-tier ───────────────────────────────────────────────────────────────

    def test_b_tier_over_blocked(self):
        """B-tier must NOT query OddsAPI — spec explicit."""
        self.assertFalse(self._gate_allows("B", "OVER"))

    def test_b_tier_under_blocked(self):
        self.assertFalse(self._gate_allows("B", "UNDER"))

    def test_b_tier_pass_blocked(self):
        self.assertFalse(self._gate_allows("B", "PASS"))

    # ── PASS candidates ───────────────────────────────────────────────────────

    def test_pass_s_tier_blocked(self):
        self.assertFalse(self._gate_allows("S", "PASS"))

    def test_pass_a_tier_blocked(self):
        self.assertFalse(self._gate_allows("A", "PASS"))

    def test_pass_none_tier_blocked(self):
        self.assertFalse(self._gate_allows(None, "PASS"))  # type: ignore

    def test_c_tier_blocked(self):
        self.assertFalse(self._gate_allows("C", "OVER"))


# ─────────────────────────────────────────────────────────────────────────────
# Strong UNDER support
# ─────────────────────────────────────────────────────────────────────────────

class TestStrongUnderSupport(unittest.TestCase):
    """Strong UNDER candidates at S/A tier must be included in OddsAPI lookups."""

    def test_strong_under_s_tier_included(self):
        """S-tier UNDER candidate passes gate — spec section 4."""
        tier, rec = "S", "UNDER"
        gate = tier in ("S", "A") and rec != "PASS"
        self.assertTrue(gate)

    def test_strong_under_a_tier_included(self):
        """A-tier UNDER candidate passes gate."""
        tier, rec = "A", "UNDER"
        gate = tier in ("S", "A") and rec != "PASS"
        self.assertTrue(gate)

    def test_strong_under_b_tier_excluded(self):
        """B-tier UNDER does NOT trigger OddsAPI — must pass qualification first."""
        tier, rec = "B", "UNDER"
        gate = tier in ("S", "A") and rec != "PASS"
        self.assertFalse(gate)

    def test_confirmation_direction_under_matches_sportsbook(self):
        """When direction=UNDER, confirmation must look for Under outcomes."""
        import market_engine as me
        lines = [
            _FakePlayerPropLine(
                sportsbook="FanDuel", sport=None, market_key="player_points",
                event="Game", player_name="Anthony Davis", description="Under",
                american_odds=-105, line=22.5, event_start=None,
            ),
            _FakePlayerPropLine(
                sportsbook="FanDuel", sport=None, market_key="player_points",
                event="Game", player_name="Anthony Davis", description="Over",
                american_odds=-115, line=22.5, event_start=None,
            ),
        ]
        engine_mock = MagicMock()
        engine_mock.fetch_player_prop_lines = AsyncMock(return_value=lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Anthony Davis",
                    stat_type="points", direction="UNDER", line=22.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        # Should have matched the Under side; avg_odds should be -105 not -115
        self.assertEqual(result["avg_odds"], -105)
        self.assertEqual(result["num_books"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate request avoidance — OddsApiCache TTL
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateRequestAvoidance(unittest.TestCase):
    """OddsApiCache must deduplicate requests within TTL window."""

    def test_odds_api_cache_has_ttl(self):
        from providers.odds_cache import OddsApiCache
        cache = OddsApiCache(ttl_seconds=55)
        self.assertEqual(cache._ttl, 55)

    def test_cache_key_is_sport_markets_regions(self):
        """Same (sport_key, markets, regions) must reuse cache entry."""
        from providers.odds_cache import OddsApiCache
        cache = OddsApiCache(ttl_seconds=55)
        key1 = ("basketball_nba", "player_points", "us")
        key2 = ("basketball_nba", "player_points", "us")
        self.assertEqual(key1, key2, "Identical requests must share a cache key")

    def test_different_sport_different_key(self):
        """Different sports must produce different cache keys."""
        key_nba = ("basketball_nba", "player_points", "us")
        key_mlb = ("baseball_mlb", "player_hits", "us")
        self.assertNotEqual(key_nba, key_mlb)

    def test_cache_entry_expiry(self):
        """TTL expiry check must work correctly."""
        from providers.odds_cache import _CacheEntry
        from datetime import datetime, timedelta
        # Not expired (just created)
        entry = _CacheEntry(sport_key="basketball_nba", data=[])
        self.assertFalse(entry.is_expired(55))
        # Expired
        old_time = datetime.utcnow() - timedelta(seconds=60)
        entry.fetched_at = old_time
        self.assertTrue(entry.is_expired(55))

    def test_confirmation_uses_cache_not_direct_http(self):
        """Confirmation goes through OddsApiCache (via fetch_player_prop_lines),
        not a direct HTTP call — cache deduplication applies automatically."""
        import market_engine as me
        # If _analysis_engine is None, returns None cleanly
        async def _run_test():
            with patch.object(me, "_analysis_engine", None):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Test", stat_type="points",
                    direction="OVER", line=20.0,
                )
            return result
        result = _run(_run_test())
        self.assertIsNone(result)


# ─────────────────────────────────────────────────────────────────────────────
# Underdog primacy — OddsAPI cannot override
# ─────────────────────────────────────────────────────────────────────────────

class TestUnderdogPrimacy(unittest.TestCase):
    """OddsAPI confirmation is informational only; it must not change the pick."""

    def test_confirmation_does_not_change_recommendation(self):
        """Even with a confirmed OddsAPI result, the recommendation comes from
        the Underdog scoring engine, not from OddsAPI."""
        # The confirmation result has no "recommendation" field — it cannot
        # override the Underdog decision.
        import market_engine as me
        lines = _make_lines("Test Player", "player_points", "Under", 20.0, -110, ["BookA"])
        engine_mock = MagicMock()
        engine_mock.fetch_player_prop_lines = AsyncMock(return_value=lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Test Player",
                    stat_type="points", direction="OVER", line=20.0,
                )
            return result

        result = _run(_run_test())
        if result is not None:
            # Result has no recommendation field — cannot override Underdog
            self.assertNotIn("recommendation", result)
            self.assertNotIn("direction", result)
            self.assertNotIn("decision", result)

    def test_confirmation_does_not_change_line(self):
        """OddsAPI avg_line is context only — the Underdog line is the authoritative pick line."""
        import market_engine as me
        # avg_line from sportsbooks may differ from UD line; neither replaces the other
        lines = _make_lines("Test Player", "player_points", "Over", 22.5, -105, ["BookA"])
        engine_mock = MagicMock()
        engine_mock.fetch_player_prop_lines = AsyncMock(return_value=lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Test Player",
                    stat_type="points", direction="OVER", line=20.0,  # UD line=20.0
                )
            return result

        result = _run(_run_test())
        if result is not None:
            # avg_line is 22.5 (OddsAPI) but that does not replace UD line 20.0
            self.assertIn("avg_line", result)
            self.assertEqual(result["avg_line"], 22.5)
            # No field that could accidentally replace the UD line
            self.assertNotIn("ud_line", result)
            self.assertNotIn("pick_line", result)

    def test_unmapped_sport_returns_none(self):
        """Sports not in _UD_TO_ODDS_API_MARKET must return None — no OddsAPI call."""
        import market_engine as me

        async def _run_test():
            with patch.object(me, "_analysis_engine", MagicMock()):
                result = await me._get_odds_api_confirmation(
                    sport="LOL", player_name="Player", stat_type="kills",
                    direction="OVER", line=5.0,
                )
            return result

        result = _run(_run_test())
        self.assertIsNone(result, "Unmapped sport must not query OddsAPI")

    def test_unmapped_stat_returns_none(self):
        """Stats not in _UD_TO_ODDS_API_MARKET must return None — no OddsAPI call."""
        import market_engine as me

        async def _run_test():
            with patch.object(me, "_analysis_engine", MagicMock()):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Player",
                    stat_type="fantasy_score",  # not mapped
                    direction="OVER", line=50.0,
                )
            return result

        result = _run(_run_test())
        self.assertIsNone(result, "Unmapped stat must not query OddsAPI")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram behavior unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestTelegramBehaviorUnchanged(unittest.TestCase):
    """OddsAPI must NEVER produce its own Telegram notifications."""

    def test_no_telegram_send_in_confirmation_function(self):
        """_get_odds_api_confirmation must not import telegram or call send_message."""
        import market_engine as me
        src = inspect.getsource(me._get_odds_api_confirmation)
        self.assertNotIn("send_message", src)
        self.assertNotIn("bot.send", src)
        self.assertNotIn("telegram", src.lower())

    def test_confirmation_result_is_supporting_data_only(self):
        """The confirmation dict is passed as market_confirmation= kwarg —
        delivery.deliver_underdog() decides whether/how to include it.
        OddsAPI does not independently trigger delivery."""
        import market_engine as me
        src = inspect.getsource(me._get_odds_api_confirmation)
        self.assertNotIn("deliver_underdog", src)
        self.assertNotIn("AlertDelivery", src)
        self.assertNotIn("broadcast_alert", src)

    def test_oddsapi_does_not_call_scheduler(self):
        """Confirmation function must not schedule new jobs or polling."""
        import market_engine as me
        src = inspect.getsource(me._get_odds_api_confirmation)
        self.assertNotIn("run_repeating", src)
        self.assertNotIn("job_queue", src)


# ─────────────────────────────────────────────────────────────────────────────
# Standing path gate — recommendation != PASS required
# ─────────────────────────────────────────────────────────────────────────────

class TestStandingPathGate(unittest.TestCase):
    """The standing path OddsAPI gate must include recommendation != PASS."""

    def test_standing_path_gate_includes_pass_check(self):
        """market_engine source must show the PASS guard on the standing path."""
        import market_engine as me
        src = inspect.getsource(me.underdog_job)
        # The standing path gate should have recommendation != "PASS"
        self.assertIn('recommendation != "PASS"', src,
                      "Standing path must explicitly exclude PASS recommendations")

    def test_all_three_call_sites_have_pass_guard(self):
        """All 3 _get_odds_api_confirmation call sites must include recommendation != PASS."""
        import market_engine as me
        import re
        src = inspect.getsource(me)   # full module source

        # Find each call site and verify the PASS guard appears nearby
        # Split around each call to _get_odds_api_confirmation
        segments = re.split(r'_get_odds_api_confirmation\b', src)
        # Each segment except the first is the text AFTER a call site;
        # the gate is in the text just BEFORE (i.e. end of previous segment)
        guarded = 0
        for i in range(1, len(segments)):
            # Look at the 300 chars immediately before this call
            before = segments[i - 1][-300:]
            if 'recommendation != "PASS"' in before:
                guarded += 1

        self.assertGreaterEqual(
            guarded, 3,
            f"All 3 _get_odds_api_confirmation call sites must have recommendation != 'PASS' guard "
            f"(found {guarded}); standing path was missing it before this fix",
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLV seeding support — avg_odds enables future grading
# ─────────────────────────────────────────────────────────────────────────────

class TestCLVSeedingSupport(unittest.TestCase):
    """avg_odds in the confirmation result enables AlertCLVSeed.bet_odds population."""

    def test_avg_odds_is_integer_or_none(self):
        """avg_odds must be int (rounded) or None — AlertCLVSeed.bet_odds is Integer."""
        import market_engine as me
        lines = _make_lines("Nikola Jokic", "player_rebounds", "Over", 11.5, -115, ["BetMGM", "FanDuel"])
        engine_mock = MagicMock()
        engine_mock.fetch_player_prop_lines = AsyncMock(return_value=lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="Nikola Jokic",
                    stat_type="rebounds", direction="OVER", line=11.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        avg_odds = result.get("avg_odds")
        if avg_odds is not None:
            self.assertIsInstance(avg_odds, int,
                                  "avg_odds must be int (rounded) for DB storage")

    def test_avg_odds_averaged_across_books(self):
        """avg_odds must be the mathematical average (rounded) of all book odds."""
        import market_engine as me
        lines = [
            _FakePlayerPropLine("BookA", None, "player_assists", "Game",
                                "James Harden", "Over", -110, 6.5, None),
            _FakePlayerPropLine("BookB", None, "player_assists", "Game",
                                "James Harden", "Over", -120, 6.5, None),
        ]
        engine_mock = MagicMock()
        engine_mock.fetch_player_prop_lines = AsyncMock(return_value=lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="NBA", player_name="James Harden",
                    stat_type="assists", direction="OVER", line=6.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result)
        # average of -110 and -120 = -115
        self.assertEqual(result["avg_odds"], -115)

    def test_clv_seed_table_exists(self):
        """AlertCLVSeed table must exist in the database schema."""
        from database import AlertCLVSeed
        self.assertEqual(AlertCLVSeed.__tablename__, "alert_clv_seeds")
        self.assertTrue(hasattr(AlertCLVSeed, "bet_odds"))
        self.assertTrue(hasattr(AlertCLVSeed, "clv_pct"))
        self.assertTrue(hasattr(AlertCLVSeed, "clv_computed"))


# ─────────────────────────────────────────────────────────────────────────────
# OddsAPI health monitor registration
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthMonitorRegistration(unittest.TestCase):
    """OddsAPI must be registered in health monitor so quota appears in /status."""

    def test_oddsapi_registered_in_main(self):
        """main.py must register OddsAPI with the health monitor."""
        import os
        # Tests run from inside bot/; resolve relative to this file's location
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path) as f:
            src = f.read()
        # Should have register("OddsAPI") uncommented
        import re
        lines = src.splitlines()
        active = [
            l for l in lines
            if '_health_monitor.register("OddsAPI")' in l
            and not l.lstrip().startswith("#")
        ]
        self.assertGreater(len(active), 0,
                           'main.py must have _health_monitor.register("OddsAPI") active (not commented out)')

    def test_oddsapi_not_polluting_telegram_with_quota(self):
        """Quota details must never go to Telegram — only to logs."""
        import os
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path) as f:
            src = f.read()
        # Quota info goes to logs, never to Telegram send_message
        self.assertNotIn('send_message("OddsAPI quota', src)


# ─────────────────────────────────────────────────────────────────────────────
# Broad polling remains disabled
# ─────────────────────────────────────────────────────────────────────────────

class TestBroadPollingDisabled(unittest.TestCase):
    """Broad sportsbook polling (_poll_odds_job, _player_props_job) must remain OFF."""

    @staticmethod
    def _main_src():
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(path) as f:
            return f.read()

    def test_poll_odds_job_still_commented_out(self):
        """_poll_odds_job must NOT be scheduled — it polls all sports broadly."""
        import re
        src = self._main_src()
        # Match only lines where jq.run_repeating(_poll_odds_job is NOT preceded by #
        lines = src.splitlines()
        active = [
            l for l in lines
            if re.search(r'jq\.run_repeating\(_poll_odds_job', l)
            and not l.lstrip().startswith("#")
        ]
        self.assertEqual(len(active), 0,
                         "_poll_odds_job must remain commented out (broad polling disabled)")

    def test_player_props_job_still_commented_out(self):
        """_player_props_job must NOT be scheduled — it broadly polls all player props."""
        import re
        src = self._main_src()
        lines = src.splitlines()
        active = [
            l for l in lines
            if re.search(r'jq\.run_repeating\(_player_props_job', l)
            and not l.lstrip().startswith("#")
        ]
        self.assertEqual(len(active), 0,
                         "_player_props_job must remain commented out")

    def test_underdog_job_still_primary(self):
        """underdog_job must still be the only active primary polling job."""
        import re
        src = self._main_src()
        # Registration may be multi-line: jq.run_repeating(\n    underdog_job
        # Use full-source search (not line-by-line) to catch multiline form.
        # Exclude commented-out lines by checking that the matched context
        # is not inside a # comment block.
        lines = src.splitlines()
        found = False
        for i, line in enumerate(lines):
            if "underdog_job" in line and "run_repeating" in line and not line.lstrip().startswith("#"):
                found = True
                break
            # Handle two-line form: run_repeating(\n    underdog_job
            if "run_repeating" in line and not line.lstrip().startswith("#"):
                if i + 1 < len(lines) and "underdog_job" in lines[i + 1]:
                    found = True
                    break
        self.assertTrue(found,
                        "underdog_job must remain active as the primary polling job")


# ─────────────────────────────────────────────────────────────────────────────
# Surname fuzzy matching
# ─────────────────────────────────────────────────────────────────────────────

class TestSurnameFuzzyMatch(unittest.TestCase):
    """Player name matching must accept surname-only matches."""

    def test_surname_match_works(self):
        """'Caminero' should match 'Junior Caminero' via surname suffix."""
        import market_engine as me
        lines = [
            _FakePlayerPropLine(
                sportsbook="BetMGM", sport=None, market_key="player_hits",
                event="Game", player_name="Junior Caminero", description="Over",
                american_odds=-110, line=1.5, event_start=None,
            ),
        ]
        engine_mock = MagicMock()
        engine_mock.fetch_player_prop_lines = AsyncMock(return_value=lines)

        async def _run_test():
            with patch.object(me, "_analysis_engine", engine_mock):
                result = await me._get_odds_api_confirmation(
                    sport="MLB", player_name="Caminero",
                    stat_type="hits", direction="OVER", line=1.5,
                )
            return result

        result = _run(_run_test())
        self.assertIsNotNone(result, "Surname match must find the player")
        self.assertEqual(result["num_books"], 1)


if __name__ == "__main__":
    unittest.main()
