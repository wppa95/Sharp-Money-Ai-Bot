from types import SimpleNamespace

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
            n_real_games=evidence_count,
            n_supplemental_games=0,
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


def test_higher_confidence_wins_even_when_processed_later():
    weaker = _candidate("Underdog", "Jordan Lee", "Points", confidence=70)
    stronger = _candidate("Underdog", "Jordan Lee", "Assists", confidence=91)

    selected = select_strongest_actionable_picks([weaker, stronger])

    assert selected == [stronger]


def test_ties_use_tier_then_bq_then_mq_then_evidence():
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

    assert selected == [high_mq]


def test_exact_strength_ties_are_order_independent():
    points = _candidate("Underdog", "Casey Jones", "Points")
    rebounds = _candidate("Underdog", "Casey Jones", "Rebounds")

    first = select_strongest_actionable_picks([points, rebounds])
    second = select_strongest_actionable_picks([rebounds, points])

    assert first[0]["stat_type"] == second[0]["stat_type"] == "Points"


def test_propline_is_not_a_standalone_actionable_provider():
    supplemental = _candidate("PropLine", "Morgan Hall", "Points")

    assert select_strongest_actionable_picks([supplemental]) == []
