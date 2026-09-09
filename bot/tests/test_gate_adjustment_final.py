"""Regression tests for the canonical direction-and-evidence delivery gate."""

from __future__ import annotations

import inspect
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("TELEGRAM_TOKEN", "test:token")

import market_engine as me
from alerts import AlertDelivery, _is_fresh_delivery_line
from engine import player_prop_market, pp_reference, pregame_watch


@pytest.mark.parametrize("sport", ["WNBA", "NHL", "SOCCER", "CS", "TENNIS", "MLB", "NFL"])
@pytest.mark.parametrize("direction", ["OVER", "UNDER"])
def test_direction_requires_evidence(sport, direction):
    assert not me._tier_delivery_gate(sport, direction, 0, 0, False, "PASS", confidence=0)
    assert me._tier_delivery_gate(sport, direction, 0, 0, True, "PASS", confidence=0)


def test_direction_is_always_required():
    assert not me._tier_delivery_gate("NHL", "PASS", 100, 100, True, "S", confidence=100)
    assert not me._tier_delivery_gate("NBA", "", 100, 100, True, "S", confidence=100)


def test_all_scan_paths_call_the_canonical_wrapper():
    source = inspect.getsource(me)
    assert source.count("_tier_delivery_gate(") >= 7
    assert "engine.delivery_gates" in source
    assert "_full_pool_rescan_job" in source


def test_legacy_tier_names_do_not_appear_in_delivery_gate_source():
    source = inspect.getsource(me._tier_delivery_gate)
    assert "decision_tier" not in source
    assert "TIER2" not in source


def test_direct_delivery_methods_keep_provider_and_freshness_guards():
    pp_source = inspect.getsource(AlertDelivery.deliver_pp)
    ud_source = inspect.getsource(AlertDelivery.deliver_underdog)
    assert "_is_fresh_delivery_line" in pp_source
    assert "sport=_pp_sport" in pp_source
    assert "_is_fresh_delivery_line" in ud_source
    assert '{"UNDERDOG", "PRIZEPICKS"}' in ud_source


def test_typed_stale_provider_line_is_not_deliverable():
    now = datetime.utcnow()
    assert _is_fresh_delivery_line(now - timedelta(minutes=1))
    assert not _is_fresh_delivery_line(now - timedelta(hours=7))


def test_live_scan_paths_pass_observation_timestamp_to_delivery():
    for fn in (
        me.underdog_job,
        me._stable_refresh_job,
        me._full_pool_rescan_job,
    ):
        assert "fetched_at" in inspect.getsource(fn)


def test_direct_compatibility_broadcasters_keep_canonical_safety_boundaries():
    for fn in (
        player_prop_market.run_player_prop_market_cycle,
        pp_reference.run_pp_reference_cycle,
        pregame_watch.PregameWatchEngine.pregame_scan,
    ):
        source = inspect.getsource(fn)
        assert "_directional_evidence_gate" in source
        assert "_is_fresh_delivery_line" in source
        assert "is_game_alertable" in source
        assert "provider" in source
        # Reference matching is context-only; it must not independently
        # broadcast a Telegram message.
        if fn is not pp_reference.run_pp_reference_cycle:
            assert "broadcast_alert" in source

    source = inspect.getsource(player_prop_market.run_player_prop_market_cycle)
    assert "_try_claim_prop_delivery" in source
    assert "_release_prop_delivery_claim" in source
