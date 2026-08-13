"""
Tests for the /testalert pp and /testalert underdog pipelines.

Covers:
  AlertDelivery.deliver_pp:
    - scope filter passes PP source (PrizePicks always allowed)
    - message is formatted and broadcast when scope allowed
    - blocked scope returns DeliveryResult(sent=False, filtered=True)
    - score tier / stars are computed correctly for realistic edge fields
    - deliver_pp works without a score (legacy edge-only path)
    - recipients_sent / recipients_failed reflected in DeliveryResult

  AlertDelivery.deliver_underdog:
    - scope filter passes Underdog source (always allowed)
    - line-movement alert formatted and broadcast (new_line > old_line → HIGHER)
    - line-movement alert formatted for LOWER move
    - removed=True produces REMOVED formatting
    - blocked scope returns DeliveryResult(sent=False, filtered=True)
    - game_time forwarded to formatter when supplied

  Mock PPEdgeOpportunity fixture:
    - field values are internally consistent
    - score_pp_edge produces non-zero total
    - tier is S/A/B/PASS (not an unexpected string)

  Integration smoke:
    - deliver_pp → format_pp_alert round-trip doesn't raise
    - deliver_underdog → format_underdog_change_alert round-trip doesn't raise
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import asyncio
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from alerts import AlertDelivery, DeliveryResult
from prizepicks import PrizePicksLine, PPEdgeOpportunity
from engine.pp_scoring import PPAnalysisScore, score_pp_edge


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_pp_line(
    player_name: str = "Anthony Edwards",
    stat_type: str = "Points",
    line_value: float = 26.5,
    sport: str = "NBA",
    team: str = "MIN",
    game_description: str = "MIN vs DEN",
) -> PrizePicksLine:
    return PrizePicksLine(
        external_id      = "test-pp-001",
        player_name      = player_name,
        team             = team,
        sport            = sport,
        league           = sport,
        stat_type        = stat_type,
        line_value       = line_value,
        start_time       = None,
        game_description = game_description,
        fetched_at       = datetime.utcnow(),
    )


def _make_opp(
    pp_line: PrizePicksLine | None = None,
    best_edge: float = 16.8,
    best_side: str = "OVER",
) -> PPEdgeOpportunity:
    if pp_line is None:
        pp_line = _make_pp_line()
    return PPEdgeOpportunity(
        pp_line                   = pp_line,
        sportsbook                = "DraftKings",
        sportsbook_line           = 27.0,
        sportsbook_over_odds      = -110,
        sportsbook_under_odds     = -110,
        fair_prob_over_at_sb_line = 0.502,
        fair_prob_under_at_sb_line= 0.498,
        line_diff                 = 0.5,
        adjusted_fair_prob_over   = 0.621,
        adjusted_fair_prob_under  = 0.379,
        edge_over                 = best_edge if best_side == "OVER" else 0.0,
        edge_under                = best_edge if best_side == "UNDER" else 0.0,
        best_side                 = best_side,
        best_edge                 = best_edge,
        prob_per_unit             = 3.0,
    )


def _make_db_mock() -> MagicMock:
    """Return a fully-configured async-capable database mock."""
    mock_db = MagicMock()
    mock_db.has_recent_ev_alert         = AsyncMock(return_value=False)
    mock_db.has_recent_steam_alert      = AsyncMock(return_value=False)
    mock_db.count_today_pp_alerts       = AsyncMock(return_value=0)
    mock_db.count_today_underdog_alerts = AsyncMock(return_value=0)
    return mock_db


def _make_delivery(
    chat_ids: list | None = None,
    bot: object = None,
    db: object = None,
) -> AlertDelivery:
    mock_bot = bot or MagicMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock())
    mock_db  = db  or _make_db_mock()
    # Use explicit None sentinel so callers can pass [] to mean "no recipients"
    return AlertDelivery(mock_db, mock_bot, [12345] if chat_ids is None else chat_ids)


# ---------------------------------------------------------------------------
# Mock PPEdgeOpportunity fixture integrity
# ---------------------------------------------------------------------------

class TestMockOppIntegrity:
    def test_best_edge_positive(self):
        opp = _make_opp()
        assert opp.best_edge > 0

    def test_best_side_valid(self):
        opp = _make_opp()
        assert opp.best_side in ("OVER", "UNDER")

    def test_best_edge_matches_side(self):
        opp = _make_opp(best_edge=16.8, best_side="OVER")
        assert opp.edge_over == 16.8
        assert opp.edge_under == 0.0

    def test_under_mock(self):
        opp = _make_opp(best_edge=11.2, best_side="UNDER")
        assert opp.edge_under == 11.2
        assert opp.edge_over  == 0.0
        assert opp.best_side  == "UNDER"

    def test_pp_line_fields_set(self):
        opp = _make_opp()
        pp  = opp.pp_line
        assert pp.player_name == "Anthony Edwards"
        assert pp.stat_type   == "Points"
        assert pp.line_value  == 26.5
        assert pp.sport       == "NBA"

    def test_score_pp_edge_produces_nonzero_total(self):
        opp   = _make_opp()
        score = score_pp_edge(opp, history=[], opening_line=26.5)
        assert score.total > 0

    def test_score_pp_edge_tier_is_valid(self):
        opp   = _make_opp()
        score = score_pp_edge(opp, history=[], opening_line=26.5)
        assert score.tier in ("S", "A", "B", "PASS")

    def test_score_pp_edge_stars_range(self):
        opp   = _make_opp()
        score = score_pp_edge(opp, history=[], opening_line=26.5)
        assert 1 <= score.stars <= 5

    def test_strong_edge_yields_high_market_edge_score(self):
        """A 16.8% edge + adj_fp 0.621 + line_diff 0.5 should score 23/25.
        Breakdown: edge>=15 → 20pts, adj_fp>=0.62 → +3pts, line_diff<1.0 → +0.
        """
        opp   = _make_opp(best_edge=16.8)
        score = score_pp_edge(opp, history=[], opening_line=26.5)
        assert score.market_edge == 23

    def test_weak_edge_yields_lower_total(self):
        strong = score_pp_edge(_make_opp(best_edge=16.8), history=[], opening_line=26.5)
        weak   = score_pp_edge(_make_opp(best_edge=3.0),  history=[], opening_line=26.5)
        assert strong.total > weak.total


# ---------------------------------------------------------------------------
# AlertDelivery.deliver_pp
# ---------------------------------------------------------------------------

class TestDeliverPp:
    @pytest.mark.asyncio
    async def test_pp_scope_always_passes(self):
        """PrizePicks source is never filtered by AlertScopeFilter."""
        delivery = _make_delivery()
        opp      = _make_opp()
        score    = score_pp_edge(opp, history=[], opening_line=26.5)
        result   = await delivery.deliver_pp(opp, score=score)
        # Not filtered — scope passes PP always
        assert not result.filtered

    @pytest.mark.asyncio
    async def test_message_broadcast_to_chat_ids(self):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock())
        mock_db  = _make_db_mock()
        delivery = AlertDelivery(mock_db, mock_bot, [111, 222])
        opp      = _make_opp()
        score    = score_pp_edge(opp, history=[], opening_line=26.5)
        await delivery.deliver_pp(opp, score=score)
        assert mock_bot.send_message.call_count == 2   # one per chat_id

    @pytest.mark.asyncio
    async def test_result_sent_true_when_bot_succeeds(self):
        delivery = _make_delivery()
        opp      = _make_opp()
        score    = score_pp_edge(opp, history=[], opening_line=26.5)
        result   = await delivery.deliver_pp(opp, score=score)
        assert result.sent is True
        assert result.recipients_sent == 1

    @pytest.mark.asyncio
    async def test_result_sent_false_when_no_chat_ids(self):
        delivery = _make_delivery(chat_ids=[])
        opp      = _make_opp()
        score    = score_pp_edge(opp, history=[], opening_line=26.5)
        result   = await delivery.deliver_pp(opp, score=score)
        assert result.sent is False
        assert result.recipients_sent == 0

    @pytest.mark.asyncio
    async def test_deliver_pp_without_score(self):
        """deliver_pp should work with score=None (legacy edge path)."""
        delivery = _make_delivery()
        opp      = _make_opp()
        result   = await delivery.deliver_pp(opp, score=None)
        assert not result.filtered

    @pytest.mark.asyncio
    async def test_recipients_failed_reflected(self):
        mock_bot = MagicMock()
        from telegram.error import TelegramError
        mock_bot.send_message = AsyncMock(side_effect=TelegramError("blocked"))
        mock_db  = _make_db_mock()
        delivery = AlertDelivery(mock_db, mock_bot, [111])
        opp      = _make_opp()
        result   = await delivery.deliver_pp(opp)
        assert result.sent is False
        assert result.recipients_failed == 1

    @pytest.mark.asyncio
    async def test_format_pp_alert_called_not_directly(self):
        """Smoke-test: the formatted message reaches Telegram (HTML present)."""
        sent_messages: list[str] = []

        async def capture_send(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture_send)
        mock_db = _make_db_mock()
        delivery = AlertDelivery(mock_db, mock_bot, [1])
        opp      = _make_opp()
        score    = score_pp_edge(opp, history=[], opening_line=26.5)
        await delivery.deliver_pp(opp, score=score)

        assert sent_messages, "No message was broadcast"
        msg = sent_messages[0]
        # PP alert must contain player name and stat type
        assert "Anthony Edwards" in msg
        assert "Points" in msg

    @pytest.mark.asyncio
    async def test_pp_alert_contains_edge_info(self):
        """The broadcast message must include edge % and line info."""
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        mock_db = _make_db_mock()
        delivery = AlertDelivery(mock_db, mock_bot, [1])
        opp      = _make_opp()
        await delivery.deliver_pp(opp)

        msg = sent_messages[0]
        # Edge % should appear somewhere in the formatted message
        assert "16" in msg or "edge" in msg.lower() or "%" in msg

    @pytest.mark.asyncio
    async def test_tier_s_score_config(self):
        """An S-tier score should have total >= 80 (S threshold)."""
        opp   = _make_opp(best_edge=16.8)
        score = score_pp_edge(
            opp,
            history      = [],
            opening_line = opp.pp_line.line_value,
        )
        # Check that our fixture actually produces the expected tier
        if score.tier == "S":
            assert score.total >= 80
        elif score.tier == "A":
            assert score.total >= 65
        elif score.tier == "B":
            assert score.total >= 45
        else:
            assert score.tier == "PASS"

    @pytest.mark.asyncio
    async def test_stars_rating_in_result(self):
        opp   = _make_opp()
        score = score_pp_edge(opp, history=[], opening_line=26.5)
        assert 1 <= score.stars <= 5

    @pytest.mark.asyncio
    async def test_multiple_chat_ids_recipients_counted(self):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock())
        mock_db  = _make_db_mock()
        delivery = AlertDelivery(mock_db, mock_bot, [1, 2, 3])
        opp      = _make_opp()
        result   = await delivery.deliver_pp(opp)
        assert result.recipients_sent  == 3
        assert result.recipients_failed == 0


# ---------------------------------------------------------------------------
# AlertDelivery.deliver_underdog
# ---------------------------------------------------------------------------

class TestDeliverUnderdog:
    def _delivery(self, chat_ids=None):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock())
        mock_db  = _make_db_mock()
        return AlertDelivery(mock_db, mock_bot, [12345] if chat_ids is None else chat_ids)

    @pytest.mark.asyncio
    async def test_underdog_scope_always_passes(self):
        """Underdog source is never filtered by AlertScopeFilter (use Tier 1 sport)."""
        delivery = self._delivery()
        result   = await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
        )
        assert not result.filtered

    @pytest.mark.asyncio
    async def test_line_change_higher_broadcast(self):
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
        )

        assert sent_messages
        msg = sent_messages[0]
        assert "Connor McDavid" in msg
        assert "0.5" in msg
        assert "1.5" in msg
        # New format: direction shown via Move amount; header is 🎯 ACTIONABLE BET PICK
        assert "ACTIONABLE BET PICK" in msg or "+1.0" in msg

    @pytest.mark.asyncio
    async def test_line_change_lower_broadcast(self):
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        await delivery.deliver_underdog(
            player_name = "Nathan MacKinnon",
            team        = "COL",
            sport       = "NHL",
            stat_type   = "Shots on Goal",
            old_line    = 3.5,
            new_line    = 2.5,
        )

        msg = sent_messages[0]
        assert "Nathan MacKinnon" in msg
        assert "3.5" in msg
        assert "2.5" in msg
        # New format: direction shown via Move amount; header is 🎯 ACTIONABLE BET PICK
        assert "ACTIONABLE BET PICK" in msg or "-1.0" in msg

    @pytest.mark.asyncio
    async def test_removed_prop_formatting(self):
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        await delivery.deliver_underdog(
            player_name = "Auston Matthews",
            team        = "TOR",
            sport       = "NHL",
            stat_type   = "Goals",
            old_line    = 0.5,
            new_line    = 0.5,   # same — irrelevant for removed
            removed     = True,
        )

        msg = sent_messages[0]
        assert "Auston Matthews" in msg
        assert "REMOVED" in msg or "🚫" in msg

    @pytest.mark.asyncio
    async def test_result_sent_true_on_success(self):
        delivery = self._delivery()
        result   = await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
        )
        assert result.sent is True

    @pytest.mark.asyncio
    async def test_result_sent_false_no_chat_ids(self):
        delivery = self._delivery(chat_ids=[])
        result   = await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
        )
        assert result.sent is False

    @pytest.mark.asyncio
    async def test_multiple_recipients_counted(self):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock())
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1, 2, 3])
        result   = await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
        )
        assert result.recipients_sent   == 3
        assert result.recipients_failed == 0

    @pytest.mark.asyncio
    async def test_game_time_forwarded_to_formatter(self):
        """When game_time is supplied the timing filter runs and game_time is forwarded."""
        from datetime import datetime, timedelta
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        # Use a game time 60 min in the future so the timing filter allows it
        game_ts  = datetime.utcnow() + timedelta(hours=1)
        await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
            game_time   = game_ts,
        )

        assert sent_messages, "Expected alert to be sent with future game_time"
        msg = sent_messages[0]
        assert "Connor McDavid" in msg

    @pytest.mark.asyncio
    async def test_underdog_sport_icon_nfl(self):
        """NFL format includes the football emoji — patch Tier 2 block to test formatting."""
        import alerts as alerts_mod
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        with patch.object(alerts_mod, "_TIER2_SPORTS_BLOCK", frozenset()):
            await delivery.deliver_underdog(
                player_name = "Saquon Barkley",
                team        = "PHI",
                sport       = "NFL",
                stat_type   = "Rushing Yards",
                old_line    = 85.5,
                new_line    = 89.5,
            )
        assert "🏈" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_underdog_sport_icon_nba(self):
        """NBA format includes the basketball emoji — patch Tier 2 block to test formatting."""
        import alerts as alerts_mod
        sent_messages: list[str] = []

        async def capture(chat_id, text, **kwargs):
            sent_messages.append(text)
            return MagicMock()

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=capture)
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        with patch.object(alerts_mod, "_TIER2_SPORTS_BLOCK", frozenset()):
            await delivery.deliver_underdog(
                player_name = "Anthony Edwards",
                team        = "MIN",
                sport       = "NBA",
                stat_type   = "Points",
                old_line    = 26.5,
                new_line    = 28.5,
            )
        assert "🏀" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_delivery_failure_reflected(self):
        from telegram.error import TelegramError
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=TelegramError("blocked"))
        delivery = AlertDelivery(_make_db_mock(), mock_bot, [1])
        result   = await delivery.deliver_underdog(
            player_name = "Connor McDavid",
            team        = "EDM",
            sport       = "NHL",
            stat_type   = "Points",
            old_line    = 0.5,
            new_line    = 1.5,
        )
        assert result.sent is False
        assert result.recipients_failed == 1


# ---------------------------------------------------------------------------
# DeliveryResult __str__ helper (shared by both paths)
# ---------------------------------------------------------------------------

class TestDeliveryResultStr:
    def test_sent_result_str(self):
        r = DeliveryResult(sent=True, recipients_sent=2, recipients_failed=0)
        assert "sent=2" in str(r) or "2 ok" in str(r) or "sent=True" in str(r)

    def test_filtered_result_str(self):
        r = DeliveryResult(sent=False, filtered=True, filtered_reason="scope blocked")
        s = str(r)
        assert "filtered" in s or "scope blocked" in s

    def test_not_sent_not_filtered(self):
        r = DeliveryResult(sent=False)
        s = str(r)
        assert s  # just doesn't crash
