"""Regression tests for the canonical internal Player Props boundary."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from engine.player_props_pipeline import (
    canonicalize_player_props,
    persist_canonical_player_props,
)


def _prop(provider: str, external_id: str, line: float, variant: str = ""):
    return SimpleNamespace(
        provider=provider,
        player_name="LeBron James",
        team="LAL",
        sport="NBA",
        stat_type="Points",
        line_value=line,
        external_id=external_id,
        event_id="game-1",
        game_time=datetime(2026, 9, 7, 20, 0),
        line_variant=variant,
        fetched_at=datetime(2026, 9, 7, 14, 0),
    )


def test_underdog_and_propline_share_the_same_canonical_shape():
    underdog = canonicalize_player_props(
        "Underdog",
        [_prop("Underdog", "ud-standard", 25.5)],
    )[0]
    propline = canonicalize_player_props(
        "PrizePicks",
        [_prop("PrizePicks", "pp-standard", 25.5)],
    )[0]

    assert (
        underdog.sport,
        underdog.player_name,
        underdog.stat_type,
        underdog.line_value,
    ) == (
        propline.sport,
        propline.player_name,
        propline.stat_type,
        propline.line_value,
    )
    assert underdog.provider == "Underdog"
    assert propline.provider == "PrizePicks"
    assert underdog.source_id == "ud-standard"
    assert propline.source_id == "pp-standard"


def test_alternate_lines_remain_distinct_in_one_provider_batch():
    props = canonicalize_player_props(
        "Underdog",
        [
            _prop("Underdog", "ud-standard", 25.5, "standard"),
            _prop("Underdog", "ud-goblin", 20.5, "goblin"),
            _prop("Underdog", "ud-demon", 30.5, "demon"),
        ],
    )

    assert len(props) == 3
    assert {prop.source_id for prop in props} == {
        "ud-standard",
        "ud-goblin",
        "ud-demon",
    }
    assert {prop.line_variant for prop in props} == {
        "standard",
        "goblin",
        "demon",
    }


def test_same_source_line_keeps_latest_observation():
    props = canonicalize_player_props(
        "PrizePicks",
        [
            _prop("PrizePicks", "pp-1", 25.5),
            _prop("PrizePicks", "pp-1", 26.5),
        ],
    )

    assert len(props) == 1
    assert props[0].line_value == 26.5


@pytest.mark.asyncio
async def test_shared_persistence_adapter_preserves_provider_and_source_metadata():
    calls = []

    class FakeDB:
        async def upsert_prop_line_history_batch(self, provider, props, **kwargs):
            calls.append((provider, list(props), kwargs))

    count = await persist_canonical_player_props(
        FakeDB(),
        "PrizePicks",
        [_prop("PrizePicks", "pp-1", 25.5, "goblin")],
        append_snapshot=True,
    )

    assert count == 1
    provider, rows, options = calls[0]
    assert provider == "PrizePicks"
    assert options == {"append_snapshot": True}
    assert rows[0].provider == "PrizePicks"
    assert rows[0].external_id == "pp-1"
    assert rows[0].line_variant == "goblin"
    assert rows[0].event_id == "game-1"


@pytest.mark.asyncio
async def test_provider_ingestion_can_write_observations_and_tracking_together():
    calls = []

    class FakeDB:
        async def save_prop_line_observations_batch(self, provider, props):
            calls.append(("observations", provider, list(props)))
            return len(props)

        async def upsert_prop_line_history_batch(self, provider, props, **kwargs):
            calls.append(("history", provider, list(props), kwargs))

        async def track_prop_records(self, records):
            calls.append(("tracking", list(records)))
            return len(records)

    count = await persist_canonical_player_props(
        FakeDB(),
        "DraftKings",
        [_prop("DraftKings", "dk-1", 25.5)],
        append_snapshot=True,
        append_observations=True,
        track=True,
    )

    assert count == 1
    assert [call[0] for call in calls] == [
        "observations",
        "history",
        "tracking",
    ]
    assert calls[0][1] == "DraftKings"
    assert calls[2][1][0]["provider"] == "DraftKings"