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
import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Database, UnderdogSnapshotRecord

logger = logging.getLogger(__name__)

_MIN_WINDOW = 3
H2H_MIN_GAMES = 3
_FETCH_CACHE_MAX = 2000

PROPLINE_COVERAGE_USABLE = "usable"
PROPLINE_COVERAGE_REDACTED = "redacted"
PROPLINE_COVERAGE_UNAVAILABLE = "unavailable"
PROPLINE_COVERAGE_MALFORMED = "malformed"
PROPLINE_COVERAGE_NOT_NEEDED = "not_needed"


def propline_result_rows(results) -> list:
    """Convert explicit event settlements to game rows.

    VOID/CANCELLED settlements have no numeric player stat. Keep those rows as
    a private settlement marker so the opportunity grader can close the
    matching prop, but never treat them as numeric player-history evidence.
    """
    from providers.player_stats import RawGameResult

    rows = []
    for evidence in results or ():
        if not bool(getattr(evidence, "is_settled", False)):
            continue
        event_id = str(getattr(evidence, "event_id", "") or "").strip()
        player_name = str(getattr(evidence, "player_name", "") or "").strip()
        sport = str(getattr(evidence, "sport", "") or "").strip().upper()
        stat_type = str(getattr(evidence, "stat_type", "") or "").strip().lower()
        actual_value = getattr(evidence, "actual_value", None)
        resolved_at = (
            getattr(evidence, "commence_time", None)
            or getattr(evidence, "resolved_at", None)
            or getattr(evidence, "observed_at", None)
        )
        resolution_state = str(
            getattr(evidence, "resolution_state", "") or ""
        ).upper()
        is_void = resolution_state in {
            "VOID",
            "CANCELLED",
            "INJURY_VOID",
            "GAME_INTERRUPTED",
        }
        if (
            not event_id
            or not player_name
            or not sport
            or not stat_type
            or resolved_at is None
            or (actual_value is None and not is_void)
        ):
            continue
        if is_void and actual_value is None:
            # RawGameResult is the existing transport type and requires a
            # numeric value. This marker is consumed by the settlement grader
            # and is never written to player_game_results.
            value = 0.0
        else:
            try:
                value = float(actual_value)
            except (TypeError, ValueError):
                continue
        rows.append(
            RawGameResult(
                player_name=player_name,
                sport=sport,
                stat_type=stat_type,
                game_date=resolved_at.date().isoformat(),
                actual_value=value,
                opponent=None,
                source=(
                    "propline_event_results_void"
                    if is_void
                    else "propline_event_results"
                ),
                game_id=event_id,
            )
        )
    return rows


def propline_player_history_rows(
    entries,
    player_name: str,
    sport: str,
    stat_type: str,
) -> list:
    """Convert explicitly settled PropLine player-history entries to game rows.

    This is shared by the scheduled supplement and the bounded read-only
    rotation. A completed-looking response is not enough: each entry must have
    an event ID, event date, numeric actual value, and explicit settlement.
    """
    from providers.player_stats import RawGameResult
    from providers.propline import _parse_datetime

    player = str(player_name or "").strip()
    sport_key = str(sport or "").strip().upper()
    stat = str(stat_type or "").strip().lower()
    if not player or not sport_key or not stat:
        return []

    settled = {"WON", "LOST", "PUSH", "VOID"}
    rows = []
    for entry in entries or ():
        def _entry_value(name: str, default=None):
            if isinstance(entry, dict):
                return entry.get(name, default)
            return getattr(entry, name, default)

        redacted = _entry_value("redacted", False)
        response_complete = _entry_value("response_complete", True)
        if (
            bool(redacted)
            or response_complete is False
            or str(response_complete).strip().lower() in {"false", "0", "no"}
        ):
            continue

        event_id = str(_entry_value("event_id") or "").strip()
        actual = _entry_value("actual_value")
        game_date = _parse_datetime(_entry_value("commence_time"))
        resolution = str(
            _entry_value("over_result") or _entry_value("under_result") or ""
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

        player_team = str(
            _entry_value("player_team") or _entry_value("team") or ""
        ).strip()
        home_team = str(
            _entry_value("home_team") or _entry_value("home") or ""
        ).strip()
        away_team = str(
            _entry_value("away_team") or _entry_value("away") or ""
        ).strip()
        opponent = None
        if player_team and home_team and away_team:
            if _fuzzy_team_match(player_team, home_team):
                opponent = away_team
            elif _fuzzy_team_match(player_team, away_team):
                opponent = home_team
        rows.append(
            RawGameResult(
                player_name=player,
                sport=sport_key,
                stat_type=stat,
                game_date=game_date.date().isoformat(),
                actual_value=value,
                opponent=opponent,
                source="propline_history",
                game_id=event_id,
            )
        )
    return rows


def _is_database_backend(db: object) -> bool:
    """Only the real database backend may trigger remote history enrichment."""
    try:
        from database import Database
        if isinstance(db, Database):
            return True
        # Lightweight async database doubles used by provider tests are safe
        # to exercise with the injected PropLine client; generic mocks are not.
        return (
            inspect.iscoroutinefunction(getattr(db, "get_player_results", None))
            and inspect.iscoroutinefunction(
                getattr(db, "upsert_player_result", None)
            )
        )
    except Exception:
        return False


@dataclass(frozen=True)
class PropLineHistoryCoverage:
    """Scheduled PropLine history attempt and its safe evidence rows."""

    rows: tuple = ()
    status: str = PROPLINE_COVERAGE_UNAVAILABLE
    failure_kind: str = ""
    error: str = ""
    sport: str = ""
    market: str = ""


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
        preloaded_results: Optional[list] = None,
    ) -> PlayerHistorySnapshot:
        sport_u = (sport or "UNKNOWN").upper()
        stat_l = stat_type.lower().strip()

        if ensure_real_results and preloaded_results is None:
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
        if preloaded_results is not None:
            db_results = list(preloaded_results)
        elif instrumentation is not None:
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
            preloaded_results is None
            and ensure_real_results
            and
            provider_scope in {"underdog", "prizepicks"}
            and _is_database_backend(db)
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
                    is_h2h=bool(
                        opponent
                        and r.opponent
                        and _fuzzy_team_match(r.opponent, opponent)
                    ),
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
            _bulk_writer = getattr(db, "upsert_player_results_bulk", None)
            if callable(_bulk_writer):
                if instrumentation is not None:
                    await instrumentation.await_db(
                        "write",
                        "upsert_player_results_bulk_for_history",
                        _bulk_writer(raw),
                    )
                else:
                    await _bulk_writer(raw)
            else:
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

        PropLine event-stat rows are durable player-game evidence.  Resolved
        prop history and the player-games convenience endpoint remain
        in-memory fallbacks because they do not provide the same event/status
        join guarantees.
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

        # Do not spend PropLine quota on a guessed market. Raw player-game
        # support is also valid for documented raw-only sports such as golf.
        # A missing capability is an explicit evidence gap, not a reason to
        # infer a player result from odds or team scores.
        has_market = True
        try:
            from providers.propline import (
                _propline_raw_player_game_stat_supported,
                official_player_market_for,
            )

            client_for_capability = self._propline_client
            if client_for_capability is None:
                from providers.propline import PropLineClient

                client_for_capability = PropLineClient()
            has_market = bool(
                official_player_market_for(stat_type, sport=sport)
            )
            has_raw_games = bool(
                _propline_raw_player_game_stat_supported(sport, stat_type)
            )
            if (
                callable(getattr(client_for_capability, "official_sport_for", None))
                and not has_market
                and not has_raw_games
            ):
                logger.info(
                    "PlayerHistoryProvider: INSUFFICIENT_EVIDENCE "
                    "sport=%s player=%s market=%s reason=unsupported_propline_market",
                    sport,
                    player_name,
                    stat_type,
                )
                self._supplement_cache[key] = []
                return []
        except Exception as exc:
            logger.debug(
                "PlayerHistoryProvider: PropLine capability check failed "
                "%s/%s/%s: %s",
                sport,
                player_name,
                stat_type,
                exc,
            )

        # Event stats are the authoritative raw box-score path.  Persist them
        # before returning so a later scan does not have to repeat the
        # provider request.  The database uniqueness constraint makes this
        # safe when a caller retries the same cycle.
        event_stats = []
        if has_market:
            event_stats = await self._fetch_propline_event_stats(
                player_name,
                sport,
                stat_type,
                instrumentation=instrumentation,
            )
        event_stats_merged = _merge_game_results(existing, event_stats)
        new_event_stats = event_stats_merged[len(existing):]
        if new_event_stats:
            try:
                _bulk_writer = getattr(db, "upsert_player_results_bulk", None)
                if callable(_bulk_writer):
                    if instrumentation is not None:
                        await instrumentation.await_db(
                            "write",
                            "upsert_player_results_bulk_propline",
                            _bulk_writer(new_event_stats),
                        )
                    else:
                        await _bulk_writer(new_event_stats)
                else:
                    for row in new_event_stats:
                        await db.upsert_player_result(row)
            except Exception as exc:
                logger.warning(
                    "PlayerHistoryProvider: PropLine history write failed "
                    "%s / %s: %s",
                    player_name,
                    stat_type,
                    exc,
                )

        # Raw game logs are also book-agnostic and may cover older games than
        # the score window.  Use them only to fill the remaining gap.
        supplemental = list(new_event_stats)
        if len(_merge_game_results(existing, supplemental)) < required:
            supplemental = _merge_game_results(
                supplemental,
                await self._fetch_propline_games(
                    player_name,
                    sport,
                    stat_type,
                    instrumentation=instrumentation,
                ),
            )
        if len(_merge_game_results(existing, supplemental)) < required:
            supplemental = _merge_game_results(
                supplemental,
                await self._fetch_propline_history(
                    player_name,
                    sport,
                    stat_type,
                    source_provider=source_provider,
                    instrumentation=instrumentation,
                ),
            )
        if supplemental:
            merged = _merge_game_results(existing, supplemental)
            supplemental = merged[len(existing):]
        else:
            supplemental = []
        if supplemental and instrumentation is not None:
            instrumentation.count("propline_supplemental_props")
        self._supplement_cache[key] = list(supplemental)
        return list(supplemental)

    async def fetch_scheduled_results(
        self,
        primary_results: list | tuple | None,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        min_samples: int = 5,
        source_provider: str = "",
        allow_propline: bool = True,
        force_propline: bool = False,
        instrumentation=None,
    ) -> PropLineHistoryCoverage:
        """Supplement a scheduled provider fetch with safe PropLine history.

        The scheduled collector already fetches the public/stat provider. This
        method only spends a PropLine request when that evidence is below the
        normal validation threshold, so the authorized feed remains a
        supplement rather than a replacement or a new delivery path.

        The fast, market-specific resolved-history endpoint is used here. Raw
        player-game and event-stat endpoints are intentionally left to the
        existing supplemental/backfill path because their shared pacing can
        wait longer than this collector's per-target budget.
        """
        primary = list(primary_results or ())
        if len(primary) >= max(1, int(min_samples)) and not force_propline:
            return PropLineHistoryCoverage(
                rows=tuple(primary),
                status=PROPLINE_COVERAGE_NOT_NEEDED,
            )
        if not allow_propline:
            return PropLineHistoryCoverage(
                rows=tuple(primary),
                status=PROPLINE_COVERAGE_NOT_NEEDED,
            )
        sport_u = str(sport or "").upper().strip()
        if not sport_u:
            return PropLineHistoryCoverage(
                rows=tuple(primary),
                status=PROPLINE_COVERAGE_NOT_NEEDED,
            )

        metadata: dict[str, str] = {}
        supplemental = await self._fetch_propline_history(
            player_name,
            sport_u,
            stat_type,
            source_provider=source_provider,
            instrumentation=instrumentation,
            coverage=metadata,
        )
        merged = _merge_game_results(primary, supplemental)
        return PropLineHistoryCoverage(
            rows=tuple(merged),
            status=metadata.get("status", PROPLINE_COVERAGE_UNAVAILABLE),
            failure_kind=metadata.get("failure_kind", ""),
            error=metadata.get("error", ""),
            sport=metadata.get("sport", sport_u),
            market=metadata.get("market", ""),
        )

    async def fetch_resolved_results(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        source_provider: str = "Underdog",
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
            source_provider=source_provider,
            instrumentation=instrumentation,
        )

    async def fetch_resolved_event_results(
        self,
        event_id: str,
        sport: str,
        *,
        source_provider: str = "Underdog",
    ) -> list:
        """Fetch explicit PropLine settlements for one known event.

        Event results are the preferred settlement path when a pending prop
        carries its provider event identity. They preserve the provider's
        result payload even when the display market is not supported by the
        player-history market catalog.
        """
        from providers.propline import PropLineClient

        event_key = str(event_id or "").strip()
        sport_u = str(sport or "").upper().strip()
        if not event_key or not sport_u:
            return []
        if self._propline_client is None:
            self._propline_client = PropLineClient()
        client = self._propline_client
        if not getattr(client, "is_configured", False):
            return []

        try:
            official_sport = client.official_sport_for(sport_u)
            if not official_sport:
                discovered = await client.discover_sports()
                official_sport = next(
                    (
                        item.official_key
                        for item in discovered
                        if str(item.canonical_key).upper().strip() == sport_u
                    ),
                    None,
                )
            bookmaker = _propline_bookmaker_for_source(source_provider)
            if not official_sport or not bookmaker:
                return []
            result = await client.fetch_event_results(
                official_sport,
                event_key,
                bookmaker=bookmaker,
            )
        except Exception as exc:
            logger.debug(
                "PlayerHistoryProvider: PropLine event settlement failed "
                "%s/%s: %s",
                sport_u,
                event_key,
                exc,
            )
            return []

        if not result.succeeded or not result.complete or result.redacted:
            return []
        return propline_result_rows(result.results)

    async def _fetch_propline_event_stats(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        instrumentation=None,
    ) -> list:
        """Fetch verified final box-score stats for one player/stat.

        PropLine exposes scores and event stats as separate read-only
        endpoints.  Neither endpoint is trusted in isolation: the score
        event supplies the date and final status, while the stats response
        must repeat the same event identity and final status.  Player and
        stat matches are exact after whitespace/case normalization; multiple
        matches are rejected instead of guessing.
        """
        from providers.player_stats import RawGameResult
        from providers.propline import (
            PropLineClient,
            _clean,
            _normalize_stat,
            _parse_datetime,
            propline_raw_player_game_value,
        )
        sport_u = str(sport or "").upper().strip()
        if not sport_u:
            return []
        if self._propline_client is None:
            self._propline_client = PropLineClient()
        client = self._propline_client
        fetch_scores = getattr(client, "fetch_scores", None)
        fetch_event_stats = getattr(client, "fetch_event_stats", None)
        try:
            configured = bool(getattr(client, "is_configured", False))
            official_sport = client.official_sport_for(sport_u)
        except Exception as exc:
            logger.debug(
                "PlayerHistoryProvider: PropLine configuration failed "
                "%s / %s: %s",
                player_name,
                stat_type,
                exc,
            )
            return []
        if not configured:
            return []
        if not official_sport or not callable(fetch_scores) or not callable(fetch_event_stats):
            return []

        requested_player = re.sub(r"\s+", " ", str(player_name or "").strip()).casefold()
        requested_stat = _normalize_stat(stat_type)
        if not requested_player or not requested_stat:
            return []

        def _field(value, *names, default=None):
            if isinstance(value, dict):
                for name in names:
                    candidate = value.get(name)
                    if candidate not in (None, ""):
                        return candidate
            else:
                for name in names:
                    candidate = getattr(value, name, None)
                    if candidate not in (None, ""):
                        return candidate
            return default

        def _payload(result):
            if not bool(_field(result, "succeeded", default=False)):
                return None
            if not bool(_field(result, "complete", default=False)):
                return None
            return _field(result, "payload")

        def _records(payload, key):
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if not isinstance(payload, dict):
                return []
            values = payload.get(key)
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
            nested = payload.get("data")
            if isinstance(nested, dict):
                return _records(nested, key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            return []

        final_statuses = {
            "closed", "complete", "completed", "final", "resolved", "settled",
        }

        def _sport_matches(value, expected_official, expected_canonical):
            provided = str(value or "").strip().lower().replace("_", " ")
            if not provided:
                return True
            official = str(expected_official or "").strip().lower().replace("_", " ")
            canonical = str(expected_canonical or "").strip().lower().replace("_", " ")
            return provided in {official, canonical}

        try:
            # Keep the discovery window aligned with PropLine's documented
            # default.  Older history is filled by the single player-games
            # request below, rather than multiplying event-stat calls.
            scores_result = await fetch_scores(official_sport, days_from=3)
            scores_payload = _payload(scores_result)
            score_rows = _records(scores_payload, "events")
        except Exception as exc:
            logger.debug(
                "PlayerHistoryProvider: PropLine score discovery failed "
                "%s / %s: %s",
                player_name,
                stat_type,
                exc,
            )
            return []

        # A repeated event ID with conflicting dates is ambiguous.  Do not
        # let an arbitrary response ordering choose the game date.
        events: dict[str, dict] = {}
        ambiguous_event_ids: set[str] = set()
        for event in score_rows:
            event_id = _clean(_field(event, "id", "event_id", "game_id", "match_id"))
            event_time = _parse_datetime(
                _field(event, "commence_time", "start_time", "event_start", "game_time")
            )
            status = _clean(_field(event, "status", "state", "event_status", "game_status")).lower()
            if not event_id or event_time is None or status not in final_statuses:
                continue
            if not _sport_matches(
                _field(event, "sport_key", "sport", "league"),
                official_sport,
                sport_u,
            ):
                continue
            prior = events.get(event_id)
            if prior is not None:
                prior_time = _parse_datetime(
                    _field(prior, "commence_time", "start_time", "event_start", "game_time")
                )
                prior_home = re.sub(
                    r"\s+",
                    " ",
                    str(_field(prior, "home_team", "home", "home_name") or "").strip(),
                ).casefold()
                prior_away = re.sub(
                    r"\s+",
                    " ",
                    str(_field(prior, "away_team", "away", "away_name") or "").strip(),
                ).casefold()
                current_home = re.sub(
                    r"\s+",
                    " ",
                    str(_field(event, "home_team", "home", "home_name") or "").strip(),
                ).casefold()
                current_away = re.sub(
                    r"\s+",
                    " ",
                    str(_field(event, "away_team", "away", "away_name") or "").strip(),
                ).casefold()
                if (
                    prior_time != event_time
                    or prior_home != current_home
                    or prior_away != current_away
                ):
                    ambiguous_event_ids.add(event_id)
                continue
            events[event_id] = event

        rows = []
        seen_keys: set[tuple[str, str, str, str]] = set()
        for event_id, event in events.items():
            if event_id in ambiguous_event_ids:
                continue
            try:
                stats_result = await fetch_event_stats(
                    official_sport,
                    event_id,
                    stat_type=requested_stat,
                )
            except Exception as exc:
                logger.debug(
                    "PlayerHistoryProvider: PropLine event stats failed "
                    "%s / %s event=%s: %s",
                    player_name,
                    stat_type,
                    event_id,
                    exc,
                )
                continue
            stats_payload = _payload(stats_result)
            if not isinstance(stats_payload, dict):
                continue
            stats_event_id = _clean(
                _field(stats_payload, "id", "event_id", "game_id", "match_id")
            )
            if stats_event_id != event_id:
                continue
            if not _sport_matches(
                _field(stats_payload, "sport_key", "sport", "league"),
                official_sport,
                sport_u,
            ):
                continue
            stats_status = _clean(
                _field(stats_payload, "status", "state", "event_status", "game_status")
            ).lower()
            if stats_status not in final_statuses:
                continue
            event_time = _parse_datetime(
                _field(event, "commence_time", "start_time", "event_start", "game_time")
            )
            stats_time = _parse_datetime(
                _field(stats_payload, "commence_time", "start_time", "event_start", "game_time")
            )
            if event_time is None or (stats_time is not None and stats_time.date() != event_time.date()):
                continue

            players = _records(stats_payload, "players")
            matching_players = []
            for player in players:
                raw_player = _field(
                    player, "name", "player_name", "display_name", "full_name",
                    "player", "athlete",
                )
                if isinstance(raw_player, dict):
                    raw_player = _field(
                        raw_player, "name", "display_name", "full_name"
                    )
                normalized_player = re.sub(
                    r"\s+", " ", str(raw_player or "").strip()
                ).casefold()
                if normalized_player == requested_player:
                    matching_players.append(player)
            if len(matching_players) != 1:
                continue

            player = matching_players[0]
            stats = _field(player, "stats", "statistics", "values")
            if not isinstance(stats, (dict, list, tuple)):
                continue
            value = propline_raw_player_game_value(
                sport_u,
                stat_type,
                stats,
            )
            if value is None:
                continue
            key = (event_id, event_time.date().isoformat(), requested_player, requested_stat)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            opponent = _field(player, "opponent", "opponent_name")
            if opponent is None:
                team = _clean(_field(player, "team", "player_team"))
                home = _clean(_field(event, "home_team", "home", "home_name"))
                away = _clean(_field(event, "away_team", "away", "away_name"))
                normalized_team = team.casefold()
                if normalized_team and normalized_team == home.casefold():
                    opponent = away or None
                elif normalized_team and normalized_team == away.casefold():
                    opponent = home or None

            rows.append(RawGameResult(
                player_name=player_name,
                sport=sport_u,
                stat_type=stat_type.lower().strip(),
                game_date=event_time.date().isoformat(),
                actual_value=value,
                opponent=str(opponent).strip() if opponent else None,
                source="propline_event_stats",
                game_id=event_id,
            ))
        return rows

    async def _fetch_propline_history(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        source_provider: str = "Underdog",
        instrumentation=None,
        coverage: Optional[dict[str, str]] = None,
    ) -> list:
        """Adapt official PropLine resolved history into game-result rows."""
        from providers.propline import (
            PropLineClient,
            official_player_market_for,
        )

        if self._propline_client is None:
            self._propline_client = PropLineClient()
        client = self._propline_client
        if not getattr(client, "is_configured", False):
            if coverage is not None:
                coverage.update(
                    status=PROPLINE_COVERAGE_UNAVAILABLE,
                    sport=str(sport or "").upper().strip(),
                )
            return []
        try:
            official_sport = client.official_sport_for(sport)
        except Exception as exc:
            if coverage is not None:
                coverage.update(
                    status=PROPLINE_COVERAGE_UNAVAILABLE,
                    failure_kind="configuration",
                    error=str(exc),
                    sport=str(sport or "").upper().strip(),
                )
            return []
        if not official_sport:
            # The official catalog is authoritative for provider-specific
            # sports; never guess an endpoint key for DOTA or another sport.
            try:
                discovered = await client.discover_sports()
            except Exception as exc:
                if coverage is not None:
                    coverage.update(
                        status=PROPLINE_COVERAGE_UNAVAILABLE,
                        failure_kind="discovery",
                        error=str(exc),
                        sport=str(sport or "").upper().strip(),
                    )
                return []
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
            if coverage is not None:
                coverage.update(
                    status=PROPLINE_COVERAGE_UNAVAILABLE,
                    failure_kind="unsupported_sport",
                    sport=str(sport or "").upper().strip(),
                )
            return []

        sport_u = str(sport or "").upper().strip()
        market = official_player_market_for(stat_type, sport=sport_u)
        if not market:
            if coverage is not None:
                coverage.update(
                    status=PROPLINE_COVERAGE_MALFORMED,
                    failure_kind="unknown_market",
                    sport=sport_u,
                    market="unmapped",
                )
            logger.info(
                "PropLine history attempt player=%r sport=%s stat=%s "
                "market=unmapped bookmaker=%s succeeded=false complete=false "
                "redacted=false raw_entries=0 usable_rows=0 failure=unknown_market",
                player_name,
                sport,
                stat_type,
                _propline_bookmaker_for_source(source_provider),
            )
            return []
        bookmaker = _propline_bookmaker_for_source(source_provider)
        try:
            result = await client.fetch_player_history(
                official_sport,
                player_name,
                market,
                bookmaker=bookmaker,
                limit=100,
            )
        except Exception as exc:
            if coverage is not None:
                coverage.update(
                    status=PROPLINE_COVERAGE_UNAVAILABLE,
                    failure_kind="request",
                    error=type(exc).__name__,
                    sport=sport_u,
                    market=market,
                )
            logger.warning(
                "PropLine history attempt player=%r sport=%s stat=%s "
                "market=%s bookmaker=%s status=%s failure=%s error=%s",
                player_name,
                sport,
                stat_type,
                market,
                bookmaker or "all",
                PROPLINE_COVERAGE_UNAVAILABLE,
                "request",
                type(exc).__name__,
            )
            return []
        raw_entries = len(getattr(result, "entries", ()) or ())
        if not result.succeeded or not result.complete:
            failure_kind = str(getattr(result, "failure_kind", "") or "")
            status = (
                PROPLINE_COVERAGE_REDACTED
                if bool(getattr(result, "redacted", False))
                else (
                    PROPLINE_COVERAGE_MALFORMED
                    if failure_kind == "incomplete"
                    else PROPLINE_COVERAGE_UNAVAILABLE
                )
            )
            if coverage is not None:
                coverage.update(
                    status=status,
                    failure_kind=failure_kind,
                    error=str(getattr(result, "error", "") or ""),
                    sport=sport_u,
                    market=market,
                )
            _log_propline_history_attempt(
                player_name,
                sport,
                stat_type,
                market,
                bookmaker,
                result,
                raw_entries=raw_entries,
                usable_rows=0,
            )
            return []

        # A complete response can still contain only unresolved rows (or be
        # redacted by the provider). Neither case is completed-game evidence.
        # Keep those outcomes distinct from malformed HTTP/schema responses so
        # coverage reports explain whether the feed was unavailable or gated.
        if bool(getattr(result, "redacted", False)):
            if coverage is not None:
                coverage.update(
                    status=PROPLINE_COVERAGE_REDACTED,
                    failure_kind="redacted",
                    sport=sport_u,
                    market=market,
                )
            _log_propline_history_attempt(
                player_name,
                sport,
                stat_type,
                market,
                bookmaker,
                result,
                raw_entries=raw_entries,
                usable_rows=0,
                status=PROPLINE_COVERAGE_REDACTED,
            )
            return []

        rows = propline_player_history_rows(
            result.entries,
            player_name=player_name,
            sport=sport_u,
            stat_type=stat_type,
        )
        if coverage is not None:
            coverage.update(
                status=(
                    PROPLINE_COVERAGE_USABLE
                    if rows
                    else PROPLINE_COVERAGE_UNAVAILABLE
                ),
                failure_kind="" if rows else "no_settled_entries",
                error="",
                sport=sport_u,
                market=market,
            )
        _log_propline_history_attempt(
            player_name,
            sport,
            stat_type,
            market,
            bookmaker,
            result,
            raw_entries=raw_entries,
            usable_rows=len(rows),
            status=(
                PROPLINE_COVERAGE_USABLE
                if rows
                else PROPLINE_COVERAGE_UNAVAILABLE
            ),
            failure_kind_override="no_settled_entries" if not rows else "",
        )
        return rows

    async def _fetch_propline_games(
        self,
        player_name: str,
        sport: str,
        stat_type: str,
        *,
        instrumentation=None,
    ) -> list:
        """Adapt complete PropLine raw game logs into supplemental rows.

        Raw game logs are only evidence after a final/completed status. In
        particular, live cumulative values must never be persisted as a
        completed game result.
        """
        from providers.player_stats import RawGameResult
        from providers.propline import (
            PropLineClient,
            _parse_datetime,
            propline_raw_player_game_value,
            _team_names_match,
        )

        sport_u = str(sport or "").upper().strip()
        if not sport_u:
            return []
        if self._propline_client is None:
            self._propline_client = PropLineClient()
        client = self._propline_client
        if not client.is_configured:
            return []
        official_sport = client.official_sport_for(sport_u)
        if not official_sport:
            return []
        fetch_games = getattr(client, "fetch_player_games", None)
        if not callable(fetch_games):
            # Keep compatibility with lightweight test/integration clients
            # that only implement the older resolved-history capability.
            return []
        result = await fetch_games(
            official_sport,
            player_name,
            limit=100,
            stat_type=stat_type,
        )
        if not result.succeeded or not result.complete:
            return []
        payload = result.payload
        if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
            return []

        rows = []
        for game in payload["games"]:
            if not isinstance(game, dict):
                continue
            status = str(game.get("status") or "").strip().lower()
            if status not in {"final", "completed", "complete", "settled"}:
                continue
            game_id = str(
                game.get("event_id") or game.get("game_id") or game.get("id") or ""
            ).strip()
            game_time = _parse_datetime(
                game.get("commence_time")
                or game.get("start_time")
                or game.get("game_time")
            )
            stats = game.get("stats", game.get("statistics", game.get("player_stats")))
            if not game_id or game_time is None:
                continue
            value = propline_raw_player_game_value(sport_u, stat_type, stats)
            if value is None:
                continue
            opponent = game.get("opponent")
            if opponent is None:
                opponent = game.get("opponent_name")
            if opponent is None:
                team = (
                    game.get("team")
                    or game.get("player_team")
                    or game.get("team_name")
                )
                home = (
                    game.get("home_team")
                    or game.get("home")
                    or game.get("home_name")
                )
                away = (
                    game.get("away_team")
                    or game.get("away")
                    or game.get("away_name")
                )
                if _team_names_match(team, home):
                    opponent = away
                elif _team_names_match(team, away):
                    opponent = home
            rows.append(RawGameResult(
                player_name=player_name,
                sport=sport_u,
                stat_type=stat_type.lower().strip(),
                game_date=game_time.date().isoformat(),
                actual_value=value,
                opponent=str(opponent).strip() if opponent else None,
                source="propline_player_games",
                game_id=game_id,
            ))
            logger.debug(
                "PlayerHistoryProvider: SPORT=%s PLAYER=%s GAME/EVENT=%s "
                "MARKET=%s PROVIDER_RESULT=%s NORMALIZED_RESULT=%.3f",
                sport_u,
                player_name,
                game_id,
                stat_type,
                "PropLine",
                value,
            )
        return rows


def _merge_game_results(existing: list, supplemental: list) -> list:
    """Keep primary game results and add only distinct PropLine events."""
    return _merge_distinct_rows(existing, supplemental)


def _propline_bookmaker_for_source(source_provider: str) -> str:
    """Map the evaluated pick source to PropLine's bookmaker namespace."""
    normalized = str(source_provider or "").strip().lower().replace("_", "")
    if normalized in {"prizepicks", "prizepick"}:
        return "prizepicks"
    if normalized in {"underdog", "underdogfantasy"}:
        return "underdog"
    return ""


def _log_propline_history_attempt(
    player_name: str,
    sport: str,
    stat_type: str,
    market: str,
    bookmaker: str,
    result,
    *,
    raw_entries: int,
    usable_rows: int,
    status: str = "",
    failure_kind_override: str = "",
) -> None:
    """Make entitlement and shape failures visible without logging secrets."""
    redacted = bool(getattr(result, "redacted", False))
    succeeded = bool(getattr(result, "succeeded", False))
    complete = bool(getattr(result, "complete", False))
    failure_kind = str(
        failure_kind_override
        or getattr(result, "failure_kind", "")
        or ""
    )
    error = str(getattr(result, "error", "") or "")
    upgrade_url = str(getattr(result, "upgrade_url", "") or "")
    logger.log(
        logging.WARNING if redacted or not succeeded or not complete else logging.INFO,
        "PropLine history attempt player=%r sport=%s stat=%s market=%s "
        "bookmaker=%s request_succeeded=%s complete=%s redacted=%s raw_entries=%d "
        "usable_rows=%d status=%s failure=%s error=%s upgrade_url=%s",
        player_name,
        sport,
        stat_type,
        market,
        bookmaker or "all",
        succeeded,
        complete,
        redacted,
        raw_entries,
        usable_rows,
        status or "unknown",
        failure_kind or "none",
        error or "none",
        upgrade_url or "none",
    )


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