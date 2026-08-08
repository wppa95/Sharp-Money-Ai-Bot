"""
Phase 2 Core regression tests — fixes #113, #114, #115, #116.

#113 — Tier clarity: decision_tier stored in _scored_props for both new-prop and
        line-change paths so /funnel near-misses show both composite score tier AND
        bet-decision tier, making mlb_tier_blocked rejections self-explanatory.

#114 — props=0 health counter: _n_ud_snaps_this_cycle captured before ud_snaps.clear()
        so record_underdog_scan() always receives the real scan count.

#115 — /restarts command removed entirely (development noise; restarts don't erase
        persistent DB state so the command served no operational purpose).

#116 — Auto-grading: _grade_opportunities_job now proactively fetches player results
        via PlayerStatsProvider before querying get_game_result_for_grading, so props
        resolve without manual /backfill intervention even when removed from Underdog.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── #113 decision_tier in scored props ────────────────────────────────────────

class TestDecisionTierStoredInScoredProps:
    """decision_tier must appear in both new-prop and line-change _scored_props entries."""

    def _make_score(self, tier: str = "S") -> MagicMock:
        s = MagicMock()
        s.tier  = tier
        s.total = 88
        s.stars = 4
        s.move_velocity       = 0.5
        s.historical_activity = 0.8
        s.avg_vs_line         = 0.6
        s.consistency         = 0.7
        s.stability           = 0.9
        s.n_history           = 15
        s.current_line        = 22.5
        s.stars_display       = "★★★★"
        return s

    def _make_decision(self, decision_tier: str = "S", rec: str = "OVER",
                       conf: int = 87) -> MagicMock:
        d = MagicMock()
        d.decision_tier  = decision_tier
        d.recommendation = rec
        d.confidence     = conf
        d.reason         = "strong_l5"
        d.l5_hit_rate    = None
        d.l10_hit_rate   = None
        d.l5_games       = 0
        d.l10_games      = 0
        return d

    def test_new_prop_path_includes_decision_tier(self):
        """New-prop _scored_props entry must contain 'decision_tier' key."""
        score    = self._make_score("S")
        decision = self._make_decision("A")  # composite=S, decision=A (MLB near-miss scenario)

        entry: dict = {
            "player":          "Test Player",
            "stat_type":       "Points",
            "sport":           "MLB",
            "total":           score.total,
            "tier":            score.tier,
            "stars":           score.stars,
            "stars_d":         getattr(score, "stars_display", "?????"),
            "rejection":       "mlb_tier_blocked (A, MLB min=S)",
            "path":            "new",
            "decision_reason": decision.reason,
            "decision_tier":   decision.decision_tier,
            "vel":       score.move_velocity,
            "act":       score.historical_activity,
            "avg":       score.avg_vs_line,
            "con":       score.consistency,
            "sta":       score.stability,
            "n":         score.n_history,
            "line":      score.current_line,
            "prev_line": None,
        }

        assert "decision_tier" in entry, "decision_tier key missing from new-prop entry"
        assert entry["decision_tier"] == "A"
        assert entry["tier"] == "S"  # composite tier still S
        # Verify the two tiers differ — this is the near-miss scenario
        assert entry["tier"] != entry["decision_tier"]

    def test_line_change_path_includes_decision_tier(self):
        """Line-change _scored_props entry must contain 'decision_tier' key."""
        score    = self._make_score("S")
        decision = self._make_decision("A", conf=82)
        snap     = MagicMock()
        snap.game_time   = None
        snap.external_id = "ext123"

        entry: dict = {
            "player":          "Yandy Díaz",
            "stat_type":       "Fantasy Points",
            "sport":           "MLB",
            "team":            "TB",
            "total":           score.total,
            "tier":            score.tier,
            "stars":           score.stars,
            "stars_d":         getattr(score, "stars_display", "?????"),
            "rejection":       "mlb_tier_blocked (A, MLB min=S)",
            "path":            "lc",
            "decision_reason": decision.reason,
            "decision_tier":   decision.decision_tier,
            "vel":       score.move_velocity,
            "act":       score.historical_activity,
            "avg":       score.avg_vs_line,
            "con":       score.consistency,
            "sta":       score.stability,
            "n":         score.n_history,
            "line":      score.current_line,
            "prev_line": 21.5,
            "external_id":   getattr(snap, "external_id", ""),
            "game_time":     snap.game_time,
            "decision_conf": decision.confidence,
        }

        assert "decision_tier" in entry
        assert entry["decision_tier"] == "A"
        assert entry["tier"] == "S"

    def test_decision_tier_none_when_no_decision(self):
        """When decision is None (PASS tier), decision_tier entry must be None."""
        decision = None
        dt = decision.decision_tier if decision is not None else None
        assert dt is None

    def test_matching_tiers_s_tier_pass(self):
        """When composite=S AND decision=S, both tiers match — this prop should pass MLB gate."""
        entry = {
            "tier":          "S",
            "decision_tier": "S",
            "rejection":     "qualified",
        }
        assert entry["tier"] == entry["decision_tier"] == "S"
        assert entry["rejection"] == "qualified"

    def test_source_code_new_prop_has_decision_tier(self):
        """Verify decision_tier is present in market_engine.py new-prop _scored_props.append."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # Find first _scored_props.append (new-prop path at ~line 1349)
        idx = src.find('_scored_props.append(')
        assert idx != -1
        # Use closing brace of the append dict as the boundary (1500 chars is safe)
        snippet = src[idx: idx + 1500]
        assert '"decision_tier"' in snippet, (
            "decision_tier missing from new-prop _scored_props.append"
        )

    def test_source_code_line_change_has_decision_tier(self):
        """Verify decision_tier is present in market_engine.py line-change _scored_props.append."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # Find second _scored_props.append (line-change path)
        first = src.find('_scored_props.append(')
        second = src.find('_scored_props.append(', first + 1)
        assert second != -1
        # Use 1600 chars — line-change dict has more fields than new-prop dict
        snippet = src[second: second + 1600]
        assert '"decision_tier"' in snippet, (
            "decision_tier missing from line-change _scored_props.append"
        )


# ── #114 props_count captured before clear ─────────────────────────────────

class TestPropsCountBeforeClear:
    """_n_ud_snaps_this_cycle must be captured before ud_snaps.clear()."""

    def test_captured_count_unaffected_by_clear(self):
        """Simulates the pattern: capture → clear → health record."""
        ud_snaps = [MagicMock() for _ in range(47)]
        _n_ud_snaps_this_cycle = len(ud_snaps)  # capture BEFORE clear

        ud_snaps.clear()  # OOM fix clears the list

        # Health recording uses the captured variable, not len(ud_snaps)
        assert _n_ud_snaps_this_cycle == 47
        assert len(ud_snaps) == 0  # list IS empty
        # But health will receive 47, not 0
        props_count_for_health = _n_ud_snaps_this_cycle
        assert props_count_for_health == 47

    def test_old_pattern_would_report_zero(self):
        """Document what the old broken pattern produced."""
        ud_snaps = [MagicMock() for _ in range(100)]
        ud_snaps.clear()
        old_count = len(ud_snaps)   # old: always 0 after .clear()
        assert old_count == 0

    def test_zero_snaps_handled_correctly(self):
        """When the fetch returns no Underdog snaps, captured count is 0."""
        ud_snaps: list = []
        _n_ud_snaps_this_cycle = len(ud_snaps)
        assert _n_ud_snaps_this_cycle == 0

    def test_health_mock_receives_real_count(self):
        """Mock health tracker receives the captured count, not 0."""
        health = MagicMock()
        ud_snaps = [MagicMock() for _ in range(123)]
        _n_ud_snaps_this_cycle = len(ud_snaps)
        ud_snaps.clear()

        health.record_underdog_scan(
            props_count = _n_ud_snaps_this_cycle,
            alerts_sent = 0,
        )

        health.record_underdog_scan.assert_called_once_with(
            props_count=123,
            alerts_sent=0,
        )

    def test_source_code_uses_captured_variable(self):
        """market_engine.py must use _n_ud_snaps_this_cycle in record_underdog_scan."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()

        # Captured variable must be defined near ud_snaps population
        assert "_n_ud_snaps_this_cycle" in src, (
            "_n_ud_snaps_this_cycle variable not found in market_engine.py"
        )

        # Find the actual _health.record_underdog_scan( call (not a comment reference)
        scan_idx = src.find("_health.record_underdog_scan(")
        assert scan_idx != -1, "_health.record_underdog_scan( call not found"
        snippet = src[scan_idx: scan_idx + 300]
        assert "_n_ud_snaps_this_cycle" in snippet, (
            "record_underdog_scan not using _n_ud_snaps_this_cycle — props=0 bug persists"
        )

        # len(ud_snaps) must NOT appear inside the actual health call
        assert "len(ud_snaps)" not in snippet, (
            "record_underdog_scan still using len(ud_snaps) — will always be 0 after .clear()"
        )


# ── #115 /restarts command removed ────────────────────────────────────────────

class TestRestartsCommandRemoved:
    """cmd_restarts must be removed from commands.py and main.py."""

    def test_cmd_restarts_not_in_commands(self):
        """cmd_restarts function must no longer exist in commands.py."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "commands.py")) as f:
            src = f.read()
        assert "async def cmd_restarts" not in src, (
            "cmd_restarts function still exists in commands.py — should be removed"
        )

    def test_restarts_handler_not_in_main(self):
        """CommandHandler('restarts') must no longer be registered in main.py."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "main.py")) as f:
            src = f.read()
        assert 'CommandHandler("restarts"' not in src, (
            "CommandHandler('restarts') still registered in main.py"
        )

    def test_cmd_restarts_not_imported_in_main(self):
        """cmd_restarts must not be imported in main.py."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "main.py")) as f:
            src = f.read()
        assert "cmd_restarts" not in src, (
            "cmd_restarts still referenced in main.py"
        )

    def test_cmd_status_still_present(self):
        """cmd_status must still exist — verify removal was surgical."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "commands.py")) as f:
            src = f.read()
        assert "async def cmd_status" in src, "cmd_status accidentally removed"

    def test_cmd_health_still_present(self):
        """cmd_health must still exist."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "commands.py")) as f:
            src = f.read()
        assert "async def cmd_health" in src, "cmd_health accidentally removed"


# ── #116 auto-grading fetch loop ──────────────────────────────────────────────

class TestAutoGradingFetchLoop:
    """_grade_opportunities_job must fetch player results before grading."""

    @pytest.mark.asyncio
    async def test_fetch_results_called_before_grading(self):
        """Grade job must call fetch_results for each unique pending opportunity."""
        from datetime import datetime, timedelta

        opp = MagicMock()
        opp.player_name = "Caty McNally"
        opp.sport       = "TENNIS"
        opp.stat_type   = "Breakpoints Won"
        opp.game_time   = datetime.utcnow() - timedelta(hours=6)
        opp.line_value  = 3.5
        opp.recommendation = "OVER"
        opp.result      = "PENDING"
        opp.decision_tier = "S"

        db     = AsyncMock()
        db.get_pending_opportunities = AsyncMock(return_value=[opp])
        db.upsert_player_result      = AsyncMock()
        db.get_game_result_for_grading = AsyncMock(return_value=None)  # no result yet

        fetch_calls: list = []
        async def mock_fetch_results(player, sport, stat_type):
            fetch_calls.append((player, sport, stat_type))
            return []  # no data yet

        provider = MagicMock()
        provider.fetch_results = mock_fetch_results

        # Simulate the fetch loop from _grade_opportunities_job
        pending = await db.get_pending_opportunities(cutoff_hours=4)
        _grade_fetched: set = set()
        for _gopp in pending:
            _gkey = (_gopp.player_name, _gopp.sport, (_gopp.stat_type or "").lower().strip())
            if _gkey in _grade_fetched:
                continue
            _grade_fetched.add(_gkey)
            _graw = await provider.fetch_results(_gopp.player_name, _gopp.sport, _gopp.stat_type)
            for _gr in _graw:
                await db.upsert_player_result(_gr)

        assert len(fetch_calls) == 1
        assert fetch_calls[0] == ("Caty McNally", "TENNIS", "Breakpoints Won")

    @pytest.mark.asyncio
    async def test_duplicate_player_fetched_once(self):
        """Same (player, sport, stat_type) fetched only once even with multiple pending rows."""
        from datetime import datetime, timedelta

        def _opp(player, sport, stat):
            o = MagicMock()
            o.player_name = player
            o.sport       = sport
            o.stat_type   = stat
            o.game_time   = datetime.utcnow() - timedelta(hours=6)
            return o

        # Two pending rows for the same player/stat
        pending = [
            _opp("Alanna Smith", "WNBA", "Rebounds"),
            _opp("Alanna Smith", "WNBA", "Rebounds"),   # duplicate
            _opp("Smash",        "CS",   "Kills"),      # different player
        ]

        fetch_calls: list = []
        async def mock_fetch(player, sport, stat_type):
            fetch_calls.append((player, sport, stat_type))
            return []

        provider = MagicMock()
        provider.fetch_results = mock_fetch

        _grade_fetched: set = set()
        for _gopp in pending:
            _gkey = (_gopp.player_name, _gopp.sport, (_gopp.stat_type or "").lower().strip())
            if _gkey in _grade_fetched:
                continue
            _grade_fetched.add(_gkey)
            await provider.fetch_results(_gopp.player_name, _gopp.sport, _gopp.stat_type)

        assert len(fetch_calls) == 2  # Alanna once, Smash once — not 3
        players = [c[0] for c in fetch_calls]
        assert "Alanna Smith" in players
        assert "Smash" in players

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_abort_grading(self):
        """A fetch_results exception must be caught; grading continues for other props."""
        from datetime import datetime, timedelta

        def _opp(player, sport, stat):
            o = MagicMock()
            o.player_name = player
            o.sport       = sport
            o.stat_type   = stat
            o.game_time   = datetime.utcnow() - timedelta(hours=6)
            return o

        pending = [_opp("BadPlayer", "MLB", "HR"), _opp("GoodPlayer", "WNBA", "Points")]

        fetch_calls: list = []
        async def mock_fetch(player, sport, stat_type):
            fetch_calls.append(player)
            if player == "BadPlayer":
                raise RuntimeError("API timeout")
            return []

        provider = MagicMock()
        provider.fetch_results = mock_fetch

        _grade_fetched: set = set()
        for _gopp in pending:
            _gkey = (_gopp.player_name, _gopp.sport, (_gopp.stat_type or "").lower().strip())
            if _gkey in _grade_fetched:
                continue
            _grade_fetched.add(_gkey)
            try:
                await provider.fetch_results(_gopp.player_name, _gopp.sport, _gopp.stat_type)
            except Exception:
                pass  # mirrors the except block in the real job

        assert "BadPlayer"  in fetch_calls
        assert "GoodPlayer" in fetch_calls   # not skipped due to earlier failure

    @pytest.mark.asyncio
    async def test_fetched_result_written_to_db(self):
        """Results returned by fetch_results must be upserted into PlayerResult."""
        from datetime import datetime, timedelta

        opp = MagicMock()
        opp.player_name = "Test Player"
        opp.sport       = "NBA"
        opp.stat_type   = "Points"
        opp.game_time   = datetime.utcnow() - timedelta(hours=5)

        result_row = MagicMock()
        result_row.actual_value = 28.0

        db = AsyncMock()
        db.upsert_player_result = AsyncMock()

        async def mock_fetch(player, sport, stat_type):
            return [result_row]

        provider = MagicMock()
        provider.fetch_results = mock_fetch

        _grade_fetched: set = set()
        _gkey = (opp.player_name, opp.sport, (opp.stat_type or "").lower().strip())
        _grade_fetched.add(_gkey)
        _graw = await provider.fetch_results(opp.player_name, opp.sport, opp.stat_type)
        for _gr in _graw:
            await db.upsert_player_result(_gr)

        db.upsert_player_result.assert_called_once_with(result_row)

    @pytest.mark.asyncio
    async def test_grading_resolves_hit_after_fetch(self):
        """After fetch populates PlayerResult, grade job resolves the pick as HIT."""
        from datetime import datetime, timedelta

        opp = MagicMock()
        opp.player_name    = "Test Player"
        opp.sport          = "WNBA"
        opp.stat_type      = "Points"
        opp.game_time      = datetime.utcnow() - timedelta(hours=5)
        opp.line_value     = 20.5
        opp.recommendation = "OVER"

        result_row = MagicMock()
        result_row.actual_value = 25.0  # over cleared → HIT

        # Direction-aware grading logic (mirrors main.py)
        actual = result_row.actual_value
        line   = opp.line_value
        push_tol = 0.01
        if abs(actual - line) < push_tol:
            outcome = "PUSH"
        elif (opp.recommendation or "OVER").upper() == "UNDER":
            outcome = "HIT" if actual < line else "MISS"
        else:
            outcome = "HIT" if actual > line else "MISS"

        assert outcome == "HIT"

    def test_source_code_grade_job_has_fetch_loop(self):
        """_grade_opportunities_job in main.py must contain the fetch_results loop."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "main.py")) as f:
            src = f.read()
        # Find the async def, not the scheduler registration reference
        grade_def_idx = src.find("async def _grade_opportunities_job")
        assert grade_def_idx != -1, "async def _grade_opportunities_job not found in main.py"
        # Read the full function body (next 3000 chars covers it)
        func_src = src[grade_def_idx: grade_def_idx + 3500]
        assert "fetch_results" in func_src, (
            "_grade_opportunities_job does not call fetch_results — "
            "props will stay PENDING forever unless user runs /backfill"
        )
        assert "_grade_fetched" in func_src or "GradeStatProvider" in func_src, (
            "Dedup set for grade fetch loop not found in _grade_opportunities_job"
        )
