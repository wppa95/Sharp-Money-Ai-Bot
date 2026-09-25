"""
engine/player_validator.py — Player prop performance validation layer (hybrid).

Priority:
1. Real game-result hit rates (PlayerHistoryProvider)
2. Underdog line-move proxy (fallback)

Public API:
  PlayerPropValidation
  validate_player_prop(..., history_snap=None)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import UnderdogSnapshotRecord
    from providers.player_history import PlayerHistorySnapshot

_MIN_WINDOW = 5


@dataclass(frozen=True)
class PlayerPropValidation:
    player_name: str
    stat_type: str
    n_history: int

    l5_rate: Optional[float]
    l10_rate: Optional[float]
    l20_rate: Optional[float]
    l30_rate: Optional[float]

    avg_line: Optional[float]
    min_line_seen: Optional[float]
    rate_at_or_below: Optional[float]

    season_hit_rate: Optional[float]
    h2h_hit_rate: Optional[float]

    has_supporting_data: bool
    reason: str

    data_source: str = "proxy"   # "real" | "proxy" | "none"
    n_real_games: int = 0
    n_proxy_snaps: int = 0
    n_supplemental_games: int = 0
    supplemental_sources: tuple[str, ...] = ()

    def to_json(self) -> str:
        def _r(v: Optional[float]) -> Optional[float]:
            return round(v, 3) if v is not None else None

        return json.dumps(
            {
                "n": self.n_history,
                "l5": _r(self.l5_rate),
                "l10": _r(self.l10_rate),
                "l20": _r(self.l20_rate),
                "l30": _r(self.l30_rate),
                "avg": _r(self.avg_line),
                "min": _r(self.min_line_seen),
                "rate_below": _r(self.rate_at_or_below),
                "season": _r(self.season_hit_rate),
                "h2h": _r(self.h2h_hit_rate),
                "has_data": self.has_supporting_data,
                "src": self.data_source,
                "n_real": self.n_real_games,
                "n_proxy": self.n_proxy_snaps,
                "n_supp": self.n_supplemental_games,
                "supp": list(self.supplemental_sources),
            },
            separators=(",", ":"),
        )

    def rate_summary(self) -> str:
        if self.n_history == 0 and self.n_real_games == 0:
            return "no history"
        parts = []
        if self.data_source in {"real", "propline", "real+propline"}:
            parts.append(f"n={self.n_real_games + self.n_supplemental_games}")
            for label, val in (("L5", self.l5_rate), ("L10", self.l10_rate), ("L20", self.l20_rate)):
                if val is not None:
                    parts.append(f"{label}={val:.0%}")
            if self.season_hit_rate is not None:
                parts.append(f"season={self.season_hit_rate:.0%}")
            if self.h2h_hit_rate is not None:
                parts.append(f"H2H={self.h2h_hit_rate:.0%}")
        else:
            parts.append(f"n={self.n_proxy_snaps}(p)")
            for label, val in (("L5", self.l5_rate), ("L10", self.l10_rate), ("L20", self.l20_rate)):
                if val is not None:
                    parts.append(f"{label}={val:.0%}(p)")
            if self.avg_line is not None:
                parts.append(f"avg={self.avg_line}")
        return "  ".join(parts)


def validate_player_prop(
    player_name: str,
    stat_type: str,
    current_line: float,
    history: "list[UnderdogSnapshotRecord]",
    *,
    min_samples: int = 5,
    history_snap: Optional["PlayerHistorySnapshot"] = None,
    completed_hit_rates: Optional[object] = None,
) -> PlayerPropValidation:
    n_proxy = len(history)

    def _proxy_rate(
        window: "list[UnderdogSnapshotRecord]",
        required_snapshots: int,
    ) -> Optional[float]:
        if len(window) < required_snapshots:
            return None
        moved = sum(1 for r in window if getattr(r, "line_moved", False))
        return round(moved / len(window), 3)

    proxy_l5 = _proxy_rate(history[:5], 5)
    proxy_l10 = _proxy_rate(history[:10], 10)
    proxy_l20 = _proxy_rate(history[:20], 20)
    proxy_l30 = _proxy_rate(history[:30], 30)

    values = [r.line_value for r in history if getattr(r, "line_value", None) is not None]
    avg_line = round(sum(values) / len(values), 3) if values else None
    min_line_seen = round(min(values), 3) if values else None
    rate_at_or_below = (
        round(sum(1 for v in values if v <= current_line) / len(values), 3)
        if values else None
    )

    n_real = 0
    n_supplemental = 0
    supplemental_sources: tuple[str, ...] = ()
    season_hr: Optional[float] = None
    h2h_hr: Optional[float] = None
    data_source = "none"
    l5_rate = l10_rate = l20_rate = l30_rate = None
    n_history = 0

    # Large live feeds skip constructing a PlayerHistorySnapshot for every
    # candidate, but still compute hit rates from the bulk-loaded completed
    # result cache. Preserve that trusted provenance in validation so the
    # delivery gate does not mistake a skipped snapshot for missing evidence.
    result_source = str(
        getattr(completed_hit_rates, "data_source", "") or ""
    ).lower().strip()
    try:
        result_total = int(getattr(completed_hit_rates, "total_games", 0) or 0)
    except (TypeError, ValueError):
        result_total = 0
    try:
        result_supplemental = int(
            getattr(completed_hit_rates, "n_supplemental_games", 0) or 0
        )
    except (TypeError, ValueError):
        result_supplemental = 0
    result_supplemental = min(max(0, result_supplemental), max(0, result_total))
    result_has_provenance = bool(
        completed_hit_rates is not None
        and getattr(completed_hit_rates, "has_real_data", False)
        and result_total > 0
        and result_source
        in {
            "primary",
            "real",
            "propline",
            "primary+propline",
            "real+propline",
        }
    )

    def _completed_rate(window_name: str) -> Optional[float]:
        window = getattr(completed_hit_rates, window_name, None)
        if window is None:
            return None
        try:
            games = int(getattr(window, "games", 0) or 0)
            rate = float(getattr(window, "hit_rate", None))
        except (TypeError, ValueError):
            return None
        if games < _MIN_WINDOW or not 0.0 <= rate <= 1.0:
            return None
        return rate

    has_snapshot_evidence = bool(
        history_snap is not None
        and (
            history_snap.has_real_data
            or getattr(history_snap, "has_supplemental_data", False)
        )
    )
    if has_snapshot_evidence:
        n_real = history_snap.total_real_games
        n_supplemental = getattr(history_snap, "total_supplemental_games", 0)
        supplemental_sources = tuple(
            getattr(history_snap, "supplemental_sources", ()) or ()
        )
        if n_supplemental:
            data_source = "propline" if n_real == 0 else "real+propline"
        else:
            data_source = "real"
        l5_rate = history_snap.preferred_l5()
        l10_rate = history_snap.preferred_l10()
        l20_rate = history_snap.preferred_l20()
        l30_rate = history_snap.preferred_l30()
        season_hr = history_snap.season_hit_rate()
        h2h_hr = history_snap.h2h_hit_rate()
        n_history = n_real + n_supplemental
    elif result_has_provenance:
        if result_source == "propline":
            n_supplemental = result_total
            data_source = "propline"
        else:
            n_supplemental = result_supplemental
            n_real = result_total - n_supplemental
            data_source = (
                "real+propline" if n_supplemental else "real"
            )
        supplemental_sources = tuple(
            getattr(completed_hit_rates, "supplemental_sources", ()) or ()
        )
        l5_rate = _completed_rate("l5")
        l10_rate = _completed_rate("l10")
        l20_rate = _completed_rate("l20")
        l30_rate = _completed_rate("l30")
        season_hr = _completed_rate("season")
        h2h_hr = _completed_rate("h2h")
        n_history = n_real + n_supplemental
    elif n_proxy > 0:
        data_source = "proxy"
        l5_rate = proxy_l5
        l10_rate = proxy_l10
        l20_rate = proxy_l20
        l30_rate = proxy_l30
        n_history = n_proxy

    has_data = (
        n_history >= min_samples
        or n_proxy >= min_samples
        or n_real + n_supplemental >= min_samples
    )

    if n_history == 0 and n_proxy == 0 and n_real == 0 and n_supplemental == 0:
        reason = "No history — first appearance; digest only"
    elif not has_data:
        reason = (
            f"Insufficient history (real={n_real}, supplemental={n_supplemental}, "
            f"proxy={n_proxy}, "
            f"need {min_samples}); digest only"
        )
    else:
        parts = [f"src={data_source}", f"n={n_history}"]
        if l5_rate is not None:
            parts.append(f"L5={l5_rate:.0%}")
        if l10_rate is not None:
            parts.append(f"L10={l10_rate:.0%}")
        if season_hr is not None:
            parts.append(f"season={season_hr:.0%}")
        if min_line_seen is not None and data_source == "proxy":
            parts.append(f"min_seen={min_line_seen}")
        reason = "Supporting data available  •  " + "  ".join(parts)

    return PlayerPropValidation(
        player_name=player_name,
        stat_type=stat_type,
        n_history=max(n_history, n_proxy, n_real),
        l5_rate=l5_rate,
        l10_rate=l10_rate,
        l20_rate=l20_rate,
        l30_rate=l30_rate,
        avg_line=avg_line,
        min_line_seen=min_line_seen,
        rate_at_or_below=rate_at_or_below,
        season_hit_rate=season_hr,
        h2h_hit_rate=h2h_hr,
        has_supporting_data=has_data,
        reason=reason,
        data_source=data_source,
        n_real_games=n_real,
        n_proxy_snaps=n_proxy,
        n_supplemental_games=n_supplemental,
        supplemental_sources=supplemental_sources,
    )