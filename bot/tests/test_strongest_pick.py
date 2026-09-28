from types import SimpleNamespace

import pytest

from engine.strongest_pick import (
    evidence_profile,
    rank_qualified_actionable_picks,
    season_directional_hit_rate_strength,
    select_strongest_actionable_picks,
)


def _candidate(
    provider: str,
    player: str,
    stat: str,
    *,
    confidence: int = 75,
    tier: str = "A",
    bq: int = 80,
    mq: int = 70,
    evidence: bool = True,
    evidence_count: int = 10,
    line: float = 20.5,
) -> dict:
    windows = {
        f"{prefix}_{field}": value
        for prefix in ("l5", "l10", "l15", "l20", "l30", "season")
        for field, value in (("games", 10), ("over", 8), ("under", 2))
    }
    return {
        "provider": provider,
        "player": player,
        "sport": "MLB",
        "stat_type": stat,
        "line_val": line,
        "decision": SimpleNamespace(
            confidence=confidence,
            decision_tier=tier,
            recommendation="OVER",
            **windows,
        ),
        "score": SimpleNamespace(total=bq),
        "market_quality": SimpleNamespace(score=mq),
        "validation": SimpleNamespace(
            has_supporting_data=evidence,
            data_source="propline" if evidence else "primary",
            n_real_games=0,
            n_supplemental_games=evidence_count if evidence else 0,
        ),
    }


def test_selects_one_winner_per_player_and_keeps_providers_independent():
    pp_points = _candidate("PrizePicks", "Alex Smith", "Points", confidence=82)
    pp_rebounds = _candidate("PrizePicks", "alex smith", "Rebounds", confidence=74)
    ud_points = _candidate("Underdog", "Alex Smith", "Points", confidence=71)

    selected = select_strongest_actionable_picks(
        [ud_points, pp_rebounds, pp_points]
    )

    assert selected == [pp_points, ud_points]


def test_evidence_ordering_is_deterministic_even_when_processed_later():
    weaker = _candidate("Underdog", "Jordan Lee", "Points", evidence_count=5)
    stronger = _candidate("Underdog", "Jordan Lee", "Assists", evidence_count=15)

    selected = select_strongest_actionable_picks([weaker, stronger])

    assert selected == [stronger]


def test_ties_ignore_tier_bq_and_mq_and_use_evidence_then_identity():
    low_tier = _candidate("PrizePicks", "Taylor Reed", "Points", tier="B")
    high_tier = _candidate("PrizePicks", "Taylor Reed", "Assists", tier="A")
    high_bq = _candidate(
        "PrizePicks", "Taylor Reed", "Rebounds", tier="A", bq=90
    )
    high_mq = _candidate(
        "PrizePicks", "Taylor Reed", "Steals", tier="A", bq=90, mq=90
    )
    no_evidence = _candidate(
        "PrizePicks",
        "Taylor Reed",
        "Blocks",
        tier="A",
        bq=90,
        mq=90,
        evidence=False,
    )

    selected = select_strongest_actionable_picks(
        [no_evidence, high_mq, high_bq, high_tier, low_tier]
    )

    assert selected == [high_tier]


def test_exact_strength_ties_are_order_independent():
    points = _candidate("Underdog", "Casey Jones", "Points")
    rebounds = _candidate("Underdog", "Casey Jones", "Rebounds")

    first = select_strongest_actionable_picks([points, rebounds])
    second = select_strongest_actionable_picks([rebounds, points])

    assert first[0]["stat_type"] == second[0]["stat_type"] == "Points"


def test_propline_is_not_a_standalone_actionable_provider():
    supplemental = _candidate("PropLine", "Morgan Hall", "Points")

    assert select_strongest_actionable_picks([supplemental]) == []


@pytest.mark.parametrize("sport", ["NFL", "NBA", "MLB"])
def test_restricted_sports_use_normal_evidence_ranking(sport):
    high_line = _candidate(
        "Underdog",
        "Jordan Lee",
        "Passing Yards",
        line=225.5,
        evidence_count=20,
    )
    low_line = _candidate(
        "Underdog",
        "Jordan Lee",
        "Passing Yards",
        line=199.5,
        evidence_count=5,
    )
    volatile = _candidate(
        "Underdog",
        "Jordan Lee",
        "Interceptions",
        line=1.5,
        evidence_count=50,
    )
    directionless = _candidate(
        "Underdog",
        "Jordan Lee",
        "Passing Yards",
        line=10.5,
        evidence_count=50,
    )
    weak_direction = _candidate(
        "Underdog",
        "Jordan Lee",
        "Passing Yards",
        line=1.5,
        confidence=74,
        evidence_count=50,
    )
    directionless["decision"].recommendation = ""
    for candidate in (high_line, low_line, volatile, weak_direction):
        candidate["sport"] = sport
    directionless["sport"] = sport

    selected = select_strongest_actionable_picks(
        [high_line, volatile, directionless, weak_direction, low_line]
    )

    # Sport and market labels no longer impose a separate green-goblin
    # allowlist or low-line preference. Evidence ranking chooses the
    # directional candidate with the largest completed-history sample.
    assert selected == [volatile]


def test_restricted_sports_do_not_require_high_floor_market():
    candidate = _candidate(
        "Underdog",
        "Jordan Lee",
        "Interceptions",
        confidence=85,
        line=1.5,
    )
    candidate["sport"] = "NFL"
    candidate["decision"] = _decision_with_windows(
        "UNDER", [0.30, 0.30, 0.30, 0.30, 0.30, 0.30]
    )

    assert select_strongest_actionable_picks([candidate]) == [candidate]


def _decision_with_windows(
    direction: str,
    rates: list[float],
    *,
    games: int = 10,
):
    """Build the serialized decision shape used by the live selector."""
    windows = {}
    for prefix, rate in zip(("l5", "l10", "l15", "l20", "l30", "season"), rates):
        over = round(games * rate)
        windows.update({
            f"{prefix}_games": games,
            f"{prefix}_over": over,
            f"{prefix}_under": games - over,
        })
    return SimpleNamespace(recommendation=direction, confidence=85, **windows)


def test_evidence_profile_uses_validation_data_source_and_trusted_samples():
    candidate = _candidate(
        "Underdog",
        "Evidence Player",
        "Hits",
        evidence_count=0,
    )
    candidate["sport"] = "MLB"
    candidate["decision"] = _decision_with_windows(
        "OVER", [0.8, 0.8, 0.8, 0.8, 0.8, 0.8]
    )
    candidate["validation"] = SimpleNamespace(
        has_supporting_data=True,
        data_source="primary",
        n_real_games=10,
        n_supplemental_games=0,
    )

    profile = evidence_profile(candidate)

    assert profile["class"] == "STRONG"
    assert profile["source"] == "primary"
    assert profile["sample_count"] == 10


def test_historical_average_is_never_labeled_strong():
    candidate = _candidate("Underdog", "Fallback Player", "Hits")
    candidate["sport"] = "MLB"
    candidate["decision"] = _decision_with_windows(
        "OVER", [0.8, 0.8, 0.8, 0.8, 0.8, 0.8]
    )
    candidate["validation"] = SimpleNamespace(
        has_supporting_data=False,
        data_source="historical_average",
        n_real_games=0,
        n_supplemental_games=0,
    )

    profile = evidence_profile(candidate)

    assert profile["class"] == "HISTORICAL-AVERAGE FALLBACK"
    assert profile["source"] == "historical_average"


def test_global_delivery_ranking_keeps_all_candidates_and_puts_strong_first():
    stronger = _candidate("Underdog", "Same Player", "Hits")
    stronger["sport"] = "MLB"
    stronger["decision"] = _decision_with_windows(
        "OVER", [0.8, 0.8, 0.8, 0.8, 0.8, 0.8]
    )
    stronger["validation"] = SimpleNamespace(
        has_supporting_data=True,
        data_source="primary",
        n_real_games=10,
        n_supplemental_games=0,
    )
    weaker = _candidate("Underdog", "Same Player", "Runs")
    weaker["sport"] = "MLB"
    weaker["decision"] = _decision_with_windows(
        "OVER", [0.6, 0.6, 0.6, 0.6, 0.6, 0.6]
    )
    weaker["validation"] = SimpleNamespace(
        has_supporting_data=True,
        data_source="primary",
        n_real_games=10,
        n_supplemental_games=0,
    )

    ranked = rank_qualified_actionable_picks([weaker, stronger])

    assert ranked == [stronger, weaker]
    assert len(ranked) == 2


def test_season_directional_strength_matches_70_over_and_30_under():
    over = _candidate("Underdog", "Over Player", "Points")
    over["decision"] = SimpleNamespace(
        recommendation="OVER",
        season_games=20,
        season_hit_rate=0.70,
    )

    under = _candidate("Underdog", "Under Player", "Points")
    under["decision"] = SimpleNamespace(
        recommendation="UNDER",
        season_games=20,
        season_hit_rate=0.30,
    )

    assert season_directional_hit_rate_strength(over) == pytest.approx(0.70)
    assert season_directional_hit_rate_strength(under) == pytest.approx(0.70)


def test_sport_queue_order_prefers_season_direction_over_recent_window(
    monkeypatch,
):
    from market_engine import _rank_delivery_candidates_by_sport

    weaker_season = _candidate("Underdog", "Recent Hot", "Points")
    weaker_season["sport"] = "NBA"
    weaker_season["decision"] = SimpleNamespace(
        recommendation="OVER",
        season_games=20,
        season_hit_rate=0.55,
        l5_games=5,
        l5_hit_rate=0.95,
    )
    stronger_season = _candidate("Underdog", "Season Strong", "Rebounds")
    stronger_season["sport"] = "NBA"
    stronger_season["decision"] = SimpleNamespace(
        recommendation="OVER",
        season_games=20,
        season_hit_rate=0.70,
        l5_games=5,
        l5_hit_rate=0.40,
    )

    ordered = _rank_delivery_candidates_by_sport(
        [weaker_season, stronger_season]
    )

    assert len(ordered) == 2
    assert [candidate["player"] for candidate in ordered] == [
        "Season Strong",
        "Recent Hot",
    ]


def test_strong_mode_excludes_mediocre_directional_history():
    weak = _candidate("Underdog", "Jo Adell", "Hits", confidence=80, line=0.5)
    weak["sport"] = "MLB"
    weak["decision"] = _decision_with_windows(
        "OVER", [0.8, 0.7, 0.7, 0.6, 0.6, 0.6]
    )

    assert select_strongest_actionable_picks(
        [weak],
        require_strong=True,
    ) == []


def test_strong_mode_keeps_consistent_history_and_ranks_it_first():
    strong = _candidate("Underdog", "Sal Stewart", "Hits", confidence=85, line=0.5)
    strong["sport"] = "MLB"
    strong["decision"] = _decision_with_windows(
        "OVER", [0.8, 0.75, 0.8, 0.8, 0.8, 0.8]
    )
    weaker_strong = _candidate(
        "Underdog", "William Contreras", "Hits", confidence=75, line=0.5
    )
    weaker_strong["sport"] = "MLB"
    weaker_strong["decision"] = _decision_with_windows(
        "OVER", [0.75, 0.75, 0.75, 0.75, 0.75, 0.75]
    )

    selected = select_strongest_actionable_picks(
        [weaker_strong, strong],
        require_strong=True,
    )

    assert selected == [strong, weaker_strong]


def test_strong_mode_uses_computed_hit_rates_when_decision_omits_windows():
    candidate = _candidate(
        "Underdog",
        "Real History Player",
        "Hits",
        confidence=85,
        line=0.5,
    )
    candidate["sport"] = "MLB"
    candidate["decision"] = SimpleNamespace(
        recommendation="OVER",
        confidence=85,
    )
    candidate["hit_rates"] = SimpleNamespace(
        recommendation="OVER",
        **{
            f"{prefix}_{field}": value
            for prefix in ("l5", "l10", "l15", "l20", "l30", "season")
            for field, value in (("games", 10), ("over", 8), ("under", 2))
        },
    )
    candidate["validation"] = SimpleNamespace(
        has_supporting_data=True,
        data_source="primary",
        n_real_games=10,
        n_supplemental_games=0,
    )

    assert select_strongest_actionable_picks(
        [candidate],
        require_strong=True,
    ) == [candidate]


@pytest.mark.parametrize("sport", ["MLB", "NBA", "NFL", "WNBA", "NHL"])
@pytest.mark.parametrize(
    "direction,rates,expected",
    [
        ("OVER", [0.75] * 6, True),
        ("OVER", [0.75, 0.75, 0.75, 0.75, 0.74, 0.75], False),
        ("UNDER", [0.25] * 6, True),
        ("UNDER", [0.25, 0.25, 0.25, 0.25, 0.26, 0.25], False),
    ],
)
def test_strong_pick_applies_history_cutoffs_across_main_leagues(
    sport,
    direction,
    rates,
    expected,
):
    candidate = _candidate(
        "Underdog",
        "Threshold Player",
        "Points",
        confidence=85,
    )
    candidate["sport"] = sport
    candidate["decision"] = _decision_with_windows(
        direction,
        rates,
        games=100,
    )

    selected = select_strongest_actionable_picks(
        [candidate],
        require_strong=True,
    )

    assert (candidate in selected) is expected


@pytest.mark.parametrize(
    "sport,confidence,expected",
    [
        ("TENNIS", 70, True),
        ("SOCCER", 80, True),
        ("FIFA", 69, False),
        ("CS", None, False),
    ],
)
def test_direction_only_sports_use_confidence_without_game_history(
    sport,
    confidence,
    expected,
):
    candidate = _candidate(
        "Underdog",
        "No History Player",
        "Points",
        confidence=confidence or 0,
    )
    candidate["sport"] = sport
    candidate["confidence"] = confidence
    candidate["decision"] = SimpleNamespace(recommendation="OVER")

    selected = select_strongest_actionable_picks(
        [candidate],
        require_strong=True,
    )

    assert (candidate in selected) is expected


@pytest.mark.parametrize("sport", ["CFB", "WNBA", "NHL", "FIFA", "KBO"])
def test_non_football_sports_still_need_propline_history(sport):
    candidate = _candidate(
        "Underdog",
        "Jordan Lee",
        "Points",
        confidence=55,
        evidence=False,
        evidence_count=0,
    )
    candidate["sport"] = sport
    candidate["decision"] = SimpleNamespace(
        recommendation="OVER",
        confidence=55,
    )

    assert select_strongest_actionable_picks(
        [candidate],
        require_strong=True,
    ) == []


@pytest.mark.parametrize("sport", ["MLB", "NFL", "NBA"])
def test_strict_history_sports_still_need_complete_history(sport):
    candidate = _candidate(
        "Underdog",
        "Jordan Lee",
        "Points",
        confidence=85,
        evidence=False,
        evidence_count=0,
    )
    candidate["sport"] = sport
    candidate["decision"] = SimpleNamespace(
        recommendation="OVER",
        confidence=85,
    )

    assert select_strongest_actionable_picks(
        [candidate],
        require_strong=True,
    ) == []
