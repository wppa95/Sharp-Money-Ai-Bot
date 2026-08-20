"""
providers/player_history.py — Normalized PlayerHistoryProvider (hybrid).

Priority:
1. Real game results (PlayerGameResult via PlayerStatsProvider)
2. Underdog line-move proxy (fallback)

Public API:
  PlayerHistoryRecord, HistoryWindow, PlayerHistorySnapshot
  PlayerHistoryProvider / get_player_history_provider()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Database, UnderdogSnapshotRecord

logger = logging.getLogger(__name__)

_MIN_WINDOW = 3
H2H_MIN_GAMES = 3
_FETCH_CACHE_MAX = 2000


@dataclass(frozen=True)
class PlayerHistoryRecord:
    player: str
    sport: str
    stat: str
    game_id: str
    date: str
    opponent: Optional[str]
    value: float
    season: Optional[str] = None
    is_h2h: bool = False
    source: str = "real"
    line_moved: Optional[bool] = None


@dataclass(frozen=True)
class HistoryWindow:
    games: int
    over_count: int
    under_count: int
    hit_rate: float
    average: float
    is_proxy: bool = False

    def display(self) -> str:
        if self.games == 0:
            return "N/A"
        tag = " (proxy)" if self.is_proxy else ""
        return f"{self.over_count}/{self.games} ({self.hit_rate:.0%})  avg {self.average:.1f}{tag}"


@dataclass(frozen=True)
class PlayerHistorySnapshot:
    player_name: str
    sport: str
    stat_type: str
    current_line: float
    l5: Optional[HistoryWindow]
    l10: Optional[HistoryWindow]
    l20: Optional[HistoryWindow]
    l30: Optional[HistoryWindow]
    season: Optional[HistoryWindow]
    h2h: Optional[HistoryWindow]
    proxy_l5: Optional[float]
    proxy_l10: Optional[float]
    proxy_l20: Optional[float]
    proxy_l30: Optional[float]
    has_real_data: bool
    has_proxy_data: bool
    total_real_games: int
    total_proxy_snaps: int
    records: list[PlayerHistoryRecord] = field(default_factory=list)

    def preferred_l5(self) -> Optional[float]:
        return self.l5.hit_rate if self.l5 else self.proxy_l5

    def preferred_l10(self) -> Optional[float]:
        return self.l10.hit_rate if self.l10 else self.proxy_l10

    def preferred_l20(self) -> Optional[float]:
        return self.l20.hit_rate if self.l20 else self.proxy_l20

    def preferred_l30(self) -> Optional[float]:
        return self.l30.hit_rate if self.l30 else self.proxy_l30

    def season_hit_rate(self) -> Optional[float]:
        return self.season.hit_rate if self.season else None

    def h2h_hit_rate(self) -> Optional[float]:
        return self.h2h.hit_rate if self.h2h else None

    def has_supporting_data(self, min_samples: int = 5) -> bool:
        return self.total_real_games >= min_samples or self.total_proxy_snaps >= min_samples

    def rate_summary(self) -> str:
        if not self.has_real_data and not self.has_proxy_data:
            return "no history"
        parts = []
        if self.has_real_data:
            parts.append(f"real_n={self.total_real_games}")
            for label, win in (("L5", self.l5), ("L10", self.l10), ("L20", self.l20)):
                if win is not None:
                    parts.append(f"{label}={win.hit_rate:.0%}")
            if self.season is not None:
                parts.append(f"season={self.season.hit_rate:.0%}")
        else:
            parts.append(f"proxy_n={self.total_proxy_snaps}")
            for label, val in (("L5", self.proxy_l5), ("L10", self.proxy_l10)):
                if val is not None:
                    parts.append(f"{label}={val:.0%}(p)")
        return "  ".join(parts)


class PlayerHistoryProvider:
    def __init__(self) -> None:
        self._fetch_cache: set[tuple[str, str, str, str]] = set()

    async def get_snapshot(
        self,
        db: "Database",
        player_name: str,
        sport: str,
        stat_type: str,
        current_line: float,
        *,
        opponent: Optional[str] = None,
        ud_history: Optional["list[UnderdogSnapshotRecord]"] = None,
        min_samples: int = 5,
        force_refresh: bool = False,
        instrumentation=None,
    ) -> PlayerHistorySnapshot:
        sport_u = (sport or "UNKNOWN").upper()
        stat_l = stat_type.lower().strip()

        await self._ensure_real_results(
            db,
            player_name,
            sport_u,
            stat_type,
            force_refresh=force_refresh,
            instrumentation=instrumentation,
        )
        if instrumentation is not None:
            db_results = await instrumentation.await_db(
                "read",
                "get_player_results_for_history",
                db.get_player_results(player_name, sport_u, stat_type, limit=40),
            )
        else:
            db_results = await db.get_player_results(
                player_name, sport_u, stat_type, limit=40
            )
        if instrumentation is not None:
            instrumentation.count("evidence_lookups")
            _processing_started = __import__("time").monotonic()

        real_records = [
            PlayerHistoryRecord(
                player=r.player_name,
                sport=r.sport,
                stat=r.stat_type,
                game_id=r.game_date if isinstance(r.game_date, str) else r.game_date.isoformat(),
                date=r.game_date if isinstance(r.game_date, str) else r.game_date.isoformat(),
                opponent=r.opponent,
                value=float(r.actual_value),
                is_h2h=bool(opponent and r.opponent and _fuzzy_team_match(r.opponent, opponent)),
                source=getattr(r, "source", "real") or "real",
            )
            for r in db_results
        ]
        real_records.sort(key=lambda x: x.date, reverse=True)

        l5 = _window_from_values(real_records[:5], current_line)
        l10 = _window_from_values(real_records[:10], current_line)
        l20 = _window_from_values(real_records[:20], current_line)
        l30 = _window_from_values(real_records[:30], current_line)
        season = _window_from_values(real_records, current_line)

        h2h = None
        if opponent:
            h2h_recs = [r for r in real_records if r.is_h2h]
            if len(h2h_recs) >= H2H_MIN_GAMES:
                h2h = _window_from_values(h2h_recs, current_line)

        proxy_l5 = proxy_l10 = proxy_l20 = proxy_l30 = None
        proxy_n = 0
        if ud_history:
            proxy_n = len(ud_history)
            proxy_l5 = _proxy_move_rate(ud_history[:5])
            proxy_l10 = _proxy_move_rate(ud_history[:10])
            proxy_l20 = _proxy_move_rate(ud_history[:20])
            proxy_l30 = _proxy_move_rate(ud_history[:30])

        result = PlayerHistorySnapshot(
            player_name=player_name,
            sport=sport_u,
            stat_type=stat_l,
            current_line=current_line,
            l5=l5, l10=l10, l20=l20, l30=l30,
            season=season, h2h=h2h,
            proxy_l5=proxy_l5, proxy_l10=proxy_l10,
            proxy_l20=proxy_l20, proxy_l30=proxy_l30,
            has_real_data=len(real_records) > 0,
            has_proxy_data=proxy_n > 0,
            total_real_games=len(real_records),
            total_proxy_snaps=proxy_n,
            records=real_records,
        )
        if instrumentation is not None:
            instrumentation.duration(
                "evidence_processing",
                __import__("time").monotonic() - _processing_started,
            )
            if result.has_real_data or result.has_proxy_data:
                instrumentation.count("props_with_evidence")
            else:
                instrumentation.count("props_without_evidence")
        return result

    async def _ensure_real_results(
        self,
        db,
        player_name,
        sport,
        stat_type,
        *,
        force_refresh=False,
        instrumentation=None,
    ) -> None:
        today = datetime.utcnow().date().isoformat()
        key = (player_name, sport, stat_type.lower().strip(), today)
        if not force_refresh and key in self._fetch_cache:
            return
        if len(self._fetch_cache) >= _FETCH_CACHE_MAX:
            self._fetch_cache.clear()
        try:
            from providers.player_stats import PlayerStatsProvider
            _provider_started = __import__("time").monotonic()
            if instrumentation is not None:
                raw = await PlayerStatsProvider().fetch_results(
                    player_name,
                    sport,
                    stat_type,
                    instrumentation=instrumentation,
                )
            else:
                raw = await PlayerStatsProvider().fetch_results(
                    player_name, sport, stat_type
                )
            if instrumentation is not None:
                instrumentation.provider(
                    f"PlayerStatsProvider/{sport.upper()}",
                    __import__("time").monotonic() - _provider_started,
                )
                instrumentation.count("provider_enrichment_props")
            for r in raw:
                if instrumentation is not None:
                    await instrumentation.await_db(
                        "write", "upsert_player_result_for_history",
                        db.upsert_player_result(r),
                    )
                else:
                    await db.upsert_player_result(r)
            self._fetch_cache.add(key)
            if raw:
                logger.debug(
                    "PlayerHistoryProvider: fetched %d results for %s / %s",
                    len(raw), player_name, stat_type,
                )
        except Exception as exc:
            logger.warning(
                "PlayerHistoryProvider: fetch failed %s / %s: %s",
                player_name, stat_type, exc,
            )
            self._fetch_cache.add(key)


_provider: Optional[PlayerHistoryProvider] = None


def get_player_history_provider() -> PlayerHistoryProvider:
    global _provider
    if _provider is None:
        _provider = PlayerHistoryProvider()
    return _provider


def _window_from_values(records, current_line) -> Optional[HistoryWindow]:
    if len(records) < _MIN_WINDOW:
        return None
    values = [r.value for r in records if r.value is not None]
    if not values:
        return None
    n = len(values)
    oc = sum(1 for v in values if v > current_line)
    avg = sum(values) / n
    return HistoryWindow(
        games=n, over_count=oc, under_count=n - oc,
        hit_rate=round(oc / n, 3), average=round(avg, 2), is_proxy=False,
    )


def _proxy_move_rate(window) -> Optional[float]:
    if len(window) < _MIN_WINDOW:
        return None
    moved = sum(1 for r in window if getattr(r, "line_moved", False))
    return round(moved / len(window), 3)


def _fuzzy_team_match(stored: str, query: str, threshold: float = 0.6) -> bool:
    from difflib import SequenceMatcher
    s, q = stored.strip().lower(), query.strip().lower()
    if s == q or s in q or q in s:
        return True
    return SequenceMatcher(None, s, q).ratio() >= threshold