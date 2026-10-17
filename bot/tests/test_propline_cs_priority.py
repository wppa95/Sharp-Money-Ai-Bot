"""Regression coverage for CS evidence enrichment boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import market_engine
from alerts import _has_strong_underdog_evidence, is_telegram_suppressed_sport
from engine.delivery_evidence import serialized_evidence_supports_delivery
from engine.player_validator import PlayerPropValidation
from providers.propline import official_player_market_for
from providers.player_stats import RawGameResult


class _DB:
    def __init__(self) -> None:
        self.get_player_results = AsyncMock(return_value=_propline_rows())
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


def _directional_history(
    direction: str = "OVER",
    *,
    over_rate: float | None = None,
) -> dict:
    """Complete six-window history meeting the universal delivery cutoff."""
    if over_rate is None:
        over_rate = 0.75 if direction.upper() == "OVER" else 0.25
    windows = ("l5", "l10", "l15", "l20", "l30", "season")
    over = round(over_rate * 100)
    return {
        "recommendation": direction,
        "confidence": 75,
        **{
            f"{window}_games": 100
            for window in windows
        },
        **{
            f"{window}_over": over
            for window in windows
        },
        **{
            f"{window}_under": 100 - over
            for window in windows
        },
    }


@pytest.mark.asyncio
async def test_cs_does_not_use_singular_propline_history_enrichment():
    db = _DB()
    primary_provider = type("PrimaryProvider", (), {})()
    primary_provider.fetch_results = AsyncMock(
        return_value=_propline_rows()
    )

    with (
        patch.object(
            market_engine,
            "_get_player_stats_provider",
            return_value=primary_provider,
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
    assert result.n_supplemental_games == 5
    # Existing complete shared history wins over another provider request.
    primary_provider.fetch_results.assert_not_awaited()


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

    assert _has_strong_underdog_evidence(
        sport="CS",
        validation=validation,
        decision=SimpleNamespace(**_directional_history()),
    ) is True
    assert market_engine._has_strong_delivery_evidence(validation=validation) is True


def test_serialized_propline_evidence_requires_sample_count():
    source_only = {"evidence": {"data_source": "propline"}}
    sufficient = {"evidence": {"data_source": "propline", "n_supp": 5}}
    sufficient_directional = {
        **sufficient,
        "recommendation": "OVER",
    }

    assert _has_strong_underdog_evidence(decision=source_only) is False
    assert market_engine._has_strong_delivery_evidence(decision=source_only) is False
    assert _has_strong_underdog_evidence(
        sport="CS",
        decision=sufficient_directional,
    ) is True
    assert market_engine._has_strong_delivery_evidence(decision=sufficient) is True


def test_serialized_validation_json_keeps_propline_provenance_and_count():
    decision = {
        "recommendation": "OVER",
        "validation_json": json.dumps(
            {"src": "propline", "n": 5, "n_real": 0, "n_supp": 5}
        )
    }

    assert _has_strong_underdog_evidence(
        sport="CS",
        decision={**decision, "recommendation": "OVER"},
    ) is True
    assert market_engine._has_strong_delivery_evidence(
        decision={"validation_json": decision["validation_json"]},
    ) is True


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
        sport="SOCCER",
        validation=validation,
        decision=decision,
    ) is False
    assert serialized_evidence_supports_delivery(
        {"evidence": {"data_source": source}}
    ) is True


def test_primary_history_without_propline_is_telegram_deliverable():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real",
        n_real_games=31,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(**_directional_history())

    assert _has_strong_underdog_evidence(
        sport="MLB",
        validation=validation,
        decision=decision,
    ) is True
    assert market_engine._has_delivery_evidence(
        sport="CS",
        validation=validation,
        decision=decision,
    ) is True
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is True


@pytest.mark.parametrize("sport", ["KBO", "NPB"])
def test_kbo_npb_allow_direction_only_but_do_not_guess_markets(sport):
    direction_only = SimpleNamespace(
        recommendation="OVER",
        evidence_source=None,
    )
    assert market_engine._has_delivery_evidence(
        sport=sport,
        decision=direction_only,
    ) is True
    assert _has_strong_underdog_evidence(
        sport=sport,
        decision=direction_only,
    ) is True

    settled_history = SimpleNamespace(
        evidence_source="propline",
        n_supplemental_games=5,
        **_directional_history(),
    )
    assert market_engine._has_delivery_evidence(
        sport=sport,
        decision=settled_history,
    ) is True
    assert _has_strong_underdog_evidence(
        sport=sport,
        decision=settled_history,
    ) is True

    # Direction-only Telegram eligibility does not authorize inventing a
    # PropLine market key for settlement or grading.
    assert official_player_market_for("Hits", sport=sport) is None


@pytest.mark.parametrize(
    "sport",
    ["NFL", "CFB"],
)
def test_gridiron_allows_mixed_primary_and_propline_results(sport):
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="real+propline",
        n_real_games=3,
        n_supplemental_games=2,
    )
    decision = SimpleNamespace(**_directional_history())

    assert _has_strong_underdog_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is True
    assert market_engine._has_delivery_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is True


@pytest.mark.parametrize(
    "sport",
    ["NFL", "CFB", "NCAAF"],
)
def test_gridiron_accepts_sufficient_settled_propline_history(sport):
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=5,
    )
    decision = SimpleNamespace(**_directional_history())

    assert _has_strong_underdog_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is True
    assert market_engine._has_delivery_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is True


@pytest.mark.parametrize(
    "sport,expected",
    [("NFL", False), ("CFB", True)],
)
def test_main_league_history_floor_and_optional_short_history(sport, expected):
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=4,
    )
    decision = SimpleNamespace(recommendation="OVER")

    assert _has_strong_underdog_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is expected
    assert market_engine._has_delivery_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is expected


@pytest.mark.parametrize(
    "sport",
    ["CFL", "AFL", "RUGBY", "RUGBY_UNION", "college_football"],
)
def test_other_football_families_are_blocked_even_with_settled_history(sport):
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=10,
    )
    decision = SimpleNamespace(recommendation="OVER")

    assert is_telegram_suppressed_sport(sport) is True
    assert market_engine._has_delivery_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is False


def test_historical_average_evidence_source_is_not_delivery_evidence():
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
        sport="SOCCER",
        validation=validation,
        decision=decision,
    ) is False


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
        sport="TENNIS",
        validation=validation,
        decision=decision,
    ) is False
    assert _has_strong_underdog_evidence(
        sport="TENNIS",
        validation=validation,
        decision=decision,
    ) is False


@pytest.mark.parametrize(
    "sport",
    ["CS", "LOL", "DOTA", "VALORANT", "TENNIS", "SOCCER", "FIFA"],
)
def test_nonfootball_sports_require_direction_but_not_completed_history(sport):
    no_history = SimpleNamespace(
        recommendation="OVER",
        evidence_source=None,
    )

    assert market_engine._has_delivery_evidence(
        sport=sport,
        decision=no_history,
    ) is True
    assert _has_strong_underdog_evidence(
        sport=sport,
        decision=no_history,
    ) is True

    completed_history = SimpleNamespace(
        evidence_source="primary",
        n_real_games=5,
        **_directional_history(),
    )
    assert market_engine._has_delivery_evidence(
        sport=sport,
        decision=completed_history,
    ) is True
    assert _has_strong_underdog_evidence(
        sport=sport,
        decision=completed_history,
    ) is True

    no_direction = SimpleNamespace(
        recommendation="",
        evidence_source=None,
    )
    assert market_engine._has_delivery_evidence(
        sport=sport,
        decision=no_direction,
    ) is False
    assert _has_strong_underdog_evidence(
        sport=sport,
        decision=no_direction,
    ) is False


@pytest.mark.parametrize("sport", ["MLB", "NBA", "NFL"])
def test_historical_average_is_not_delivery_evidence_for_strict_sports(sport):
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="historical_average",
        n_real_games=0,
        n_supplemental_games=0,
    )
    decision = SimpleNamespace(
        recommendation="OVER",
        evidence_source="historical_average",
        avg_vs_line_pct=0.12,
    )

    assert market_engine._has_delivery_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is False
    assert _has_strong_underdog_evidence(
        sport=sport,
        validation=validation,
        decision=decision,
    ) is False


@pytest.mark.parametrize("sport", ["MLB", "NBA", "WNBA"])
def test_primary_basketball_and_baseball_sports_require_history(sport):
    decision = SimpleNamespace(
        recommendation="OVER",
        evidence_source=None,
    )

    assert market_engine._has_delivery_evidence(
        sport=sport,
        decision=decision,
    ) is False
    assert _has_strong_underdog_evidence(
        sport=sport,
        decision=decision,
    ) is False


def test_telegram_delivery_requires_strong_over_in_every_window():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=10,
    )
    decision = SimpleNamespace(
        recommendation="OVER",
        **{
            **{
                f"{window}_games": 10
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
            **{
                f"{window}_over": 8
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
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=10,
    )
    decision = SimpleNamespace(
        recommendation="UNDER",
        **{
            **{
                f"{window}_games": 10
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
            **{
                # 2/10 OVER = 20%, so the complementary UNDER count is 8/10.
                f"{window}_under": 8
                for window in ("l5", "l10", "l15", "l20", "l30", "season")
            },
        },
    )
    assert market_engine._has_strong_delivery_evidence(
        validation=validation,
        decision=decision,
    ) is True


def test_legacy_strong_delivery_helper_rejects_direction_without_strong_window():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=10,
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


def test_legacy_strong_delivery_helper_rejects_when_one_window_is_missing():
    validation = SimpleNamespace(
        has_supporting_data=True,
        data_source="propline",
        n_real_games=0,
        n_supplemental_games=50,
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