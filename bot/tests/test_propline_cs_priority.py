"""Regression coverage for PropLine-first CS evidence enrichment."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import market_engine
from alerts import _has_strong_underdog_evidence
from engine.player_validator import PlayerPropValidation
from providers.player_stats import RawGameResult


class _DB:
    def __init__(self) -> None:
        self.get_player_results = AsyncMock(return_value=[])
        self.upsert_player_result = AsyncMock()


def _propline_rows() -> list[RawGameResult]:
    return [
        RawGameResult(
            player_name="CS Player",
            sport="CS",
            stat_type="kills",
            game_date=f"2026-08-{29 - index:02d}",
            actual_value=20.0 + index,
            opponent=None,
            source="propline_history",
            game_id=f"propline-{index}",
        )
        for index in range(5)
    ]


@pytest.mark.asyncio
async def test_cs_uses_sufficient_propline_history_without_pandascore():
    db = _DB()
    primary_provider = type("PrimaryProvider", (), {})()
    primary_provider.fetch_results = AsyncMock(
        side_effect=AssertionError("PandaScore-backed provider should be skipped")
    )
    history_provider = type("HistoryProvider", (), {})()
    history_provider.supplement_missing_results = AsyncMock(
        return_value=_propline_rows()
    )

    with (
        patch.object(
            market_engine,
            "_get_player_stats_provider",
            return_value=primary_provider,
        ),
        patch(
            "providers.player_history.get_player_history_provider",
            return_value=history_provider,
        ),
        patch.object(
            market_engine.config,
            "UD_VALIDATION_MIN_SAMPLES",
            5,
        ),
    ):
        result = await market_engine._fetch_and_compute_hit_rates(
            db,
            "CS Player",
            "CS",
            "Kills",
            19.5,
            source_provider="Underdog",
        )

    assert result is not None
    assert result.has_real_data is True
    assert result.total_games == 5
    assert result.data_source == "propline"
    assert result.n_supplemental_games == 5
    primary_provider.fetch_results.assert_not_awaited()
    history_provider.supplement_missing_results.assert_awaited_once()


def test_propline_history_is_valid_delivery_evidence():
    validation = PlayerPropValidation(
        player_name="CS Player",
        stat_type="Kills",
        n_history=5,
        l5_rate=0.8,
        l10_rate=None,
        l20_rate=None,
        l30_rate=None,
        avg_line=None,
        min_line_seen=None,
        rate_at_or_below=None,
        season_hit_rate=0.8,
        h2h_hit_rate=None,
        has_supporting_data=True,
        reason="Supporting data available",
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=5,
        supplemental_sources=("propline_history",),
    )

    assert _has_strong_underdog_evidence(validation=validation) is True
    assert market_engine._has_strong_delivery_evidence(validation=validation) is True