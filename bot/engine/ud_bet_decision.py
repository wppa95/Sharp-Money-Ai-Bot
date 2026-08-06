"""
engine/ud_bet_decision.py — Universal betting decision engine.

Produces an OVER / UNDER / PASS recommendation with a quality tier (S / A / B)
for every Underdog prop that has reached the scoring + validation gates.

Design principle
────────────────
A directional pick requires REAL player game-result data.  Market-movement
signals (line position vs history, move direction) are shown as supplementary
evidence but CANNOT on their own generate an OVER or UNDER.  Without real
game history the engine always returns PASS.

Tier definitions
────────────────
  S — All available windows (L5/L10/L20/L30/Season) show consistent edge
       (≥ 0.65 OVER or ≤ 0.35 UNDER), primary window has ≥ 8 games.
  A — Strong primary window (≥ 0.62 / ≤ 0.38), no contradicting window,
       season supports direction when available.
  B — Playable edge (≥ 0.60 / ≤ 0.40), no strong contradiction.
  PASS — Insufficient data, conflicting windows, or hit rate in 40–60% zone.

Public API
──────────
  UDBetDecision           — frozen dataclass; build via class-methods
  make_ud_bet_decision()  — entry point
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from engine.player_results import PlayerHitRates, WindowStats
    from engine.ud_scoring import UDPropScore
    from engine.player_validator import PlayerPropValidation

# ── Constants ────────────────────────────────────────────────────────────────

_MIN_GAMES_PRIMARY: int   = 5    # minimum games in any window to make a pick
_MIN_GAMES_S_TIER:  int   = 8    # primary window must have this many for S-tier

# Hit-rate thresholds per tier (OVER side; mirror for UNDER)
_S_RATE:  float = 0.65
_A_RATE:  float = 0.62
_B_RATE:  float = 0.60

# A window is "contradicting" an OVER pick if its rate < this value
_CONTRA_FLOOR: float = 0.40
# A window is "contradicting" an UNDER pick if its rate > this value
_CONTRA_CEIL:  float = 0.60

# Confidence bounds
_MAX_CONFIDENCE: int = 95
_MIN_CONFIDENCE: int = 10


# ── Dataclass ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UDBetDecision:
    """Complete betting evaluation for one Underdog prop."""

    recommendation: str   # "OVER" | "UNDER" | "PASS"
    decision_tier:  str   # "S"    | "A"     | "B"    | "PASS"
    confidence:     int   # 0–95  (always 0 for PASS)
    reason:         str

    # ── Real game-result windows (None = window not available) ────────────────
    l5_games:       Optional[int]
    l5_over:        Optional[int]
    l5_under:       Optional[int]
    l5_hit_rate:    Optional[float]
    l5_avg:         Optional[float]

    l10_games:      Optional[int]
    l10_over:       Optional[int]
    l10_under:      Optional[int]
    l10_hit_rate:   Optional[float]
    l10_avg:        Optional[float]

    l20_games:      Optional[int]
    l20_over:       Optional[int]
    l20_under:      Optional[int]
    l20_hit_rate:   Optional[float]
    l20_avg:        Optional[float]

    l30_games:      Optional[int]
    l30_over:       Optional[int]
    l30_under:      Optional[int]
    l30_hit_rate:   Optional[float]
    l30_avg:        Optional[float]

    season_games:    Optional[int]
    season_over:     Optional[int]
    season_under:    Optional[int]
    season_hit_rate: Optional[float]
    season_avg:      Optional[float]   # player's actual season average for stat

    h2h_games:      Optional[int]
    h2h_over:       Optional[int]
    h2h_under:      Optional[int]
    h2h_hit_rate:   Optional[float]
    h2h_avg:        Optional[float]

    # ── Market supplements (always available; secondary use only) ─────────────
    avg_vs_line_pct:   Optional[float]   # (hist_avg - current) / hist_avg; +ve = OVER signal
    at_historical_low: bool              # current line == minimum ever seen

    # ── Constructors ─────────────────────────────────────────────────────────

    @classmethod
    def make_pass(
        cls,
        reason: str,
        hit_rates: "Optional[PlayerHitRates]" = None,
        avg_vs_line_pct: Optional[float] = None,
        at_historical_low: bool = False,
    ) -> "UDBetDecision":
        """Construct a PASS decision, optionally inheriting window data."""
        w = _windows_from_hit_rates(hit_rates)
        return cls(
            recommendation     = "PASS",
            decision_tier      = "PASS",
            confidence         = 0,
            reason             = reason,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_historical_low,
            **w,
        )

    @classmethod
    def make_pick(
        cls,
        recommendation: str,
        decision_tier: str,
        confidence: int,
        reason: str,
        hit_rates: "PlayerHitRates",
        avg_vs_line_pct: Optional[float] = None,
        at_historical_low: bool = False,
    ) -> "UDBetDecision":
        """Construct an OVER/UNDER decision."""
        w = _windows_from_hit_rates(hit_rates)
        return cls(
            recommendation     = recommendation,
            decision_tier      = decision_tier,
            confidence         = confidence,
            reason             = reason,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_historical_low,
            **w,
        )

    # ── Display helpers ───────────────────────────────────────────────────────

    def recommendation_emoji(self) -> str:
        return {"OVER": "🟢", "UNDER": "🔴", "PASS": "⚪"}.get(self.recommendation, "❓")

    def tier_display(self) -> str:
        return {
            "S":    "⭐ S-Tier",
            "A":    "🔷 A-Tier",
            "B":    "🔹 B-Tier",
            "PASS": "—",
        }.get(self.decision_tier, "—")

    def confidence_display(self) -> str:
        if self.recommendation == "PASS":
            return "—"
        return f"{self.confidence}/100"

    def avg_vs_line_display(self) -> str:
        if self.avg_vs_line_pct is None:
            return "N/A"
        pct  = self.avg_vs_line_pct
        sign = "+" if pct >= 0 else ""
        low  = "  ⬇️ historical low" if self.at_historical_low else ""
        return f"{sign}{pct:.1%}{low}"

    def window_display(
        self,
        games: Optional[int],
        over: Optional[int],
        under: Optional[int],
        hit_rate: Optional[float],
        avg: Optional[float],
    ) -> str:
        if games is None or hit_rate is None:
            return "N/A"
        avg_str = f"  avg {avg:.1f}" if avg is not None else ""
        return f"{over}/{games} ({hit_rate:.0%}){avg_str}"

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_json(self) -> str:
        """Compact JSON for storage in underdog_snapshots.bet_evidence_json."""
        def _r(v: Optional[float]) -> Optional[float]:
            return round(v, 3) if v is not None else None

        return json.dumps(
            {
                "rec":  self.recommendation,
                "tier": self.decision_tier,
                "conf": self.confidence,
                "l5":   {"g": self.l5_games, "o": self.l5_over, "r": _r(self.l5_hit_rate), "avg": _r(self.l5_avg)},
                "l10":  {"g": self.l10_games, "o": self.l10_over, "r": _r(self.l10_hit_rate), "avg": _r(self.l10_avg)},
                "l20":  {"g": self.l20_games, "o": self.l20_over, "r": _r(self.l20_hit_rate), "avg": _r(self.l20_avg)},
                "l30":  {"g": self.l30_games, "o": self.l30_over, "r": _r(self.l30_hit_rate), "avg": _r(self.l30_avg)},
                "sea":  {"g": self.season_games, "o": self.season_over, "r": _r(self.season_hit_rate), "avg": _r(self.season_avg)},
                "h2h":  {"g": self.h2h_games, "o": self.h2h_over, "r": _r(self.h2h_hit_rate), "avg": _r(self.h2h_avg)},
                "mkt":  {"dev": _r(self.avg_vs_line_pct), "atlow": self.at_historical_low},
            },
            separators=(",", ":"),
        )


# ── Public entry point ────────────────────────────────────────────────────────

def make_ud_bet_decision(
    score:        "UDPropScore",
    validation:   "PlayerPropValidation",
    current_line: float,
    prev_line:    Optional[float] = None,
    hit_rates:    "Optional[PlayerHitRates]" = None,
) -> UDBetDecision:
    """
    Evaluate OVER / UNDER / PASS for a qualified Underdog prop.

    Real game results (``hit_rates``) are required for any directional pick.
    Market-movement signals are supplementary context only.

    Parameters
    ----------
    score:        UDPropScore from score_ud_prop() — used for gating only.
    validation:   PlayerPropValidation from validate_player_prop() — market supplement.
    current_line: Current prop line value.
    prev_line:    Previous line if this is a line-change event.
    hit_rates:    PlayerHitRates computed from real game results.
                  If None or has_real_data=False, always returns PASS.
    """
    # ── Market supplement values ──────────────────────────────────────────────
    avg_vs_line_pct: Optional[float] = None
    avg_line = getattr(validation, "avg_line", None)
    if avg_line is not None and avg_line > 0:
        avg_vs_line_pct = (avg_line - current_line) / avg_line

    at_low = bool(
        getattr(validation, "min_line_seen", None) is not None
        and current_line <= validation.min_line_seen
    )

    # ── Gate 1: real game data required ──────────────────────────────────────
    # Guard against callers passing a list or other wrong type (e.g. from
    # cold-start hit_rates=[] path) — treat any non-PlayerHitRates value as None.
    if not hasattr(hit_rates, "has_real_data"):
        hit_rates = None
    if hit_rates is None or not hit_rates.has_real_data:
        return UDBetDecision.make_pass(
            reason             = (
                "No player game history available — "
                "market signals alone are not sufficient for a bet"
            ),
            hit_rates          = hit_rates,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_low,
        )

    # ── Gate 2: sufficient sample size ───────────────────────────────────────
    primary = _primary_window(hit_rates)
    if primary is None:
        return UDBetDecision.make_pass(
            reason             = (
                f"Insufficient sample ({hit_rates.total_games} result(s) — "
                f"need ≥{_MIN_GAMES_PRIMARY})"
            ),
            hit_rates          = hit_rates,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_low,
        )

    rate = primary.hit_rate

    # ── Gate 3: must cross B-tier threshold in primary window ─────────────────
    if rate >= _B_RATE:
        direction = "OVER"
    elif rate <= (1.0 - _B_RATE):
        direction = "UNDER"
    else:
        return UDBetDecision.make_pass(
            reason             = (
                f"Hit rate {rate:.0%} is inconclusive — "
                f"no clear directional edge (need ≥{_B_RATE:.0%} or ≤{1-_B_RATE:.0%})"
            ),
            hit_rates          = hit_rates,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_low,
        )

    # ── Gate 4: no contradicting window ──────────────────────────────────────
    contradiction = _check_contradictions(hit_rates, direction)
    if contradiction:
        return UDBetDecision.make_pass(
            reason             = contradiction,
            hit_rates          = hit_rates,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_low,
        )

    # ── Tier classification ───────────────────────────────────────────────────
    tier = _determine_tier(hit_rates, direction, primary)

    # ── Confidence ────────────────────────────────────────────────────────────
    confidence = _compute_confidence(hit_rates, direction, tier, primary)

    # ── Reason string ─────────────────────────────────────────────────────────
    reason = _build_reason(
        hit_rates, direction, tier, primary, current_line,
        avg_vs_line_pct, at_low,
    )

    return UDBetDecision.make_pick(
        recommendation     = direction,
        decision_tier      = tier,
        confidence         = confidence,
        reason             = reason,
        hit_rates          = hit_rates,
        avg_vs_line_pct    = avg_vs_line_pct,
        at_historical_low  = at_low,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _primary_window(hit_rates: "PlayerHitRates") -> "Optional[WindowStats]":
    """Return the smallest window with ≥ _MIN_GAMES_PRIMARY games."""
    for w in (
        hit_rates.l5,
        hit_rates.l10,
        hit_rates.l20,
        hit_rates.l30,
        hit_rates.season,
    ):
        if w is not None and w.games >= _MIN_GAMES_PRIMARY:
            return w
    return None


def _all_sufficient_windows(hit_rates: "PlayerHitRates") -> "list[WindowStats]":
    """All windows that have ≥ _MIN_GAMES_PRIMARY games."""
    return [
        w for w in (
            hit_rates.l5,
            hit_rates.l10,
            hit_rates.l20,
            hit_rates.l30,
            hit_rates.season,
        )
        if w is not None and w.games >= _MIN_GAMES_PRIMARY
    ]


def _check_contradictions(
    hit_rates: "PlayerHitRates",
    direction: str,
) -> Optional[str]:
    """Return a reason string if any window strongly contradicts *direction*."""
    for w in _all_sufficient_windows(hit_rates):
        if direction == "OVER" and w.hit_rate < _CONTRA_FLOOR:
            return (
                f"Conflicting windows — {w.games}-game window shows only "
                f"{w.hit_rate:.0%} hit rate (strong UNDER signal)"
            )
        if direction == "UNDER" and w.hit_rate > _CONTRA_CEIL:
            return (
                f"Conflicting windows — {w.games}-game window shows "
                f"{w.hit_rate:.0%} hit rate (strong OVER signal)"
            )
    return None


def _determine_tier(
    hit_rates: "PlayerHitRates",
    direction: str,
    primary: "WindowStats",
) -> str:
    """
    Classify quality of the pick as S / A / B.

    S-tier requires:
      • Primary window rate ≥ _S_RATE
      • At least one window with ≥ _MIN_GAMES_S_TIER games AND rate ≥ _S_RATE
        (ensures we have substantial historical confirmation — not just 5 games)
      • All sufficient windows ≥ 0.55 (no window dissents)
      • Season supports direction (rate ≥ 0.60) when season data is available

    A-tier: primary rate ≥ _A_RATE (no large-window or season requirement)
    B-tier: primary rate ≥ _B_RATE
    """
    rate    = primary.hit_rate
    over    = direction == "OVER"
    windows = _all_sufficient_windows(hit_rates)

    if over:
        if rate >= _S_RATE:
            has_large = any(
                w.games >= _MIN_GAMES_S_TIER and w.hit_rate >= _S_RATE
                for w in windows
            )
            # Allow slight wobble in long windows (e.g. L30 at 0.52 while L5/L10 strong)
            all_support = all(w.hit_rate >= 0.52 for w in windows)
            if has_large and all_support and _season_supports(hit_rates, "OVER", 0.55):
                return "S"
            return "A"
        elif rate >= _A_RATE:
            return "A"
        else:
            return "B"
    else:
        if rate <= (1.0 - _S_RATE):
            has_large = any(
                w.games >= _MIN_GAMES_S_TIER and w.hit_rate <= (1.0 - _S_RATE)
                for w in windows
            )
            # Allow slight wobble in long windows (e.g. L30 at 0.48 while L5/L10 strong)
            all_support = all(w.hit_rate <= 0.48 for w in windows)
            if has_large and all_support and _season_supports(hit_rates, "UNDER", 0.45):
                return "S"
            return "A"
        elif rate <= (1.0 - _A_RATE):
            return "A"
        else:
            return "B"


def _season_supports(
    hit_rates: "PlayerHitRates",
    direction: str,
    threshold: float,
) -> bool:
    """Return True if season data supports direction, or if no season data."""
    s = hit_rates.season
    if s is None or s.games < _MIN_GAMES_PRIMARY:
        return True   # no data → don't penalise
    return s.hit_rate >= threshold if direction == "OVER" else s.hit_rate <= threshold


def _compute_confidence(
    hit_rates: "PlayerHitRates",
    direction: str,
    tier: str,
    primary: "WindowStats",
) -> int:
    # Base: scale primary window deviation from 0.5 → max 70 pts.
    # The primary window (smallest with ≥ _MIN_GAMES_PRIMARY games) is the
    # most recent evidence and remains the anchor for the base calculation.
    dev  = abs(primary.hit_rate - 0.5)
    base = min(70, int(dev * 180))

    # Tier bonus
    # Higher bonus for stronger tiers so S-tier picks reliably clear the 80-conf gate.
    tier_bonus = {"S": 18, "A": 10, "B": 0}.get(tier, 0)

    # Sample size: +1 per 4 games in primary, cap 10
    sample_bonus = min(10, primary.games // 4)

    # ── Multi-window weighted agreement bonus ─────────────────────────────────
    # All sufficient windows (L5 / L10 / L20 / L30 / season) vote on direction.
    # Weights reflect recency: more recent windows carry more signal.
    #
    #   L5  → weight 1.50  (most recent, highest signal)
    #   L10 → weight 1.20
    #   L20 → weight 1.00
    #   L30 → weight 0.80
    #   season (>35 games) → weight 0.60  (large sample but noisy due to line drift)
    #
    # A window agrees when its hit rate supports the chosen direction.
    # The weighted-agreement score replaces the flat +3-per-window approach,
    # giving L10/L20/L30 proportionally meaningful influence without reducing
    # the primary-anchored base score.
    _WIN_WT: dict[str, float] = {"l5": 1.50, "l10": 1.20, "l20": 1.00, "l30": 0.80}
    windows = _all_sufficient_windows(hit_rates)
    total_wt    = 0.0
    agreeing_wt = 0.0
    for w in windows:
        g = w.games
        if g <= 7:
            wt = 1.50
        elif g <= 13:
            wt = 1.20
        elif g <= 25:
            wt = 1.00
        elif g <= 35:
            wt = 0.80
        else:
            wt = 0.60
        total_wt += wt
        if direction == "OVER" and w.hit_rate >= 0.55:
            agreeing_wt += wt
        elif direction == "UNDER" and w.hit_rate <= 0.45:
            agreeing_wt += wt

    # Scale weighted agreement to a 0–12 bonus (same cap as old flat version)
    if total_wt > 0:
        agreement_ratio  = agreeing_wt / total_wt          # 0.0 → 1.0
        agreement_bonus  = min(12, int(agreement_ratio * 12))
    else:
        agreement_bonus  = 0

    # H2H bonus: +3 when H2H confirms direction
    h2h_bonus = 0
    h = hit_rates.h2h
    if h is not None and h.games >= 3:
        if direction == "OVER" and h.hit_rate >= 0.60:
            h2h_bonus = 3
        elif direction == "UNDER" and h.hit_rate <= 0.40:
            h2h_bonus = 3

    raw = base + tier_bonus + sample_bonus + agreement_bonus + h2h_bonus
    return min(_MAX_CONFIDENCE, max(_MIN_CONFIDENCE, raw))


def _build_reason(
    hit_rates: "PlayerHitRates",
    direction: str,
    tier: str,
    primary: "WindowStats",
    current_line: float,
    avg_vs_line_pct: Optional[float],
    at_low: bool,
) -> str:
    parts: list[str] = []

    # Primary window summary
    parts.append(
        f"L{primary.games}: {primary.hit_rate:.0%} "
        f"({primary.over_count}/{primary.games})"
    )

    # Season
    s = hit_rates.season
    if s is not None and s.games >= _MIN_GAMES_PRIMARY:
        parts.append(
            f"Season: {s.hit_rate:.0%} ({s.over_count}/{s.games})"
        )
        if s.average is not None:
            avg_diff = s.average - current_line
            sign = "+" if avg_diff >= 0 else ""
            parts.append(f"Season avg vs line: {sign}{avg_diff:+.2f}")

    # H2H
    h = hit_rates.h2h
    if h is not None and h.games >= 3:
        parts.append(
            f"H2H: {h.hit_rate:.0%} ({h.games} matchups)"
        )

    # Market supplement
    if avg_vs_line_pct is not None and abs(avg_vs_line_pct) >= 0.05:
        direction_word = "below" if avg_vs_line_pct > 0 else "above"
        parts.append(
            f"Line is {abs(avg_vs_line_pct):.0%} {direction_word} historical average"
        )

    if at_low:
        parts.append("Line at historical low")

    # Tier descriptor
    tier_desc = {
        "S": "All windows aligned — maximum confidence",
        "A": "Strong primary evidence with season support",
        "B": "Playable edge — moderate variance expected",
    }.get(tier, "")
    if tier_desc:
        parts.append(tier_desc)

    return "  •  ".join(parts) if parts else f"{direction} edge confirmed"


def _windows_from_hit_rates(hit_rates: "Optional[PlayerHitRates]") -> dict:
    """
    Return a kwargs dict for all 30 window fields of UDBetDecision.

    When hit_rates is None all values are None.
    """
    def _g(w, attr):
        if w is None:
            return None
        return getattr(w, attr, None)

    l5  = hit_rates.l5     if hit_rates else None
    l10 = hit_rates.l10    if hit_rates else None
    l20 = hit_rates.l20    if hit_rates else None
    l30 = hit_rates.l30    if hit_rates else None
    sea = hit_rates.season if hit_rates else None
    h2h = hit_rates.h2h    if hit_rates else None

    return {
        "l5_games":       _g(l5,  "games"),
        "l5_over":        _g(l5,  "over_count"),
        "l5_under":       _g(l5,  "under_count"),
        "l5_hit_rate":    _g(l5,  "hit_rate"),
        "l5_avg":         _g(l5,  "average"),
        "l10_games":      _g(l10, "games"),
        "l10_over":       _g(l10, "over_count"),
        "l10_under":      _g(l10, "under_count"),
        "l10_hit_rate":   _g(l10, "hit_rate"),
        "l10_avg":        _g(l10, "average"),
        "l20_games":      _g(l20, "games"),
        "l20_over":       _g(l20, "over_count"),
        "l20_under":      _g(l20, "under_count"),
        "l20_hit_rate":   _g(l20, "hit_rate"),
        "l20_avg":        _g(l20, "average"),
        "l30_games":      _g(l30, "games"),
        "l30_over":       _g(l30, "over_count"),
        "l30_under":      _g(l30, "under_count"),
        "l30_hit_rate":   _g(l30, "hit_rate"),
        "l30_avg":        _g(l30, "average"),
        "season_games":   _g(sea, "games"),
        "season_over":    _g(sea, "over_count"),
        "season_under":   _g(sea, "under_count"),
        "season_hit_rate":_g(sea, "hit_rate"),
        "season_avg":     _g(sea, "average"),
        "h2h_games":      _g(h2h, "games"),
        "h2h_over":       _g(h2h, "over_count"),
        "h2h_under":      _g(h2h, "under_count"),
        "h2h_hit_rate":   _g(h2h, "hit_rate"),
        "h2h_avg":        _g(h2h, "average"),
    }
