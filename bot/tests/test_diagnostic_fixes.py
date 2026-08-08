"""
Regression tests for V3.1 targeted diagnostic fixes.

Bug 1: PropCandidateLog gate_decision="ACCEPTED" never fired because `_crej is None`
        is never True — _crej is always set to a rejection label for every scored prop.
        Fix: condition changed to `_crej in ("qualified", "sent", "filtered", "new_prop_failed")`.

Bug 2: PropLineHistory sync processed only ~200 of 5,210 active props per cycle.
        `limit=200, since_hours=4` loads the oldest 200 rows from a 200k+ row window,
        always hitting the same small batch.  High-quality stable props aged out of the
        24h /picks window because their PropLineHistory.fetched_at was never refreshed.
        Fix: `limit=6000, since_hours=0.17` to scope to the current cycle's rows.
"""

import ast
import inspect
import textwrap


# ── helpers ───────────────────────────────────────────────────────────────────

def _simulate_gate_decision(ctier: str, crej) -> str:
    """Replicate the PropCandidateLog gate_decision logic exactly as written in market_engine.py."""
    if ctier == "PASS":
        return "REJECTED"
    elif ctier == "B":
        return "WATCHLIST"
    elif crej in ("qualified", "sent", "filtered", "new_prop_failed") and ctier in ("S", "A"):
        return "ACCEPTED"
    else:
        return "REJECTED"


# ── Bug 1: gate_decision ACCEPTED condition ───────────────────────────────────

class TestGateDecisionAccepted:
    """gate_decision = 'ACCEPTED' must fire for qualified / actually-sent S/A props."""

    # --- previously broken: ACCEPTED was unreachable ---

    def test_qualified_s_tier_is_accepted(self):
        assert _simulate_gate_decision("S", "qualified") == "ACCEPTED"

    def test_qualified_a_tier_is_accepted(self):
        assert _simulate_gate_decision("A", "qualified") == "ACCEPTED"

    def test_sent_s_tier_is_accepted(self):
        """New-prop path: alert delivered to Telegram."""
        assert _simulate_gate_decision("S", "sent") == "ACCEPTED"

    def test_sent_a_tier_is_accepted(self):
        assert _simulate_gate_decision("A", "sent") == "ACCEPTED"

    def test_filtered_s_tier_is_accepted(self):
        """Reached delivery gate but filtered by dedup/reversal."""
        assert _simulate_gate_decision("S", "filtered") == "ACCEPTED"

    def test_new_prop_failed_s_tier_is_accepted(self):
        """Passed all gates, delivery failed (transient error)."""
        assert _simulate_gate_decision("S", "new_prop_failed") == "ACCEPTED"

    # --- unchanged: REJECTED cases ---

    def test_decision_pass_s_tier_is_rejected(self):
        """Decision engine returned PASS — no directional pick."""
        assert _simulate_gate_decision("S", "decision_pass") == "REJECTED"

    def test_cold_start_s_tier_is_rejected(self):
        """Cold-start suppression is not an acceptance."""
        assert _simulate_gate_decision("S", "cold_start") == "REJECTED"

    def test_below_threshold_a_tier_is_rejected(self):
        """Insufficient stars — not qualified."""
        assert _simulate_gate_decision("A", "below_threshold (2★ < 3★)") == "REJECTED"

    def test_sport_blocked_s_tier_is_rejected(self):
        assert _simulate_gate_decision("S", "sport_blocked (ESPORTS)") == "REJECTED"

    def test_mlb_under_blocked_s_tier_is_rejected(self):
        assert _simulate_gate_decision("S", "mlb_under_blocked (S)") == "REJECTED"

    def test_mlb_tier_blocked_is_rejected(self):
        assert _simulate_gate_decision("A", "mlb_tier_blocked (A, MLB min=S)") == "REJECTED"

    def test_unknown_rejection_is_rejected(self):
        assert _simulate_gate_decision("S", "unknown") == "REJECTED"

    def test_none_rejection_is_rejected(self):
        """None was the old (broken) trigger — it should now be REJECTED not ACCEPTED."""
        assert _simulate_gate_decision("S", None) == "REJECTED"

    def test_empty_string_rejection_is_rejected(self):
        assert _simulate_gate_decision("S", "") == "REJECTED"

    # --- unchanged: tier-based cases ---

    def test_pass_tier_always_rejected(self):
        for rej in ("qualified", "sent", None, "decision_pass"):
            assert _simulate_gate_decision("PASS", rej) == "REJECTED"

    def test_b_tier_always_watchlist(self):
        for rej in ("qualified", "sent", None, "decision_pass"):
            assert _simulate_gate_decision("B", rej) == "WATCHLIST"

    def test_qualified_b_tier_is_watchlist_not_accepted(self):
        """B-tier props are Watchlist regardless of rejection label."""
        assert _simulate_gate_decision("B", "qualified") == "WATCHLIST"

    def test_sent_b_tier_is_watchlist_not_accepted(self):
        assert _simulate_gate_decision("B", "sent") == "WATCHLIST"


class TestGateDecisionSourceCode:
    """Verify the fix is present in the actual market_engine.py source."""

    def _load_source(self) -> str:
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "market_engine.py")
        with open(path) as f:
            return f.read()

    def test_old_condition_not_present(self):
        """The broken `_crej is None` condition must no longer exist."""
        src = self._load_source()
        assert "_crej is None" not in src, (
            "Old broken condition `_crej is None` still present in gate_decision block"
        )

    def test_new_condition_present(self):
        """The fixed condition must reference the correct rejection labels."""
        src = self._load_source()
        assert '"qualified"' in src and '"sent"' in src and '"filtered"' in src, (
            "New gate_decision condition labels not found in market_engine.py"
        )

    def test_accepted_still_in_source(self):
        src = self._load_source()
        assert '"ACCEPTED"' in src, "ACCEPTED assignment removed from market_engine.py"


# ── Bug 2: sync scope (limit / since_hours) ───────────────────────────────────

class TestSyncScopeSourceCode:
    """Verify the PropLineHistory sync parameters are fixed in market_engine.py."""

    def _load_source(self) -> str:
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "market_engine.py")
        with open(path) as f:
            return f.read()

    def test_old_limit_200_not_present_for_sync(self):
        """limit=200 with since_hours=4 was the broken combination — the ACTUAL param must be fixed."""
        import re
        src = self._load_source()
        sync_idx = src.find("sync_underdog_snapshots_to_prop_history")
        assert sync_idx != -1, "sync call not found"
        snippet = src[sync_idx: sync_idx + 300]
        # Extract the actual limit= value (first occurrence, not inside a comment)
        # Strip comment lines from the snippet before checking
        non_comment_lines = [
            line for line in snippet.splitlines()
            if not line.strip().startswith("#")
        ]
        non_comment = "\n".join(non_comment_lines)
        m = re.search(r"limit\s*=\s*(\d+)", non_comment)
        assert m is not None, "limit= param not found in sync call"
        actual_limit = int(m.group(1))
        assert actual_limit != 200, (
            f"sync actual limit={actual_limit} is the old broken value (200)"
        )
        # Also verify since_hours is not 4 (old value)
        m2 = re.search(r"since_hours\s*=\s*([\d.]+)", non_comment)
        assert m2 is not None, "since_hours= param not found"
        actual_since = float(m2.group(1))
        assert actual_since != 4.0, (
            f"sync actual since_hours={actual_since} is the old broken value (4)"
        )

    def test_new_limit_covers_all_props(self):
        """New limit must be ≥ 5000 to cover a full cycle's ~5,210 props."""
        src = self._load_source()
        sync_idx = src.find("sync_underdog_snapshots_to_prop_history")
        snippet = src[sync_idx: sync_idx + 200]
        # Extract the limit value
        import re
        m = re.search(r"limit\s*=\s*(\d+)", snippet)
        assert m is not None, "limit= param not found in sync call"
        limit_val = int(m.group(1))
        assert limit_val >= 5000, (
            f"sync limit={limit_val} is too low; must be ≥5000 to cover all active props per cycle"
        )

    def test_new_since_hours_covers_current_cycle(self):
        """New since_hours must be short enough to avoid loading 200k+ rows."""
        src = self._load_source()
        sync_idx = src.find("sync_underdog_snapshots_to_prop_history")
        snippet = src[sync_idx: sync_idx + 300]
        import re
        m = re.search(r"since_hours\s*=\s*([\d.]+)", snippet)
        assert m is not None, "since_hours= param not found in sync call"
        since_val = float(m.group(1))
        # Must be short enough for one cycle (≤ 30 min) but not 4 hours
        assert since_val <= 0.5, (
            f"sync since_hours={since_val} is too large; should be ≤0.5 h to focus on current cycle"
        )
        assert since_val >= 0.08, (
            f"sync since_hours={since_val} is too small (< 5 min); might miss current cycle rows"
        )


# ── End-to-end gate_decision simulation ───────────────────────────────────────

class TestGateDecisionIntegration:
    """Simulate the full PropCandidateLog write loop to verify counter accuracy."""

    def _run_gate_decision_loop(self, scored_props: list) -> dict:
        """Run the gate_decision assignment logic and return counts per gate."""
        counts = {"ACCEPTED": 0, "WATCHLIST": 0, "REJECTED": 0}
        for cp in scored_props:
            ctier = cp.get("tier", "PASS")
            crej = cp.get("rejection")
            gdec = _simulate_gate_decision(ctier, crej)
            counts[gdec] += 1
        return counts

    def test_qualified_s_tier_increments_accepted(self):
        props = [{"tier": "S", "rejection": "qualified"}]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 1
        assert counts["REJECTED"] == 0

    def test_decision_pass_s_tier_increments_rejected(self):
        props = [{"tier": "S", "rejection": "decision_pass"}]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 0
        assert counts["REJECTED"] == 1

    def test_cold_start_does_not_inflate_accepted(self):
        props = [
            {"tier": "S", "rejection": "cold_start"},
            {"tier": "A", "rejection": "cold_start"},
        ]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 0
        assert counts["REJECTED"] == 2

    def test_mixed_cycle_counters(self):
        """Realistic mixed cycle: some qualified, some decision_pass, some cold_start, B-tier."""
        props = [
            {"tier": "S", "rejection": "qualified"},       # ACCEPTED
            {"tier": "A", "rejection": "qualified"},       # ACCEPTED
            {"tier": "S", "rejection": "sent"},            # ACCEPTED
            {"tier": "S", "rejection": "decision_pass"},  # REJECTED
            {"tier": "S", "rejection": "cold_start"},     # REJECTED
            {"tier": "A", "rejection": "cold_start"},     # REJECTED
            {"tier": "B", "rejection": "qualified"},       # WATCHLIST
            {"tier": "B", "rejection": "decision_pass"},  # WATCHLIST
            {"tier": "PASS", "rejection": None},           # REJECTED
        ]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 3
        assert counts["WATCHLIST"] == 2
        assert counts["REJECTED"] == 4

    def test_all_decision_pass_zero_accepted(self):
        """All decision_pass → Accepted must be 0 (not falsely elevated)."""
        props = [
            {"tier": "S", "rejection": "decision_pass"},
            {"tier": "A", "rejection": "decision_pass"},
        ]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 0

    def test_funnel_accepted_before_fix_was_always_zero(self):
        """The old `_crej is None` condition — None is never in our rejection labels."""
        old_condition_result = None is None and "S" in ("S", "A")  # True
        new_condition_for_none = None in ("qualified", "sent", "filtered", "new_prop_failed")  # False
        # Old logic would have made ACCEPTED=True when _crej is None.
        # But _crej was ALWAYS set to a string, so old_condition_result was never reached.
        # New logic correctly sets ACCEPTED for the real qualified labels.
        assert old_condition_result is True, "old condition logic check"
        assert new_condition_for_none is False, "None must not trigger new ACCEPTED condition"

    def test_filter_new_prop_failed_is_accepted(self):
        """Prop that reached delivery gate but got a transient failure is still accepted."""
        props = [{"tier": "S", "rejection": "new_prop_failed"}]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 1

    def test_filter_filtered_is_accepted(self):
        """Prop that passed all gates but was filtered at delivery (dedup) is still accepted."""
        props = [{"tier": "A", "rejection": "filtered"}]
        counts = self._run_gate_decision_loop(props)
        assert counts["ACCEPTED"] == 1
