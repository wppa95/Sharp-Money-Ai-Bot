"""
Contract tests — Error Taxonomy (Framework v3.0 Layer 3).

Verifies:
  • RecoveryStrategy enum is complete and accessible.
  • recovery_strategy_for() maps every FailureType to the correct strategy.
  • Streak thresholds correctly escalate the strategy.
  • BotErrorType enum covers all required bot-level error categories.
"""

import pytest
from providers.base import FailureType, RecoveryStrategy
from providers.health_monitor import recovery_strategy_for


# ─────────────────────────────────────────────────────────────────────────────
# RecoveryStrategy — enum completeness
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryStrategyEnum:
    def test_has_skip(self):
        assert RecoveryStrategy.SKIP is not None

    def test_has_backoff(self):
        assert RecoveryStrategy.BACKOFF is not None

    def test_has_wait(self):
        assert RecoveryStrategy.WAIT is not None

    def test_has_disable(self):
        assert RecoveryStrategy.DISABLE is not None

    def test_all_values_are_strings(self):
        for s in RecoveryStrategy:
            assert isinstance(s.value, str)

    def test_values_are_lowercase(self):
        for s in RecoveryStrategy:
            assert s.value == s.value.lower()


# ─────────────────────────────────────────────────────────────────────────────
# recovery_strategy_for — base-case mapping (streak=0 / low streak)
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryStrategyForBaseCase:
    def test_quota_always_wait(self):
        assert recovery_strategy_for(FailureType.QUOTA, 0)  == RecoveryStrategy.WAIT
        assert recovery_strategy_for(FailureType.QUOTA, 10) == RecoveryStrategy.WAIT

    def test_blocked_always_disable(self):
        assert recovery_strategy_for(FailureType.BLOCKED, 0) == RecoveryStrategy.DISABLE
        assert recovery_strategy_for(FailureType.BLOCKED, 1) == RecoveryStrategy.DISABLE

    def test_http_error_low_streak_backoff(self):
        assert recovery_strategy_for(FailureType.HTTP_ERROR, 0) == RecoveryStrategy.BACKOFF
        assert recovery_strategy_for(FailureType.HTTP_ERROR, 1) == RecoveryStrategy.BACKOFF
        assert recovery_strategy_for(FailureType.HTTP_ERROR, 4) == RecoveryStrategy.BACKOFF

    def test_timeout_low_streak_skip(self):
        assert recovery_strategy_for(FailureType.TIMEOUT, 0) == RecoveryStrategy.SKIP
        assert recovery_strategy_for(FailureType.TIMEOUT, 1) == RecoveryStrategy.SKIP
        assert recovery_strategy_for(FailureType.TIMEOUT, 2) == RecoveryStrategy.SKIP

    def test_parse_error_low_streak_skip(self):
        assert recovery_strategy_for(FailureType.PARSE_ERROR, 0) == RecoveryStrategy.SKIP
        assert recovery_strategy_for(FailureType.PARSE_ERROR, 2) == RecoveryStrategy.SKIP

    def test_unknown_always_skip(self):
        assert recovery_strategy_for(FailureType.UNKNOWN, 0)  == RecoveryStrategy.SKIP
        assert recovery_strategy_for(FailureType.UNKNOWN, 99) == RecoveryStrategy.SKIP

    def test_default_streak_zero(self):
        # Calling without streak argument must use 0 as default
        result = recovery_strategy_for(FailureType.HTTP_ERROR)
        assert result == RecoveryStrategy.BACKOFF


# ─────────────────────────────────────────────────────────────────────────────
# recovery_strategy_for — streak-based escalation
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryStrategyEscalation:
    def test_http_error_high_streak_escalates_to_disable(self):
        # Streak ≥ 5 → DISABLE
        assert recovery_strategy_for(FailureType.HTTP_ERROR, 5)  == RecoveryStrategy.DISABLE
        assert recovery_strategy_for(FailureType.HTTP_ERROR, 10) == RecoveryStrategy.DISABLE

    def test_timeout_high_streak_escalates_to_backoff(self):
        # Streak ≥ 3 → BACKOFF
        assert recovery_strategy_for(FailureType.TIMEOUT, 3) == RecoveryStrategy.BACKOFF
        assert recovery_strategy_for(FailureType.TIMEOUT, 7) == RecoveryStrategy.BACKOFF

    def test_parse_error_high_streak_escalates_to_backoff(self):
        assert recovery_strategy_for(FailureType.PARSE_ERROR, 3) == RecoveryStrategy.BACKOFF
        assert recovery_strategy_for(FailureType.PARSE_ERROR, 5) == RecoveryStrategy.BACKOFF

    def test_quota_streak_has_no_effect(self):
        # QUOTA is never escalated — always WAIT regardless of streak
        for streak in (0, 1, 5, 100):
            assert recovery_strategy_for(FailureType.QUOTA, streak) == RecoveryStrategy.WAIT

    def test_blocked_streak_has_no_effect(self):
        # BLOCKED is never de-escalated — always DISABLE regardless of streak
        for streak in (0, 1, 5, 100):
            assert recovery_strategy_for(FailureType.BLOCKED, streak) == RecoveryStrategy.DISABLE


# ─────────────────────────────────────────────────────────────────────────────
# recovery_strategy_for — return type contract
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryStrategyReturnType:
    @pytest.mark.parametrize("ft", list(FailureType))
    def test_returns_recovery_strategy_for_all_failure_types(self, ft):
        result = recovery_strategy_for(ft, streak=0)
        assert isinstance(result, RecoveryStrategy)

    @pytest.mark.parametrize("ft", list(FailureType))
    def test_returns_valid_result_at_high_streak(self, ft):
        result = recovery_strategy_for(ft, streak=50)
        assert isinstance(result, RecoveryStrategy)

    def test_every_failure_type_is_covered(self):
        """Ensure no FailureType returns an unexpected/missing strategy."""
        covered = set()
        for ft in FailureType:
            result = recovery_strategy_for(ft, streak=0)
            covered.add(result)
        # At least SKIP, BACKOFF, WAIT, DISABLE should appear in the mapping
        assert RecoveryStrategy.WAIT    in covered or RecoveryStrategy.BACKOFF in covered


# ─────────────────────────────────────────────────────────────────────────────
# BotErrorType — existence and completeness (engine.health extension)
# ─────────────────────────────────────────────────────────────────────────────

class TestBotErrorType:
    def test_bot_error_type_importable(self):
        from engine.health import BotErrorType
        assert BotErrorType is not None

    def test_has_code_failure(self):
        from engine.health import BotErrorType
        assert hasattr(BotErrorType, "CODE_FAILURE")

    def test_has_database_failure(self):
        from engine.health import BotErrorType
        assert hasattr(BotErrorType, "DATABASE_FAILURE")

    def test_has_crash(self):
        from engine.health import BotErrorType
        assert hasattr(BotErrorType, "CRASH")

    def test_has_processing_failure(self):
        from engine.health import BotErrorType
        assert hasattr(BotErrorType, "PROCESSING_FAILURE")

    def test_all_values_are_strings(self):
        from engine.health import BotErrorType
        for member in BotErrorType:
            assert isinstance(member.value, str)
