"""
Data models for the Sharp Money +EV Detection Bot.
All models are plain dataclasses; SQLAlchemy ORM models live in database.py.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ── Enums ──────────────────────────────────────────────────────────────────────

class Sport(str, enum.Enum):
    NFL = "NFL"
    NBA = "NBA"
    MLB = "MLB"
    NHL = "NHL"
    NCAAF = "NCAAF"
    NCAAB = "NCAAB"
    UFC = "UFC"
    SOCCER = "Soccer"
    OTHER = "Other"


class MarketType(str, enum.Enum):
    MONEYLINE = "Moneyline"
    SPREAD = "Spread"
    TOTAL = "Total (O/U)"
    PLAYER_PROP = "Player Prop"
    TEAM_PROP = "Team Prop"
    FUTURES = "Futures"


class AlertType(str, enum.Enum):
    STEAM = "Steam Move"
    SHARP = "Sharp Money"
    EV_POSITIVE = "+EV Opportunity"
    REVERSE_LINE = "Reverse Line Move"
    PRIZEPICKS = "PrizePicks +EV"


class Recommendation(str, enum.Enum):
    STRONG_BET = "Strong Bet"
    BET = "Bet"
    LEAN = "Lean"
    PASS = "Pass"
    FADE = "Fade"


# ── Core data models ───────────────────────────────────────────────────────────

@dataclass
class OddsLine:
    """A single odds snapshot from one sportsbook at one point in time."""
    sportsbook: str
    sport: Sport
    market_type: MarketType
    event: str                        # e.g. "Chiefs vs Raiders"
    selection: str                    # e.g. "Chiefs -3.5" or "Patrick Mahomes Over 285.5 Passing Yds"
    american_odds: int                # e.g. -110, +150
    line: Optional[float] = None      # spread / total value
    timestamp: datetime = field(default_factory=datetime.utcnow)
    event_start: Optional[datetime] = None

    @property
    def implied_probability(self) -> float:
        """Convert American odds to raw implied probability (includes vig)."""
        if self.american_odds < 0:
            return abs(self.american_odds) / (abs(self.american_odds) + 100)
        return 100 / (self.american_odds + 100)

    @property
    def decimal_odds(self) -> float:
        """Convert American odds to decimal odds."""
        if self.american_odds < 0:
            return 1 + (100 / abs(self.american_odds))
        return 1 + (self.american_odds / 100)


@dataclass
class OddsMovement:
    """Tracks movement of a line from opening to current."""
    opening: OddsLine
    current: OddsLine

    @property
    def odds_change(self) -> int:
        return self.current.american_odds - self.opening.american_odds

    @property
    def line_change(self) -> Optional[float]:
        if self.opening.line is not None and self.current.line is not None:
            return self.current.line - self.opening.line
        return None


@dataclass
class FairOdds:
    """De-vigged (fair) probability and corresponding American odds."""
    selection: str
    fair_probability: float           # 0.0 – 1.0
    fair_american_odds: int
    vig_percentage: float             # e.g. 4.76 for a standard -110/-110 market
    market_width: float               # total implied probability of both sides


@dataclass
class EVResult:
    """Expected value calculation for a single selection."""
    selection: str
    fair_odds: FairOdds
    offered_american_odds: int
    ev_percentage: float              # positive = +EV
    edge: float                       # fair_prob - implied_prob (raw edge)
    kelly_fraction: float             # full Kelly stake fraction
    half_kelly: float                 # conservative half-Kelly


@dataclass
class SteamAlert:
    """Detected steam / sharp money move."""
    alert_type: AlertType
    sport: Sport
    market_type: MarketType
    event: str
    selection: str
    opening_odds: int
    current_odds: int
    steam_score: int                  # 0–100
    steam_direction: str              # "UP" or "DOWN"
    books_moved: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    notes: str = ""


@dataclass
class EVOpportunity:
    """A full +EV opportunity with all supporting data."""
    ev_result: EVResult
    steam_alert: Optional[SteamAlert]
    sport: Sport
    market_type: MarketType
    event: str
    player: Optional[str]             # for player props
    line: Optional[float]
    best_odds: int
    best_book: str
    fair_probability: float
    expected_value: float             # percentage
    steam_score: int                  # 0–100
    ai_confidence: int                # 0–100
    recommendation: Recommendation
    stars: int                        # 1–5
    reason_codes: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def star_display(self) -> str:
        return "★" * self.stars + "☆" * (5 - self.stars)


@dataclass
class BotStats:
    """Runtime statistics for the /status command."""
    uptime_seconds: float
    total_alerts_sent: int
    total_steam_detected: int
    total_ev_found: int
    books_monitored: int
    last_odds_update: Optional[datetime]
    active_markets: int
    db_records: int
