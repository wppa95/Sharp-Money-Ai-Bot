"""Coverage and settlement tests for the bounded PropLine read-only rotation."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from providers.player_history import propline_result_rows
from providers.propline import (
    PropLineReadOnlyRotation,
    PropLineEvent,
    PropLineResultEvidence,
    PropLineResultsFetchResult,
    PropLineSport,
    audit_propline_discovery,
    build_propline_read_only_targets,
)


def test_targets_use_discovered_official_aliases_only():
    sports = [
        PropLineSport("basketball_nba", "NBA", "NBA"),
        PropLineSport("tennis_atp", "TENNIS", "ATP"),
    ]
    targets = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                stat_type="Points",
                event_id="event-1",
            ),
            SimpleNamespace(
                sport="CRICKET",
                player_name="A Player",
                stat_type="Runs",
                event_id="event-2",
            ),
        ],
        sports=sports,
    )

    assert {
        target.official_sport
        for target in targets
        if target.player_name or target.event_id
    } == {"basketball_nba"}
    assert all(target.canonical_sport != "CRICKET" for target in targets)
    assert {
        target.family
        for target in targets
        if target.player_name
    } == {"player_games", "player_history", "player_trends"}
    assert {
        target.family
        for target in targets
        if target.event_id
    } == {
        "event_results",
        "event_stats",
        "event_markets",
        "event_odds_history",
        "event_movement",
        "event_best_line",
        "event_ev",
    }


def test_raw_player_game_targets_use_only_documented_sports_and_fields():
    sports = [
        PropLineSport("golf", "GOLF", "Golf"),
        PropLineSport("mma_ufc", "UFC", "UFC"),
        PropLineSport("esports", "ESPORTS", "Esports"),
        PropLineSport("soccer_fifa_world_cup", "FIFA", "FIFA World Cup"),
        PropLineSport(
            "soccer_uefa_champions_league",
            "UCL",
            "UEFA Champions League",
        ),
        PropLineSport("table_tennis", "TT", "Table Tennis"),
        PropLineSport("badminton", "BADMINTON", "Badminton"),
    ]
    active_props = [
        SimpleNamespace(sport="PGA", player_name="Golfer", stat_type="strokes"),
        SimpleNamespace(sport="GOLF", player_name="Golfer", stat_type="unlisted"),
        SimpleNamespace(
            sport="MMA", player_name="Fighter", stat_type="significant strikes"
        ),
        SimpleNamespace(sport="CS", player_name="Counter Player", stat_type="kills"),
        SimpleNamespace(
            sport="CS",
            player_name="Counter Player",
            stat_type="kills_maps_1_2",
        ),
        SimpleNamespace(sport="FIFA", player_name="Soccer Player", stat_type="goals"),
        SimpleNamespace(
            sport="UCL", player_name="European Player", stat_type="shots on target"
        ),
        SimpleNamespace(sport="DOTA", player_name="Dota Player", stat_type="kills"),
        SimpleNamespace(sport="LOL", player_name="LoL Player", stat_type="kills"),
        SimpleNamespace(sport="VAL", player_name="Valorant Player", stat_type="kills"),
        SimpleNamespace(sport="TT", player_name="Table Tennis Player", stat_type="aces"),
        SimpleNamespace(
            sport="BADMINTON", player_name="Badminton Player", stat_type="aces"
        ),
        SimpleNamespace(sport="KBO", player_name="KBO Player", stat_type="hits"),
        SimpleNamespace(sport="NPB", player_name="NPB Player", stat_type="hits"),
    ]

    targets = build_propline_read_only_targets(active_props, sports=sports)
    game_targets = {
        (target.canonical_sport, target.official_sport, target.market)
        for target in targets
        if target.family == "player_games" and target.player_name
    }

    assert game_targets == {
        ("GOLF", "golf", "strokes"),
        ("MMA", "mma_ufc", "significant strikes"),
        ("CS", "esports", "kills"),
        ("CS", "esports", "kills maps 1 2"),
        ("FIFA", "soccer_fifa_world_cup", "goals"),
        ("UCL", "soccer_uefa_champions_league", "shots on target"),
    }
    assert {
        target.family
        for target in targets
        if target.player_name and target.canonical_sport in {"GOLF", "CS", "MMA"}
    } == {"player_games"}


def test_propline_result_rows_preserves_explicit_void_as_private_marker():
    evidence = PropLineResultEvidence(
        observed_at=datetime(2026, 9, 22, 12, 0),
        resolution_state="VOID",
        source_resolution="void",
        actual_value=None,
        resolved_at=None,
        provider="PropLine",
        player_name="Void Tennis Player",
        sport="TENNIS",
        stat_type="aces",
        event_id="tennis-event-32",
        bookmaker_key="prizepicks",
        commence_time=datetime(2026, 9, 22, 5, 0),
    )

    rows = propline_result_rows([evidence])

    assert len(rows) == 1
    assert rows[0].source == "propline_event_results_void"
    assert rows[0].actual_value == 0.0
    assert rows[0].game_date == "2026-09-22"


def test_discovery_audit_reports_new_sport_with_provider_key_and_samples():
    audit = audit_propline_discovery(
        [
            {"key": "basketball_nba", "title": "NBA", "active": True},
            {
                "key": "basketball_nba_3x3",
                "title": "NBA 3x3",
                "active": True,
                "sample_count": 3,
            },
        ]
    )

    assert audit.status == "complete"
    assert [
        (
            finding.provider_key,
            finding.canonical_sport,
            finding.sample_count,
        )
        for finding in audit.unmapped_sports
    ] == [("basketball_nba_3x3", "NBA 3X3", 3)]
    assert audit.unmapped_markets == ()
    identities = {
        (identity.kind, identity.provider_key): identity
        for identity in audit.catalog_identities
    }
    assert identities[("sport", "basketball_nba_3x3")].mapped is False
    assert identities[("sport", "basketball_nba_3x3")].sample_count == 3
    assert identities[("sport", "basketball_nba")].mapped is True


def test_discovery_audit_reports_renamed_market_label_in_canonical_context():
    audit = audit_propline_discovery(
        [
            {
                "key": "basketball_nba",
                "title": "NBA",
                "markets": [
                    {
                        "key": "player_points_renamed",
                        "name": "Player Points",
                        "sample_count": 4,
                    }
                ],
            }
        ]
    )

    assert audit.status == "complete"
    assert [
        (
            finding.provider_key,
            finding.canonical_sport,
            finding.canonical_market,
            finding.sample_count,
        )
        for finding in audit.unmapped_markets
    ] == [("player_points_renamed", "NBA", "points", 4)]
    assert audit.unmapped_markets[0].as_dict()["canonical_context"] == {
        "sport": "NBA",
        "market": "points",
    }


def test_discovery_audit_stays_pending_when_catalog_is_unavailable():
    audit = audit_propline_discovery(None, complete=False)

    assert audit.pending is True
    assert audit.reason == "discovery_incomplete"
    assert audit.unmapped_sports == ()
    assert audit.unmapped_markets == ()
    assert audit.catalog_identities == ()


def test_unmapped_discovered_sport_never_creates_scoring_targets():
    targets = build_propline_read_only_targets(
        sports=[
            PropLineSport(
                "basketball_nba_v2",
                "NBA",
                "NBA renamed",
                mapped=False,
            )
        ]
    )

    assert targets == []


def test_targets_include_officially_discovered_events_without_prop_event_ids():
    targets = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                stat_type="Points",
            )
        ],
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
        events_by_sport={
            "basketball_nba": [
                PropLineEvent(
                    sport_key="basketball_nba",
                    event_id="discovered-event",
                )
            ]
        },
    )

    event_targets = [target for target in targets if target.event_id]
    assert {target.family for target in event_targets} == {
        "event_results",
        "event_stats",
        "event_markets",
        "event_odds_history",
        "event_movement",
        "event_best_line",
        "event_ev",
    }
    assert {target.event_id for target in event_targets} == {"discovered-event"}
    assert {
        target.event_status for target in event_targets
    } == {""}


def test_event_targets_reject_guessed_or_cross_sport_event_ids():
    targets = build_propline_read_only_targets(
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
        events_by_sport={
            "NBA": [
                {"id": "missing-sport-key"},
                {"id": "wrong-sport", "sport_key": "football_nfl"},
                {"id": "accepted-event", "sport_key": "basketball_nba"},
            ]
        },
    )

    assert {
        (target.family, target.event_id)
        for target in targets
        if target.event_id
    } == {
        ("event_results", "accepted-event"),
        ("event_stats", "accepted-event"),
        ("event_markets", "accepted-event"),
        ("event_odds_history", "accepted-event"),
        ("event_movement", "accepted-event"),
        ("event_best_line", "accepted-event"),
        ("event_ev", "accepted-event"),
    }


def test_player_target_identity_prefers_provider_player_id_and_dedupes():
    targets = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A. Player",
                player_id="player-7",
                stat_type="Points",
                provider="Underdog",
            ),
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                player_id="player-7",
                stat_type="Points",
                provider="Underdog",
            ),
        ],
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
    )

    player_targets = [target for target in targets if target.family.startswith("player_")]
    assert len(player_targets) == 3
    assert {target.player_id for target in player_targets} == {"player-7"}
    assert {target.bookmaker for target in player_targets} == {"underdog"}
    assert all("player-7" in target.identity for target in player_targets)


def test_unmapped_player_market_is_not_requested():
    targets = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                stat_type="Unmapped Stat",
            )
        ],
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
    )

    assert not any(target.family.startswith("player_") for target in targets)


def test_event_level_results_and_markets_dedupe_across_prop_markets():
    targets = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                stat_type="Points",
                event_id="event-1",
                provider="PrizePicks",
            ),
            SimpleNamespace(
                sport="NBA",
                player_name="B Player",
                stat_type="Rebounds",
                event_id="event-1",
                provider="PrizePicks",
            ),
        ],
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
    )

    event_targets = [target for target in targets if target.event_id == "event-1"]
    assert sum(target.family == "event_results" for target in event_targets) == 1
    assert sum(target.family == "event_markets" for target in event_targets) == 1
    assert sum(target.family == "event_stats" for target in event_targets) == 2


def test_fetch_uses_documented_endpoints_and_never_single_player_stats(tmp_path):
    class EndpointClient:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            if name == "fetch_single_player_stats":
                raise AssertionError("excluded single-player stats endpoint used")
            if not name.startswith("fetch_"):
                raise AttributeError(name)

            async def call(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return SimpleNamespace(
                    succeeded=True, complete=True, redacted=False, payload={}
                )

            return call

    targets = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                player_id="player-1",
                stat_type="Points",
                event_id="event-1",
                provider="PrizePicks",
            )
        ],
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
    )
    client = EndpointClient()
    rotation = PropLineReadOnlyRotation(
        client,
        state_path=str(tmp_path / "rotation.sqlite3"),
        max_requests_per_cycle=12,
    )

    async def fetch_all():
        for target in targets:
            await rotation._fetch(target)

    asyncio.run(fetch_all())
    calls = {name: kwargs for name, _args, kwargs in client.calls}

    assert set(calls) >= {
        "fetch_player_games",
        "fetch_player_history",
        "fetch_player_trends",
        "fetch_event_results",
        "fetch_event_stats",
        "fetch_event_markets",
        "fetch_event_odds_history",
        "fetch_event_movement",
        "fetch_event_best_line",
        "fetch_event_ev",
        "fetch_scores",
    }
    assert "markets" not in calls["fetch_event_results"]
    assert calls["fetch_event_stats"]["stat_type"] == "points"
    assert calls["fetch_player_games"] == {"limit": 100}
    assert calls["fetch_player_history"]["limit"] == 100
    assert calls["fetch_player_history"]["bookmaker"] == "prizepicks"


class _RotationClient:
    is_configured = True

    def __init__(self):
        self.calls = []

    async def _result(self, family, *args, **kwargs):
        self.calls.append((family, args, kwargs))
        if family == "event_results":
            return PropLineResultsFetchResult(
                results=(),
                fetched_at=datetime.utcnow(),
                event_id="event-1",
                succeeded=True,
                complete=True,
            )
        return SimpleNamespace(
            payload={"family": family},
            succeeded=True,
            complete=True,
            redacted=False,
        )

    async def fetch_event_results(self, *args, **kwargs):
        return await self._result("event_results", *args, **kwargs)

    async def fetch_event_stats(self, *args, **kwargs):
        return await self._result("event_stats", *args, **kwargs)

    async def fetch_event_markets(self, *args, **kwargs):
        return await self._result("event_markets", *args, **kwargs)

    async def fetch_player_games(self, *args, **kwargs):
        return await self._result("player_games", *args, **kwargs)

    async def fetch_player_history(self, *args, **kwargs):
        return await self._result("player_history", *args, **kwargs)

    async def fetch_player_trends(self, *args, **kwargs):
        return await self._result("player_trends", *args, **kwargs)

    async def fetch_scores(self, *args, **kwargs):
        return await self._result("scores", *args, **kwargs)


def _all_family_targets():
    return [
        SimpleNamespace(
            family=family,
            official_sport="basketball_nba",
            canonical_sport="NBA",
            player_name="A Player" if family.startswith("player_") else "",
            market="points" if family.startswith("player_") else "",
            event_id="event-1" if family.startswith("event_") else "",
            bookmaker="prizepicks",
        )
        for family in (
            "event_results",
            "event_stats",
            "event_markets",
            "scores",
        )
    ]


def test_rotation_checkpoints_each_request_and_resumes_after_restart(tmp_path):
    targets = [
        # Use the real target type so identity and event input construction are
        # part of this restart contract.
        target
        for target in build_propline_read_only_targets(
            [
                SimpleNamespace(
                    sport="NBA",
                    player_name="A Player",
                    stat_type="Points",
                    event_id="event-1",
                )
            ],
            sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
        )
    ]
    first_client = _RotationClient()
    first = asyncio.run(
        PropLineReadOnlyRotation(
            first_client,
            state_path=str(tmp_path / "rotation.sqlite3"),
            max_requests_per_cycle=2,
        ).run(targets)
    )
    second_client = _RotationClient()
    second = asyncio.run(
        PropLineReadOnlyRotation(
            second_client,
            state_path=str(tmp_path / "rotation.sqlite3"),
            max_requests_per_cycle=2,
        ).run(targets)
    )

    assert first.attempted == second.attempted == 2
    assert [call[0] for call in first_client.calls] != [
        call[0] for call in second_client.calls
    ]
    assert first.provenance
    assert second.provenance


def test_rotation_reports_unresolved_results_without_persisting_them(tmp_path):
    target = build_propline_read_only_targets(
        [
            SimpleNamespace(
                sport="NBA",
                player_name="A Player",
                stat_type="Points",
                event_id="event-1",
            )
        ],
        sports=[PropLineSport("basketball_nba", "NBA", "NBA")],
    )[0]
    client = _RotationClient()
    report = asyncio.run(
        PropLineReadOnlyRotation(
            client,
            state_path=str(tmp_path / "rotation.sqlite3"),
            max_requests_per_cycle=1,
        ).run([target])
    )

    assert report.completed == 0
    assert report.pending == 1
    assert report.persisted == 0
    assert report.provenance[0]["reason"] == "no_explicit_settlement"


def test_redacted_retired_and_incomplete_results_never_become_rows():
    common = dict(
        observed_at=datetime.utcnow(),
        source_resolution="WON",
        actual_value=20,
        resolved_at=datetime.utcnow(),
        provider="PrizePicks",
        player_name="A Player",
        sport="NBA",
        stat_type="points",
        event_id="event-1",
        bookmaker_key="prizepicks",
        market_key="player_points",
    )
    for extra in (
        {"redacted": True},
        {"provider_status": "retired"},
        {"response_complete": False},
    ):
        evidence = PropLineResultEvidence(
            resolution_state="UNRESOLVED" if extra.get("redacted") else "WON",
            **common,
            **extra,
        )
        assert evidence.is_settled is False
        assert propline_result_rows([evidence]) == []