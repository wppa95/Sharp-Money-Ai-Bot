"""
engine/steam.py — Professional steam detection engine.

Steam (sharp money movement) is identified by tracking rapid, coordinated
line movement across multiple sportsbooks — especially when led by sharp books
(Pinnacle, Circa, Bookmaker) and moving against public betting percentages.

Public API
----------
    # Build movement snapshots from your odds feed
    event = LineMovementEvent(sportsbook="Pinnacle", american_odds=-115, ...)
    movement = SteamMovement(opening=open_evt, current=curr_evt, previous=prev_evt)

    # Run the steam engine
    result = compute_steam(movements=movements, context=SteamContext(...))

    # Or use the simplified helper for two-snapshot comparisons
    result = compute_steam_simple(
        market="Chiefs vs Raiders",
        sport=Sport.NFL,
        market_type=MarketType.SPREAD,
        selection="Chiefs -3",
        book_snapshots=[
            {"sportsbook": "Pinnacle", "open_odds": -110, "current_odds": -118},
            {"sportsbook": "DraftKings", "open_odds": -110, "current_odds": -117},
        ],
    )

Steam Score (0–100)
-------------------
    Signal                       Max pts
    ────────────────────────────────────
    Books moving                   30
    Sharp book weighting           25
    Speed of movement              20
    Magnitude of movement          15
    Book consensus / agreement     10
    ────────────────────────────────────
    Total                         100

    Reverse line movement bonus: up to +10 pts applied after scoring
    (capped at 100).

Alert tiers
-----------
    90–100   CRITICAL_STEAM
    75–89    STRONG_STEAM
    60–74    MODERATE_STEAM
    < 60     NO_ALERT
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence


# ── Constants — sharp book registry ───────────────────────────────────────────

# Weight 1.0 = pinnacle-tier (market-setting sharp book)
# Weight 0.6 = semi-sharp (respected limits, but not market-setting)
# Weight 0.3 = soft/recreational book
SPORTSBOOK_WEIGHTS: dict[str, float] = {
    # Tier 1 — true market setters
    "Pinnacle":        1.00,
    "Circa":           0.95,
    "Bookmaker":       0.90,
    "BetOnline":       0.85,
    "Heritage":        0.80,
    # Tier 2 — respected sharp books
    "BetNow":          0.65,
    "SportsBetting":   0.65,
    "Jazz":            0.60,
    "5Dimes":          0.60,
    # Tier 3 — major US recreational books
    "DraftKings":      0.40,
    "FanDuel":         0.40,
    "BetMGM":          0.35,
    "Caesars":         0.35,
    "PointsBet":       0.30,
    "WynnBet":         0.30,
    "Barstool":        0.25,
    "SI Sportsbook":   0.25,
}

DEFAULT_BOOK_WEIGHT = 0.30  # for unknown books

SHARP_BOOK_THRESHOLD = 0.80  # weight ≥ this → considered a sharp book


# ── Enums ──────────────────────────────────────────────────────────────────────

class MovementDirection(str, enum.Enum):
    UP   = "UP"    # odds increasing (e.g. −110 → −105, or +150 → +155)
    DOWN = "DOWN"  # odds decreasing (e.g. −110 → −115, or +150 → +145)
    FLAT = "FLAT"  # no change

    @property
    def emoji(self) -> str:
        return {"UP": "📈", "DOWN": "📉", "FLAT": "➡️"}[self.value]


class SteamTier(str, enum.Enum):
    CRITICAL_STEAM = "CRITICAL_STEAM"    # 90–100
    STRONG_STEAM   = "STRONG_STEAM"      # 75–89
    MODERATE_STEAM = "MODERATE_STEAM"    # 60–74
    NO_ALERT       = "NO_ALERT"          # < 60

    @classmethod
    def from_score(cls, score: int) -> "SteamTier":
        if score >= 90:
            return cls.CRITICAL_STEAM
        if score >= 75:
            return cls.STRONG_STEAM
        if score >= 60:
            return cls.MODERATE_STEAM
        return cls.NO_ALERT

    @property
    def emoji(self) -> str:
        return {
            SteamTier.CRITICAL_STEAM: "🚨",
            SteamTier.STRONG_STEAM:   "🔥",
            SteamTier.MODERATE_STEAM: "⚠️",
            SteamTier.NO_ALERT:       "⚪",
        }[self]

    @property
    def should_alert(self) -> bool:
        return self != SteamTier.NO_ALERT


class ConfidenceLevel(str, enum.Enum):
    VERY_HIGH = "VERY_HIGH"
    HIGH      = "HIGH"
    MEDIUM    = "MEDIUM"
    LOW       = "LOW"

    @classmethod
    def from_score(cls, score: int) -> "ConfidenceLevel":
        if score >= 85:
            return cls.VERY_HIGH
        if score >= 70:
            return cls.HIGH
        if score >= 55:
            return cls.MEDIUM
        return cls.LOW

    @property
    def emoji(self) -> str:
        return {
            ConfidenceLevel.VERY_HIGH: "🟢",
            ConfidenceLevel.HIGH:      "🟡",
            ConfidenceLevel.MEDIUM:    "🟠",
            ConfidenceLevel.LOW:       "🔴",
        }[self]


# ── Data objects ───────────────────────────────────────────────────────────────

@dataclass
class LineMovementEvent:
    """
    A single odds snapshot at one sportsbook at one point in time.
    The fundamental unit of all steam detection.
    """
    sportsbook: str
    american_odds: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    line: Optional[float] = None        # spread or total value (None for moneylines)
    is_live: bool = False               # True if the game is already in progress

    @property
    def weight(self) -> float:
        """Sharp book weight for this sportsbook (0.0–1.0)."""
        return SPORTSBOOK_WEIGHTS.get(self.sportsbook, DEFAULT_BOOK_WEIGHT)

    @property
    def is_sharp_book(self) -> bool:
        return self.weight >= SHARP_BOOK_THRESHOLD


@dataclass
class SteamMovement:
    """
    Full movement record for one sportsbook: opening → previous → current.
    ``previous`` is optional — it represents the last recorded snapshot
    before ``current``, used to measure movement speed.
    """
    opening: LineMovementEvent
    current: LineMovementEvent
    previous: Optional[LineMovementEvent] = None

    # ── Direction ────────────────────────────────────────────────────────────

    @property
    def direction(self) -> MovementDirection:
        diff = self.current.american_odds - self.opening.american_odds
        if diff > 0:
            return MovementDirection.UP
        if diff < 0:
            return MovementDirection.DOWN
        return MovementDirection.FLAT

    @property
    def is_moving(self) -> bool:
        return self.direction != MovementDirection.FLAT

    # ── Magnitude ────────────────────────────────────────────────────────────

    @property
    def odds_change(self) -> int:
        """Signed change in American odds (current − opening)."""
        return self.current.american_odds - self.opening.american_odds

    @property
    def abs_odds_change(self) -> int:
        return abs(self.odds_change)

    @property
    def line_change(self) -> Optional[float]:
        """Change in spread/total value (None for moneylines)."""
        if self.opening.line is not None and self.current.line is not None:
            return self.current.line - self.opening.line
        return None

    @property
    def abs_line_change(self) -> float:
        return abs(self.line_change) if self.line_change is not None else 0.0

    # ── Speed ────────────────────────────────────────────────────────────────

    @property
    def elapsed_minutes(self) -> float:
        """Total time from opening to current snapshot (minutes)."""
        delta = self.current.timestamp - self.opening.timestamp
        mins = delta.total_seconds() / 60
        return max(mins, 0.01)   # avoid divide-by-zero on same-timestamp events

    @property
    def recent_elapsed_minutes(self) -> float:
        """Time from previous snapshot to current (minutes). Falls back to total."""
        if self.previous is None:
            return self.elapsed_minutes
        delta = self.current.timestamp - self.previous.timestamp
        mins = delta.total_seconds() / 60
        return max(mins, 0.01)

    @property
    def movement_speed(self) -> float:
        """Absolute American odds change per minute (from opening)."""
        return round(self.abs_odds_change / self.elapsed_minutes, 4)

    @property
    def recent_speed(self) -> float:
        """Odds change per minute over the most recent interval only."""
        if self.previous is None:
            return self.movement_speed
        recent_change = abs(self.current.american_odds - self.previous.american_odds)
        return round(recent_change / self.recent_elapsed_minutes, 4)

    @property
    def movement_pct(self) -> float:
        """
        Odds change as a percentage of the opening implied probability shift.
        Normalises magnitude across favoured and underdog lines.
        """
        if self.opening.american_odds == 0:
            return 0.0
        open_decimal = 1 + (100 / abs(self.opening.american_odds)) \
            if self.opening.american_odds < 0 \
            else 1 + (self.opening.american_odds / 100)
        curr_decimal = 1 + (100 / abs(self.current.american_odds)) \
            if self.current.american_odds < 0 \
            else 1 + (self.current.american_odds / 100)
        return round(abs(curr_decimal - open_decimal) / open_decimal * 100, 4)


# ── Steam context (future hook for public % and CLV data) ─────────────────────

@dataclass
class SteamContext:
    """
    Optional market-level context that improves steam scoring accuracy.
    All fields are optional — the engine degrades gracefully when absent.
    Populate these from a public betting % API when available.
    """
    market: str = ""
    sport: str = ""
    market_type: str = ""
    selection: str = ""
    player: Optional[str] = None

    # Public betting percentages (0–100). None = not available.
    public_bet_pct: Optional[float] = None       # % of bets on this side
    public_money_pct: Optional[float] = None     # % of money on this side

    # If True, the line is moving AGAINST the public (classic sharp signal).
    # Computed automatically when public_bet_pct is provided.
    reverse_line_move: Optional[bool] = None

    # Timestamp when the opening line was first posted
    opening_timestamp: Optional[datetime] = None

    def compute_reverse_line_move(self, consensus_direction: MovementDirection) -> bool:
        """
        Determine whether the line is moving against public money.
        Returns False if public_money_pct is unavailable.
        """
        if self.public_money_pct is None:
            return False
        if consensus_direction == MovementDirection.DOWN:
            # Line shortening → sharp money; public typically on the fav
            return self.public_money_pct > 55
        if consensus_direction == MovementDirection.UP:
            # Line lengthening → sharp money on the other side
            return self.public_money_pct < 45
        return False


# ── Steam Score calculator ─────────────────────────────────────────────────────

class _SteamScorer:
    """
    Internal scorer. All component scores are documented with their
    exact formula so future contributors can adjust weights independently.
    """

    # ── Component: books moving (max 30 pts) ─────────────────────────────────
    @staticmethod
    def score_book_count(n_moving: int) -> int:
        """
        1 book  →  5 pts   (single-book move; could be error)
        2 books → 12 pts
        3 books → 19 pts
        4 books → 25 pts
        5+      → 30 pts
        """
        thresholds = [(5, 30), (4, 25), (3, 19), (2, 12), (1, 5)]
        for threshold, pts in thresholds:
            if n_moving >= threshold:
                return pts
        return 0

    # ── Component: sharp book weighting (max 25 pts) ──────────────────────────
    @staticmethod
    def score_sharp_books(movements: list[SteamMovement]) -> int:
        """
        Sum the sharp-book weights of all moving books, then normalise to 0–25.
        A single Pinnacle move (weight 1.0) alone scores ~13 pts.
        Five sharp books moving together maxes this component.
        """
        moving = [m for m in movements if m.is_moving]
        if not moving:
            return 0
        weight_sum = sum(m.opening.weight for m in moving)
        # Normalisation: 2.0 total weight → 25 pts (two top-tier sharp books)
        normalised = min(weight_sum / 2.0, 1.0)
        return int(round(normalised * 25))

    # ── Component: movement speed (max 20 pts) ────────────────────────────────
    @staticmethod
    def score_speed(movements: list[SteamMovement]) -> int:
        """
        Uses the maximum recent_speed across all moving books.
        ≥ 5 cents/min → 20 pts   (very fast — steam is live)
        ≥ 2 cents/min → 14 pts
        ≥ 1 cent/min  →  9 pts
        ≥ 0.5/min     →  5 pts
        anything      →  2 pts   (at least something moved)
        """
        moving = [m for m in movements if m.is_moving]
        if not moving:
            return 0
        max_speed = max(m.recent_speed for m in moving)
        if max_speed >= 5.0:
            return 20
        if max_speed >= 2.0:
            return 14
        if max_speed >= 1.0:
            return 9
        if max_speed >= 0.5:
            return 5
        return 2

    # ── Component: magnitude (max 15 pts) ────────────────────────────────────
    @staticmethod
    def score_magnitude(movements: list[SteamMovement]) -> int:
        """
        Uses the maximum absolute odds change across all moving books.
        Also accounts for line (spread/total) movement.
        Odds ≥ 20 OR line ≥ 2.0 → 15 pts
        Odds ≥ 15 OR line ≥ 1.5 → 12 pts
        Odds ≥ 10 OR line ≥ 1.0 → 8 pts
        Odds ≥  5 OR line ≥ 0.5 → 4 pts
        Odds >  0                → 1 pt
        """
        moving = [m for m in movements if m.is_moving]
        if not moving:
            return 0
        max_odds  = max(m.abs_odds_change  for m in moving)
        max_line  = max(m.abs_line_change  for m in moving)
        if max_odds >= 20 or max_line >= 2.0:
            return 15
        if max_odds >= 15 or max_line >= 1.5:
            return 12
        if max_odds >= 10 or max_line >= 1.0:
            return 8
        if max_odds >= 5  or max_line >= 0.5:
            return 4
        return 1

    # ── Component: consensus / book agreement (max 10 pts) ───────────────────
    @staticmethod
    def score_consensus(movements: list[SteamMovement]) -> tuple[int, MovementDirection]:
        """
        Measures directional agreement across all moving books.
        Returns (score, consensus_direction).

        100% agreement → 10 pts
        ≥ 80%          →  7 pts
        ≥ 60%          →  4 pts
        < 60%          →  0 pts  (conflicting signals; suppress)
        """
        moving = [m for m in movements if m.is_moving]
        if not moving:
            return 0, MovementDirection.FLAT

        up   = sum(1 for m in moving if m.direction == MovementDirection.UP)
        down = sum(1 for m in moving if m.direction == MovementDirection.DOWN)
        total = up + down

        if total == 0:
            return 0, MovementDirection.FLAT

        majority_dir = MovementDirection.UP if up >= down else MovementDirection.DOWN
        majority_n   = max(up, down)
        agreement    = majority_n / total

        if agreement >= 1.0:
            return 10, majority_dir
        if agreement >= 0.80:
            return 7, majority_dir
        if agreement >= 0.60:
            return 4, majority_dir
        return 0, majority_dir   # too conflicted; still return direction for reference

    # ── Bonus: reverse line movement (max +10 pts) ────────────────────────────
    @staticmethod
    def score_rlm(context: SteamContext, consensus_direction: MovementDirection) -> int:
        """
        If public betting % data is available and the line is moving AGAINST
        the public, this is a classic sharp money signal (+10 pts bonus).
        Returns 0 when public data is unavailable.
        """
        rlm = context.reverse_line_move
        if rlm is None:
            rlm = context.compute_reverse_line_move(consensus_direction)
        return 10 if rlm else 0

    # ── Master scorer ────────────────────────────────────────────────────────
    def compute(
        self,
        movements: list[SteamMovement],
        context: SteamContext,
    ) -> tuple[int, MovementDirection, dict[str, int]]:
        """
        Run all components and return (steam_score, consensus_direction, breakdown).
        Score is capped at 100.
        """
        n_moving  = sum(1 for m in movements if m.is_moving)
        s_books   = self.score_book_count(n_moving)
        s_sharp   = self.score_sharp_books(movements)
        s_speed   = self.score_speed(movements)
        s_mag     = self.score_magnitude(movements)
        s_cons, direction = self.score_consensus(movements)
        s_rlm     = self.score_rlm(context, direction)

        raw_score = s_books + s_sharp + s_speed + s_mag + s_cons + s_rlm
        final     = min(raw_score, 100)

        breakdown = {
            "books_moving":    s_books,
            "sharp_books":     s_sharp,
            "speed":           s_speed,
            "magnitude":       s_mag,
            "consensus":       s_cons,
            "reverse_line":    s_rlm,
            "total_raw":       raw_score,
            "capped_at_100":   final,
        }
        return final, direction, breakdown


_scorer = _SteamScorer()


# ── Structured result ──────────────────────────────────────────────────────────

@dataclass
class SteamResult:
    """
    Complete steam detection output for one market / selection.
    Produced by compute_steam() or compute_steam_simple().
    """
    # ── Identification ─────────────────────────────────────────────────────
    market: str                          # e.g. "Chiefs vs Raiders"
    selection: str                       # e.g. "Chiefs -3"
    sport: str                           # e.g. "NFL"
    market_type: str                     # e.g. "Spread"
    player: Optional[str]                # for player props

    # ── Line data ──────────────────────────────────────────────────────────
    opening_odds: int                    # American odds at open
    current_odds: int                    # American odds now
    opening_line: Optional[float]        # spread/total at open
    current_line: Optional[float]        # spread/total now

    # ── Books ──────────────────────────────────────────────────────────────
    books_triggered: list[str]           # all sportsbooks that moved
    sharp_books_triggered: list[str]     # only the sharp-tier books that moved
    n_books_moving: int

    # ── Movement ───────────────────────────────────────────────────────────
    movement_direction: MovementDirection
    movement_speed: float                # max cents/min across triggered books
    odds_change: int                     # signed: current − opening
    line_change: Optional[float]         # signed line change (spread/total)

    # ── Steam score ────────────────────────────────────────────────────────
    steam_score: int                     # 0–100
    score_breakdown: dict[str, int]      # component scores
    steam_tier: SteamTier
    confidence_level: ConfidenceLevel

    # ── Context ────────────────────────────────────────────────────────────
    reverse_line_move: bool              # moving against public betting %
    public_bet_pct: Optional[float]      # % of bets on this side (if available)
    public_money_pct: Optional[float]    # % of money on this side (if available)
    detected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ── Display helpers ────────────────────────────────────────────────────

    @property
    def odds_change_fmt(self) -> str:
        sign = "+" if self.odds_change > 0 else ""
        return f"{sign}{self.odds_change}"

    @property
    def opening_odds_fmt(self) -> str:
        return f"+{self.opening_odds}" if self.opening_odds > 0 else str(self.opening_odds)

    @property
    def current_odds_fmt(self) -> str:
        return f"+{self.current_odds}" if self.current_odds > 0 else str(self.current_odds)

    @property
    def sharp_lead(self) -> bool:
        return len(self.sharp_books_triggered) > 0

    def to_dict(self) -> dict:
        return {
            "market":                  self.market,
            "selection":               self.selection,
            "sport":                   self.sport,
            "market_type":             self.market_type,
            "player":                  self.player,
            "opening_odds":            self.opening_odds,
            "current_odds":            self.current_odds,
            "opening_line":            self.opening_line,
            "current_line":            self.current_line,
            "books_triggered":         self.books_triggered,
            "sharp_books_triggered":   self.sharp_books_triggered,
            "n_books_moving":          self.n_books_moving,
            "movement_direction":      self.movement_direction.value,
            "movement_speed":          self.movement_speed,
            "odds_change":             self.odds_change,
            "line_change":             self.line_change,
            "steam_score":             self.steam_score,
            "score_breakdown":         self.score_breakdown,
            "steam_tier":              self.steam_tier.value,
            "confidence_level":        self.confidence_level.value,
            "reverse_line_move":       self.reverse_line_move,
            "public_bet_pct":          self.public_bet_pct,
            "public_money_pct":        self.public_money_pct,
            "detected_at":             self.detected_at.isoformat(),
        }

    # ── Telegram HTML alert ────────────────────────────────────────────────

    def to_telegram(self) -> str:
        """HTML-formatted string for Telegram send_message(parse_mode=HTML)."""
        sharp_line = ""
        if self.sharp_books_triggered:
            sharp_line = f"\n<b>📍 Sharp Books:</b>    {', '.join(self.sharp_books_triggered)}"

        rlm_line = ""
        if self.reverse_line_move:
            pct = f" ({self.public_bet_pct:.0f}% public on other side)" \
                if self.public_bet_pct else ""
            rlm_line = f"\n<b>🔄 Reverse Line Move:</b> YES{pct}"

        player_line = f"\n<b>Player:</b>       {self.player}" if self.player else ""

        breakdown = "\n".join(
            f"  {k:<18} <code>{v:>3}</code>"
            for k, v in self.score_breakdown.items()
            if k not in ("total_raw", "capped_at_100")
        )

        return (
            f"{self.steam_tier.emoji} <b>{self.steam_tier.value.replace('_', ' ')}</b>\n"
            f"\n"
            f"<b>Sport:</b>        {self.sport}\n"
            f"<b>Market:</b>       {self.market_type}\n"
            f"<b>Event:</b>        {self.market}\n"
            f"{player_line}"
            f"<b>Selection:</b>    {self.selection}\n"
            f"\n"
            f"{self.movement_direction.emoji} <b>Line Movement</b>\n"
            f"  Opening:  <code>{self.opening_odds_fmt}</code>\n"
            f"  Current:  <code>{self.current_odds_fmt}</code>\n"
            f"  Change:   <code>{self.odds_change_fmt}</code>\n"
            f"\n"
            f"<b>📊 Books Moved:</b>    {self.n_books_moving}  "
            f"({', '.join(self.books_triggered)})\n"
            f"{sharp_line}"
            f"<b>⚡ Speed:</b>        <code>{self.movement_speed:.2f} pts/min</code>\n"
            f"{rlm_line}\n"
            f"\n"
            f"<b>Steam Score:</b>   <code>{self.steam_score}/100</code>\n"
            f"<b>Tier:</b>          {self.steam_tier.emoji} {self.steam_tier.value}\n"
            f"<b>Confidence:</b>    {self.confidence_level.emoji} {self.confidence_level.value}\n"
            f"\n"
            f"<b>Score Breakdown:</b>\n"
            f"{breakdown}\n"
            f"\n"
            f"🕐 <i>{self.detected_at.strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )

    def to_console(self) -> str:
        return (
            f"[Steam] {self.steam_tier.value:<18} score={self.steam_score:>3}/100  "
            f"confidence={self.confidence_level.value:<10}  "
            f"{self.market} | {self.selection}  "
            f"move={self.opening_odds_fmt}→{self.current_odds_fmt} ({self.odds_change_fmt})  "
            f"books={self.n_books_moving}  speed={self.movement_speed:.2f}/min  "
            f"rlm={'YES' if self.reverse_line_move else 'no'}"
        )


# ── Public factory functions ───────────────────────────────────────────────────

def compute_steam(
    movements: list[SteamMovement],
    context: SteamContext,
) -> SteamResult:
    """
    Full steam analysis from a list of SteamMovement objects.

    Parameters
    ----------
    movements   One SteamMovement per sportsbook that has an opening and
                current snapshot. Books with no movement are included for
                consensus scoring but contribute 0 pts to other components.
    context     Market-level metadata (sport, market, public %, etc.).

    Returns
    -------
    SteamResult
    """
    if not movements:
        raise ValueError("movements list cannot be empty.")

    score, direction, breakdown = _scorer.compute(movements, context)

    moving   = [m for m in movements if m.is_moving]
    n_moving = len(moving)

    # Aggregate book lists
    books_triggered = sorted({m.opening.sportsbook for m in moving})
    sharp_triggered = sorted({
        m.opening.sportsbook for m in moving if m.opening.is_sharp_book
    })

    # Representative opening / current odds (use sharpest book if available)
    def _pick_representative(mvmts: list[SteamMovement], attr: str) -> int:
        sharp = [m for m in mvmts if m.opening.is_sharp_book]
        source = sharp if sharp else mvmts
        return getattr(source[0].opening if attr == "opening" else source[0].current,
                       "american_odds")

    opening_odds = _pick_representative(movements, "opening")
    current_odds = _pick_representative(movements, "current")

    opening_line: Optional[float] = None
    current_line: Optional[float] = None
    if movements[0].opening.line is not None:
        opening_line = movements[0].opening.line
        current_line = movements[0].current.line

    # Line change (consensus: use the largest absolute change)
    line_changes = [m.line_change for m in moving if m.line_change is not None]
    agg_line_change: Optional[float] = None
    if line_changes:
        agg_line_change = max(line_changes, key=abs)

    # Movement speed (max across all moving books)
    max_speed = max((m.recent_speed for m in moving), default=0.0)

    # Consensus odds change
    odds_change = current_odds - opening_odds

    # Reverse line move
    rlm = context.reverse_line_move
    if rlm is None:
        rlm = context.compute_reverse_line_move(direction)

    tier       = SteamTier.from_score(score)
    confidence = ConfidenceLevel.from_score(score)

    return SteamResult(
        market=context.market,
        selection=context.selection,
        sport=context.sport,
        market_type=context.market_type,
        player=context.player,
        opening_odds=opening_odds,
        current_odds=current_odds,
        opening_line=opening_line,
        current_line=current_line,
        books_triggered=books_triggered,
        sharp_books_triggered=sharp_triggered,
        n_books_moving=n_moving,
        movement_direction=direction,
        movement_speed=round(max_speed, 4),
        odds_change=odds_change,
        line_change=agg_line_change,
        steam_score=score,
        score_breakdown=breakdown,
        steam_tier=tier,
        confidence_level=confidence,
        reverse_line_move=bool(rlm),
        public_bet_pct=context.public_bet_pct,
        public_money_pct=context.public_money_pct,
    )


def compute_steam_simple(
    market: str,
    sport: str,
    market_type: str,
    selection: str,
    book_snapshots: Sequence[dict],
    elapsed_minutes: float = 30.0,
    player: Optional[str] = None,
    public_bet_pct: Optional[float] = None,
    public_money_pct: Optional[float] = None,
    reverse_line_move: Optional[bool] = None,
) -> SteamResult:
    """
    Simplified helper for two-snapshot (open → current) comparisons.

    Each entry in ``book_snapshots`` must be a dict with:
        sportsbook    str    — book name (used for weight lookup)
        open_odds     int    — American odds at opening
        current_odds  int    — American odds now
        open_line     float  — (optional) spread/total at open
        current_line  float  — (optional) spread/total now

    ``elapsed_minutes`` is applied uniformly to all snapshots.

    Example
    -------
        result = compute_steam_simple(
            market="Chiefs vs Raiders",
            sport="NFL",
            market_type="Spread",
            selection="Chiefs -3",
            elapsed_minutes=20,
            book_snapshots=[
                {"sportsbook": "Pinnacle",   "open_odds": -110, "current_odds": -118},
                {"sportsbook": "DraftKings", "open_odds": -110, "current_odds": -116},
            ],
        )
    """
    from datetime import timedelta

    t_open = datetime.now(timezone.utc) - timedelta(minutes=elapsed_minutes)
    t_now  = datetime.now(timezone.utc)

    movements: list[SteamMovement] = []
    for snap in book_snapshots:
        open_evt = LineMovementEvent(
            sportsbook=snap["sportsbook"],
            american_odds=snap["open_odds"],
            timestamp=t_open,
            line=snap.get("open_line"),
        )
        curr_evt = LineMovementEvent(
            sportsbook=snap["sportsbook"],
            american_odds=snap["current_odds"],
            timestamp=t_now,
            line=snap.get("current_line"),
        )
        movements.append(SteamMovement(opening=open_evt, current=curr_evt))

    context = SteamContext(
        market=market,
        sport=sport,
        market_type=market_type,
        selection=selection,
        player=player,
        public_bet_pct=public_bet_pct,
        public_money_pct=public_money_pct,
        reverse_line_move=reverse_line_move,
    )
    return compute_steam(movements=movements, context=context)
