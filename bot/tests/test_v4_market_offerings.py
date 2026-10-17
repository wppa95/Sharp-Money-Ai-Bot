"""Source-shaped deterministic regression fixtures; no live HTTP required."""
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from engine.offering_coverage import (
    coverage,
    market_identity,
    directions_from,
    prioritize_standard_line_candidates,
)
from engine.player_props_pipeline import canonicalize_player_props, persist_canonical_player_props
from engine.player_results import compute_hit_rates
from engine.ud_bet_decision import UDBetDecision
from alerts_multiplatform import format_underdog_new_prop_alert, _format_history_block
from providers.propline import normalize_propline_prizepicks_record
from connectors.underdog import UnderdogConnector


@pytest.fixture(autouse=True)
def clean_inventory():
    coverage.__init__()
    yield
    coverage.__init__()


def prop(provider="Underdog", stat="Hits", line=1.5, **extra):
    values = dict(
        provider=provider, sport="MLB", player_name="Alex Example", team="A",
        stat_type=stat, line_value=line, external_id=f"{provider}:{stat}:{line}",
        game_id="event", game_time=datetime.utcnow() + timedelta(days=1),
        fetched_at=datetime.utcnow(), opponent="B", event="A at B",
        available_directions=("OVER", "UNDER"), line_variant="standard",
    )
    values.update(extra)
    return NS(**values)


@pytest.mark.parametrize("sport,markets", [
    ("MLB", ["Hits", "Home Runs", "Runs", "RBIs", "Hits + Runs + RBIs", "Total Bases",
             "Walks", "Batter Strikeouts", "Singles", "Doubles", "Triples", "Stolen Bases",
             "Pitcher Strikeouts", "Walks Allowed", "Hits Allowed", "Earned Runs Allowed",
             "Outs Recorded", "New Provider Market"]),
    ("NHL", ["Shots on Goal", "Hits", "Goals", "Assists", "Points", "Blocked Shots", "Goalie Saves"]),
    ("CS", ["Kills on Maps 1+2", "Headshots on Maps 1+2", "Kills on Maps 1+2+3"]),
    ("TENNIS", ["Aces", "Double Faults", "Games Won", "New Tennis Market"]),
    ("NBA", ["Points", "Rebounds", "Assists", "Blocks"]),
    ("SOCCER", ["Shots", "Shots on Target", "Goals"]),
    ("NFL", ["Passing Yards", "Rushing Yards", "Receptions"]),
    ("DOTA", ["Fantasy Points in Games 1+2", "Kills"]),
])
def test_every_fixture_market_is_observed_independent_of_qualification(sport, markets):
    rows = canonicalize_player_props("Underdog", [
        prop(stat=market, sport=sport, score_tier="PASS") for market in markets
    ])
    assert len(rows) == len(markets)
    assert {row.stat_type for row in rows} == set(markets)
    assert len(coverage.rows) == len(markets)
    assert all("NORMALIZED" in stages for stages in coverage.stages.values())
    assert all("EVALUATED" not in stages for stages in coverage.stages.values())


def test_distinct_lines_sources_events_and_variants_survive():
    rows = [prop(line=line, line_variant=variant, external_id="same")
            for line, variant in [(0.5, "goblin"), (1.5, "standard"), (2.5, "demon")]]
    rows.append(prop(line=1.5, external_id="same", game_id="another"))
    ud = canonicalize_player_props("Underdog", rows)
    pp = canonicalize_player_props("PrizePicks", rows)
    assert len(ud) == len(pp) == 4
    assert len(coverage.rows) == 8


@pytest.mark.parametrize("sport,left,right", [
    ("MLB", "HRR", "Hits + Runs + RBIs"), ("MLB", "batter hits", "Hits"),
    ("NHL", "SOG", "Shots on Goal"), ("NHL", "Saves", "Goalie Saves"),
    ("NBA", "pts", "points"),
])
def test_safe_equivalent_market_aliases(sport, left, right):
    assert market_identity(sport, left) == market_identity(sport, right)


@pytest.mark.parametrize("sport,left,right", [
    ("MLB", "Batter Strikeouts", "Pitcher Strikeouts"),
    ("MLB", "HRR", "Total Bases"), ("MLB", "Walks", "Walks Allowed"),
    ("NHL", "Hits", "Goals"), ("NHL", "Goals", "Goalie Saves"),
    ("SOCCER", "Shots", "Shots on Goal"),
    ("CS", "Kills on Maps 1+2", "Kills on Maps 1+2+3"),
])
def test_different_definitions_never_become_equivalent(sport, left, right):
    assert market_identity(sport, left) != market_identity(sport, right)


def test_propline_direction_and_special_line_metadata_survive_canonicalization():
    raw = dict(provider="PrizePicks", market_type="player_hits", sport="MLB",
               player="Alex Example", stat="Hits", line=0.5, direction="Over",
               dfs_odds_type="goblin", external_id="pp", event={"id": "event"})
    normalized = normalize_propline_prizepicks_record(raw)
    assert normalized is not None
    row = canonicalize_player_props("PrizePicks", [normalized])[0]
    assert row.available_directions == ("OVER",)
    assert row.line_variant == "goblin"
    assert row.original_stat_type == "Hits"
    assert row.market_id == "player_hits"


def test_explicit_sides_union_only_for_same_source_offering():
    rows = canonicalize_player_props("PrizePicks", [
        prop(direction="OVER", available_directions=()),
        prop(direction="UNDER", available_directions=()),
    ])
    assert len(rows) == 1
    assert rows[0].available_directions == ("OVER", "UNDER")
    assert directions_from(None) == ()
    assert directions_from([{"choice": "higher"}]) == ("OVER",)


def test_unknown_line_not_fabricated_and_duplicate_normalization_not_double_counted():
    good = prop()
    assert len(canonicalize_player_props("Underdog", [good, prop(line=None), prop(line=float("nan"))])) == 1
    canonicalize_player_props("Underdog", [good])
    assert sum(coverage.observations.values()) == 1
    assert sum(coverage.failures.values()) == 2


def paired(line_a=1.5, line_b=2.5):
    a = prop(line=line_a)
    b = prop("PrizePicks", line=line_b, game_id="other-provider-id",
             game_time=a.game_time, line_variant="demon")
    return (canonicalize_player_props("Underdog", [a])[0],
            canonicalize_player_props("PrizePicks", [b])[0])


def test_comparison_requires_exact_event_context_and_preserves_own_lines():
    a, b = paired()
    assert coverage.comparisons(a) == [b]
    assert a.line_value == 1.5 and b.line_value == 2.5
    b.opponent = "Unrelated"
    assert coverage.comparisons(a) == []
    b.opponent = a.opponent
    b.game_time += timedelta(days=1)
    assert coverage.comparisons(a) == []


def test_unknown_directions_or_stale_counterpart_never_form_playable_comparison():
    a, b = paired()
    b.available_directions = ()
    assert coverage.comparisons(a) == []
    b.available_directions = ("OVER",)
    b.fetched_at -= timedelta(hours=1)
    assert coverage.comparisons(a) == []


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
def test_telegram_uses_platform_line_difference_and_actual_classification(provider):
    a, b = paired(0.5, 2.5)
    chosen = a if provider == "Underdog" else b
    text = format_underdog_new_prop_alert(
        chosen.player_name, chosen.team, chosen.sport, chosen.stat_type,
        chosen.line_value, chosen.game_time,
        decision=NS(recommendation="OVER"), source=provider,
    )
    assert f"<b>Platform:</b> {provider}" in text
    assert "<b>Diff:</b> 2" in text
    assert "demon" in text and "standard" in text
    assert "line difference alone" in text
    assert "A at B" in text
    assert "Monitoring player props across multiple sports. Not betting advice." in text
    assert "STRONG BET" not in text and "★" not in text


def test_single_platform_does_not_invent_comparison():
    row = canonicalize_player_props("Underdog", [prop()])[0]
    text = format_underdog_new_prop_alert(
        row.player_name, row.team, row.sport, row.stat_type, row.line_value,
        row.game_time, decision=NS(recommendation="OVER"),
    )
    assert "not verified" in text
    assert "<b>Diff:</b>" not in text


def games(values):
    return [NS(player_name="Alex Example", stat_type="Hits", game_date=f"2026-09-{30-i:02d}",
               actual_value=value, opponent="B") for i, value in enumerate(values)]


def test_valid_games_filtered_before_slicing_and_pushes_are_not_under_hits():
    rows = games([None, float("nan"), 0, 0, 0, 0, 1, 0.5])
    rows[0].is_dnp = True
    rates = compute_hit_rates(rows, 0.5, opponent="B")
    assert rates.total_games == 6
    assert rates.l5.games == 5 and rates.l5.under_count == 4
    assert rates.season.push_count == 1 and rates.season.under_count == 4
    assert rates.l10 is None
    decision = UDBetDecision.make_pick("UNDER", "evidence", hit_rates=rates)
    text = _format_history_block(decision)
    assert "80% (4/5)" in text and "(UNDER)" in text
    assert "<b>L10:</b> N/A" in text
    assert "<b>L20:</b> N/A" in text and "<b>L30:</b> N/A" in text


def test_history_window_values_counts_order_and_h2h():
    rows = games([0, 0, 0, 0, 1] * 6)
    for row in rows[5:]:
        row.opponent = "Other"
    rates = compute_hit_rates(list(reversed(rows)), 0.5, opponent="B")
    for size in [5, 10, 20, 30]:
        window = getattr(rates, f"l{size}")
        assert window.games == size
        assert window.under_count == size * 4 // 5
        assert window.over_count == size // 5
    assert rates.h2h.games == 5
    assert dict(rates.sample_values)["l5"] == (0, 0, 0, 0, 1)


@pytest.mark.asyncio
async def test_inventory_persistence_uses_existing_writer_and_records_only_actual_stages():
    class DB:
        upsert_propline_evidence_batch = AsyncMock(return_value=1)
        upsert_prop_line_history_batch = AsyncMock()
        save_prop_line_observations_batch = AsyncMock(return_value=1)
    row = prop(available_directions=("OVER",))
    assert await persist_canonical_player_props(DB(), "Underdog", [row], append_observations=True) == 1
    saved = DB.upsert_propline_evidence_batch.call_args.args[0][0]
    assert saved["family"] == "offering_inventory"
    assert saved["payload"]["offerings"][0]["directions"] == ("OVER",)
    assert saved["payload"]["full_source_coverage"] == "unknown"
    text = "\n".join(coverage.report("MLB", "Underdog"))
    assert "STORED=1" in text and "EVALUATED=" not in text
    canonical = next(iter(coverage.rows.values()))
    coverage.evaluation(provider="Underdog", player=row.player_name, sport="MLB",
                        stat="Hits", line=1.5, event_id="event",
                        decision=NS(recommendation=None, reason="insufficient sample"),
                        hit_rates=None)
    text = "\n".join(coverage.report("MLB", "Underdog"))
    assert "EVALUATED=1" in text and "insufficient sample" in text


def test_underdog_source_options_and_all_market_records_are_retained():
    fixture = {
        "players": [{"id": "p", "first_name": "Alex", "last_name": "Example", "sport_id": "NHL"}],
        "appearances": [{"id": "a", "player_id": "p", "game_id": "g"}],
        "games": [{"id": "g", "scheduled_at": "2026-12-01T20:00:00Z"}],
        "over_under_lines": [
            {"id": str(i), "stat_value": line, "options": [{"choice": "higher"}],
             "over_under": {"appearance_stat": {"id": stat, "appearance_id": "a", "display_stat": stat}}}
            for i, (stat, line) in enumerate([("Goals", 0.5), ("Hits", 2.5), ("Shots on Goal", 3.5), ("Hits", 4.5)])
        ],
    }
    rows = UnderdogConnector()._parse(fixture)
    assert len(rows) == 4
    assert all(row.available_directions == ("OVER",) for row in rows)
    assert {row.stat_type for row in rows} == {"Goals", "Hits", "Shots on Goal"}


@pytest.mark.parametrize("sport,stat", [
    ("MLB", "Hits"), ("MLB", "Hits + Runs + RBIs"), ("MLB", "Pitcher Strikeouts"),
    ("MLB", "Batter Strikeouts"), ("MLB", "Outs Recorded"),
    ("MLB", "Hits Allowed"), ("MLB", "Singles"), ("MLB", "Doubles"),
    ("MLB", "Triples"), ("MLB", "Stolen Bases"), ("MLB", "Earned Runs Allowed"),
    ("NHL", "Hits"), ("NHL", "Shots on Goal"), ("NHL", "Goalie Saves"),
    ("CS", "Kills on Maps 1+2+3"), ("CS", "Headshots on Maps 1+2"),
    ("SOCCER", "Shots"), ("TENNIS", "Double Faults"),
])
def test_source_markets_can_enter_history_evaluation_without_market_allowlist(sport, stat):
    from engine.ud_bet_decision import make_ud_bet_decision
    raw = dict(provider="PrizePicks", market_type="player_prop", sport=sport,
               player="Alex Example", stat=stat, line=0.5, direction="Over", id=stat)
    observation = normalize_propline_prizepicks_record(raw)
    assert observation is not None
    canonical = canonicalize_player_props("PrizePicks", [observation])[0]
    evidence = games([1] * 30)
    for row in evidence:
        row.stat_type = canonical.stat_type
    rates = compute_hit_rates(evidence, canonical.line_value)
    decision = make_ud_bet_decision(
        score=NS(total=50, tier="PASS"), validation=NS(has_supporting_data=True, avg_line=0.5),
        current_line=canonical.line_value, hit_rates=rates, sport=sport,
    )
    assert decision.recommendation == "OVER"
    assert canonical.original_stat_type == stat
    if "Maps" in stat:
        assert "maps" in canonical.stat_type
    if sport == "SOCCER":
        assert canonical.stat_type == "shots"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_explicit_unavailable_direction_not_delivered_but_available_pick_is(provider, monkeypatch):
    import alerts
    import alert_scope_filter
    import engine.timing
    from unittest.mock import MagicMock
    from alerts import AlertDelivery
    from alert_scope_filter import FilterResult
    source = prop(provider, sport="TENNIS", stat="Aces", available_directions=("OVER",))
    canonical = canonicalize_player_props(provider, [source])[0]
    db = MagicMock()
    db.get_telegram_delivery_state = AsyncMock(return_value={"tracked": True, "directional": True})
    monkeypatch.setattr(alert_scope_filter, "check", lambda *a, **k: FilterResult(allowed=True))
    monkeypatch.setattr(engine.timing, "is_game_alertable", lambda *a, **k: (True, ""))
    # Isolate selectable-side safety from this fixture's intentionally
    # tomorrow-dated inventory. Calendar behavior has separate real-gate tests.
    monkeypatch.setattr("reporting.is_current_reporting_day", lambda *_a, **_k: True)
    monkeypatch.setattr(alerts, "_has_strong_underdog_evidence", lambda **k: True)
    broadcaster = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert", broadcaster)
    monkeypatch.setattr(alerts, "send_discord_alert", AsyncMock(return_value=False))
    delivery = AlertDelivery(db, MagicMock(), [123])
    result = await delivery.deliver_underdog(
        source.player_name, source.team, source.sport, source.stat_type, 1.5, 1.5,
        game_time=source.game_time, source=provider, source_id=source.external_id,
        event_id=source.game_id,
        line_variant=canonical.line_variant,
        decision=NS(recommendation="UNDER"), validation=NS(has_supporting_data=True),
    )
    assert not result.sent and result.filtered_reason == "DIRECTION_NOT_OFFERED"
    broadcaster.assert_not_awaited()
    result = await delivery.deliver_underdog(
        source.player_name, source.team, source.sport, source.stat_type, 1.5, 1.5,
        game_time=source.game_time, source=provider, source_id=source.external_id,
        event_id=source.game_id,
        line_variant=canonical.line_variant,
        decision=NS(recommendation="OVER"), validation=NS(has_supporting_data=True),
    )
    assert result.sent
    assert "TELEGRAM_SENT=1" in "\n".join(coverage.report())
    assert source.external_id in "\n".join(coverage.detail(source.external_id)) or "line 1.5" in "\n".join(coverage.detail(source.external_id))


async def _attempt_queued_pick(
    monkeypatch, source, direction, *, line=None, use_delivery_buffer=True
):
    import alerts
    import alert_scope_filter
    import engine.timing
    from unittest.mock import MagicMock
    from alerts import AlertDelivery
    from alert_scope_filter import FilterResult

    monkeypatch.setattr(
        alert_scope_filter, "check", lambda *a, **k: FilterResult(allowed=True)
    )
    monkeypatch.setattr(
        engine.timing, "is_game_alertable", lambda *a, **k: (True, "")
    )
    monkeypatch.setattr(
        "reporting.is_current_reporting_day", lambda *_a, **_k: True
    )
    monkeypatch.setattr(alerts, "_has_strong_underdog_evidence", lambda **_k: True)
    monkeypatch.setattr(alerts, "broadcast_alert", AsyncMock())
    monkeypatch.setattr(alerts, "send_discord_alert", AsyncMock(return_value=False))

    db = MagicMock()
    db.get_telegram_delivery_state = AsyncMock(
        return_value={"tracked": True, "directional": True}
    )
    db.mark_opportunity_alert_sent = AsyncMock()
    db.enqueue_telegram_pick_deliveries = AsyncMock(return_value=[])
    delivery = AlertDelivery(db, MagicMock(), [123])
    delivery._audit_telegram_attempt = AsyncMock(return_value=True)
    buffer = [] if use_delivery_buffer else None
    canonical = canonicalize_player_props(source.provider, [source])[0]
    candidate_line = source.line_value if line is None else line
    result = await delivery.deliver_underdog(
        source.player_name,
        source.team,
        source.sport,
        source.stat_type,
        candidate_line,
        candidate_line,
        game_time=source.game_time,
        source=source.provider,
        source_id=source.external_id,
        event_id=source.game_id,
        line_variant=canonical.line_variant,
        fetched_at=source.fetched_at,
        standing=True,
        strict_pregame=True,
        decision=NS(recommendation=direction),
        validation=NS(has_supporting_data=True),
        defer_delivery=True,
        delivery_buffer=buffer,
    )
    return result, buffer, delivery, db


@pytest.mark.asyncio
async def test_reported_underdog_line_and_under_selection_fail_closed(monkeypatch):
    source = prop(
        provider="Underdog",
        sport="MLB",
        stat="Batter Strikeouts",
        line=0.5,
        external_id="ud-caminero-strikeouts",
        available_directions=("OVER",),
    )
    canonicalize_player_props("Underdog", [source])

    wrong_line, queue, delivery, db = await _attempt_queued_pick(
        monkeypatch, source, "UNDER", line=1.5
    )
    assert wrong_line.filtered
    assert wrong_line.filtered_reason == "EXACT_OFFERING_UNVERIFIED"
    assert queue == []
    delivery._audit_telegram_attempt.assert_not_awaited()
    db.mark_opportunity_alert_sent.assert_not_awaited()
    db.enqueue_telegram_pick_deliveries.assert_not_awaited()

    unavailable_side, queue, delivery, db = await _attempt_queued_pick(
        monkeypatch, source, "UNDER"
    )
    assert unavailable_side.filtered
    assert unavailable_side.filtered_reason == "DIRECTION_NOT_OFFERED"
    assert queue == []
    delivery._audit_telegram_attempt.assert_not_awaited()
    db.mark_opportunity_alert_sent.assert_not_awaited()
    db.enqueue_telegram_pick_deliveries.assert_not_awaited()
    persisted_path, no_buffer, delivery, db = await _attempt_queued_pick(
        monkeypatch, source, "UNDER", use_delivery_buffer=False
    )
    assert persisted_path.filtered_reason == "DIRECTION_NOT_OFFERED"
    assert no_buffer is None
    db.enqueue_telegram_pick_deliveries.assert_not_awaited()
    delivery._audit_telegram_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_prizepicks_goblin_and_demon_offers_never_validate_under(monkeypatch):
    sources = [
        prop(
            provider="PrizePicks",
            sport="MLB",
            stat="Batter Strikeouts",
            line=line,
            external_id=f"pp-{variant}-{line}",
            line_variant=variant,
            available_directions=("OVER",),
        )
        for line, variant in ((0.5, "goblin"), (1.5, "demon"), (2.5, "demon"))
    ]
    canonicalize_player_props("PrizePicks", sources)
    for source in sources:
        result, queue, delivery, db = await _attempt_queued_pick(
            monkeypatch, source, "UNDER"
        )
        assert result.filtered_reason == "DIRECTION_NOT_OFFERED"
        assert queue == []
        delivery._audit_telegram_attempt.assert_not_awaited()
        db.mark_opportunity_alert_sent.assert_not_awaited()


def test_exact_offering_identity_rejects_alternate_source_line_and_variant():
    source = prop(
        provider="PrizePicks",
        sport="MLB",
        stat="Batter Strikeouts",
        line=1.5,
        external_id="pp-exact",
        line_variant="demon",
        available_directions=("OVER",),
    )
    row = canonicalize_player_props("PrizePicks", [source])[0]
    base = dict(
        provider="PrizePicks",
        source_id="pp-exact",
        event_id="event",
        player="Alex Example",
        sport="MLB",
        stat="Batter Strikeouts",
        line=1.5,
        direction="OVER",
        line_variant="demon",
        game_time=row.game_time,
    )
    current, reason = coverage.validate_selection(**base)
    assert current is row and reason == ""

    for field, value in (
        ("source_id", "other-source"),
        ("event_id", "other-event"),
        ("player", "Other Player"),
        ("sport", "NBA"),
        ("stat", "Hits"),
        ("line", 0.5),
    ):
        changed = dict(base)
        changed[field] = value
        current, reason = coverage.validate_selection(**changed)
        assert current is None and reason == "EXACT_OFFERING_UNVERIFIED"

    changed = dict(base, line_variant="goblin")
    current, reason = coverage.validate_selection(**changed)
    assert current is None and reason == "LINE_VARIANT_NOT_OFFERED"
    changed = dict(base, direction="UNDER")
    current, reason = coverage.validate_selection(**changed)
    assert current is None and reason == "DIRECTION_NOT_OFFERED"


@pytest.mark.parametrize(
    "provider_sport,canonical_sport",
    [
        ("CS2", "CS"),
        ("VALORANT", "VAL"),
        ("DOTA2", "DOTA"),
        ("LEAGUE OF LEGENDS", "LOL"),
    ],
)
def test_esports_aliases_use_same_exact_current_offering_identity(
    provider_sport, canonical_sport
):
    source = prop(
        provider="Underdog",
        sport=provider_sport,
        stat="Kills",
        line=10.5,
        external_id=f"esports-{provider_sport}",
        available_directions=("OVER",),
    )
    row = canonicalize_player_props("Underdog", [source])[0]
    assert row.sport == canonical_sport
    current, reason = coverage.validate_selection(
        provider="Underdog",
        source_id=row.external_id,
        event_id=row.game_id,
        player=row.player_name,
        sport=provider_sport,
        stat=row.stat_type,
        line=row.line_value,
        direction="OVER",
        line_variant=None,
        game_time=row.game_time,
    )
    assert current is row and reason == ""
    coverage.clear_scope("Underdog", provider_sport)
    assert row not in coverage.rows.values()


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda row: setattr(row, "fetched_at", datetime.utcnow() - timedelta(seconds=901)),
         "OFFERING_STALE"),
        (lambda row: setattr(row, "removed", True), "OFFERING_REMOVED"),
        (lambda row: setattr(row, "available_directions", ()),
         "OFFERING_DIRECTIONS_UNVERIFIED"),
    ],
)
def test_stale_removed_and_unknown_direction_offers_are_unverified(mutate, expected):
    source = prop(
        provider="Underdog",
        external_id="ud-safety",
        available_directions=("OVER",),
    )
    row = canonicalize_player_props("Underdog", [source])[0]
    mutate(row)
    current, reason = coverage.validate_selection(
        provider="Underdog",
        source_id=row.external_id,
        event_id=row.game_id,
        player=row.player_name,
        sport=row.sport,
        stat=row.stat_type,
        line=row.line_value,
        direction="OVER",
        line_variant=None,
        game_time=row.game_time,
    )
    assert current is None and reason == expected


def test_successful_feed_replacement_discards_removed_old_lines():
    old = prop(provider="Underdog", line=1.5, external_id="ud-moving")
    canonicalize_player_props("Underdog", [old])
    coverage.clear_scope("Underdog")
    current = prop(
        provider="Underdog",
        line=0.5,
        external_id="ud-moving",
        available_directions=("OVER",),
    )
    canonicalize_player_props("Underdog", [current])
    match, reason = coverage.validate_selection(
        provider="Underdog",
        source_id="ud-moving",
        event_id="event",
        player="Alex Example",
        sport="MLB",
        stat="Hits",
        line=1.5,
        direction="UNDER",
        line_variant=None,
        game_time=current.game_time,
    )
    assert match is None and reason == "EXACT_OFFERING_UNVERIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
async def test_exact_over_selection_is_queued_with_original_source_identity(provider, monkeypatch):
    source = prop(
        provider=provider,
        external_id=f"{provider}-source",
        line=0.5,
        line_variant="standard",
        available_directions=("OVER",),
    )
    canonicalize_player_props(provider, [source])
    result, queue, delivery, db = await _attempt_queued_pick(
        monkeypatch, source, "OVER"
    )
    assert result.queued
    assert len(queue) == 1
    assert queue[0]["provider"] == provider
    assert queue[0]["source_id"] == source.external_id
    assert queue[0]["event_id"] == source.game_id
    assert queue[0]["line_value"] == source.line_value
    assert queue[0]["direction"] == "OVER"
    assert queue[0]["line_variant"] == "standard"
    delivery._audit_telegram_attempt.assert_not_awaited()
    db.mark_opportunity_alert_sent.assert_not_awaited()


def test_standard_priority_is_equal_across_platforms_and_drops_only_same_source_alternatives():
    ud_standard = {
        "provider": "Underdog",
        "prop": prop(provider="Underdog", line_variant="standard"),
        "decision": NS(recommendation="OVER"),
    }
    pp_standard = {
        "provider": "PrizePicks",
        "prop": prop(provider="PrizePicks", line_variant="standard"),
        "decision": NS(recommendation="OVER"),
    }
    kept, secondary = prioritize_standard_line_candidates(
        [ud_standard, pp_standard]
    )
    assert kept == [ud_standard, pp_standard]
    assert secondary == []

    pp_goblin = {
        "provider": "PrizePicks",
        "prop": prop(provider="PrizePicks", line=0.5, line_variant="goblin"),
        "decision": NS(recommendation="OVER"),
    }
    pp_demon = {
        "provider": "PrizePicks",
        "prop": prop(provider="PrizePicks", line=2.5, line_variant="demon"),
        "decision": NS(recommendation="OVER"),
    }
    kept, secondary = prioritize_standard_line_candidates(
        [pp_goblin, pp_standard, pp_demon]
    )
    assert kept == [pp_standard]
    assert secondary == [pp_goblin, pp_demon]

    # With no same-source standard candidate, special lines remain explicitly
    # classified secondary options rather than being relabeled or discarded.
    kept, secondary = prioritize_standard_line_candidates([pp_goblin, pp_demon])
    assert kept == [pp_goblin, pp_demon]
    assert secondary == []
    assert {item["prop"].line_variant for item in kept} == {"goblin", "demon"}


def test_standard_priority_does_not_duplicate_or_merge_platform_candidates():
    pp_standard = {
        "provider": "PrizePicks",
        "prop": prop(provider="PrizePicks", line_variant="standard"),
        "decision": NS(recommendation="OVER"),
    }
    ud_special = {
        "provider": "Underdog",
        "prop": prop(provider="Underdog", line_variant="demon"),
        "decision": NS(recommendation="OVER"),
    }
    kept, secondary = prioritize_standard_line_candidates(
        [pp_standard, ud_special]
    )
    assert kept == [pp_standard, ud_special]
    assert secondary == []


@pytest.mark.asyncio
async def test_durable_worker_rechecks_live_offered_direction_before_telegram(monkeypatch):
    import alerts
    from unittest.mock import MagicMock, PropertyMock

    source = prop(
        provider="Underdog",
        external_id="ud-durable-check",
        line=0.5,
        available_directions=("OVER",),
    )
    canonicalize_player_props("Underdog", [source])
    fetched_at = datetime.utcnow()
    qualification = {
        "qualified": True,
        "direction_and_evidence": {"passed": True},
        # A permissive old snapshot must not overrule the current source feed.
        "originating_platform_direction": {
            "available_directions": ["OVER", "UNDER"]
        },
    }
    item = {
        "id": 44,
        "attempt_id": "attempt-44",
        "provider": "Underdog",
        "source_id": source.external_id,
        "event_id": source.game_id,
        "player_name": source.player_name,
        "sport": source.sport,
        "stat_type": source.stat_type,
        "line_value": source.line_value,
        "line_variant": source.line_variant,
        "direction": "UNDER",
        "delivery_path": "underdog",
        "game_time": source.game_time,
        "game_status": "",
        "source_fetched_at": fetched_at,
        "qualification_json": __import__("json").dumps(qualification),
        "evidence_snapshot_json": "{}",
        "message_html": "synthetic fixture only",
        "sent_recipient_keys": [],
    }
    db = MagicMock()
    db.claim_next_telegram_pick_delivery = AsyncMock(return_value=item)
    db.finish_telegram_pick_delivery = AsyncMock(return_value=True)
    db.validate_telegram_pick_source = AsyncMock(return_value={
        "valid": True,
        "provider": "Underdog",
        "source_id": source.external_id,
        "event_id": source.game_id,
        "player_name": source.player_name,
        "sport": source.sport,
        "stat_type": source.stat_type,
        "line_value": source.line_value,
        "line_variant": source.line_variant,
        "game_time": source.game_time,
        "game_status": "",
        "fetched_at": fetched_at,
    })
    monkeypatch.setattr(
        alerts.config.__class__,
        "allowed_user_ids",
        PropertyMock(return_value=[123]),
    )
    monkeypatch.setattr(
        "engine.timing.is_game_alertable", lambda *a, **k: (True, "")
    )
    monkeypatch.setattr("reporting.is_current_reporting_day", lambda *_a: True)
    broadcaster = AsyncMock(return_value={"sent": 1, "failed": 0})
    monkeypatch.setattr(alerts, "broadcast_alert_detailed", broadcaster)

    context = NS(bot_data={"db": db}, bot=MagicMock())
    await alerts.telegram_pick_delivery_job(context)

    broadcaster.assert_not_awaited()
    db.finish_telegram_pick_delivery.assert_awaited_once()
    kwargs = db.finish_telegram_pick_delivery.await_args.kwargs
    assert kwargs["status"] == "invalidated"
    assert kwargs["reason"] == "DIRECTION_NOT_OFFERED"


def test_pushes_cannot_satisfy_strict_under_quality_and_opponent_fragments_do_not_match():
    from engine.delivery_evidence import has_strong_directional_history
    from engine.player_results import _fuzzy_team_match
    rates = compute_hit_rates(games([1] * 30), 1)
    assert not has_strong_directional_history(rates, "UNDER")
    assert rates.season.push_count == 30
    assert not _fuzzy_team_match("New York Rangers", "New Jersey Devils")
    assert not _fuzzy_team_match("LA", "Dallas")


@pytest.mark.asyncio
async def test_market_inventory_is_accessible_from_existing_funnel_command(monkeypatch):
    import commands
    canonicalize_player_props("Underdog", [prop()])
    update = NS(message=NS(reply_text=AsyncMock()))
    monkeypatch.setattr(commands, "_check_allowed", lambda _: True)
    await commands.cmd_funnel(update, NS(args=["markets", "MLB", "Underdog"]))
    assert "MARKET INVENTORY" in update.message.reply_text.call_args.args[0]
    update.message.reply_text.reset_mock()
    await commands.cmd_funnel(update, NS(args=["offering", "Underdog:Hits:1.5"]))
    assert "line 1.5" in update.message.reply_text.call_args.args[0]


@pytest.mark.parametrize("provider", ["Underdog", "PrizePicks"])
def test_simultaneous_alt_lines_have_independent_claims_without_enabling_move_spam(provider):
    from engine.player_prop_market import _record_prop_alerted, _is_prop_deduped
    from alerts import AlertDelivery
    from unittest.mock import MagicMock
    stamp = datetime.utcnow()
    rows = canonicalize_player_props(provider, [
        prop(provider, line=0.5, fetched_at=stamp),
        prop(provider, line=2.5, fetched_at=stamp, line_variant="demon"),
    ])
    a, b = rows
    store = {}
    common = dict(provider=provider, event_id="event", direction="OVER")
    _record_prop_alerted(store, a.player_name, a.sport, a.stat_type, a.line_value,
                         source_id=a.external_id, **common)
    assert not _is_prop_deduped(
        store, b.player_name, b.sport, b.stat_type, b.line_value, 86400, 0.5,
        source_id=b.external_id, **common,
    )
    _record_prop_alerted(store, b.player_name, b.sport, b.stat_type, b.line_value,
                         source_id=b.external_id, **common)
    assert len(store) == 2
    assert _is_prop_deduped(
        store, a.player_name, a.sport, a.stat_type, a.line_value, 86400, 0.5,
        source_id=a.external_id, **common,
    )
    delivery = AlertDelivery(MagicMock(), MagicMock(), [123])
    for row in rows:
        assert delivery._try_claim_prop_delivery(
            provider=provider, source=provider, event_id="event",
            player_name=row.player_name, sport=row.sport, stat_type=row.stat_type,
            direction="OVER", line_value=row.line_value, offering=row,
        )
    assert not delivery._try_claim_prop_delivery(
        provider=provider, source=provider, event_id="event",
        player_name=a.player_name, sport=a.sport, stat_type=a.stat_type,
        direction="OVER", line_value=a.line_value, offering=a,
    )
    # A later moving quote, without a simultaneous old alternate, retains the
    # existing logical no-repeat policy.
    moved = canonicalize_player_props(provider, [
        prop(provider, line=3.5, external_id=b.external_id,
             fetched_at=stamp + timedelta(seconds=1)),
    ])[0]
    assert _is_prop_deduped(
        store, moved.player_name, moved.sport, moved.stat_type, moved.line_value,
        86400, 0.5, source_id=moved.external_id, **common,
    )
