"""
Tests for the sport-agnostic OddsAPI pacing priority system.

Covers:
  - infer_call_priority: player props always HIGH regardless of sport
  - infer_call_priority: any sport in active_sports config → HIGH (not MLB-only)
  - infer_call_priority: MLB still HIGH (regression guard, config-driven)
  - infer_call_priority: WNBA → HIGH when WNBA is in active_sports
  - infer_call_priority: MLB → LOW when MLB is NOT in active_sports (not hardcoded)
  - infer_call_priority: unknown / inactive sport → LOW
  - Budget blocking: LOW blocked at ≥ 90 %, MEDIUM blocked at ≥ 100 %
  - Budget blocking: HIGH / CRITICAL always pass regardless of budget
  - Budget warning message is sport-agnostic (no MLB-specific text)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

from unittest.mock import MagicMock, patch

import pytest

from providers.usage_tracker import (
    ApiUsageTracker,
    CallPriority,
    infer_call_priority,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _infer_with_active_sports(
    active_sports: list[str],
    sport_key: str,
    markets: str,
) -> CallPriority:
    """
    Call infer_call_priority with a controlled config.active_sports list.

    The function does a lazy ``from config import config as _cfg`` so we patch
    the singleton object that already lives in the config module.
    """
    import config as _config_module
    original = _config_module.config
    mock_cfg = MagicMock(wraps=original)
    mock_cfg.active_sports = active_sports
    try:
        _config_module.config = mock_cfg
        return infer_call_priority(sport_key, markets)
    finally:
        _config_module.config = original


# ── Priority classification ────────────────────────────────────────────────────

class TestInferCallPriority:
    """infer_call_priority is sport-agnostic and driven by active_sports config."""

    # --- Player props are always HIGH regardless of sport ---

    def test_player_props_market_mlb_is_high(self):
        result = _infer_with_active_sports(["MLB"], "baseball_mlb", "player_props")
        assert result == CallPriority.HIGH

    def test_player_props_market_wnba_is_high(self):
        # HIGH even when WNBA is NOT in active_sports — player props trump everything
        result = _infer_with_active_sports([], "basketball_wnba", "player_props")
        assert result == CallPriority.HIGH

    def test_player_props_unknown_sport_is_high(self):
        result = _infer_with_active_sports([], "esports_fake", "player_props")
        assert result == CallPriority.HIGH

    def test_player_prefix_market_is_high(self):
        result = _infer_with_active_sports([], "baseball_mlb", "player_points")
        assert result == CallPriority.HIGH

    # --- Active configured sports get HIGH for game lines ---

    def test_mlb_game_line_is_high_when_in_active_sports(self):
        """Regression guard: MLB still protected when it's in active_sports."""
        result = _infer_with_active_sports(["MLB"], "baseball_mlb", "h2h")
        assert result == CallPriority.HIGH

    def test_wnba_game_line_is_high_when_in_active_sports(self):
        """WNBA should be HIGH when in active_sports — not MLB-only."""
        result = _infer_with_active_sports(["MLB", "WNBA"], "basketball_wnba", "h2h")
        assert result == CallPriority.HIGH

    def test_nfl_game_line_is_high_when_in_active_sports(self):
        result = _infer_with_active_sports(["NFL"], "americanfootball_nfl", "h2h")
        assert result == CallPriority.HIGH

    def test_nba_game_line_is_high_when_in_active_sports(self):
        result = _infer_with_active_sports(["NBA"], "basketball_nba", "h2h")
        assert result == CallPriority.HIGH

    # --- MLB is NOT uniquely hardcoded — priority follows config ---

    def test_mlb_game_line_is_low_when_not_in_active_sports(self):
        """MLB must NOT receive HIGH when excluded from active_sports — it's config-driven."""
        result = _infer_with_active_sports([], "baseball_mlb", "h2h")
        assert result == CallPriority.LOW

    def test_wnba_game_line_is_low_when_not_in_active_sports(self):
        result = _infer_with_active_sports(["MLB"], "basketball_wnba", "h2h")
        assert result == CallPriority.LOW

    # --- Inactive / unknown sports → LOW ---

    def test_unknown_sport_key_is_low(self):
        result = _infer_with_active_sports(["MLB"], "esports_dota2", "h2h")
        assert result == CallPriority.LOW

    def test_empty_active_sports_non_prop_is_low(self):
        result = _infer_with_active_sports([], "baseball_mlb", "totals")
        assert result == CallPriority.LOW

    # --- DOTA / TENNIS / CS don't use Odds API → their hypothetical keys → LOW ---

    def test_dota_odds_api_key_is_low(self):
        # DOTA uses OpenDota; if an Odds API call were ever made with this key it would be LOW
        result = _infer_with_active_sports(["MLB", "WNBA", "DOTA"], "esports_dota2_ti", "h2h")
        assert result == CallPriority.LOW

    def test_tennis_odds_api_key_is_low_when_not_in_active_sports(self):
        # TENNIS is in UD_ALERT_SPORTS but NOT in ACTIVE_SPORTS (Odds API scope)
        result = _infer_with_active_sports(["MLB"], "tennis_atp_french_open", "h2h")
        assert result == CallPriority.LOW

    # --- Multiple active sports all receive HIGH ---

    def test_all_configured_sports_get_high(self):
        """Every sport in active_sports gets HIGH for its Odds API game-line calls."""
        from engine.analysis import _SPORT_TO_ODDS_API_KEY
        from models import Sport

        for sp, odds_key in _SPORT_TO_ODDS_API_KEY.items():
            if not isinstance(sp, Sport):
                continue
            result = _infer_with_active_sports([sp.value], odds_key, "h2h")
            assert result == CallPriority.HIGH, (
                f"Expected HIGH for sport {sp.value} (Odds API key: {odds_key!r}) "
                f"when in active_sports, got {result.name}"
            )


# ── Budget enforcement ─────────────────────────────────────────────────────────

class TestBudgetBlocking:
    """Budget enforcement: LOW blocked at ≥ 90 %, MEDIUM at ≥ 100 %, HIGH always passes."""

    def _tracker(self, budget: int = 100) -> ApiUsageTracker:
        return ApiUsageTracker(
            monthly_budgets={"OddsAPI": budget},
            data_dir="/tmp/test_pacing_priority",
        )

    def _force_pct(self, tracker: ApiUsageTracker, provider: str, pct: float) -> None:
        """Override _get_authoritative_pct to return a controlled value."""
        tracker._get_authoritative_pct = lambda p: pct if p == provider else 0.0

    # ── At 100 %: LOW + MEDIUM blocked; HIGH + CRITICAL pass ──────────────────

    def test_critical_passes_at_100_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 100.0)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.CRITICAL)
        assert allowed is True

    def test_high_passes_at_100_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 100.0)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.HIGH)
        assert allowed is True

    def test_medium_blocked_at_100_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 100.0)
        allowed, reason = t.should_allow("OddsAPI", CallPriority.MEDIUM)
        assert allowed is False
        assert "blocked" in reason.lower()

    def test_low_blocked_at_100_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 100.0)
        allowed, reason = t.should_allow("OddsAPI", CallPriority.LOW)
        assert allowed is False
        assert "blocked" in reason.lower()

    # ── At 90 %: only LOW is blocked ──────────────────────────────────────────

    def test_low_blocked_at_90_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 90.0)
        allowed, reason = t.should_allow("OddsAPI", CallPriority.LOW)
        assert allowed is False
        assert "LOW" in reason

    def test_medium_passes_at_90_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 90.0)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.MEDIUM)
        assert allowed is True

    def test_high_passes_at_90_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 90.0)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.HIGH)
        assert allowed is True

    def test_critical_passes_at_90_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 90.0)
        allowed, _ = t.should_allow("OddsAPI", CallPriority.CRITICAL)
        assert allowed is True

    # ── Below 90 %: all priorities pass ───────────────────────────────────────

    def test_all_priorities_pass_below_90_pct(self):
        t = self._tracker()
        self._force_pct(t, "OddsAPI", 89.9)
        for priority in CallPriority:
            allowed, _ = t.should_allow("OddsAPI", priority)
            assert allowed is True, f"{priority.name} should pass at 89.9%"

    # ── Unlimited budget: nothing is ever blocked ──────────────────────────────

    def test_unlimited_budget_all_pass(self):
        t = ApiUsageTracker(
            monthly_budgets={"OddsAPI": 0},
            data_dir="/tmp/test_pacing_priority",
        )
        for priority in CallPriority:
            allowed, _ = t.should_allow("OddsAPI", priority)
            assert allowed is True, f"{priority.name} should always pass with unlimited budget"


# ── Budget warning message is sport-agnostic ──────────────────────────────────

class TestBudgetWarningMessage:
    """The budget alert messages must NOT hardcode MLB and MUST be sport-agnostic."""

    def _budget_job_source(self) -> str:
        main_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")
        with open(main_path) as fh:
            source = fh.read()
        # Find the function *definition*, not the scheduler call-site
        start = source.find("async def _budget_check_job")
        # Return enough context to cover all message strings in the function body
        return source[start: start + 6000]

    def test_no_mlb_specific_text_in_message(self):
        section = self._budget_job_source()
        assert "MLB odds" not in section, (
            "Budget warning still contains 'MLB odds' — must be sport-agnostic"
        )
        assert "MLB odds, player props" not in section

    def test_100pct_message_mentions_active_sports(self):
        section = self._budget_job_source()
        assert "active" in section.lower()

    def test_100pct_message_has_protected_section(self):
        section = self._budget_job_source()
        assert "Protected" in section

    def test_100pct_message_has_blocked_section(self):
        section = self._budget_job_source()
        assert "Blocked" in section

    def test_message_includes_sport_breakdown(self):
        """Message should include a dynamic sport breakdown, not a hardcoded list."""
        section = self._budget_job_source()
        # The sport breakdown is built from config.ud_alert_sports
        assert "ud_alert_sports" in section or "alert_sports" in section or "sport_lines" in section


# ── Priority enum ordering contract ───────────────────────────────────────────

class TestPriorityEnumContract:
    def test_critical_has_lowest_numeric_value(self):
        assert CallPriority.CRITICAL.value < CallPriority.HIGH.value

    def test_high_lower_than_medium(self):
        assert CallPriority.HIGH.value < CallPriority.MEDIUM.value

    def test_medium_lower_than_low(self):
        assert CallPriority.MEDIUM.value < CallPriority.LOW.value

    def test_medium_and_low_blocked_at_100_pct(self):
        """Enum ordering ensures MEDIUM and LOW are blocked by the > HIGH.value guard."""
        assert CallPriority.MEDIUM.value > CallPriority.HIGH.value
        assert CallPriority.LOW.value    > CallPriority.HIGH.value

    def test_critical_and_high_not_blocked_at_100_pct(self):
        """CRITICAL and HIGH must not be caught by the > HIGH.value guard."""
        assert CallPriority.CRITICAL.value <= CallPriority.HIGH.value
        assert CallPriority.HIGH.value     <= CallPriority.HIGH.value
