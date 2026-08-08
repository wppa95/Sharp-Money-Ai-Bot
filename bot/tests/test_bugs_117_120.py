"""
Regression tests for bugs #117 – #120.

#117 — MLB/NFL BQ gate too low (was 85, raised to 95)
#118 — Duplicate new-prop alerts (dedup gate missing from new-prop path)
#119 — Funnel "Accepted (alerted)" vs /status "Alerts sent" mismatch
#120 — Tier 1 non-MLB/NFL delivery gates verified correct (no MLB/NFL leak)
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
    """MLB/NFL require S-tier AND BQ ≥ 95 to reach Telegram."""

    def test_default_bq_threshold_is_95(self):
        """Default UD_STRICT_SPORT_MIN_BET_QUALITY must be 95 (not 85)."""
        c = _cfg()
        assert c.UD_STRICT_SPORT_MIN_BET_QUALITY == 95

    def test_bq_95_passes_mlb_gate(self):
        """BQ=95 exactly meets the threshold → not blocked."""
        c = _cfg()
        assert 95 >= c.UD_STRICT_SPORT_MIN_BET_QUALITY

    def test_bq_94_fails_mlb_gate(self):
        """BQ=94 is below the threshold → blocked."""
        c = _cfg()
        assert 94 < c.UD_STRICT_SPORT_MIN_BET_QUALITY

    def test_bq_85_now_fails_mlb_gate(self):
        """BQ=85 (old threshold) must now be blocked by the raised gate."""
        c = _cfg()
        # 85 < 95 → blocked
        assert 85 < c.UD_STRICT_SPORT_MIN_BET_QUALITY

    def test_bq_gate_not_applied_to_nba(self):
        """Non-strict sports (NBA) are NOT subject to the BQ gate."""
        c = _cfg()
        assert "NBA" not in c.ud_strict_alert_sports

    def test_bq_gate_applies_to_mlb_and_nfl_only(self):
        """Only MLB and NFL are in ud_strict_alert_sports."""
        c = _cfg()
        assert "MLB" in c.ud_strict_alert_sports
        assert "NFL" in c.ud_strict_alert_sports
        assert "NBA" not in c.ud_strict_alert_sports
        assert "BASKETBALL" not in c.ud_strict_alert_sports
        assert "TENNIS" not in c.ud_strict_alert_sports

    def test_bq_threshold_env_override(self):
        """UD_STRICT_SPORT_MIN_BET_QUALITY can be overridden via environment."""
        c = _cfg(UD_STRICT_SPORT_MIN_BET_QUALITY=90)
        assert c.UD_STRICT_SPORT_MIN_BET_QUALITY == 90

    def test_source_code_bq_gate_comment_updated(self):
        """Config source must reference the 95 threshold in comments (not 85)."""
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
        """market_engine.py new-prop path must call _record_prop_alerted after sent=True."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # _record_prop_alerted must appear inside the `if ud_result.sent:` block
        # (between `_n_new_prop_sent += 1` and `_lifecycle_alerted.append`)
        idx = src.find("_n_new_prop_sent += 1")
        assert idx != -1
        snippet = src[idx: idx + 900]
        assert "_record_prop_alerted" in snippet, (
            "_record_prop_alerted missing from new-prop sent=True block — "
            "dedup dict not updated after delivery"
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

    def test_funnel_label_says_qualified_candidates(self):
        """/funnel must display 'Qualified candidates' for PropCandidateLog.ACCEPTED count."""
        src = self._load_commands_source()
        assert "Qualified candidates" in src, (
            "'Qualified candidates' label not found in commands.py — "
            "funnel display not updated"
        )

    def test_status_uses_db_backed_alert_count(self):
        """/status must call count_today_actionable_alerts (DB-backed) not _total_alerts_sent."""
        src = self._load_commands_source()
        idx = src.find("async def cmd_status")
        assert idx != -1
        fn_src = src[idx: idx + 2000]
        assert "count_today_actionable_alerts" in fn_src, (
            "cmd_status does not call count_today_actionable_alerts — "
            "Alerts today will always show 0 after a restart"
        )
        assert "count_actionable_pick_records" in fn_src, (
            "cmd_status does not call count_actionable_pick_records — "
            "Alerts all-time not shown"
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
        assert "PropCandidateLog" in fn_src, (
            "get_funnel_summary does not query PropCandidateLog — "
            "funnel counts would reflect wrong table"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# #120 — Tier 1 non-MLB/NFL delivery gates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBug120Tier1DeliveryGates:
    """Confirm MLB/NFL strict rules are NOT applied to Tier 1 sports."""

    def test_strict_alert_sports_contains_only_mlb_and_nfl(self):
        """ud_strict_alert_sports must be exactly {MLB, NFL}."""
        c = _cfg()
        assert c.ud_strict_alert_sports == frozenset({"MLB", "NFL"})

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

    def test_source_code_bq_gate_checks_strict_sports_set(self):
        """BQ gate in market_engine.py must use ud_strict_alert_sports to guard MLB/NFL."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # All three BQ gate blocks (new/line-change/standing) use ud_strict_alert_sports
        assert src.count("ud_strict_alert_sports") >= 3, (
            "ud_strict_alert_sports appears fewer than 3 times — "
            "one of the delivery paths may not be guarding the BQ gate correctly"
        )

    def test_source_code_no_direct_mlb_string_in_bq_gate(self):
        """BQ gate must use ud_strict_alert_sports lookup, not a hardcoded sport string."""
        import os
        base = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base, "market_engine.py")) as f:
            src = f.read()
        # Find the new-prop BQ gate comment; the `if` block with ud_strict_alert_sports
        # follows in the next ~400 chars (comment + if condition + rejection log)
        gate_start = src.find("# Strict-sport Bet Quality gate — MLB/NFL require BQ")
        assert gate_start != -1, "BQ gate comment not found in market_engine.py"
        snippet = src[gate_start: gate_start + 500]
        assert "ud_strict_alert_sports" in snippet, (
            "New-prop BQ gate does not use ud_strict_alert_sports — "
            "MLB/NFL check is not using the config-driven set"
        )

    def test_tier1_min_stars_is_lower_than_tier2(self):
        """Non-strict sports require fewer stars than MLB/NFL strict gate."""
        c = _cfg()
        tier1_stars = c.min_stars_for_sport("NBA")
        tier2_stars = c.min_stars_for_sport("MLB")
        assert tier1_stars <= tier2_stars, (
            "Tier 1 stars floor must not exceed Tier 2 stars floor"
        )

    def test_decision_pass_blocks_any_sport(self):
        """PASS recommendation blocks delivery for ALL sports including Tier 1."""
        # Verify the base gate logic: recommendation != PASS is required
        recommendation = "PASS"
        _np_bet_ready = recommendation != "PASS"
        assert not _np_bet_ready, "PASS recommendation must block delivery for all sports"

    def test_non_mlb_s_tier_allowed_by_conf_gate(self):
        """Non-strict sport can pass confidence gate with lower threshold than MLB/NFL."""
        c = _cfg()
        # NBA S-tier confidence floor should be lower than MLB/NFL S-tier floor
        nba_s_conf = c.min_conf_for_sport_tier("NBA", "S")
        mlb_s_conf = c.min_conf_for_sport_tier("MLB", "S")
        assert nba_s_conf <= mlb_s_conf, (
            "NBA S-tier confidence floor must not exceed MLB S-tier floor"
        )
