"""
test_player_block.py — Contract tests for engine/player_block.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from engine.player_block import (
    PlayerBlock,
    BLOCKABLE_REASONS,
    NON_BLOCKABLE_REASONS,
    ALL_REASON_CODES,
    is_blocked,
    filter_blocked,
    validate_reason_code,
    reason_code_explanation,
    blocks_summary_telegram,
)
from engine.candidate import candidate_from_ud_decision
from types import SimpleNamespace


def _block(
    player_key: str   = "lebron_james",
    player_name: str  = "LeBron James",
    sport: str        = "NBA",
    reason: str       = "INJURY",
    block_type: str   = "PERMANENT",
    expires_at        = None,
    created_at        = None,
) -> PlayerBlock:
    if block_type == "TEMPORARY" and expires_at is None:
        expires_at = datetime.utcnow() + timedelta(days=7)
    return PlayerBlock(
        player_key  = player_key,
        player_name = player_name,
        sport       = sport,
        reason_code = reason,
        description = "Test block",
        block_type  = block_type,
        expires_at  = expires_at,
        created_at  = created_at or datetime.utcnow(),
    )


def _cand(player_name="LeBron James", sport="NBA"):
    dec = SimpleNamespace(
        confidence=70, decision_tier="A",
        recommendation="OVER", reason="test",
        hit_rates={}, window_agreement=0,
    )
    score = SimpleNamespace(total=65, n_history=10)
    return candidate_from_ud_decision(
        player_name=player_name, sport=sport, stat_type="points",
        line=25.5, decision=dec, score=score,
    )


# ── BLOCKABLE_REASONS / codes ─────────────────────────────────────────────────

class TestReasonCodes:
    def test_blockable_reasons_non_empty(self):
        assert len(BLOCKABLE_REASONS) > 0

    def test_non_blockable_non_empty(self):
        assert len(NON_BLOCKABLE_REASONS) > 0

    def test_disjoint(self):
        assert BLOCKABLE_REASONS.isdisjoint(NON_BLOCKABLE_REASONS)

    def test_union_is_all(self):
        assert ALL_REASON_CODES == BLOCKABLE_REASONS | NON_BLOCKABLE_REASONS

    def test_validate_reason_code_true_for_blockable(self):
        for code in BLOCKABLE_REASONS:
            assert validate_reason_code(code)

    def test_validate_reason_code_false_for_non_blockable(self):
        for code in NON_BLOCKABLE_REASONS:
            assert not validate_reason_code(code)

    def test_reason_code_explanation_returns_string(self):
        for code in ALL_REASON_CODES:
            exp = reason_code_explanation(code)
            assert isinstance(exp, str) and len(exp) > 0

    def test_reason_code_explanation_unknown(self):
        exp = reason_code_explanation("UNKNOWN_CODE")
        assert "Unknown" in exp or "unknown" in exp.lower()


# ── PlayerBlock dataclass ─────────────────────────────────────────────────────

class TestPlayerBlock:
    def test_permanent_block_is_frozen(self):
        b = _block(block_type="PERMANENT")
        with pytest.raises((AttributeError, TypeError)):
            b.reason_code = "AVAILABILITY"

    def test_permanent_block_is_active(self):
        b = _block(block_type="PERMANENT")
        assert b.is_active is True

    def test_temporary_block_active_before_expiry(self):
        b = _block(block_type="TEMPORARY", expires_at=datetime.utcnow() + timedelta(hours=1))
        assert b.is_active is True

    def test_temporary_block_expired_is_inactive(self):
        b = _block(block_type="TEMPORARY", expires_at=datetime.utcnow() - timedelta(hours=1))
        assert b.is_active is False

    def test_invalid_reason_code_raises(self):
        with pytest.raises(ValueError, match="not blockable"):
            _block(reason="NORMAL_VARIANCE")

    def test_invalid_block_type_raises(self):
        with pytest.raises(ValueError, match="block_type"):
            PlayerBlock(
                player_key="x", player_name="x", sport="NBA",
                reason_code="INJURY", description="x",
                block_type="INVALID",
            )

    def test_temporary_without_expiry_raises(self):
        with pytest.raises(ValueError, match="expires_at"):
            PlayerBlock(
                player_key="x", player_name="x", sport="NBA",
                reason_code="INJURY", description="x",
                block_type="TEMPORARY",
                expires_at=None,
            )

    def test_to_dict_has_required_keys(self):
        b = _block()
        d = b.to_dict()
        for key in ("player_key", "player_name", "sport", "reason_code",
                    "block_type", "is_active", "created_at"):
            assert key in d

    def test_to_telegram_returns_string(self):
        b = _block()
        t = b.to_telegram()
        assert isinstance(t, str)
        assert "LeBron James" in t

    def test_reason_label_readable(self):
        for reason in BLOCKABLE_REASONS:
            b = _block(reason=reason)
            assert b.reason_label and len(b.reason_label) > 0

    def test_all_sports_block_has_empty_sport(self):
        b = _block(sport="")
        assert b.sport == ""

    def test_temporary_block_to_dict_has_expires_at(self):
        exp = datetime.utcnow() + timedelta(days=3)
        b = _block(block_type="TEMPORARY", expires_at=exp)
        d = b.to_dict()
        assert d["expires_at"] is not None


# ── is_blocked ────────────────────────────────────────────────────────────────

class TestIsBlocked:
    def test_returns_block_when_matching(self):
        blocks = [_block(player_key="lebron_james", sport="NBA", block_type="PERMANENT")]
        result = is_blocked("lebron_james", "NBA", blocks)
        assert result is not None
        assert result.player_key == "lebron_james"

    def test_returns_none_when_no_blocks(self):
        assert is_blocked("lebron_james", "NBA", []) is None

    def test_returns_none_when_different_player(self):
        blocks = [_block(player_key="anthony_davis", sport="NBA")]
        assert is_blocked("lebron_james", "NBA", blocks) is None

    def test_returns_none_when_different_sport(self):
        blocks = [_block(player_key="player_x", sport="MLB")]
        assert is_blocked("player_x", "NBA", blocks) is None

    def test_all_sports_block_matches_any_sport(self):
        # player_key in blocks must match the candidate key format
        c_nba = _cand(player_name="Test Player", sport="NBA")
        pkey = c_nba.player_key
        blocks = [_block(player_key=pkey, sport="")]
        # should match regardless of sport when block.sport is ""
        assert is_blocked(pkey, "NBA", blocks) is not None
        assert is_blocked(pkey, "MLB", blocks) is not None

    def test_expired_temporary_block_not_returned(self):
        c = _cand(player_name="LeBron James")
        expired = _block(
            player_key=c.player_key,
            block_type="TEMPORARY",
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        result = is_blocked(c.player_key, "NBA", [expired])
        assert result is None

    def test_active_temporary_block_returned(self):
        c = _cand(player_name="LeBron James")
        active = _block(
            player_key=c.player_key,
            block_type="TEMPORARY",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        result = is_blocked(c.player_key, "NBA", [active])
        assert result is not None

    def test_sport_case_insensitive(self):
        c = _cand(player_name="LeBron James")
        blocks = [_block(player_key=c.player_key, sport="NBA")]
        assert is_blocked(c.player_key, "nba", blocks) is not None

    def test_first_active_block_returned(self):
        c = _cand(player_name="LeBron James")
        pkey = c.player_key
        b1 = _block(player_key=pkey, sport="NBA", reason="INJURY")
        b2 = _block(player_key=pkey, sport="NBA", reason="AVAILABILITY")
        result = is_blocked(pkey, "NBA", [b1, b2])
        assert result is not None


# ── filter_blocked ────────────────────────────────────────────────────────────

class TestFilterBlocked:
    def test_no_blocks_all_allowed(self):
        candidates = [_cand(player_name=f"Player {i}") for i in range(3)]
        allowed, blocked = filter_blocked(candidates, [])
        assert len(allowed) == 3
        assert len(blocked) == 0

    def test_blocked_player_separated(self):
        c1 = _cand(player_name="LeBron James", sport="NBA")
        c2 = _cand(player_name="Anthony Davis", sport="NBA")
        full_key_c1 = c1.player_key   # e.g. "NBA:lebron_james"
        blocks = [_block(player_key=full_key_c1, sport="NBA")]
        allowed, blocked_pairs = filter_blocked([c1, c2], blocks)
        assert len(allowed) == 1
        assert len(blocked_pairs) == 1
        assert blocked_pairs[0][0].player_key == full_key_c1

    def test_blocked_pair_contains_block_object(self):
        c = _cand(player_name="LeBron James")
        b = _block(player_key=c.player_key)  # use the full key from the candidate
        _, blocked_pairs = filter_blocked([c], [b])
        assert blocked_pairs[0][1] is b


# ── blocks_summary_telegram ───────────────────────────────────────────────────

class TestBlocksSummaryTelegram:
    def test_no_blocks_returns_clean_message(self):
        s = blocks_summary_telegram([])
        assert "No active" in s

    def test_with_blocks_shows_count(self):
        blocks = [_block(), _block(player_key="another_player")]
        s = blocks_summary_telegram(blocks)
        assert "2" in s or "LeBron James" in s

    def test_returns_string(self):
        assert isinstance(blocks_summary_telegram([]), str)

    def test_expired_blocks_not_shown(self):
        expired = _block(block_type="TEMPORARY", expires_at=datetime.utcnow() - timedelta(hours=1))
        s = blocks_summary_telegram([expired])
        assert "No active" in s
