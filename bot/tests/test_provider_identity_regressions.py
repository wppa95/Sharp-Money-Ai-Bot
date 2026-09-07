"""Focused regressions for provider-owned identity and user-facing labels."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from alerts import format_pp_alert
from alerts_multiplatform import format_underdog_change_alert
from database import Database
from engine.player_prop_market import (
    _is_prop_deduped,
    _record_prop_alerted,
)
from prizepicks import PrizePicksLine, compare_pp_to_sportsbook


def test_extended_dedup_keeps_provider_event_and_direction_separate():
    alerted: dict = {}
    _record_prop_alerted(
        alerted,
        "LeBron James",
        "NBA",
        "Points",
        25.5,
        now_ts=100.0,
        provider="PrizePicks",
        source_id="projection-1",
        event_id="game-1",
        direction="OVER",
    )

    common = dict(
        dedup_window_seconds=3600,
        min_line_change=0.5,
        now_ts=101.0,
    )
    assert _is_prop_deduped(
        alerted,
        "LeBron James",
        "NBA",
        "Points",
        25.5,
        provider="PrizePicks",
        source_id="projection-1",
        event_id="game-1",
        direction="OVER",
        **common,
    )
    assert not _is_prop_deduped(
        alerted,
        "LeBron James",
        "NBA",
        "Points",
        25.5,
        provider="Underdog",
        source_id="projection-1",
        event_id="game-1",
        direction="OVER",
        **common,
    )


def test_logical_dedup_blocks_rotated_source_and_event_ids():
    alerted: dict = {}
    _record_prop_alerted(
        alerted,
        "Cedric Mullins",
        "MLB",
        "Hits",
        1.5,
        now_ts=100.0,
        provider="Underdog",
        source_id="projection-old",
        event_id="event-old",
        direction="OVER",
    )

    assert _is_prop_deduped(
        alerted,
        "Cedric Mullins",
        "MLB",
        "Hits",
        1.5,
        dedup_window_seconds=3600,
        min_line_change=0.5,
        now_ts=101.0,
        provider="Underdog",
        source_id="projection-new",
        event_id="event-new",
        direction="OVER",
    )
    assert not _is_prop_deduped(
        alerted,
        "Cedric Mullins",
        "MLB",
        "Hits",
        2.0,
        dedup_window_seconds=3600,
        min_line_change=0.5,
        now_ts=101.0,
        provider="Underdog",
        source_id="projection-new",
        event_id="event-new",
        direction="OVER",
    )


@pytest.mark.asyncio
async def test_restart_restore_keeps_rotated_identity_dedup(tmp_path):
    """SQLite restore must retain logical dedup across provider ID rotation."""
    import market_engine as me

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'restart-dedup.sqlite'}")
    await db.init()
    original_alerted = dict(me._prop_market_alerted)
    me._prop_market_alerted.clear()

    try:
        await db.log_prop_opportunity(
            external_id="projection-old",
            player_name="Cedric Mullins",
            team="BAL",
            sport="MLB",
            stat_type="Hits",
            line_value=1.5,
            recommendation="OVER",
            decision_tier="A",
            confidence=80,
            game_time=None,
            game_id="event-old",
            provider="Underdog",
        )
        assert await db.record_telegram_play_delivery(
            provider="Underdog",
            delivery_path="standing",
            player_name="Cedric Mullins",
            sport="MLB",
            stat_type="Hits",
            external_id="projection-old",
            event_id="event-old",
            line_value=1.5,
            recipients_sent=1,
            recipients_failed=0,
        )

        # Simulate a fresh process: close the first pool, reopen the persisted
        # database, and start with no in-memory identity.
        await db.close()
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'restart-dedup.sqlite'}")
        await db.init()
        await me._init_state_from_db(db)

        common = dict(
            dedup_window_seconds=3600,
            min_line_change=0.5,
            provider="Underdog",
            source_id="projection-new",
            event_id="event-new",
            now_ts=time.time(),
        )
        assert _is_prop_deduped(
            me._prop_market_alerted,
            "Cedric Mullins",
            "MLB",
            "Hits",
            1.5,
            direction="OVER",
            **common,
        )
        assert not _is_prop_deduped(
            me._prop_market_alerted,
            "Cedric Mullins",
            "MLB",
            "Hits",
            2.0,
            direction="OVER",
            **common,
        )
        assert not _is_prop_deduped(
            me._prop_market_alerted,
            "Cedric Mullins",
            "MLB",
            "Hits",
            1.5,
            direction="UNDER",
            **common,
        )
    finally:
        me._prop_market_alerted.clear()
        me._prop_market_alerted.update(original_alerted)
        await db.close()


def test_prizepicks_edge_alert_uses_shared_player_prop_heading_and_line():
    pp = PrizePicksLine(
        external_id="projection-1",
        player_name="LeBron James",
        team="LAL",
        sport="NBA",
        league="NBA",
        stat_type="Points",
        line_value=25.5,
        start_time=datetime(2026, 8, 28, 20, 0),
        game_description="vs LAC",
    )
    opportunity = compare_pp_to_sportsbook(
        pp,
        sportsbook="Pinnacle",
        sb_line=27.5,
        sb_over_odds=-110,
        sb_under_odds=-110,
    )

    message = format_pp_alert(opportunity)
    assert "📊 <b>PLAYER PROP PLAY</b>" in message
    assert "Player Prop Line" in message
    assert "ACTIONABLE BET PICK" not in message
    assert "verify before placing" not in message.lower()


def test_change_alert_uses_shared_player_prop_heading_and_line_label():
    for provider in ("Underdog", "PrizePicks"):
        message = format_underdog_change_alert(
            player_name="LeBron James",
            team="LAL",
            sport="NBA",
            stat_type="Points",
            old_line=24.5,
            new_line=25.5,
            source=provider,
        )
        assert "📊 <b>PLAYER PROP PLAY</b>" in message
        assert "PLAYER PROP LINE" in message