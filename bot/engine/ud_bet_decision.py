"""
engine/ud_bet_decision.py — Universal betting decision engine.

Produces an OVER / UNDER recommendation, or an internal no-direction result,
for every Underdog prop that has reached the scoring + validation gates.

Design principle
────────────────
A directional pick requires supporting evidence/history, but a completed
player-game hit-rate window is not itself a delivery gate.  When settled
hit-rate data is available it remains the preferred directional signal.  When
it is unavailable or inconclusive, the stored historical line average provides
the fallback direction: a current line below that average suggests OVER and a
current line above it suggests UNDER.

Tier definitions
────────────────
The decision object has no direction when the evidence gates are not met.
Tier labels remain optional reporting metadata for directional evaluations; they
are not qualification or delivery gates.

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

# Strong directional labels require every requested result window to agree.
_STRONG_OVER_RATE: float = 0.70
_STRONG_UNDER_RATE: float = 0.30

# Confidence bounds
_MAX_CONFIDENCE: int = 95
_MIN_CONFIDENCE: int = 10

EVIDENCE_SETTLED_RESULTS = "settled_results"
EVIDENCE_HISTORICAL_AVERAGE = "historical_average"


def evidence_source_label(source: Optional[str]) -> str:
    """Return the honest user-facing label for a decision's evidence basis."""
    return {
        EVIDENCE_SETTLED_RESULTS: "Settled player-game results",
        EVIDENCE_HISTORICAL_AVERAGE: "Historical-average fallback",
    }.get(str(source or "").strip().lower(), "Unavailable")


# ── Dataclass ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UDBetDecision:
    """Complete betting evaluation for one Underdog prop."""

    recommendation: Optional[str]   # "OVER" | "UNDER" | None
    # Deprecated compatibility fields.  Directional qualification no longer
    # produces or consumes tiers or bet-quality confidence.
    decision_tier:  Optional[str]
    confidence:     Optional[int]
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

    l15_games:      Optional[int]
    l15_over:       Optional[int]
    l15_under:      Optional[int]
    l15_hit_rate:   Optional[float]
    l15_avg:        Optional[float]

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

    h2h_l5_games:      Optional[int]
    h2h_l5_over:       Optional[int]
    h2h_l5_under:      Optional[int]
    h2h_l5_hit_rate:   Optional[float]
    h2h_l5_avg:        Optional[float]

    h2h_l10_games:      Optional[int]
    h2h_l10_over:       Optional[int]
    h2h_l10_under:      Optional[int]
    h2h_l10_hit_rate:   Optional[float]
    h2h_l10_avg:        Optional[float]

    h2h_l20_games:      Optional[int]
    h2h_l20_over:       Optional[int]
    h2h_l20_under:      Optional[int]
    h2h_l20_hit_rate:   Optional[float]
    h2h_l20_avg:        Optional[float]

    # ── Market supplements (always available; secondary use only) ─────────────
    avg_vs_line_pct:   Optional[float]   # (hist_avg - current) / hist_avg; +ve = OVER signal
    at_historical_low: bool              # current line == minimum ever seen
    # What supplied the direction. A pick can retain settled windows while
    # using the validated historical-average fallback.
    evidence_source: Optional[str] = None

    # ── Constructors ─────────────────────────────────────────────────────────

    @classmethod
    def make_no_direction(
        cls,
        reason: str,
        hit_rates: "Optional[PlayerHitRates]" = None,
        avg_vs_line_pct: Optional[float] = None,
        at_historical_low: bool = False,
    ) -> "UDBetDecision":
        """Construct a monitored evaluation with no directional result."""
        w = _windows_from_hit_rates(hit_rates)
        return cls(
            recommendation     = None,
            decision_tier      = None,
            confidence         = 0,
            reason             = reason,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_historical_low,
            evidence_source    = None,
            **w,
        )

    @classmethod
    def make_pass(
        cls,
        reason: str,
        hit_rates: "Optional[PlayerHitRates]" = None,
        avg_vs_line_pct: Optional[float] = None,
        at_historical_low: bool = False,
    ) -> "UDBetDecision":
        """Compatibility constructor for a monitored, non-directional result."""
        return cls.make_no_direction(
            reason=reason,
            hit_rates=hit_rates,
            avg_vs_line_pct=avg_vs_line_pct,
            at_historical_low=at_historical_low,
        )

    @classmethod
    def make_pick(
        cls,
        recommendation: str,
        reason: str,
        hit_rates: "PlayerHitRates | int | float | None",
        avg_vs_line_pct: Optional[float] = None,
        at_historical_low: bool = False,
        evidence_source: Optional[str] = None,
        # Compatibility-only arguments for older callers.  They are ignored.
        decision_tier: Optional[str] = None,
        confidence: Optional[int] = None,
    ) -> "UDBetDecision":
        """Construct an OVER/UNDER decision."""
        # Older callers used:
        #   make_pick(recommendation, tier, confidence, reason, hit_rates)
        # Keep that positional contract working while production callers use
        # the named, evidence-first arguments above.
        if (
            isinstance(hit_rates, (int, float))
            and not isinstance(hit_rates, bool)
            and isinstance(avg_vs_line_pct, str)
            and not isinstance(at_historical_low, bool)
        ):
            legacy_tier = reason
            legacy_confidence = int(round(hit_rates))
            legacy_reason = avg_vs_line_pct
            hit_rates = at_historical_low
            reason = legacy_reason
            avg_vs_line_pct = None
            at_historical_low = False
            decision_tier = legacy_tier
            confidence = legacy_confidence
        w = _windows_from_hit_rates(hit_rates)
        return cls(
            recommendation     = recommendation,
            decision_tier      = decision_tier,
            confidence         = confidence,
            reason             = reason,
            avg_vs_line_pct    = avg_vs_line_pct,
            at_historical_low  = at_historical_low,
            evidence_source    = (
                evidence_source
                or (
                    EVIDENCE_SETTLED_RESULTS
                    if getattr(hit_rates, "has_real_data", False)
                    else None
                )
            ),
            **w,
        )

    # ── Display helpers ───────────────────────────────────────────────────────

    def recommendation_emoji(self) -> str:
        return {"OVER": "🟢", "UNDER": "🔴"}.get(self.recommendation, "⚪")

    def confidence_display(self) -> str:
        if self.confidence is None or self.confidence <= 0:
            return "—"
        return f"{self.confidence}/100"

    def tier_display(self) -> str:
        return self.decision_tier or "—"

    def avg_vs_line_display(self) -> str:
        if self.avg_vs_line_pct is None:
            return "N/A"
        pct  = self.avg_vs_line_pct
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1%}"

    def evidence_source_display(self) -> str:
        """Return the evidence basis in plain language for reports/alerts."""
        return evidence_source_label(self.evidence_source)

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
                "conf": self.confidence or 0,
                "evidence_source": self.evidence_source,
                "evidence_label": self.evidence_source_display(),
                "l5":   {"g": self.l5_games, "o": self.l5_over, "r": _r(self.l5_hit_rate), "avg": _r(self.l5_avg)},
                "l10":  {"g": self.l10_games, "o": self.l10_over, "r": _r(self.l10_hit_rate), "avg": _r(self.l10_avg)},
                "l15":  {"g": self.l15_games, "o": self.l15_over, "r": _r(self.l15_hit_rate), "avg": _r(self.l15_avg)},
                "l20":  {"g": self.l20_games, "o": self.l20_over, "r": _r(self.l20_hit_rate), "avg": _r(self.l20_avg)},
                "l30":  {"g": self.l30_games, "o": self.l30_over, "r": _r(self.l30_hit_rate), "avg": _r(self.l30_avg)},
                "sea":  {"g": self.season_games, "o": self.season_over, "r": _r(self.season_hit_rate), "avg": _r(self.season_avg)},
                "h2h":  {"g": self.h2h_games, "o": self.h2h_over, "r": _r(self.h2h_hit_rate), "avg": _r(self.h2h_avg)},
                "h2h_l5": {"g": self.h2h_l5_games, "o": self.h2h_l5_over, "r": _r(self.h2h_l5_hit_rate), "avg": _r(self.h2h_l5_avg)},
                "h2h_l10": {"g": self.h2h_l10_games, "o": self.h2h_l10_over, "r": _r(self.h2h_l10_hit_rate), "avg": _r(self.h2h_l10_avg)},
                "h2h_l20": {"g": self.h2h_l20_games, "o": self.h2h_l20_over, "r": _r(self.h2h_l20_hit_rate), "avg": _r(self.h2h_l20_avg)},
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
    sport:        str = "",
) -> UDBetDecision:
    """
    Evaluate OVER / UNDER / PASS for a qualified Underdog prop.

    Real game results (``hit_rates``) are preferred for direction and tier
    quality, but are not required when supporting evidence/history exists.
    The historical line average is used as the fallback directional signal.

    Parameters
    ----------
    score:        UDPropScore from score_ud_prop() — used for gating only.
    validation:   PlayerPropValidation from validate_player_prop() — market supplement.
    current_line: Current prop line value.
    prev_line:    Previous line if this is a line-change event.
    hit_rates:    PlayerHitRates computed from real game results.  If absent,
                   insufficient, or inconclusive, the decision falls back to
                   validation.avg_line when supporting data is available.
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

    def _historical_fallback(reason: str) -> UDBetDecision:
        """Use stored line history when result hit-rates cannot decide."""
        if not bool(getattr(validation, "has_supporting_data", False)):
            return UDBetDecision.make_no_direction(
                reason=(
                    "No supporting evidence/history available — "
                    "hit-rate data and historical line evidence are both missing"
                ),
                hit_rates=hit_rates,
                avg_vs_line_pct=avg_vs_line_pct,
                at_historical_low=at_low,
            )

        direction = _historical_average_direction(validation, current_line)
        if direction is None:
            return UDBetDecision.make_no_direction(
                reason=(
                    f"{reason}; historical average does not establish "
                    "an OVER or UNDER direction"
                ),
                hit_rates=hit_rates,
                avg_vs_line_pct=avg_vs_line_pct,
                at_historical_low=at_low,
            )

        avg_line = float(getattr(validation, "avg_line"))
        relation = "below" if direction == "OVER" else "above"
        legacy_tier = str(getattr(score, "tier", "") or "").upper()
        if legacy_tier not in {"S", "A", "B", "C", "PASS"}:
            legacy_tier = None
        raw_confidence = getattr(score, "total", None)
        if isinstance(raw_confidence, (int, float)) and not isinstance(
            raw_confidence, bool
        ):
            legacy_confidence = max(0, min(_MAX_CONFIDENCE, int(round(raw_confidence))))
        else:
            legacy_confidence = 0
        return UDBetDecision.make_pick(
            recommendation=direction,
            reason=(
                f"Historical average fallback — current line {current_line:g} "
                f"is {relation} historical average {avg_line:g}; "
                "settled hit-rate history is retained for reporting and "
                "quality enrichment, not used as a delivery blocker"
            ),
            hit_rates=hit_rates,
            avg_vs_line_pct=avg_vs_line_pct,
            at_historical_low=at_low,
            evidence_source=EVIDENCE_HISTORICAL_AVERAGE,
            decision_tier=legacy_tier,
            confidence=legacy_confidence,
        )

    # ── Gate 1: supporting evidence/history required ─────────────────────────
    # Guard against callers passing a list or other wrong type (e.g. from
    # cold-start hit_rates=None path) — treat any non-PlayerHitRates value as None.
    if not hasattr(hit_rates, "has_real_data"):
        hit_rates = None
    if hit_rates is None or not hit_rates.has_real_data:
        return _historical_fallback("No settled player-game hit-rate window available")

    # ── Gate 2: sufficient sample size ───────────────────────────────────────
    primary = _primary_window(hit_rates)
    if primary is None:
        return _historical_fallback(
            f"Insufficient settled sample ({hit_rates.total_games} result(s))"
        )

    rate = primary.hit_rate

    # ── Gate 3: must cross the direction threshold in the primary window ────
    _b = _B_RATE
    if rate >= _b:
        direction = "OVER"
    elif rate <= (1.0 - _b):
        direction = "UNDER"
    else:
        return _historical_fallback(
            f"Hit rate {rate:.0%} is inconclusive"
        )

    # ── Gate 4: no contradicting window ──────────────────────────────────────
    contradiction = _check_contradictions(hit_rates, direction)
    if contradiction:
        return _historical_fallback(contradiction)

    # ── Reason string ─────────────────────────────────────────────────────────
    reason = _build_reason(
        hit_rates, direction, None, primary, current_line,
        avg_vs_line_pct, at_low,
    )

    tier = _determine_tier(hit_rates, direction, primary)
    confidence = _compute_confidence(hit_rates, direction, tier, primary)
    return UDBetDecision.make_pick(
        recommendation     = direction,
        reason             = reason,
        hit_rates          = hit_rates,
        avg_vs_line_pct    = avg_vs_line_pct,
        at_historical_low  = at_low,
        evidence_source    = EVIDENCE_SETTLED_RESULTS,
        decision_tier      = tier,
        confidence         = confidence,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _historical_average_direction(
    validation: "PlayerPropValidation",
    current_line: float,
) -> Optional[str]:
    """Return the line-history direction, or None when history is neutral."""
    avg_line = getattr(validation, "avg_line", None)
    if not isinstance(avg_line, (int, float)) or isinstance(avg_line, bool):
        return None
    if not isinstance(current_line, (int, float)) or isinstance(current_line, bool):
        return None
    if avg_line > current_line:
        return "OVER"
    if avg_line < current_line:
        return "UNDER"
    return None


def _primary_window(hit_rates: "PlayerHitRates") -> "Optional[WindowStats]":
    """Return the smallest window with ≥ _MIN_GAMES_PRIMARY games."""
    for w in (
        hit_rates.l5,
        hit_rates.l10,
        hit_rates.l15,
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
            hit_rates.l15,
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
            required = _required_strong_windows(hit_rates)
            if required and all(w.hit_rate >= _STRONG_OVER_RATE for w in required):
                return "S"
            return "A"
        elif rate >= _A_RATE:
            return "A"
        else:
            return "B"
    else:
        if rate <= (1.0 - _S_RATE):
            required = _required_strong_windows(hit_rates)
            if required and all(w.hit_rate <= _STRONG_UNDER_RATE for w in required):
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


def _required_strong_windows(
    hit_rates: "PlayerHitRates",
) -> "Optional[list[WindowStats]]":
    """Return complete L5/L10/L15/L20/L30 + season coverage."""
    windows = [
        hit_rates.l5,
        hit_rates.l10,
        hit_rates.l15,
        hit_rates.l20,
        hit_rates.l30,
        hit_rates.season,
    ]
    if any(w is None or w.games < _MIN_GAMES_PRIMARY for w in windows):
        return None
    return windows


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
    l15 = hit_rates.l15    if hit_rates else None
    l20 = hit_rates.l20    if hit_rates else None
    l30 = hit_rates.l30    if hit_rates else None
    sea = hit_rates.season if hit_rates else None
    h2h = hit_rates.h2h    if hit_rates else None
    h2h_l5 = hit_rates.h2h_l5 if hit_rates else None
    h2h_l10 = hit_rates.h2h_l10 if hit_rates else None
    h2h_l20 = hit_rates.h2h_l20 if hit_rates else None

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
        "l15_games":      _g(l15, "games"),
        "l15_over":       _g(l15, "over_count"),
        "l15_under":      _g(l15, "under_count"),
        "l15_hit_rate":   _g(l15, "hit_rate"),
        "l15_avg":        _g(l15, "average"),
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
        "h2h_l5_games":   _g(h2h_l5, "games"),
        "h2h_l5_over":    _g(h2h_l5, "over_count"),
        "h2h_l5_under":   _g(h2h_l5, "under_count"),
        "h2h_l5_hit_rate":_g(h2h_l5, "hit_rate"),
        "h2h_l5_avg":     _g(h2h_l5, "average"),
        "h2h_l10_games":  _g(h2h_l10, "games"),
        "h2h_l10_over":   _g(h2h_l10, "over_count"),
        "h2h_l10_under":  _g(h2h_l10, "under_count"),
        "h2h_l10_hit_rate": _g(h2h_l10, "hit_rate"),
        "h2h_l10_avg":    _g(h2h_l10, "average"),
        "h2h_l20_games":  _g(h2h_l20, "games"),
        "h2h_l20_over":   _g(h2h_l20, "over_count"),
        "h2h_l20_under":  _g(h2h_l20, "under_count"),
        "h2h_l20_hit_rate": _g(h2h_l20, "hit_rate"),
        "h2h_l20_avg":    _g(h2h_l20, "average"),
    }
