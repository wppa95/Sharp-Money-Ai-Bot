"""
engine/player_validator.py — Player prop performance validation layer.

Validates a prop against available historical data before allowing a
Telegram alert.  Since game-result data (wins/losses) is not yet
tracked, validation uses Underdog line-movement history as a market
proxy.  Rates are analogous to hit rates — they measure how frequently
the market has adjusted this prop's line.

Public API
──────────
  PlayerPropValidation  — frozen dataclass with all validation metrics
  validate_player_prop  — compute validation from DB history list

Rate labels
───────────
  l5_rate   Move-rate over last 5 snapshots  (proxy for L5 hit rate)
  l10_rate  Move-rate over last 10 snapshots
  l20_rate  Move-rate over last 20 snapshots
  l30_rate  Move-rate over last 30 snapshots

  season_hit_rate — Reserved; always None until result tracking added
  h2h_hit_rate    — Reserved; always None until result tracking added

has_supporting_data
───────────────────
  True when n_history >= min_samples (config.UD_VALIDATION_MIN_SAMPLES,
  default 5).  Immediate individual alerts are blocked when False.

  Consequence for new props:
    A prop flagged as is_new_prop (first-ever appearance) has zero DB
    history → has_supporting_data=False → no immediate alert → digest.
    This is intentional: "a 0.5 HR prop should not alert just because
    the number is low."

Note
────
  Rate fields are None (not 0.0) when the window has fewer than the
  minimum required records (3).  Callers should treat None as
  "data unavailable", not "zero activity".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import UnderdogSnapshotRecord

_MIN_WINDOW = 3   # minimum records to compute a meaningful rate


@dataclass(frozen=True)
class PlayerPropValidation:
    """Validation result for a single player + stat prop."""

    player_name: str
    stat_type:   str
    n_history:   int           # total DB snapshots used

    # Market-proxy rates: fraction of records where line_moved=True.
    # None when the window has fewer than _MIN_WINDOW records.
    l5_rate:  Optional[float]
    l10_rate: Optional[float]
    l20_rate: Optional[float]
    l30_rate: Optional[float]

    # Line context
    avg_line:         Optional[float]  # mean line value across all history
    min_line_seen:    Optional[float]  # lowest line ever recorded
    rate_at_or_below: Optional[float]  # fraction of history at or below current line

    # Reserved — always None until game-result tracking is integrated
    season_hit_rate: Optional[float]
    h2h_hit_rate:    Optional[float]

    # Decision flag
    has_supporting_data: bool   # n_history >= min_samples
    reason: str                 # human-readable explanation for audit/debug

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_json(self) -> str:
        """Compact JSON for storage in underdog_snapshots.validation_json."""

        def _r(v: Optional[float]) -> Optional[float]:
            return round(v, 3) if v is not None else None

        return json.dumps(
            {
                "n":          self.n_history,
                "l5":         _r(self.l5_rate),
                "l10":        _r(self.l10_rate),
                "l20":        _r(self.l20_rate),
                "l30":        _r(self.l30_rate),
                "avg":        _r(self.avg_line),
                "min":        _r(self.min_line_seen),
                "rate_below": _r(self.rate_at_or_below),
                "season":     self.season_hit_rate,
                "h2h":        self.h2h_hit_rate,
                "has_data":   self.has_supporting_data,
            },
            separators=(",", ":"),
        )

    # ── Display helper ───────────────────────────────────────────────────────

    def rate_summary(self) -> str:
        """One-line display for use inside alert messages."""
        if self.n_history == 0:
            return "no history"
        parts = [f"n={self.n_history}"]
        for label, val in (("L5", self.l5_rate), ("L10", self.l10_rate),
                           ("L20", self.l20_rate)):
            if val is not None:
                parts.append(f"{label}={val:.0%}")
        if self.avg_line is not None:
            parts.append(f"avg={self.avg_line}")
        return "  ".join(parts)


# ── Public entry point ────────────────────────────────────────────────────────

def validate_player_prop(
    player_name:  str,
    stat_type:    str,
    current_line: float,
    history:      "list[UnderdogSnapshotRecord]",
    *,
    min_samples:  int = 5,
) -> PlayerPropValidation:
    """
    Validate a prop against available DB history.

    Parameters
    ----------
    player_name:   Player display name.
    stat_type:     Normalised stat category (e.g. "Home Runs").
    current_line:  Line value currently being evaluated.
    history:       Most-recent-first list of UnderdogSnapshotRecord rows
                   from get_ud_prop_history().  Pass [] for brand-new
                   props — has_supporting_data will be False.
    min_samples:   Minimum records required for has_supporting_data=True.
                   Maps to config.UD_VALIDATION_MIN_SAMPLES.

    Returns
    -------
    PlayerPropValidation (frozen dataclass).
    """
    n = len(history)

    # ── Window move rates ─────────────────────────────────────────────────────
    def _rate(window: "list[UnderdogSnapshotRecord]") -> Optional[float]:
        if len(window) < _MIN_WINDOW:
            return None
        moved = sum(1 for r in window if r.line_moved)
        return round(moved / len(window), 3)

    l5_rate  = _rate(history[:5])
    l10_rate = _rate(history[:10])
    l20_rate = _rate(history[:20])
    l30_rate = _rate(history[:30])

    # ── Line context ──────────────────────────────────────────────────────────
    values = [r.line_value for r in history if r.line_value is not None]

    avg_line = (
        round(sum(values) / len(values), 3) if values else None
    )
    min_line_seen = round(min(values), 3) if values else None
    rate_at_or_below = (
        round(sum(1 for v in values if v <= current_line) / len(values), 3)
        if values else None
    )

    # ── Decision ──────────────────────────────────────────────────────────────
    has_data = n >= min_samples

    if n == 0:
        reason = "No history — first appearance; digest only"
    elif not has_data:
        reason = (
            f"Insufficient history ({n} snapshot{'s' if n != 1 else ''}, "
            f"need {min_samples}); digest only"
        )
    else:
        parts = [f"n={n}"]
        if l5_rate is not None:
            parts.append(f"L5={l5_rate:.0%}")
        if l10_rate is not None:
            parts.append(f"L10={l10_rate:.0%}")
        if min_line_seen is not None:
            parts.append(f"min_seen={min_line_seen}")
        reason = "Supporting data available  •  " + "  ".join(parts)

    return PlayerPropValidation(
        player_name         = player_name,
        stat_type           = stat_type,
        n_history           = n,
        l5_rate             = l5_rate,
        l10_rate            = l10_rate,
        l20_rate            = l20_rate,
        l30_rate            = l30_rate,
        avg_line            = avg_line,
        min_line_seen       = min_line_seen,
        rate_at_or_below    = rate_at_or_below,
        season_hit_rate     = None,    # reserved
        h2h_hit_rate        = None,    # reserved
        has_supporting_data = has_data,
        reason              = reason,
    )
