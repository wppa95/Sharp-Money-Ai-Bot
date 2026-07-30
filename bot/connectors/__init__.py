"""
connectors/ — Modular platform connectors for the Multi-Platform Market Engine.

Each connector is a plug-in that implements BaseConnector and returns
normalized MarketSnapshot objects. Connectors are registered in config
with enable/disable flags and per-connector polling intervals.

Public re-exports:
    BaseConnector, MarketSnapshot (from .base)
    DraftKingsConnector           (from .draftkings)
    FanDuelConnector              (from .fanduel)
    UnderdogConnector             (from .underdog)
    ConnectorRegistry             (from .registry)
"""

from .base import BaseConnector, MarketSnapshot, ConnectorStatus
from .draftkings import DraftKingsConnector
from .fanduel import FanDuelConnector
from .underdog import UnderdogConnector
from .registry import ConnectorRegistry

__all__ = [
    "BaseConnector",
    "MarketSnapshot",
    "ConnectorStatus",
    "DraftKingsConnector",
    "FanDuelConnector",
    "UnderdogConnector",
    "ConnectorRegistry",
]
