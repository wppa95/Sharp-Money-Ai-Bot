from types import SimpleNamespace

from engine.pregame_watch import _is_watch_candidate_eligible


def test_market_direction_without_recommendation_is_not_a_pick():
    comparison = SimpleNamespace(
        direction="OVER",
        evidence_available=True,
    )

    assert not _is_watch_candidate_eligible(comparison, "TENNIS")


def test_optional_sport_accepts_explicit_direction_without_history():
    comparison = SimpleNamespace(recommendation="OVER")

    assert _is_watch_candidate_eligible(comparison, "TENNIS")


def test_history_sport_requires_completed_history():
    comparison = SimpleNamespace(recommendation="UNDER")

    assert not _is_watch_candidate_eligible(comparison, "NFL")


def test_optional_sport_rejects_claimed_but_unverified_evidence():
    comparison = SimpleNamespace(
        recommendation="OVER",
        evidence_available=True,
    )

    assert not _is_watch_candidate_eligible(comparison, "TENNIS")