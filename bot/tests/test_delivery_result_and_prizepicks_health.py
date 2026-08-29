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
    assert "rate-limited" in str(
        DeliveryResult(sent=False, rate_limited=True, filtered_reason="flood brake")
    )
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


def test_provider_database_write_isolated_and_persisted(tmp_path: Path):
    tracker = HealthTracker(path=tmp_path / "health.json")
    tracker.record_provider_database_write("PrizePicks", "prop_line_history")

    pp_info = tracker.get_provider_info("PrizePicks")
    ud_info = tracker.get_provider_info("Underdog")
    assert pp_info["last_db_write_operation"] == "prop_line_history"
    assert tracker.provider_last_db_write_str("PrizePicks") != "—"
    assert "last_db_write_ts" not in ud_info


def test_prizepicks_pipeline_renderer_does_not_borrow_underdog_counts():
    lines: list[str] = []
    commands._append_provider_pipeline(
        lines,
        provider="PrizePicks",
        health_tracker=None,
        reconciliation={
            "observed": 12,
            "tracked": 7,
            "evaluated": 10,
            "actionable_persisted": 3,
            "delivered_alerts": 2,
        },
    )

    text = "\n".join(lines)
    assert "PrizePicks Pipeline" in text
    assert "Observed:" in text and "12" in text
    assert "Tracked active:" in text and "7" in text
    assert "Evaluated:" in text and "10" in text
    assert "Directional/actionable:" in text and "3" in text
    assert "Delivered alerts:" in text and "2" in text
    assert "Underdog" not in text


def test_prizepicks_pipeline_renderer_has_clean_empty_state():
    lines: list[str] = []
    commands._append_provider_pipeline(
        lines,
        provider="PrizePicks",
        health_tracker=None,
        reconciliation=None,
    )

    text = "\n".join(lines)
    assert "No normalized observations in the last 24h." in text
    assert "unavailable" in text


@pytest.mark.asyncio
async def test_health_includes_provider_isolated_prizepicks_pipeline(tmp_path: Path):
    update, context = _update_and_context()
    tracker = HealthTracker(path=tmp_path / "health.json")
    tracker.record_provider_fetch("PrizePicks")
    tracker.record_provider_database_write("PrizePicks")

    db = MagicMock()
    db.get_telegram_reconciliation = AsyncMock(return_value={
        "by_provider": {
            "PrizePicks": {
                "observed": 12,
                "tracked": 7,
                "evaluated": 10,
                "actionable_persisted": 3,
                "delivered_alerts": 2,
            }
        }
    })

    with (
        patch.object(commands, "_check_allowed", return_value=True),
        patch.object(commands, "_db", db),
        patch("engine.health.get_health_tracker", return_value=tracker),
    ):
        await commands.cmd_health(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "PrizePicks Pipeline" in text
    assert "Observed:" in text and "12" in text
    assert "Delivered alerts:" in text and "2" in text


@pytest.mark.asyncio
async def test_status_includes_prizepicks_pipeline_without_merging_ud():
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
    assert "PrizePicks Pipeline" in text
    assert "Observed:" in text and "5" in text
    assert "Delivered alerts:" in text and "1" in text
    assert "Observed:            99" not in text