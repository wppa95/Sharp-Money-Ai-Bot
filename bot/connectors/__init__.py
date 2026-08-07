"""
connectors/ — Modular platform connectors for the Multi-Platform Market Engine.

Each connector is a plug-in that implements BaseConnector and returns
normalized MarketSnapshot objects. Connectors are registered in config
with enable/disable flags and per-connector polling intervals.

Public re-exports:
    BaseConnector, MarketSnapshot (from .base)
    UnderdogConnector             (from .underdog)
    ConnectorRegistry             (from .registry)

Note: DraftKings and FanDuel connectors were removed (Aug 2026).
Their snapshots did not feed into Underdog confidence scoring, so
they provided no marginal value over the existing OddsAPI confirmation
layer. Provider rule: only keep providers that improve actionable picks.
"""

from .base import BaseConnector, MarketSnapshot, ConnectorStatus
from .underdog import UnderdogConnector
from .registry import ConnectorRegistry
from .mock import MockOddsConnector, MockScenario, make_mock_dk, make_mock_fd

__all__ = [
    "BaseConnector",
    "MarketSnapshot",
    "ConnectorStatus",
    "UnderdogConnector",
    "ConnectorRegistry",
    # ── Testing only — never registered in production ──────────────────────
    "MockOddsConnector",
    "MockScenario",
    "make_mock_dk",
    "make_mock_fd",
]
