from types import SimpleNamespace

import pytest

from engine.strongest_pick import select_strongest_actionable_picks


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
    return {
        "provider": provider,
        "player": player,
        "stat_type": stat,
        "line_val": line,
        "decision": SimpleNamespace(
            confidence=confidence,
            decision_tier=tier,
            recommendation="OVER",
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
def test_nfl_nba_and_mlb_keep_only_directional_goblin_lowest_line(sport):
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

    assert selected == [low_line]


@pytest.mark.parametrize("sport", ["NFL", "NBA", "MLB"])
def test_nfl_nba_and_mlb_reject_non_directional_goblin_candidates(sport):
    candidate = _candidate("Underdog", "Jordan Lee", "Passing Yards", line=1.5)
    candidate["sport"] = sport
    candidate["decision"].recommendation = ""

    assert select_strongest_actionable_picks([candidate]) == []


def test_restricted_sports_keep_strong_under_at_the_lowest_line():
    low_under = _candidate(
        "Underdog",
        "Jordan Lee",
        "Rushing Yards",
        confidence=20,
        line=49.5,
    )
    high_under = _candidate(
        "Underdog",
        "Jordan Lee",
        "Rushing Yards",
        confidence=35,
        line=59.5,
    )
    low_under["sport"] = "NFL"
    high_under["sport"] = "NFL"
    low_under["decision"].recommendation = "UNDER"
    high_under["decision"].recommendation = "UNDER"

    assert select_strongest_actionable_picks([high_under, low_under]) == [
        low_under
    ]


def _decision_with_windows(direction: str, rates: list[float]):
    """Build the serialized decision shape used by the live selector."""
    windows = {}
    for prefix, rate in zip(("l5", "l10", "l15", "l20", "l30", "season"), rates):
        games = 10
        over = round(games * rate)
        windows.update({
            f"{prefix}_games": games,
            f"{prefix}_over": over,
            f"{prefix}_under": games - over,
        })
    return SimpleNamespace(recommendation=direction, confidence=85, **windows)


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
        "OVER", [0.8, 0.7, 0.8, 0.8, 0.8, 0.8]
    )
    weaker_strong = _candidate(
        "Underdog", "William Contreras", "Hits", confidence=75, line=0.5
    )
    weaker_strong["sport"] = "MLB"
    weaker_strong["decision"] = _decision_with_windows(
        "OVER", [0.7, 0.7, 0.7, 0.7, 0.7, 0.7]
    )

    selected = select_strongest_actionable_picks(
        [weaker_strong, strong],
        require_strong=True,
    )

    assert selected == [strong, weaker_strong]


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
