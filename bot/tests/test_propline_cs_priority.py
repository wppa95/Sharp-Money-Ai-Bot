"""Regression coverage for PropLine-first CS evidence enrichment."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import market_engine
from alerts import _has_strong_underdog_evidence
from engine.delivery_evidence import serialized_evidence_supports_delivery
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


def test_serialized_propline_evidence_requires_sample_count():
    source_only = {"evidence": {"data_source": "propline"}}
    sufficient = {"evidence": {"data_source": "propline", "n_supp": 5}}

    assert _has_strong_underdog_evidence(decision=source_only) is False
    assert market_engine._has_strong_delivery_evidence(decision=source_only) is False
    assert _has_strong_underdog_evidence(decision=sufficient) is True
    assert market_engine._has_strong_delivery_evidence(decision=sufficient) is True


def test_serialized_validation_json_keeps_propline_provenance_and_count():
    decision = {
        "validation_json": json.dumps(
            {"src": "propline", "n": 5, "n_real": 0, "n_supp": 5}
        )
    }

    assert _has_strong_underdog_evidence(decision=decision) is True
    assert market_engine._has_strong_delivery_evidence(decision=decision) is True


@pytest.mark.parametrize(
    "source",
    ["historical_average", "historical-average", "historical line average fallback"],
)
def test_historical_average_without_windows_is_not_telegram_deliverable(source):
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source=source,
        n_real_games=0,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="OVER",
        avg_vs_line_pct=0.12,
        l5_games=None,
        l10_games=None,
    )

    assert _has_strong_underdog_evidence(
        validation=validation,
        decision=decision,
    ) is False
    assert serialized_evidence_supports_delivery(
        {"evidence": {"data_source": source}}
    ) is True


def test_mixed_over_history_is_not_telegram_deliverable():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=31,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="OVER",
        **{
            **{
                f"{window}_games": games
                for window, games in (
                    ("l5", 5),
                    ("l10", 10),
                    ("l15", 15),
                    ("l20", 20),
                    ("l30", 30),
                    ("season", 31),
                )
            },
            **{
                f"{window}_over": over
                for window, over in (
                    ("l5", 5),
                    ("l10", 6),
                    ("l15", 8),
                    ("l20", 11),
                    ("l30", 17),
                    ("season", 17),
                )
            },
        },
    )

    assert _has_strong_underdog_evidence(
        validation=validation,
        decision=decision,
    ) is False
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is False


def test_historical_average_evidence_source_is_read_from_decision_json():
    decision = {
        "recommendation": "OVER",
        "evidence_source": "historical_average",
        "l5_games": None,
        "l10_games": None,
    }
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="historical-average",
        n_real_games=0,
        n_supplemental_games=0,
    )

    assert serialized_evidence_supports_delivery(decision) is True
    assert market_engine._has_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is True


def test_historical_average_evidence_source_without_windows_is_not_deliverable():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="proxy",
        n_real_games=0,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="UNDER",
        evidence_source="historical_average",
        avg_vs_line_pct=-0.12,
    )

    assert market_engine._has_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is True
    assert _has_strong_underdog_evidence(
        validation=validation,
        decision=decision,
    ) is False


def test_telegram_delivery_requires_strong_over_in_every_window():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=10,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="OVER",
        **{
            **{
                f"{window}_games": 10
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
            **{
                f"{window}_over": 7
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
        },
    )
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is True


def test_telegram_delivery_requires_strong_under_in_every_window():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=10,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="UNDER",
        **{
            **{
                f"{window}_games": 10
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
            **{
                # 3/10 OVER = 30%, so the complementary UNDER count is 7/10.
                f"{window}_under": 7
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
        },
    )
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is True


def test_telegram_delivery_rejects_direction_without_strong_window():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=10,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="UNDER",
        l5_games=10,
        l5_over=5,
        l10_games=10,
        l10_over=5,
        l15_games=10,
        l15_over=5,
        l20_games=10,
        l20_over=5,
        l30_games=10,
        l30_over=5,
        season_games=10,
        season_over=5,
    )
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is False


def test_telegram_delivery_rejects_when_one_required_window_is_missing():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=50,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="OVER",
        l5_games=10,
        l5_over=7,
        l10_games=10,
        l10_over=7,
        l20_games=20,
        l20_over=14,
        l30_games=None,
        l30_over=None,
        season_games=50,
        season_over=35,
    )
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is False