"""
connectors/base.py — Common connector interface and MarketSnapshot model.

All platform connectors implement BaseConnector. Each fetch() call returns
a list of MarketSnapshot objects normalized to the standard market data model.

MarketSnapshot is the single standardized record that flows through the
consensus engine, steam detector, and CLV tracker. Pick'em markets
(PrizePicks, Underdog) set is_pickem=True and are kept isolated from
sportsbook moneyline/spread analysis.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Market snapshot (standardized cross-platform record) ──────────────────────

@dataclass
class MarketSnapshot:
    """
    One normalized odds/projection snapshot from one platform at one moment.

    Fields
    ------
    sportsbook   Platform name (e.g. "DraftKings", "FanDuel", "Underdog")
    sport        Sport code (e.g. "NFL", "NBA")
    league       League code (same as sport for US markets)
    event        Canonical event string (e.g. "Chiefs @ Raiders")
    market_type  "Moneyline" | "Spread" | "Total (O/U)" | "Player Prop" | "Pick'em"
    selection    What is being wagered (e.g. "Chiefs -3.5" or "LeBron Over 27.5 Pts")
    player       Player name for player props / pick'em; None for team markets
    team         Team abbreviation for context
    line         Numeric spread/total/prop value (None for pure moneylines)
    odds         American odds (e.g. -110). For pick'em use 0 (no odds offered).
    timestamp    When this snapshot was captured (UTC)
    game_time    When the event starts (UTC); None if unknown
    opening_odds First recorded odds for this market at this book (None until set)
    is_pickem    True for pick'em platforms (Underdog, PrizePicks) — keep isolated
    """
    sportsbook:   str
    sport:        str
    league:       str
    event:        str
    market_type:  str
    selection:    str
    odds:         int                          # 0 for pick'em
    timestamp:    datetime = field(default_factory=datetime.utcnow)
    player:       Optional[str] = None
    team:         Optional[str] = None
    line:         Optional[float] = None
    game_time:    Optional[datetime] = None
    opening_odds: Optional[int] = None        # set on first sight
    external_id:  Optional[str] = None
    is_pickem:    bool = False

    @property
    def market_key(self) -> tuple[str, str, str, str]:
        """
        Canonical grouping key for the consensus engine.
        Groups snapshots that represent the same side of the same market
        across different sportsbooks.
        """
        return (self.sport, self.event, self.market_type, self.selection)

    @property
    def implied_probability(self) -> float:
        """Raw implied probability from American odds. Returns 0.5 for pick'em."""
        if self.odds == 0 or self.is_pickem:
            return 0.5
        if self.odds < 0:
            return abs(self.odds) / (abs(self.odds) + 100)
        return 100 / (self.odds + 100)

    @property
    def odds_change(self) -> Optional[int]:
        """Change from opening odds to current. None if opening not recorded."""
        if self.opening_odds is None:
            return None
        return self.odds - self.opening_odds

    def __repr__(self) -> str:
        odds_str = f"+{self.odds}" if self.odds > 0 else str(self.odds)
        return (
            f"MarketSnapshot({self.sportsbook!r}, {self.event!r}, "
            f"{self.selection!r}, {odds_str})"
        )


# ── Connector health status ───────────────────────────────────────────────────

class ConnectorStatus(str, enum.Enum):
    OK       = "ok"
    ERROR    = "error"
    DISABLED = "disabled"
    NO_KEY   = "no_key"


# ── Base connector interface ──────────────────────────────────────────────────

class BaseConnector(abc.ABC):
    """
    Abstract base class for all market data connectors.

    Subclasses must implement:
        fetch()         — retrieve and normalize current market snapshots
        health_check()  — verify the data source is reachable

    Config keys (set in subclass __init__):
        name            Human-readable connector name
        enabled         Whether this connector is active
        is_pickem       True for pick'em connectors (Underdog, PrizePicks)
        poll_interval   Seconds between fetch() calls (read from config)
    """

    name:         str  = "BaseConnector"
    enabled:      bool = True
    is_pickem:    bool = False
    poll_interval: int = 60

    @abc.abstractmethod
    async def fetch(self) -> list[MarketSnapshot]:
        """
        Fetch current market data and normalize to MarketSnapshot objects.

        Returns an empty list (with a warning log) on any transient failure
        so the polling loop can continue.
        """
        ...

    @abc.abstractmethod
    async def health_check(self) -> ConnectorStatus:
        """Return ConnectorStatus.OK when the data source is reachable."""
        ...

    def __repr__(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"{self.__class__.__name__}(name={self.name!r}, {state})"
