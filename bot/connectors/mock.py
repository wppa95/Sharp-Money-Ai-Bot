"""
connectors/mock.py — Synthetic odds connector for offline testing.

Emits deterministic MarketSnapshot objects for a set of pre-built NBA
games, covering four distinct market states:

    OPENING    — baseline odds; DraftKings and FanDuel are close together
                 with no line movement recorded yet.

    STEAM      — both books moved significantly in the same direction
                 after sharp money was placed on Boston Celtics -3.5.
                 Opener: -110/-110; after move: ~-135/+113 on DK, -130/+110 on FD.

    EV_WINDOW  — DraftKings has already fully adjusted to the sharp side
                 (-145); FanDuel is stale (-118).  The Celtics at FanDuel
                 are now +EV (offered at -118, fair value ~-135).

    CONSENSUS  — three separate markets across both books all moved in the
                 same direction simultaneously, generating multi-market
                 consensus signal.

No network requests are made.  No API key is required.
This connector is **never registered in production** — import it only in
tests and debug sessions.

Usage in tests
--------------
    connector = MockOddsConnector()
    snaps_opening = await connector.fetch()          # OPENING state

    connector.tick(MockScenario.STEAM)
    snaps_after_steam = await connector.fetch()      # after sharp move

    connector.tick(MockScenario.EV_WINDOW)
    snaps_ev = await connector.fetch()               # stale FD line
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime
from typing import Optional

from .base import BaseConnector, ConnectorStatus, MarketSnapshot

logger = logging.getLogger(__name__)

# ── Fixed game times (UTC) — far enough in the future to be "pre-game" ────────

_GAME_A_TIME = datetime(2026, 8, 5, 19, 30)   # Lakers @ Celtics
_GAME_B_TIME = datetime(2026, 8, 5, 22,  0)   # Warriors @ Bucks


# ── Scenario enum ─────────────────────────────────────────────────────────────

class MockScenario(str, enum.Enum):
    """
    Pre-built market states for the mock connector.

    OPENING   Baseline — no movement, both books tight.
    STEAM     Sharp money on Celtics -3.5 moved both books significantly.
    EV_WINDOW DraftKings fully adjusted; FanDuel stale → Celtics +EV at FD.
    CONSENSUS Three separate markets moved same direction across both books.
    """
    OPENING   = "opening"
    STEAM     = "steam"
    EV_WINDOW = "ev_window"
    CONSENSUS = "consensus"


# ── Row type (internal) ───────────────────────────────────────────────────────

class _Row:
    """One raw odds record used to build a MarketSnapshot."""
    __slots__ = (
        "book", "sport", "event", "market_type", "selection",
        "odds", "line", "game_time",
    )

    def __init__(
        self,
        book:        str,
        sport:       str,
        event:       str,
        market_type: str,
        selection:   str,
        odds:        int,
        line:        Optional[float] = None,
        game_time:   Optional[datetime] = None,
    ) -> None:
        self.book        = book
        self.sport       = sport
        self.event       = event
        self.market_type = market_type
        self.selection   = selection
        self.odds        = odds
        self.line        = line
        self.game_time   = game_time


# ── Scenario data tables ──────────────────────────────────────────────────────
#
# Each scenario is a complete snapshot of every market at that point in time.
# The same (event, selection, book) key may appear across scenarios with
# different odds to simulate line movement.
#
# Abbreviations:
#   DK = DraftKings   FD = FanDuel
#   BOS = Boston Celtics -3.5   LAL = Los Angeles Lakers +3.5
#   ML = Moneyline   SP = Spread   OU = Total (O/U)

_DK = "DraftKings"
_FD = "FanDuel"

_GAME_A = "Los Angeles Lakers @ Boston Celtics"
_GAME_B = "Golden State Warriors @ Milwaukee Bucks"

_NBA = "NBA"
_SP  = "Spread"
_ML  = "Moneyline"
_OU  = "Total (O/U)"

_BOS_SEL = "Boston Celtics"
_LAL_SEL = "Los Angeles Lakers"
_MIL_SEL = "Milwaukee Bucks"
_GSW_SEL = "Golden State Warriors"


# Helper: rows for one spread market at both books (returns 4 rows)
def _spread(dk_fav: int, dk_dog: int, fd_fav: int, fd_dog: int,
            fav_sel: str, dog_sel: str,
            event: str, sport: str, line: float, game_time: datetime) -> list[_Row]:
    return [
        _Row(_DK, sport, event, _SP, fav_sel, dk_fav, -line, game_time),
        _Row(_DK, sport, event, _SP, dog_sel, dk_dog,  line, game_time),
        _Row(_FD, sport, event, _SP, fav_sel, fd_fav, -line, game_time),
        _Row(_FD, sport, event, _SP, dog_sel, fd_dog,  line, game_time),
    ]


def _ml(dk_fav: int, dk_dog: int, fd_fav: int, fd_dog: int,
        fav_sel: str, dog_sel: str,
        event: str, sport: str, game_time: datetime) -> list[_Row]:
    return [
        _Row(_DK, sport, event, _ML, fav_sel, dk_fav, None, game_time),
        _Row(_DK, sport, event, _ML, dog_sel, dk_dog, None, game_time),
        _Row(_FD, sport, event, _ML, fav_sel, fd_fav, None, game_time),
        _Row(_FD, sport, event, _ML, dog_sel, fd_dog, None, game_time),
    ]


def _ou(dk_over: int, dk_under: int, fd_over: int, fd_under: int,
        event: str, sport: str, total: float, game_time: datetime) -> list[_Row]:
    return [
        _Row(_DK, sport, event, _OU, f"Over {total}",  dk_over,  total, game_time),
        _Row(_DK, sport, event, _OU, f"Under {total}", dk_under, total, game_time),
        _Row(_FD, sport, event, _OU, f"Over {total}",  fd_over,  total, game_time),
        _Row(_FD, sport, event, _OU, f"Under {total}", fd_under, total, game_time),
    ]


# ── OPENING: baseline market, no movement yet ─────────────────────────────────

_OPENING_ROWS: list[_Row] = [
    # Game A — Spread (tight, DK and FD within 2 pts)
    *_spread(-110, -110, -112, -108,
             _BOS_SEL, _LAL_SEL, _GAME_A, _NBA, 3.5, _GAME_A_TIME),
    # Game A — Moneyline
    *_ml(-165, +140, -168, +142,
         _BOS_SEL, _LAL_SEL, _GAME_A, _NBA, _GAME_A_TIME),
    # Game A — Total (O/U 224.5)
    *_ou(-110, -110, -112, -108,
         _GAME_A, _NBA, 224.5, _GAME_A_TIME),
    # Game B — Moneyline
    *_ml(-155, +132, -152, +130,
         _MIL_SEL, _GSW_SEL, _GAME_B, _NBA, _GAME_B_TIME),
]


# ── STEAM: sharp money on BOS -3.5; both books moved decisively ───────────────
#
# DK movement: -110 → -138  (-28 pts on the sharp side)
# FD movement: -112 → -132  (-20 pts on the sharp side)
# Both books' dog side also drifted the other way (+)
# This triggers steam detection when compared against OPENING odds.

_STEAM_ROWS: list[_Row] = [
    # Game A — Spread (STEAM on Celtics)
    *_spread(-138, +115, -132, +110,
             _BOS_SEL, _LAL_SEL, _GAME_A, _NBA, 3.5, _GAME_A_TIME),
    # Game A — Moneyline (also moved)
    *_ml(-185, +155, -180, +152,
         _BOS_SEL, _LAL_SEL, _GAME_A, _NBA, _GAME_A_TIME),
    # Game A — Total (O/U unchanged)
    *_ou(-110, -110, -112, -108,
         _GAME_A, _NBA, 224.5, _GAME_A_TIME),
    # Game B — unchanged
    *_ml(-155, +132, -152, +130,
         _MIL_SEL, _GSW_SEL, _GAME_B, _NBA, _GAME_B_TIME),
]


# ── EV_WINDOW: DK fully adjusted, FD stale on Celtics spread ─────────────────
#
# DK Celtics spread: -145  (sharp money already digested)
# FD Celtics spread: -118  (stale line — market hasn't caught up)
#
# Fair value (from DK de-vig):
#   DK Celtics -145 → implied 59.2%
#   DK Lakers  +122 → implied 45.0%
#   Total vig  → 104.2%  →  fair Celtics = 56.8%, fair Lakers = 43.2%
#
# FD Celtics at -118  → implied 54.1%  vs fair 56.8% → edge +2.7%  ← +EV ✓
# FD Lakers  at +100  → implied 50.0%  vs fair 43.2% → edge −6.8%  ← negative EV

_EV_WINDOW_ROWS: list[_Row] = [
    # Game A — Spread (DK fully moved, FD stale)
    _Row(_DK, _NBA, _GAME_A, _SP, _BOS_SEL, -145, -3.5, _GAME_A_TIME),
    _Row(_DK, _NBA, _GAME_A, _SP, _LAL_SEL, +122, +3.5, _GAME_A_TIME),
    _Row(_FD, _NBA, _GAME_A, _SP, _BOS_SEL, -118, -3.5, _GAME_A_TIME),  # stale
    _Row(_FD, _NBA, _GAME_A, _SP, _LAL_SEL, +100, +3.5, _GAME_A_TIME),  # stale
    # Game A — Moneyline (also moved on DK, FD stale)
    _Row(_DK, _NBA, _GAME_A, _ML, _BOS_SEL, -195, None, _GAME_A_TIME),
    _Row(_DK, _NBA, _GAME_A, _ML, _LAL_SEL, +162, None, _GAME_A_TIME),
    _Row(_FD, _NBA, _GAME_A, _ML, _BOS_SEL, -168, None, _GAME_A_TIME),  # stale
    _Row(_FD, _NBA, _GAME_A, _ML, _LAL_SEL, +142, None, _GAME_A_TIME),  # stale
    # Game A — Total (moved on DK, FD stale)
    _Row(_DK, _NBA, _GAME_A, _OU, "Over 224.5",  -125, 224.5, _GAME_A_TIME),
    _Row(_DK, _NBA, _GAME_A, _OU, "Under 224.5", +105, 224.5, _GAME_A_TIME),
    _Row(_FD, _NBA, _GAME_A, _OU, "Over 224.5",  -112, 224.5, _GAME_A_TIME),  # stale
    _Row(_FD, _NBA, _GAME_A, _OU, "Under 224.5", -108, 224.5, _GAME_A_TIME),  # stale
    # Game B — unchanged
    *_ml(-155, +132, -152, +130,
         _MIL_SEL, _GSW_SEL, _GAME_B, _NBA, _GAME_B_TIME),
]


# ── CONSENSUS: three separate markets all moved in the same direction ─────────
#
# Spread, Moneyline, and Total all shifted toward Boston across both books.
# This is the strongest confluence signal — independent markets confirming
# the same directional pressure.

_CONSENSUS_ROWS: list[_Row] = [
    # Spread: decisive move on Celtics
    *_spread(-148, +124, -145, +122,
             _BOS_SEL, _LAL_SEL, _GAME_A, _NBA, 3.5, _GAME_A_TIME),
    # Moneyline: also moved to Celtics
    *_ml(-195, +162, -192, +160,
         _BOS_SEL, _LAL_SEL, _GAME_A, _NBA, _GAME_A_TIME),
    # Total: OVER moved (more scoring expected — Celtics offense dominant)
    *_ou(-135, +112, -132, +110,
         _GAME_A, _NBA, 224.5, _GAME_A_TIME),
    # Game B: Bucks moved too
    *_ml(-172, +144, -168, +142,
         _MIL_SEL, _GSW_SEL, _GAME_B, _NBA, _GAME_B_TIME),
]


_SCENARIO_TABLE: dict[MockScenario, list[_Row]] = {
    MockScenario.OPENING:   _OPENING_ROWS,
    MockScenario.STEAM:     _STEAM_ROWS,
    MockScenario.EV_WINDOW: _EV_WINDOW_ROWS,
    MockScenario.CONSENSUS: _CONSENSUS_ROWS,
}


# ── MockOddsConnector ─────────────────────────────────────────────────────────

class MockOddsConnector(BaseConnector):
    """
    Synthetic odds connector for offline testing.

    Emits pre-built MarketSnapshot objects without any network calls.
    No API key or external service is needed.

    This connector is **never used in production**.  It is intended only
    for unit/integration tests and local development runs.

    Opening-odds tracking
    ---------------------
    Like the live connectors, MockOddsConnector tracks the first-seen
    odds for each (book, event, selection) key.  Calling tick() to a new
    scenario then calling fetch() will populate odds_change on each
    snapshot, which is required for steam detection.

    Typical test sequence
    ---------------------
    1. connector = MockOddsConnector()
    2. snaps_t0 = await connector.fetch()        # OPENING — sets opening odds
    3. connector.tick(MockScenario.STEAM)
    4. snaps_t1 = await connector.fetch()        # STEAM — odds_change populated
    5. assert snaps_t1[0].odds_change <= -20     # sharp move detected
    """

    name          = "MockOdds"
    is_pickem     = False
    poll_interval = 0    # tests don't wait between calls

    def __init__(
        self,
        scenario:      MockScenario = MockScenario.OPENING,
        books:         Optional[list[str]] = None,
        active_sports: Optional[list[str]] = None,
    ) -> None:
        """
        Parameters
        ----------
        scenario        Starting scenario (default: OPENING).
        books           Which books to emit snapshots for.
                        Default: ["DraftKings", "FanDuel"].
        active_sports   Filter to specific sports.  Default: all sports in data.
        """
        self._scenario      = scenario
        self._books         = set(books or [_DK, _FD])
        self._active_sports = set(active_sports) if active_sports else None
        self.enabled        = True

        # (book, event, market_type, selection) → first-seen American odds
        # market_type is included so same-named selections across Spread /
        # Moneyline / Total don't overwrite each other's opening odds.
        self._opening: dict[tuple[str, str, str, str], int] = {}

    # ── Scenario control ──────────────────────────────────────────────────────

    def tick(self, scenario: MockScenario) -> None:
        """
        Advance to a new market scenario.

        Opening odds recorded from previous fetch() calls are preserved,
        so odds_change will be non-zero on the next fetch() if the
        scenario moved the odds.
        """
        self._scenario = scenario
        logger.debug("MockOddsConnector: scenario → %s", scenario.value)

    @property
    def scenario(self) -> MockScenario:
        return self._scenario

    def reset(self) -> None:
        """Reset opening-odds memory and return to OPENING scenario."""
        self._opening.clear()
        self._scenario = MockScenario.OPENING

    # ── BaseConnector implementation ──────────────────────────────────────────

    async def fetch(self) -> list[MarketSnapshot]:
        """Return snapshots for the current scenario. Never raises."""
        rows = _SCENARIO_TABLE[self._scenario]
        now  = datetime.utcnow()
        out: list[MarketSnapshot] = []

        for row in rows:
            if row.book not in self._books:
                continue
            if self._active_sports and row.sport not in self._active_sports:
                continue

            key = (row.book, row.event, row.market_type, row.selection)
            opening = self._opening.setdefault(key, row.odds)

            out.append(MarketSnapshot(
                sportsbook   = row.book,
                sport        = row.sport,
                league       = row.sport,
                event        = row.event,
                market_type  = row.market_type,
                selection    = row.selection,
                odds         = row.odds,
                timestamp    = now,
                line         = row.line,
                game_time    = row.game_time,
                opening_odds = opening,
                is_pickem    = False,
            ))

        logger.debug(
            "MockOddsConnector(%s): %d snapshots returned",
            self._scenario.value, len(out),
        )
        return out

    async def health_check(self) -> ConnectorStatus:
        """Always returns OK — no external dependency."""
        return ConnectorStatus.OK


# ── Convenience factory functions ─────────────────────────────────────────────

def make_mock_dk(scenario: MockScenario = MockScenario.OPENING) -> MockOddsConnector:
    """Return a mock connector that emits only DraftKings snapshots."""
    return MockOddsConnector(scenario=scenario, books=[_DK])


def make_mock_fd(scenario: MockScenario = MockScenario.OPENING) -> MockOddsConnector:
    """Return a mock connector that emits only FanDuel snapshots."""
    return MockOddsConnector(scenario=scenario, books=[_FD])
