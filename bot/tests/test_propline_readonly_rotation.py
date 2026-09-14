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
    } == {"event_results", "event_stats", "event_markets"}


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
    }
    assert {target.event_id for target in event_targets} == {"discovered-event"}


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
    }


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
            "player_games",
            "player_history",
            "player_trends",
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