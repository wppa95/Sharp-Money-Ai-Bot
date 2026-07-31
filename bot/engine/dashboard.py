"""
engine/dashboard.py — Performance Dashboard aggregation engine.

Gathers statistics across all alert tables and produces a DashboardReport
suitable for the /dashboard Telegram command.

This module is intentionally read-only: it never writes to the database
and never fires alerts.  All I/O goes through the Database class.

Aggregated sources
------------------
    ev_records          → total EV alerts, avg EV%, avg CLV, win rate, by-sport/market
    steam_records       → total steam alerts, avg steam score
    underdog_snapshots  → total UD alerts, tier breakdown (S/A/B), by-sport
    pp_edge_records     → total PP alerts, tier breakdown
    clv_records         → avg CLV%, beat-close rate
    AlertCLVSeed        → pending CLV seeds (seeded but not yet computed)

Public API
----------
    DashboardEngine.gather(db)  →  DashboardReport   (async)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Database


# ── Component dataclasses ──────────────────────────────────────────────────────

@dataclass
class TierPerf:
    """Performance stats for one confidence tier (S / A / B / PASS)."""
    tier:      str
    count:     int
    avg_edge:  Optional[float] = None   # avg best_edge from pp_edge_records
    avg_clv:   Optional[float] = None   # avg clv_pct from clv_records (if linked)
    hit_rate:  Optional[float] = None   # resolved WIN / (WIN+LOSS)

    @property
    def tier_emoji(self) -> str:
        return {"S": "🔥", "A": "🟢", "B": "🟡", "PASS": "⚪"}.get(self.tier, "⚪")


@dataclass
class SportPerf:
    """Alert performance broken down by sport."""
    sport:     str
    ev_count:  int   = 0
    ud_count:  int   = 0
    pp_count:  int   = 0
    avg_ev:    Optional[float] = None
    avg_clv:   Optional[float] = None

    @property
    def total(self) -> int:
        return self.ev_count + self.ud_count + self.pp_count


@dataclass
class MarketPerf:
    """Alert performance broken down by market type."""
    market:    str
    count:     int   = 0
    avg_ev:    Optional[float] = None
    win_rate:  Optional[float] = None


@dataclass
class DailyTrend:
    """Alert counts for one UTC day."""
    date_str:    str     # "YYYY-MM-DD"
    ev_count:    int = 0
    ud_count:    int = 0
    steam_count: int = 0
    pp_count:    int = 0

    @property
    def total(self) -> int:
        return self.ev_count + self.ud_count + self.steam_count + self.pp_count


@dataclass
class DashboardReport:
    """
    Full performance dashboard report.

    Produced by DashboardEngine.gather().  All Optional fields are None when
    there is insufficient data (e.g. no CLV records yet).
    """
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ── Overall totals ────────────────────────────────────────────────────────
    total_ev_alerts:    int = 0
    total_steam_alerts: int = 0
    total_ud_alerts:    int = 0
    total_pp_alerts:    int = 0
    total_clv_records:  int = 0

    # ── Today's counts ────────────────────────────────────────────────────────
    today_ud_alerts:    int = 0
    today_pp_alerts:    int = 0

    # ── EV performance ────────────────────────────────────────────────────────
    avg_ev_pct:         Optional[float] = None   # avg expected_value from ev_records
    ev_win_rate:        Optional[float] = None   # WIN / (WIN+LOSS) for ev_records
    ev_wins:            int = 0
    ev_losses:          int = 0
    ev_pushes:          int = 0

    # ── CLV performance ───────────────────────────────────────────────────────
    avg_clv_pct:        Optional[float] = None   # avg clv_pct from clv_records
    clv_beat_close_rate:Optional[float] = None   # fraction with clv_pct > 0
    clv_seeds_pending:  int = 0                  # seeds not yet harvested

    # ── Underdog breakdown ────────────────────────────────────────────────────
    ud_tier_breakdown:  dict[str, int] = field(default_factory=dict)
    ud_avg_score:       Optional[float] = None   # avg score_total for alerted UDs

    # ── Sport / market breakdowns ─────────────────────────────────────────────
    by_sport:           list[SportPerf]  = field(default_factory=list)
    by_market:          list[MarketPerf] = field(default_factory=list)

    # ── Historical trend (last 7 days) ────────────────────────────────────────
    daily_trend:        list[DailyTrend] = field(default_factory=list)

    # ── Best / worst ──────────────────────────────────────────────────────────
    best_sport:         Optional[str] = None
    worst_sport:        Optional[str] = None
    best_market:        Optional[str] = None

    # ── Formatting ────────────────────────────────────────────────────────────

    @property
    def total_all_alerts(self) -> int:
        return (
            self.total_ev_alerts
            + self.total_steam_alerts
            + self.total_ud_alerts
            + self.total_pp_alerts
        )

    def to_telegram(self) -> str:
        """Render the full dashboard as Telegram HTML (safe for message.reply_text)."""
        ts = self.generated_at.strftime("%b %d, %Y  %H:%M UTC")
        parts: list[str] = [
            f"📊 <b>Performance Dashboard</b>",
            f"<i>{ts}</i>",
            "",
        ]

        # ── Overall alert totals ──────────────────────────────────────────────
        parts += [
            "📬 <b>All-Time Alerts</b>",
            f"  EV:       <b>{self.total_ev_alerts:,}</b>",
            f"  Steam:    <b>{self.total_steam_alerts:,}</b>",
            f"  Underdog: <b>{self.total_ud_alerts:,}</b>",
            f"  PP:       <b>{self.total_pp_alerts:,}</b>",
            f"  Total:    <b>{self.total_all_alerts:,}</b>",
            "",
        ]

        # ── EV performance ────────────────────────────────────────────────────
        parts.append("💰 <b>EV Alert Performance</b>")
        if self.avg_ev_pct is not None:
            sign = "+" if self.avg_ev_pct >= 0 else ""
            parts.append(f"  Avg EV:   <code>{sign}{self.avg_ev_pct:.2f}%</code>")
        if self.ev_win_rate is not None:
            parts.append(
                f"  Win rate: <code>{self.ev_win_rate * 100:.1f}%</code>"
                f"  W/L/P: <code>{self.ev_wins}/{self.ev_losses}/{self.ev_pushes}</code>"
            )
        else:
            parts.append("  Win rate: <i>awaiting resolved results</i>")
        parts.append("")

        # ── CLV performance ───────────────────────────────────────────────────
        parts.append("💎 <b>Closing Line Value</b>")
        if self.total_clv_records > 0:
            sign = "+" if (self.avg_clv_pct or 0) >= 0 else ""
            bc   = f"{self.clv_beat_close_rate * 100:.0f}%" if self.clv_beat_close_rate is not None else "—"
            parts += [
                f"  Records:   <code>{self.total_clv_records}</code>",
                f"  Avg CLV:   <code>{sign}{self.avg_clv_pct:.2f}%</code>",
                f"  Beat close:<code>{bc}</code>",
            ]
        else:
            parts.append("  <i>No CLV records yet — computed when markets close</i>")
        if self.clv_seeds_pending > 0:
            parts.append(f"  Pending:   <code>{self.clv_seeds_pending}</code> seeds awaiting close")
        parts.append("")

        # ── Underdog tier breakdown ───────────────────────────────────────────
        if self.total_ud_alerts > 0:
            parts.append("🐶 <b>Underdog Tier Breakdown</b>")
            tier_order = ["S", "A", "B", "PASS"]
            tier_emojis = {"S": "🔥", "A": "🟢", "B": "🟡", "PASS": "⚪"}
            for t in tier_order:
                n = self.ud_tier_breakdown.get(t, 0)
                if n:
                    pct = n / self.total_ud_alerts * 100
                    parts.append(
                        f"  {tier_emojis.get(t, '⚪')} {t:<4} "
                        f"<code>{n:>4}</code>  ({pct:.0f}%)"
                    )
            if self.ud_avg_score is not None:
                parts.append(f"  Avg score: <code>{self.ud_avg_score:.0f}/100</code>")
            parts.append("")

        # ── By sport ──────────────────────────────────────────────────────────
        active_sports = [s for s in self.by_sport if s.total >= 1]
        if active_sports:
            parts.append("🏆 <b>By Sport</b>")
            for sp in sorted(active_sports, key=lambda s: s.total, reverse=True)[:8]:
                ev_str  = f"  EV {sp.avg_ev:+.1f}%"  if sp.avg_ev  is not None else ""
                clv_str = f"  CLV {sp.avg_clv:+.1f}%" if sp.avg_clv is not None else ""
                parts.append(
                    f"  {sp.sport:<10} n=<code>{sp.total:>3}</code>"
                    f"{ev_str}{clv_str}"
                )
            if self.best_sport:
                parts.append(f"  🥇 Best: <b>{self.best_sport}</b>")
            if self.worst_sport and self.worst_sport != self.best_sport:
                parts.append(f"  📉 Worst: <b>{self.worst_sport}</b>")
            parts.append("")

        # ── By market ─────────────────────────────────────────────────────────
        active_markets = [m for m in self.by_market if m.count >= 1]
        if active_markets:
            parts.append("🏷 <b>By Market</b>")
            for m in sorted(active_markets, key=lambda m: m.count, reverse=True)[:6]:
                ev_str = f"  avg EV {m.avg_ev:+.1f}%" if m.avg_ev is not None else ""
                parts.append(
                    f"  {m.market:<18} n=<code>{m.count:>3}</code>{ev_str}"
                )
            if self.best_market:
                parts.append(f"  🥇 Best market: <b>{self.best_market}</b>")
            parts.append("")

        # ── 7-day trend ───────────────────────────────────────────────────────
        trend_days = [d for d in self.daily_trend if d.total > 0]
        if trend_days:
            parts.append("📅 <b>Last 7 Days</b>")
            for d in trend_days[-7:]:
                bar = "▪" * min(d.total, 10)
                parts.append(
                    f"  {d.date_str}  <code>{d.total:>3}</code>  {bar}"
                )

        return "\n".join(parts)


# ── Engine ─────────────────────────────────────────────────────────────────────

class DashboardEngine:
    """
    Stateless dashboard aggregation engine.

    All heavy lifting is async DB I/O — call ``gather()`` from an async context
    (e.g. inside a Telegram command handler).
    """

    @classmethod
    async def gather(cls, db: "Database") -> DashboardReport:
        """
        Query all relevant tables and produce a DashboardReport.

        Never raises — individual query failures are caught and treated as
        zero / None for that metric so the rest of the dashboard still renders.
        """
        report = DashboardReport()

        # Run all independent queries (best-effort)
        await cls._gather_totals(db, report)
        await cls._gather_ev_performance(db, report)
        await cls._gather_clv_performance(db, report)
        await cls._gather_ud_breakdown(db, report)
        await cls._gather_by_sport(db, report)
        await cls._gather_by_market(db, report)
        await cls._gather_daily_trend(db, report)
        cls._compute_best_worst(report)

        return report

    # ── Query helpers ──────────────────────────────────────────────────────────

    @staticmethod
    async def _gather_totals(db: "Database", r: DashboardReport) -> None:
        try:
            r.total_ev_alerts    = await db.count_ev_records()
            r.total_steam_alerts = await db.count_steam_records()
            r.total_ud_alerts    = await db.count_underdog_records()
            r.total_pp_alerts    = await db.count_pp_edge_records()
            r.total_clv_records  = await db.count_clv_records()
            r.today_ud_alerts    = await db.count_today_underdog_alerts()
            r.today_pp_alerts    = await db.count_today_pp_alerts()
        except Exception:
            pass

    @staticmethod
    async def _gather_ev_performance(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import EVRecord
            async with db.session() as s:
                # Average EV across all alerted records
                res = await s.execute(
                    select(func.avg(EVRecord.expected_value))
                    .where(EVRecord.alert_sent == True)  # noqa: E712
                )
                r.avg_ev_pct = res.scalar()

                # Win/loss/push breakdown
                res2 = await s.execute(
                    select(EVRecord.result, func.count())
                    .where(
                        EVRecord.result.in_(["WIN", "LOSS", "PUSH"]),
                        EVRecord.alert_sent == True,  # noqa: E712
                    )
                    .group_by(EVRecord.result)
                )
                for row in res2.all():
                    result_val, cnt = row
                    if result_val == "WIN":
                        r.ev_wins = cnt
                    elif result_val == "LOSS":
                        r.ev_losses = cnt
                    elif result_val == "PUSH":
                        r.ev_pushes = cnt

            denom = r.ev_wins + r.ev_losses
            if denom >= 5:
                r.ev_win_rate = r.ev_wins / denom
        except Exception:
            pass

    @staticmethod
    async def _gather_clv_performance(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import CLVRecord
            async with db.session() as s:
                res = await s.execute(
                    select(
                        func.avg(CLVRecord.clv_pct),
                        func.count(),
                    )
                )
                row = res.one_or_none()
                if row and row[1]:
                    r.avg_clv_pct = row[0]

                # Beat-close rate
                beat_res = await s.execute(
                    select(func.count())
                    .select_from(CLVRecord)
                    .where(CLVRecord.clv_pct > 0)
                )
                beaten = beat_res.scalar() or 0
                if r.total_clv_records > 0:
                    r.clv_beat_close_rate = beaten / r.total_clv_records
        except Exception:
            pass

        # Pending seeds
        try:
            r.clv_seeds_pending = await db.count_pending_clv_seeds()
        except Exception:
            pass

    @staticmethod
    async def _gather_ud_breakdown(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import UnderdogSnapshotRecord
            async with db.session() as s:
                # Tier breakdown for alerted snapshots only
                res = await s.execute(
                    select(UnderdogSnapshotRecord.score_tier, func.count())
                    .where(
                        UnderdogSnapshotRecord.alert_sent == True,  # noqa: E712
                        UnderdogSnapshotRecord.score_tier.isnot(None),
                    )
                    .group_by(UnderdogSnapshotRecord.score_tier)
                )
                for row in res.all():
                    tier_val, cnt = row
                    if tier_val:
                        r.ud_tier_breakdown[tier_val] = cnt

                # Avg score for alerted snapshots
                score_res = await s.execute(
                    select(func.avg(UnderdogSnapshotRecord.score_total))
                    .where(
                        UnderdogSnapshotRecord.alert_sent == True,  # noqa: E712
                        UnderdogSnapshotRecord.score_total.isnot(None),
                    )
                )
                r.ud_avg_score = score_res.scalar()
        except Exception:
            pass

    @staticmethod
    async def _gather_by_sport(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import EVRecord, UnderdogSnapshotRecord, PPEdgeRecord

            sport_map: dict[str, SportPerf] = {}

            def _get(sport: str) -> SportPerf:
                if sport not in sport_map:
                    sport_map[sport] = SportPerf(sport=sport)
                return sport_map[sport]

            async with db.session() as s:
                # EV alerts by sport
                ev_res = await s.execute(
                    select(EVRecord.sport, func.count(), func.avg(EVRecord.expected_value))
                    .where(EVRecord.alert_sent == True)  # noqa: E712
                    .group_by(EVRecord.sport)
                )
                for sport_val, cnt, avg_ev in ev_res.all():
                    if sport_val:
                        sp = _get(sport_val)
                        sp.ev_count = cnt
                        sp.avg_ev   = round(avg_ev, 2) if avg_ev is not None else None

                # UD alerts by sport
                ud_res = await s.execute(
                    select(UnderdogSnapshotRecord.sport, func.count())
                    .where(UnderdogSnapshotRecord.alert_sent == True)  # noqa: E712
                    .group_by(UnderdogSnapshotRecord.sport)
                )
                for sport_val, cnt in ud_res.all():
                    if sport_val:
                        _get(sport_val).ud_count = cnt

                # PP alerts by sport
                pp_res = await s.execute(
                    select(PPEdgeRecord.sport, func.count())
                    .where(PPEdgeRecord.alert_sent == True)  # noqa: E712
                    .group_by(PPEdgeRecord.sport)
                )
                for sport_val, cnt in pp_res.all():
                    if sport_val:
                        _get(sport_val).pp_count = cnt

            r.by_sport = sorted(sport_map.values(), key=lambda s: s.total, reverse=True)
        except Exception:
            pass

    @staticmethod
    async def _gather_by_market(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import EVRecord

            async with db.session() as s:
                res = await s.execute(
                    select(
                        EVRecord.market_type,
                        func.count(),
                        func.avg(EVRecord.expected_value),
                    )
                    .where(EVRecord.alert_sent == True)  # noqa: E712
                    .group_by(EVRecord.market_type)
                )
                mkt_map: dict[str, MarketPerf] = {}
                for mkt, cnt, avg_ev in res.all():
                    if mkt:
                        mkt_map[mkt] = MarketPerf(
                            market  = mkt,
                            count   = cnt,
                            avg_ev  = round(avg_ev, 2) if avg_ev is not None else None,
                        )

                # Win rate per market (where results exist)
                wr_res = await s.execute(
                    select(EVRecord.market_type, EVRecord.result, func.count())
                    .where(EVRecord.result.in_(["WIN", "LOSS"]))
                    .group_by(EVRecord.market_type, EVRecord.result)
                )
                wins_by_mkt: dict[str, int]   = {}
                loss_by_mkt: dict[str, int]   = {}
                for mkt, res_val, cnt in wr_res.all():
                    if res_val == "WIN":
                        wins_by_mkt[mkt] = wins_by_mkt.get(mkt, 0) + cnt
                    else:
                        loss_by_mkt[mkt] = loss_by_mkt.get(mkt, 0) + cnt

                for mkt, mp in mkt_map.items():
                    w = wins_by_mkt.get(mkt, 0)
                    l = loss_by_mkt.get(mkt, 0)
                    if w + l >= 5:
                        mp.win_rate = w / (w + l)

            r.by_market = sorted(mkt_map.values(), key=lambda m: m.count, reverse=True)
        except Exception:
            pass

    @staticmethod
    async def _gather_daily_trend(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import EVRecord, UnderdogSnapshotRecord, SteamRecord, PPEdgeRecord

            now  = datetime.utcnow()
            days: list[DailyTrend] = []

            async with db.session() as s:
                for delta in range(6, -1, -1):
                    day_start = (now - timedelta(days=delta)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    day_end   = day_start + timedelta(days=1)
                    date_str  = day_start.strftime("%m/%d")

                    dt = DailyTrend(date_str=date_str)

                    ev_res = await s.execute(
                        select(func.count())
                        .select_from(EVRecord)
                        .where(
                            EVRecord.alert_sent == True,  # noqa: E712
                            EVRecord.detected_at >= day_start,
                            EVRecord.detected_at <  day_end,
                        )
                    )
                    dt.ev_count = ev_res.scalar() or 0

                    ud_res = await s.execute(
                        select(func.count())
                        .select_from(UnderdogSnapshotRecord)
                        .where(
                            UnderdogSnapshotRecord.alert_sent == True,  # noqa: E712
                            UnderdogSnapshotRecord.fetched_at >= day_start,
                            UnderdogSnapshotRecord.fetched_at <  day_end,
                        )
                    )
                    dt.ud_count = ud_res.scalar() or 0

                    st_res = await s.execute(
                        select(func.count())
                        .select_from(SteamRecord)
                        .where(
                            SteamRecord.alert_sent == True,  # noqa: E712
                            SteamRecord.detected_at >= day_start,
                            SteamRecord.detected_at <  day_end,
                        )
                    )
                    dt.steam_count = st_res.scalar() or 0

                    pp_res = await s.execute(
                        select(func.count())
                        .select_from(PPEdgeRecord)
                        .where(
                            PPEdgeRecord.alert_sent == True,  # noqa: E712
                            PPEdgeRecord.detected_at >= day_start,
                            PPEdgeRecord.detected_at <  day_end,
                        )
                    )
                    dt.pp_count = pp_res.scalar() or 0

                    days.append(dt)

            r.daily_trend = days
        except Exception:
            pass

    @staticmethod
    def _compute_best_worst(r: DashboardReport) -> None:
        """Derive best/worst sport from gathered data."""
        if r.by_sport:
            # Best = most active sport with the highest avg EV (fallback: total count)
            sports_with_ev = [s for s in r.by_sport if s.avg_ev is not None]
            if sports_with_ev:
                r.best_sport  = max(sports_with_ev, key=lambda s: s.avg_ev).sport  # type: ignore[arg-type]
                r.worst_sport = min(sports_with_ev, key=lambda s: s.avg_ev).sport  # type: ignore[arg-type]
            else:
                r.best_sport = r.by_sport[0].sport if r.by_sport else None

        if r.by_market:
            mkts_with_ev = [m for m in r.by_market if m.avg_ev is not None]
            if mkts_with_ev:
                r.best_market = max(mkts_with_ev, key=lambda m: m.avg_ev).market  # type: ignore[arg-type]
