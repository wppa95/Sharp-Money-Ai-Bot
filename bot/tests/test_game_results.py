import pytest

from providers.base import ProviderStatus
from providers.game_results import (
    OddsApiResultsProvider,
    grade_pp_pick,
)


@pytest.mark.asyncio
async def test_odds_team_score_provider_is_disabled_without_configured_key(monkeypatch):
    # Tests must never issue provider traffic, even in a workspace with the
    # optional production secret configured.
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    provider = OddsApiResultsProvider()

    assert await provider.fetch_scores("basketball_nba") == []
    assert (await provider.health_check()).status is ProviderStatus.DISABLED


def test_generic_prop_grading_requires_explicit_player_result():
    no_result = grade_pp_pick(
        "Player One", "Points", 10.5, "OVER", [], actual_value=None
    )
    win = grade_pp_pick(
        "Player One", "Points", 10.5, "OVER", [], actual_value=11
    )

    assert no_result.result == "NO_DATA"
    assert win.result == "WIN"