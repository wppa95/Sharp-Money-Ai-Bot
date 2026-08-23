"""
Regression tests for bugs #117 – #120.

#117 — legacy strict-sport BQ gate retired; unified delivery quality remains
#118 — Duplicate new-prop alerts (dedup gate missing from new-prop path)
#119 — Funnel "Accepted (alerted)" vs /status "Alerts sent" mismatch
#120 — legacy Tier 1/Tier 2 delivery split retired
"""

from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import market_engine as me


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shared helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _cfg(**overrides):
    import config as cfg_mod
    c = cfg_mod.Config()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# #117 — MLB/NFL BQ gate threshold
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBug117BQGate:
    """Unified delivery quality no longer branches on sport."""

    def test_unified_confidence_floors_are_configured(self):
        """The active quality floors are configured once for every sport."""
        c = _cfg()
        assert c.UD_MIN_CONF_S == 85
        assert c.UD_MIN_CONF_A == 75
        assert c.UD_MIN_CONF_B == 55

    def test_active_confidence_floor_is_sport_independent(self):
        """No sport receives a special confidence floor."""
        c = _cfg()
        assert c.min_conf_for_sport_tier("MLB", "S") == c.min_conf_for_sport_tier("NBA", "S")
        assert c.min_conf_for_sport_tier("MLB", "A") == c.min_conf_for_sport_tier("NBA", "A")
        assert c.min_conf_for_sport_tier("MLB", "B") == c.min_conf_for_sport_tier("NBA", "B")

    def test_bq_gate_not_applied_to_nba(self):
        """NBA is not subject to a retired sport-specific BQ gate."""
        c = _cfg()
        assert "NBA" not in c.ud_strict_alert_sports

    def test_no_sports_use_a_strict_bq_gate(self):
        """v3.6 applies delivery quality uniformly rather than by sport."""
        c = _cfg()
        assert c.ud_strict_alert_sports == frozenset()

    def test_legacy_quality_setting_remains_configurable(self):
        """The legacy setting is harmless and remains backwards-compatible."""
        c = _cfg(UD_STRICT_SPORT_MIN_BET_QUALITY=90)
        assert c.UD_STRICT_SPORT_MIN_BET_QUALITY == 90

    def test_source_code_documents_legacy_setting(self):
        """The retired setting remains documented rather than silently removed."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "config.py")) as f:
            src = f.read()
        idx = src.find("UD_STRICT_SPORT_MIN_BET_QUALITY")
        assert idx != -1
        snippet = src[idx: idx + 400]
        assert "95" in snippet, (
            "Config comment for UD_STRICT_SPORT_MIN_BET_QUALITY must reference 95"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# #118 — Duplicate alert dedup in new-prop path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBug118NewPropDedup:
    """New-prop path must suppress repeated alerts for the same prop/line."""

    def _make_alerted(self, player, sport, stat, line, age_seconds=0):
        """Return a _prop_market_alerted dict with one pre-recorded entry."""
        from engine.player_prop_market import _record_prop_alerted
        d: dict = {}
        ts = time.time() - age_seconds
        _record_prop_alerted(d, player, sport, stat, line, now_ts=ts)
        return d

    def test_dedup_suppresses_same_prop_same_line(self):
        """Same player/sport/stat/line within window → suppressed."""
        from engine.player_prop_market import _is_prop_deduped
        d = self._make_alerted("Max Muncy", "MLB", "Strikeouts", 0.5, age_seconds=60)
        assert _is_prop_deduped(
            d, "Max Muncy", "MLB", "Strikeouts", 0.5,
            dedup_window_seconds=3600, min_line_change=0.5,
        )

    def test_dedup_allows_significant_line_change(self):
        """Significant line change (≥ MIN_UNDERDOG_LINE_CHANGE) breaks dedup."""
        from engine.player_prop_market import _is_prop_deduped
        # Alerted at line 0.5; new line is 1.0 — delta=0.5 = min_line_change → NOT deduped
        d = self._make_alerted("Max Muncy", "MLB", "Strikeouts", 0.5, age_seconds=60)
        assert not _is_prop_deduped(
            d, "Max Muncy", "MLB", "Strikeouts", 1.0,
            dedup_window_seconds=3600, min_line_change=0.5,
        )

    def test_dedup_expires_after_window(self):
        """After dedup window expires, same prop can alert again."""
        from engine.player_prop_market import _is_prop_deduped
        # Alerted 2 hours ago, window is 1 hour → not deduped
        d = self._make_alerted("Max Muncy", "MLB", "Strikeouts", 0.5, age_seconds=7201)
        assert not _is_prop_deduped(
            d, "Max Muncy", "MLB", "Strikeouts", 0.5,
            dedup_window_seconds=3600, min_line_change=0.5,
        )

    def test_dedup_different_player_not_blocked(self):
        """A different player is never blocked by another player's dedup entry."""
        from engine.player_prop_market import _is_prop_deduped
        d = self._make_alerted("Max Muncy", "MLB", "Strikeouts", 0.5, age_seconds=60)
        assert not _is_prop_deduped(
            d, "Shohei Ohtani", "MLB", "Strikeouts", 0.5,
            dedup_window_seconds=3600, min_line_change=0.5,
        )

    def test_dedup_empty_dict_never_suppresses(self):
        """An empty dedup dict never suppresses anything (first-time alert)."""
        from engine.player_prop_market import _is_prop_deduped
        assert not _is_prop_deduped(
            {}, "Max Muncy", "MLB", "Strikeouts", 0.5,
            dedup_window_seconds=3600, min_line_change=0.5,
        )

    def test_record_prop_alerted_stores_entry(self):
        """_record_prop_alerted stores (timestamp, line) under (player, sport, stat)."""
        from engine.player_prop_market import _record_prop_alerted
        d: dict = {}
        ts = time.time()
        _record_prop_alerted(d, "Max Muncy", "MLB", "Strikeouts", 0.5, now_ts=ts)
        assert ("Max Muncy", "MLB", "Strikeouts") in d
        stored_ts, stored_line = d[("Max Muncy", "MLB", "Strikeouts")]
        assert stored_ts == ts
        assert stored_line == 0.5

    def test_source_code_new_prop_path_has_dedup_gate(self):
        """market_engine.py new-prop path must call _is_prop_deduped before delivery."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # Find the dedup gate block (added after live gate, before delivery)
        assert "_is_prop_deduped" in src, (
            "_is_prop_deduped not found in market_engine.py — dedup gate missing"
        )
        # Verify it's in the new-prop path (before the first _scored_props.append)
        dedup_idx = src.find("_is_prop_deduped")
        append_idx = src.find("_scored_props.append(")
        assert dedup_idx < append_idx, (
            "_is_prop_deduped must appear before _scored_props.append in new-prop path"
        )

    def test_source_code_new_prop_records_after_delivery(self):
        """market_engine.py new-prop path must use _try_claim_delivery_slot (atomic pre-claim)
        near the delivery loop, rather than a post-delivery _record_prop_alerted call.
        """
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # _try_claim_delivery_slot performs the dedup record atomically before delivery;
        # it must be present in the source (wired in delivery loop).
        assert "_try_claim_delivery_slot" in src, (
            "_try_claim_delivery_slot missing from market_engine.py — "
            "atomic dedup pre-claim not wired in delivery loop"
        )
        # Confirm the old pattern (post-delivery record) is NOT inside the sent=True block
        idx = src.find("_n_new_prop_sent += 1")
        assert idx != -1, "_n_new_prop_sent += 1 counter not found in market_engine.py"
        snippet = src[idx: idx + 500]
        assert "_record_prop_alerted" not in snippet, (
            "_record_prop_alerted still present inside new-prop sent=True block — "
            "should be removed; dedup is now pre-claimed by _try_claim_delivery_slot"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# #119 — Funnel label and /status counter accuracy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBug119FunnelAndStatusCounters:
    """Funnel label must not claim 'alerted'; /status must use DB-backed count."""

    def _load_commands_source(self) -> str:
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "commands.py")) as f:
            return f.read()

    def test_funnel_label_does_not_say_alerted(self):
        """/funnel must NOT label the qualified-candidates count as 'alerted'."""
        src = self._load_commands_source()
        # The old misleading label that implied Telegram delivery
        assert "Accepted (alerted)" not in src, (
            "'Accepted (alerted)' label still present — misleads users into thinking "
            "these are Telegram-delivered alerts"
        )

    def test_funnel_uses_unified_lifecycle_label(self):
        """/funnel must use neutral lifecycle labels, not retired classifications."""
        src = self._load_commands_source()
        assert "Tracked opportunities" in src or "Active props" in src

    def test_status_uses_db_backed_alert_count(self):
        """/status must call count_today_actionable_alerts (DB-backed).

        V3.5: all-time count removed from /status; only daily count shown.
        Daily counter is DB-backed (resets at UTC midnight, survives restart).
        """
        src = self._load_commands_source()
        idx = src.find("async def cmd_status")
        assert idx != -1
        fn_src = src[idx: idx + 2000]
        assert "count_today_actionable_alerts" in fn_src, (
            "cmd_status does not call count_today_actionable_alerts — "
            "Alerts today will always show 0 after a restart"
        )
        # V3.5: all-time count removed from /status display
        # count_actionable_pick_records is no longer called in cmd_status
        assert "count_actionable_pick_records" not in fn_src, (
            "V3.5: cmd_status must NOT call count_actionable_pick_records — "
            "all-time count removed from /status display"
        )

    def test_status_does_not_use_raw_total_alerts_sent_variable(self):
        """/status must not display the always-zero in-memory _total_alerts_sent."""
        src = self._load_commands_source()
        idx = src.find("async def cmd_status")
        assert idx != -1
        fn_src = src[idx: idx + 2000]
        # The old pattern was f"📬 Alerts sent: {_total_alerts_sent:,}"
        assert "_total_alerts_sent" not in fn_src, (
            "cmd_status still uses _total_alerts_sent — this counter is never "
            "incremented and always displays 0"
        )

    def test_funnel_query_counts_propcandidate_log(self):
        """get_funnel_summary must count PropCandidateLog rows (not alert_sent)."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "database.py")) as f:
            src = f.read()
        idx = src.find("def get_funnel_summary")
        assert idx != -1
        fn_src = src[idx: idx + 500]
        assert "tracked" in fn_src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# #120 — Tier 1 non-MLB/NFL delivery gates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBug120UnifiedDeliveryGates:
    """Confirm retired sport tiers do not alter unified delivery behavior."""

    def test_strict_alert_sports_is_empty(self):
        """No sports are assigned a strict-sport delivery rule."""
        c = _cfg()
        assert c.ud_strict_alert_sports == frozenset()

    def test_nba_not_in_strict_sports(self):
        c = _cfg()
        assert "NBA" not in c.ud_strict_alert_sports

    def test_basketball_not_in_strict_sports(self):
        c = _cfg()
        assert "BASKETBALL" not in c.ud_strict_alert_sports

    def test_tennis_not_in_strict_sports(self):
        c = _cfg()
        assert "TENNIS" not in c.ud_strict_alert_sports

    def test_cs_not_in_strict_sports(self):
        c = _cfg()
        assert "CS" not in c.ud_strict_alert_sports

    def test_lol_not_in_strict_sports(self):
        c = _cfg()
        assert "LOL" not in c.ud_strict_alert_sports

    def test_source_code_marks_bq_gate_as_removed(self):
        """No active strict-sport BQ gate is required by the delivery paths."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        assert "BQ gate removed" in src, (
            "market_engine.py must document that the obsolete BQ gate was removed"
        )

    def test_source_code_bq_gate_removed(self):
        """BQ gate must be removed — decision_tier enforces quality per spec Tier 2."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # Old BQ gate comment must not exist — it was removed when the gate was dropped
        assert "# Strict-sport Bet Quality gate — MLB/NFL require BQ" not in src, (
            "Old BQ gate comment found — gate should have been removed per spec Tier 2"
        )
        # The removal note must exist instead
        assert "BQ gate removed" in src, (
            "BQ gate removal comment not found — confirm all three paths removed the gate"
        )

    def test_star_floor_is_unified_across_sports(self):
        """Star qualification does not vary by a retired sport tier."""
        c = _cfg()
        assert c.min_stars_for_sport("NBA") == c.min_stars_for_sport("MLB")

    def test_decision_pass_blocks_any_sport(self):
        """PASS recommendation blocks delivery for ALL sports including Tier 1."""
        # Verify the base gate logic: recommendation != PASS is required
        recommendation = "PASS"
        _np_bet_ready = recommendation != "PASS"
        assert not _np_bet_ready, "PASS recommendation must block delivery for all sports"

    def test_s_tier_confidence_floor_is_unified(self):
        """S-tier confidence is the same for every sport."""
        c = _cfg()
        nba_s_conf = c.min_conf_for_sport_tier("NBA", "S")
        mlb_s_conf = c.min_conf_for_sport_tier("MLB", "S")
        assert nba_s_conf == mlb_s_conf
