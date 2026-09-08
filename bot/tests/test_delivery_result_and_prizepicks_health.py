"""Focused regressions for delivery-result handling and PrizePicks reporting."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import commands
import market_engine
from alerts import DeliveryResult
from engine.health import HealthTracker


def _update_and_context():
    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    return update, context


def test_delivery_result_string_covers_expected_outcomes():
    assert "filtered" in str(
        DeliveryResult(sent=False, filtered=True, filtered_reason="stale line")
    )
    assert str(DeliveryResult(sent=False, deduped=True)) == "deduped"
    assert "2 ok" in str(
        DeliveryResult(sent=True, recipients_sent=2, recipients_failed=1)
    )


def test_ranked_delivery_uses_delivery_result_contract_not_reason():
    source = inspect.getsource(market_engine.underdog_job)
    assert "_dq_result.reason" not in source
    assert "str(_dq_result)" in source


def test_legacy_prizepicks_scheduler_callback_is_removed():
    import main

    assert not hasattr(main, "_prizepicks_job")
    assert not hasattr(main, "prizepicks_job")


def test_provider_database_write_isolated_and_persisted(tmp_path: Path):
    tracker = HealthTracker(path=tmp_path / "health.json")
    tracker.record_provider_database_write("PrizePicks", "prop_line_history")

    pp_info = tracker.get_provider_info("PrizePicks")
    ud_info = tracker.get_provider_info("Underdog")
    assert pp_info["last_db_write_operation"] == "prop_line_history"
    assert tracker.provider_last_db_write_str("PrizePicks") != "—"
    assert "last_db_write_ts" not in ud_info


def test_internal_propline_feed_counts_are_not_rendered_in_telegram(tmp_path: Path):
    tracker = HealthTracker(path=tmp_path / "health.json")
    tracker.record_provider_feed_counts(
        "PrizePicks", "NBA", 0, 0, succeeded=True
    )
    tracker.record_provider_feed_counts(
        "PrizePicks", "MLB", 9, 4, succeeded=True
    )
    tracker.record_provider_feed_counts(
        "PrizePicks", "WNBA", 0, 0, succeeded=False
    )

    counts = tracker.get_provider_feed_counts("PrizePicks")
    assert counts["NBA"]["state"] == "empty"
    assert counts["NBA"]["raw_records"] == 0
    assert counts["NBA"]["normalized_props"] == 0
    assert counts["MLB"]["state"] == "ok"
    assert counts["WNBA"]["state"] == "failed"

    rendered = "\n".join(commands._prizepicks_feed_count_lines(tracker))
    assert rendered == ""
    assert "PrizePicks" not in rendered
    assert "PropLine" not in rendered


@pytest.mark.asyncio
async def test_health_keeps_only_compact_player_props_runtime_summary(tmp_path: Path):
    update, context = _update_and_context()
    tracker = HealthTracker(path=tmp_path / "health.json")
    tracker.record_provider_fetch("PrizePicks")
    tracker.record_provider_database_write("PrizePicks")

    with (
        patch.object(commands, "_check_allowed", return_value=True),
        patch("engine.health.get_health_tracker", return_value=tracker),
    ):
        await commands.cmd_health(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "Player Props Pipeline" in text
    assert "Observed:" not in text
    assert "Delivered alerts:" not in text


@pytest.mark.asyncio
async def test_status_uses_shared_monitoring_cycle_without_merging_ud():
    update, context = _update_and_context()
    db = MagicMock()
    db.count_today_actionable_alerts = AsyncMock(return_value=0)
    db.count_prop_line_history = AsyncMock(side_effect=lambda provider=None: {
        None: 20,
        "Underdog": 15,
        "PrizePicks": 5,
    }[provider])
    db.get_telegram_reconciliation = AsyncMock(return_value={
        "by_provider": {
            "Underdog": {"observed": 99},
            "PrizePicks": {
                "observed": 5,
                "tracked": 4,
                "evaluated": 5,
                "actionable_persisted": 1,
                "delivered_alerts": 1,
            },
        }
    })
    monitor = MagicMock()
    monitor.get_all_health.return_value = {}

    with (
        patch.object(commands, "_check_allowed", return_value=True),
        patch.object(commands, "_db", db),
        patch("providers.get_health_monitor", return_value=monitor),
        patch("engine.health.get_health_tracker", return_value=None),
    ):
        await commands.cmd_status(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "Player Props Pipeline" not in text
    assert "Latest Underdog scan" not in text
    assert "Latest monitoring cycle" not in text
    assert "Observed:            99" not in text
    assert "History: Underdog: 15" in text
    assert "PrizePicks" not in text


@pytest.mark.asyncio
async def test_status_shows_prizepicks_feed_counts_by_sport(tmp_path: Path):
    update, context = _update_and_context()
    db = MagicMock()
    db.count_today_actionable_alerts = AsyncMock(return_value=0)
    db.count_prop_line_history = AsyncMock(side_effect=lambda provider=None: {
        None: 20,
        "Underdog": 15,
        "PrizePicks": 5,
    }[provider])
    tracker = HealthTracker(path=tmp_path / "health.json")
    tracker.record_provider_feed_counts(
        "PrizePicks", "NBA", 0, 0, succeeded=True
    )
    tracker.record_provider_feed_counts(
        "PrizePicks", "MLB", 6, 3, succeeded=True
    )

    with (
        patch.object(commands, "_check_allowed", return_value=True),
        patch.object(commands, "_db", db),
        patch("providers.get_health_monitor", return_value=MagicMock()),
        patch("engine.health.get_health_tracker", return_value=tracker),
    ):
        await commands.cmd_status(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "Player Props feed by sport" not in text
    assert "NBA: 0 raw records · 0 normalized props · empty" not in text
    assert "MLB: 6 raw records · 3 normalized props · ok" not in text