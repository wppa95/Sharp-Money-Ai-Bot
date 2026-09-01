"""Focused regressions for provider-owned identity and user-facing labels."""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from alerts import format_pp_alert
from alerts_multiplatform import format_underdog_change_alert
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
    assert not _is_prop_deduped(
        alerted,
        "LeBron James",
        "NBA",
        "Points",
        25.5,
        provider="PrizePicks",
        source_id="projection-1",
        event_id="game-2",
        direction="OVER",
        **common,
    )
    assert not _is_prop_deduped(
        alerted,
        "LeBron James",
        "NBA",
        "Points",
        25.5,
        provider="PrizePicks",
        source_id="projection-1",
        event_id="game-1",
        direction="UNDER",
        **common,
    )


def test_prizepicks_edge_alert_has_provider_specific_heading_and_line():
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
    assert "📊 <b>SHARP MONEY PLAY</b>" in message
    assert "PrizePicks Line" in message
    assert "ACTIONABLE BET PICK" not in message
    assert "verify before placing" not in message.lower()


def test_change_alert_preserves_source_heading_and_line_label():
    for provider, line_label in (
        ("Underdog", "LINE</b> · Underdog"),
        ("PrizePicks", "LINE</b> · PrizePicks"),
    ):
        message = format_underdog_change_alert(
            player_name="LeBron James",
            team="LAL",
            sport="NBA",
            stat_type="Points",
            old_line=24.5,
            new_line=25.5,
            source=provider,
        )
        assert "📊 <b>SHARP MONEY PLAY</b>" in message
        assert line_label in message