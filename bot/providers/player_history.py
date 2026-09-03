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

    @property
    def player_name(self) -> str:
        """Compatibility view for the shared game-result calculator."""
        return self.player

    @property
    def stat_type(self) -> str:
        """Compatibility view for the shared game-result calculator."""
        return self.stat

    @property
    def game_date(self) -> str:
        """Compatibility view for the shared game-result calculator."""
        return self.date

    @property
    def actual_value(self) -> float:
        """Compatibility view for the shared game-result calculator."""
        return self.value


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
    supplemental_sources: tuple[str, ...] = ()
    supplemental_records: list[PlayerHistoryRecord] = field(default_factory=list)
    total_supplemental_games: int = 0

    @property
    def has_supplemental_data(self) -> bool:
        return self.total_supplemental_games > 0

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
        return (
            self.total_real_games + self.total_supplemental_games >= min_samples
            or self.total_proxy_snaps >= min_samples
        )

    def rate_summary(self) -> str:
        if not self.has_real_data and not self.has_supplemental_data and not self.has_proxy_data:
            return "no history"
        parts = []
        if self.has_real_data or self.has_supplemental_data:
            evidence_n = self.total_real_games + self.total_supplemental_games
            if self.has_real_data and self.has_supplemental_data:
                parts.append(
                    f"real_n={self.total_real_games}"
                    f"  supplemental_n={self.total_supplemental_games}"
                )
            elif self.has_real_data:
                parts.append(f"real_n={self.total_real_games}")
            else:
                parts.append(f"supplemental_n={self.total_supplemental_games}")
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
    def __init__(self, propline_client=None) -> None:
        self._fetch_cache: set[tuple[str, str, str, str, str]] = set()
        self._supplement_cache: dict[tuple[str, str, str, str, str], list] = {}
        self._propline_client = propline_client

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
        source_provider: str = "Underdog",
        instrumentation=None,
        ensure_real_results: bool = True,
    ) -> PlayerHistorySnapshot:
        sport_u = (sport or "UNKNOWN").upper()
        stat_l = stat_type.lower().strip()

        if ensure_real_results:
            await self._ensure_real_results(
                db,
                player_name,
                sport_u,
                stat_type,
                source_provider=source_provider,
                min_samples=min_samples,
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
        supplemental_records: list[PlayerHistoryRecord] = []
        provider_scope = str(source_provider or "").strip().lower()
        if (
            provider_scope in {"underdog", "prizepicks"}
            and len(real_records) < max(1, int(min_samples))
        ):
            supplemental_results = await self.supplement_missing_results(
                db,
                player_name,
                sport_u,
                stat_type,
                min_samples=min_samples,
                source_provider=source_provider,
                instrumentation=instrumentation,
            )
            supplemental_records = [
                PlayerHistoryRecord(
                    player=r.player_name,
                    sport=r.sport,
                    stat=r.stat_type,
                    game_id=r.game_id or r.game_date,
                    date=r.game_date,
                    opponent=r.opponent,
                    value=float(r.actual_value),
                    source=getattr(r, "source", "propline_history"),
                )
                for r in supplemental_results
            ]
            supplemental_records.sort(key=lambda x: x.date, reverse=True)

        analysis_records = _merge_history_records(real_records, supplemental_records)
        l5 = _window_from_values(analysis_records[:5], current_line, required_games=5)
        l10 = _window_from_values(analysis_records[:10], current_line, required_games=10)
        l20 = _window_from_values(analysis_records[:20], current_line, required_games=20)
        l30 = _window_from_values(analysis_records[:30], current_line, required_games=30)
        season = _window_from_values(analysis_records, current_line, required_games=1)

        h2h = None
        if opponent:
            h2h_recs = [r for r in analysis_records if r.is_h2h]
            if len(h2h_recs) >= H2H_MIN_GAMES:
                h2h = _window_from_values(h2h_recs, current_line)

        proxy_l5 = proxy_l10 = proxy_l20 = proxy_l30 = None
        proxy_n = 0
        if ud_history:
            proxy_n = len(ud_history)
            proxy_l5 = _proxy_move_rate(ud_history[:5], required_snaps=5)
            proxy_l10 = _proxy_move_rate(ud_history[:10], required_snaps=10)
            proxy_l20 = _proxy_move_rate(ud_history[:20], required_snaps=20)
            proxy_l30 = _proxy_move_rate(ud_history[:30], required_snaps=30)

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
            supplemental_sources=tuple(sorted({
                record.source
                for record in supplemental_records
                if record.source.lower().startswith("propline")
            })),
            supplemental_records=supplemental_records,
            total_supplemental_games=len(supplemental_records),
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
        source_provider="",
        min_samples=5,
        force_refresh=False,
        instrumentation=None,
    ) -> None:
        today = datetime.utcnow().date().isoformat()
        provider_scope = str(source_provider or "").strip().lower()
        key = (
            player_name,
            sport,
            stat_type.lower().strip(),
            today,
            provider_scope,
        )
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

            # Underdog remains authoritative for the live line and market
            # identity.  PropLine is consulted only when the existing player
            # result evidence is still below the validation sample threshold.
            # PrizePicks never enters this branch.
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

    async def supplement_missing_results(
        self,
        db,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        min_samples: int = 5,
        source_provider: str = "Underdog",
        instrumentation=None,
    ) -> list:
        """Return missing primary-provider evidence from official PropLine history.

        The rows intentionally stay out of ``player_game_results`` for now.
        That table is the primary result source used by pick calculations;
        persistence of supplemental rows is a separate follow-up concern.
        """
        today = datetime.utcnow().date().isoformat()
        provider_scope = str(source_provider or "").strip().lower()
        if provider_scope not in {"underdog", "prizepicks"}:
            return []
        key = (player_name, sport, stat_type.lower().strip(), today, provider_scope)
        if key in self._supplement_cache:
            return list(self._supplement_cache[key])
        try:
            existing = await db.get_player_results(
                player_name, sport, stat_type, limit=max(1, int(min_samples))
            )
        except Exception:
            existing = []
        required = max(1, int(min_samples))
        if len(existing) >= required:
            self._supplement_cache[key] = []
            return []

        supplemental = await self._fetch_propline_history(
            player_name,
            sport,
            stat_type,
            instrumentation=instrumentation,
        )
        merged = _merge_game_results(existing, supplemental)
        supplemental = merged[len(existing):]
        if supplemental and instrumentation is not None:
            instrumentation.count("propline_supplemental_props")
        self._supplement_cache[key] = list(supplemental)
        return list(supplemental)

    async def fetch_resolved_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        instrumentation=None,
    ) -> list:
        """Fetch resolved PropLine results for settlement reconciliation.

        Unlike ``supplement_missing_results``, this intentionally does not
        short-circuit when the primary history already has five rows: grading
        needs the specific completed event, not merely a sufficient sample.
        """
        return await self._fetch_propline_history(
            player_name,
            sport,
            stat_type,
            instrumentation=instrumentation,
        )

    async def _fetch_propline_history(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        instrumentation=None,
    ) -> list:
        """Adapt official PropLine resolved history into game-result rows."""
        from providers.player_stats import RawGameResult
        from providers.propline import PropLineClient, _normalize_stat, _parse_datetime

        if self._propline_client is None:
            self._propline_client = PropLineClient()
        client = self._propline_client
        if not client.is_configured:
            return []
        official_sport = client.official_sport_for(sport)
        if not official_sport:
            # The official catalog is authoritative for provider-specific
            # sports; never guess an endpoint key for DOTA or another sport.
            discovered = await client.discover_sports()
            canonical = str(sport or "").upper().strip()
            official_sport = next(
                (
                    item.official_key
                    for item in discovered
                    if str(item.canonical_key).upper().strip() == canonical
                ),
                None,
            )
        if not official_sport:
            return []

        market = f"player_{_normalize_stat(stat_type).replace(' ', '_')}"
        result = await client.fetch_player_history(
            official_sport,
            player_name,
            market,
            bookmaker="prizepicks",
            limit=100,
        )
        if not result.succeeded or not result.complete:
            return []

        settled = {"WON", "LOST", "PUSH", "VOID"}
        rows = []
        for entry in result.entries:
            def _entry_value(name: str, default=None):
                if isinstance(entry, dict):
                    return entry.get(name, default)
                return getattr(entry, name, default)

            event_id = str(_entry_value("event_id") or "").strip()
            actual = _entry_value("actual_value")
            game_date = _parse_datetime(_entry_value("commence_time"))
            resolution = str(
                _entry_value("over_result")
                or _entry_value("under_result")
                or ""
            ).strip().upper()
            if (
                not event_id
                or game_date is None
                or actual is None
                or resolution not in settled
            ):
                continue
            try:
                value = float(actual)
            except (TypeError, ValueError):
                continue
            rows.append(RawGameResult(
                player_name=player_name,
                sport=str(sport or "").upper(),
                stat_type=stat_type.lower().strip(),
                game_date=game_date.date().isoformat(),
                actual_value=value,
                # PropLine's history schema does not identify which side the
                # player played for.  Do not guess an opponent from home/away.
                opponent=None,
                source="propline_history",
                game_id=event_id,
            ))
        return rows


def _merge_game_results(existing: list, supplemental: list) -> list:
    """Keep primary game results and add only distinct PropLine events."""
    return _merge_distinct_rows(existing, supplemental)


def _merge_history_records(
    primary: list[PlayerHistoryRecord],
    supplemental: list[PlayerHistoryRecord],
) -> list[PlayerHistoryRecord]:
    """Merge primary and supplemental records without double-counting games."""
    merged = _merge_distinct_rows(primary, supplemental)
    merged.sort(key=lambda record: record.date, reverse=True)
    return merged


def _merge_distinct_rows(primary: list, supplemental: list) -> list:
    """Merge rows by event ID, falling back to date when an ID is unavailable."""
    merged = list(primary)
    seen_ids: set[tuple[str, str]] = set()
    seen_dates: set[tuple[str, str]] = set()
    seen_idless_dates: set[tuple[str, str]] = set()

    def _parts(row) -> tuple[str, str, str]:
        event_id = str(
            getattr(row, "game_id", "")
            or getattr(row, "event_id", "")
            or ""
        ).strip()
        game_date = str(
            getattr(row, "game_date", "")
            or getattr(row, "date", "")
            or ""
        ).strip()
        stat = str(
            getattr(row, "stat_type", "")
            or getattr(row, "stat", "")
            or ""
        ).lower().strip()
        return event_id, game_date, stat

    for row in primary:
        event_id, game_date, stat = _parts(row)
        if event_id:
            seen_ids.add((event_id, stat))
        if game_date:
            seen_dates.add((game_date, stat))
            if not event_id:
                seen_idless_dates.add((game_date, stat))

    for row in supplemental:
        event_id, game_date, stat = _parts(row)
        duplicate = (
            (event_id, stat) in seen_ids
            if event_id
            else (game_date, stat) in seen_dates
        )
        # When the primary source has no event ID, its date is the only safe
        # join key. Conversely, an ID-bearing row must not duplicate it.
        if event_id and (game_date, stat) in seen_idless_dates:
            duplicate = True
        if duplicate:
            continue
        merged.append(row)
        if event_id:
            seen_ids.add((event_id, stat))
        if game_date:
            seen_dates.add((game_date, stat))
            if not event_id:
                seen_idless_dates.add((game_date, stat))
    return merged


_provider: Optional[PlayerHistoryProvider] = None


def get_player_history_provider() -> PlayerHistoryProvider:
    global _provider
    if _provider is None:
        _provider = PlayerHistoryProvider()
    return _provider


def _window_from_values(
    records, current_line, *, required_games: int = _MIN_WINDOW
) -> Optional[HistoryWindow]:
    if len(records) < required_games:
        return None
    values = [r.value for r in records if r.value is not None]
    if len(values) < required_games:
        return None
    n = len(values)
    oc = sum(1 for v in values if v > current_line)
    avg = sum(values) / n
    return HistoryWindow(
        games=n, over_count=oc, under_count=n - oc,
        hit_rate=round(oc / n, 3), average=round(avg, 2), is_proxy=False,
    )


def _proxy_move_rate(window, *, required_snaps: int = _MIN_WINDOW) -> Optional[float]:
    if len(window) < required_snaps:
        return None
    moved = sum(1 for r in window if getattr(r, "line_moved", False))
    return round(moved / len(window), 3)


def _fuzzy_team_match(stored: str, query: str, threshold: float = 0.6) -> bool:
    from difflib import SequenceMatcher
    s, q = stored.strip().lower(), query.strip().lower()
    if s == q or s in q or q in s:
        return True
    return SequenceMatcher(None, s, q).ratio() >= threshold