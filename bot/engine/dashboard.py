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

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


# ── Component dataclasses ──────────────────────────────────────────────────────

@dataclass
class TierPerf:
    """Legacy confidence-tier performance container."""
    tier:      str
    count:     int
    avg_edge:  Optional[float] = None   # avg best_edge from pp_edge_records
    avg_clv:   Optional[float] = None   # avg clv_pct from clv_records (if linked)
    hit_rate:  Optional[float] = None   # resolved WIN / (WIN+LOSS)

    @property
    def tier_emoji(self) -> str:
        return {"S": "🔥", "A": "🟢", "B": "🟡"}.get(self.tier, "⚪")


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
    """Alert counts for one canonical reporting day."""
    date_str:    str     # "YYYY-MM-DD"
    ev_count:    int = 0
    ud_count:    int = 0
    steam_count: int = 0
    pp_count:    int = 0

    @property
    def total(self) -> int:
        return self.ev_count + self.ud_count + self.steam_count + self.pp_count


@dataclass
class SystemPanel:
    """System health and operational status for the dashboard."""
    uptime_str:      str                 = "unknown"
    crash_count:     int                 = 0
    last_restart:    Optional[str]       = None   # ISO datetime string
    provider_status: dict                = field(default_factory=dict)  # name → "OK"/"DEGRADED"/"DOWN"
    active_blocks:   int                 = 0   # active player reliability blocks
    db_size_mb:      Optional[float]     = None


@dataclass
class IntelligencePanel:
    """Model learning and intelligence performance summary for the dashboard."""
    total_graded:      int             = 0
    total_hits:        int             = 0
    total_misses:      int             = 0
    hit_rate:          Optional[float] = None
    model_errors:      int             = 0
    market_errors:     int             = 0
    settlement_errors: int             = 0
    variance_errors:   int             = 0
    model_error_rate:  Optional[float] = None    # model_errors / total_misses
    strongest_sport:   Optional[str]   = None
    weakest_sport:     Optional[str]   = None
    strongest_market:  Optional[str]   = None
    provider_breakdown: dict           = field(default_factory=dict)


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
    # Constructor compatibility for older callers; never populated or
    # rendered as a monitoring classification in the v3.6 dashboard.
    ud_tier_breakdown:  dict = field(default_factory=dict, repr=False)
    total_pp_alerts:    int = 0
    total_clv_records:  int = 0

    # ── Today's counts ────────────────────────────────────────────────────────
    today_ud_alerts:    int = 0
    today_pp_alerts:    int = 0
    # Persistent monitoring stream, intentionally independent of delivery.
    recent_tracked_props: list = field(default_factory=list)
    # Reporting populations are kept separate: monitoring observations,
    # directional evaluations, Telegram deliveries, and settlement state.
    tracked_observations: int = 0
    active_tracked_props: int = 0
    directional_opportunities: int = 0
    delivered_opportunities: int = 0
    pending_opportunities: int = 0
    resolved_opportunities: int = 0
    outcome_hits: int = 0
    outcome_misses: int = 0
    outcome_pushes: int = 0
    tracking_by_provider: dict = field(default_factory=dict)
    tracking_by_sport: dict = field(default_factory=dict)
    provider_pipeline: dict = field(default_factory=dict)
    # A failed read must remain distinguishable from a real empty population.
    # Keys are report sections such as tracking, totals, ev, clv, or sport.
    unavailable: set[str] = field(default_factory=set, repr=False)

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

    # ── Underdog monitoring stream ───────────────────────────────────────────
    ud_tracked_snapshots: int = 0
    ud_avg_score:       Optional[float] = None

    # ── Sport / market breakdowns ─────────────────────────────────────────────
    by_sport:           list[SportPerf]  = field(default_factory=list)
    by_market:          list[MarketPerf] = field(default_factory=list)

    # ── Historical trend (last 7 days) ────────────────────────────────────────
    daily_trend:        list[DailyTrend] = field(default_factory=list)

    # ── Best / worst ──────────────────────────────────────────────────────────
    best_sport:         Optional[str] = None
    worst_sport:        Optional[str] = None
    best_market:        Optional[str] = None

    # ── System panel (v3.0 upgrade) ───────────────────────────────────────────
    system_panel:       Optional[SystemPanel]      = None

    # ── Intelligence panel (v3.0 upgrade) ─────────────────────────────────────
    intelligence_panel: Optional[IntelligencePanel] = None

    # ── Formatting ────────────────────────────────────────────────────────────

    @property
    def total_all_alerts(self) -> int:
        return (
            self.total_ev_alerts
            + self.total_steam_alerts
            + self.total_ud_alerts
            + self.total_pp_alerts
        )

    def _normalized_activity(self, provider: str, legacy_total: int) -> int:
        """Return normalized provider activity, falling back for old reports."""
        values = self.tracking_by_provider.get(provider)
        if not isinstance(values, dict):
            return int(legacy_total or 0)
        if "observed" in values:
            return int(values.get("observed") or 0)
        if "active" in values:
            return int(values.get("active") or 0)
        return int(legacy_total or 0)

    def to_telegram(self) -> str:
        """Render the full dashboard as Telegram HTML (safe for message.reply_text)."""
        ts = self.generated_at.strftime("%b %d, %Y  %H:%M UTC")
        tracking_unavailable = "tracking" in self.unavailable

        def _count(value: object, *, section: str = "tracking") -> str:
            if section in self.unavailable:
                return "UNAVAILABLE"
            try:
                return f"{int(value or 0):,}"
            except (TypeError, ValueError):
                return "UNAVAILABLE"

        parts: list[str] = [
            f"📊 <b>Performance Dashboard</b>",
            f"<i>{ts}</i>",
            "",
        ]

        # Activity Summary is intentionally consolidated into this one
        # normalized, provider-separated pipeline section.
        # ── Single provider pipeline (normalized, provider-separated) ───────
        # This is the only dashboard presentation of provider pipeline
        # populations. ``provider_pipeline`` is populated by gather(); the
        # tracking map remains a compatibility fallback for older callers.
        parts += [
            "📡 <b>Provider Pipeline</b>  <i>(normalized)</i>",
            "",
        ]
        for provider, legacy_total, today_total in (
            ("Underdog", self.total_ud_alerts, self.today_ud_alerts),
            ("PrizePicks", self.total_pp_alerts, self.today_pp_alerts),
        ):
            values = self.provider_pipeline.get(provider)
            if not isinstance(values, dict):
                values = self.tracking_by_provider.get(provider, {})
            if not isinstance(values, dict):
                values = {}

            status = values.get("status")
            if not status and self.system_panel is not None:
                status = (self.system_panel.provider_status or {}).get(provider)
            status = str(status or "unavailable")
            last_fetch = str(values.get("last_fetch") or "unavailable")
            last_success = str(values.get("last_success") or "unavailable")
            active = values.get("active", values.get("tracked", 0))
            observed = values.get("observed", legacy_total)
            evaluated = values.get("evaluated", 0)
            directional = values.get(
                "directional", values.get("actionable_persisted", 0)
            )
            actionable = values.get(
                "actionable_persisted", values.get("actionable", directional)
            )
            delivered = values.get("delivered", values.get("delivered_alerts", 0))
            pending = values.get("pending", 0)
            resolved = values.get("resolved", 0)
            hit = values.get("hit", 0)
            miss = values.get("miss", 0)
            push = values.get("push", 0)
            metric_section = "tracking" if tracking_unavailable else "ok"
            parts += [
                f"  <b>{provider}</b>",
                f"    Status: <code>{status}</code>"
                f"  ·  Last fetch: <code>{last_fetch}</code>",
                f"    Last successful activity: <code>{last_success}</code>",
                f"    Observed: <code>{_count(observed, section=metric_section)}</code>"
                f"  ·  Active tracked: <code>{_count(active, section=metric_section)}</code>",
                f"    Evaluated: <code>{_count(evaluated, section=metric_section)}</code>"
                f"  ·  Directional/actionable: <code>{_count(directional, section=metric_section)}</code>",
                f"    Actionable persisted: <code>{_count(actionable, section=metric_section)}</code>"
                f"  ·  Delivered: <code>{_count(delivered, section=metric_section)}</code>",
                f"    Pending grading: <code>{_count(pending, section=metric_section)}</code>"
                f"  ·  Resolved: <code>{_count(resolved, section=metric_section)}</code>",
                f"    HIT / MISS / PUSH: <code>{_count(hit, section=metric_section)} / "
                f"{_count(miss, section=metric_section)} / {_count(push, section=metric_section)}</code>"
                f"  ·  Today: <code>{_count(today_total, section='totals' if 'totals' in self.unavailable else 'ok')}</code>",
            ]
        parts.append("")

        if tracking_unavailable:
            parts.append("  By current sport: <i>UNAVAILABLE</i>")
            parts.append("")
        elif self.tracking_by_sport:
            parts.append("  By current sport:")
            for sport, values in sorted(self.tracking_by_sport.items()):
                parts.append(
                    f"    {sport}: active <code>{values.get('active', 0):,}</code>"
                    f" · observed <code>{values.get('observed', 0):,}</code>"
                    f" · directional <code>{values.get('directional', 0):,}</code>"
                    f" · delivered <code>{values.get('delivered', 0):,}</code>"
                )
            parts.append("")

        if self.recent_tracked_props:
            parts.append("🔔 <b>Recently Tracked Props</b>  <i>(last hour · all sports)</i>")
            for prop in self.recent_tracked_props[:5]:
                provider = prop.get("provider", "Underdog")
                source_emoji = "🟣" if provider == "PrizePicks" else "🐶"
                parts.append(
                    f"  {source_emoji} <b>{provider.upper()} PICK</b> · "
                    f"<b>{prop['player_name']}</b>  {prop['stat_type']} "
                    f"<code>{prop['line_value']:.1f}</code> · <i>{prop['sport']}</i>"
                )
            parts.append("")

        # ── CLV performance ───────────────────────────────────────────────────
        parts.append("💎 <b>Closing Line Value</b>")
        if "clv" in self.unavailable:
            parts.append("  <i>CLV data is UNAVAILABLE.</i>")
        elif self.total_clv_records > 0:
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

        # ── By sport ──────────────────────────────────────────────────────────
        # All-time cumulative alert distribution across EV + Underdog + PrizePicks.
        # Differs from Today count — shows historical model performance by sport.
        if "sport" in self.unavailable:
            parts += ["🏆 <b>By Sport</b>", "  <i>UNAVAILABLE.</i>", ""]
        active_sports = [s for s in self.by_sport if s.total >= 1]
        if "sport" not in self.unavailable and active_sports:
            parts.append("🏆 <b>By Sport</b>  <i>(All-time)</i>")
            for sp in sorted(active_sports, key=lambda s: s.total, reverse=True)[:8]:
                ev_str  = f"  EV {sp.avg_ev:+.1f}%"  if sp.avg_ev  is not None else ""
                clv_str = f"  CLV {sp.avg_clv:+.1f}%" if sp.avg_clv is not None else ""
                parts.append(
                    f"  {sp.sport:<10} UD <code>{sp.ud_count:>3}</code>"
                    f" · PP <code>{sp.pp_count:>3}</code>"
                    f" · EV <code>{sp.ev_count:>3}</code>"
                    f"{ev_str}{clv_str}"
                )
            if self.best_sport:
                parts.append(f"  🥇 Best: <b>{self.best_sport}</b>")
            if self.worst_sport and self.worst_sport != self.best_sport:
                parts.append(f"  📉 Worst: <b>{self.worst_sport}</b>")
            parts.append("")

        # ── By market ─────────────────────────────────────────────────────────
        if "market" in self.unavailable:
            parts += ["📈 <b>By Market</b>", "  <i>UNAVAILABLE.</i>", ""]
        active_markets = [m for m in self.by_market if m.count >= 1]
        if "market" not in self.unavailable and active_markets:
            parts.append("🏷 <b>By Market</b>")
            for m in sorted(active_markets, key=lambda m: m.count, reverse=True)[:6]:
                ev_str = f"  avg EV {m.avg_ev:+.1f}%" if m.avg_ev is not None else ""
                parts.append(
                    f"  {m.market:<18} n=<code>{m.count:>3}</code>{ev_str}"
                )
            if self.best_market:
                parts.append(f"  🥇 Best market: <b>{self.best_market}</b>")
            parts.append("")

        # ── Today's activity ──────────────────────────────────────────────────
        # daily_trend now contains only today's reporting-day entry.
        # Historical trend data is preserved in DB for backtesting/reporting.
        trend_days = [d for d in self.daily_trend if d.total > 0]
        if trend_days:
            parts.append("📅 <b>Today</b>")
            for d in trend_days[-1:]:
                parts.append(
                    f"  {d.date_str}  UD <code>{d.ud_count}</code>"
                    f" · PP <code>{d.pp_count}</code>"
                    f" · EV <code>{d.ev_count}</code>"
                    f" · Steam <code>{d.steam_count}</code>"
                )

        # ── System panel ──────────────────────────────────────────────────────
        sp = self.system_panel
        if sp is not None:
            parts += ["", "🖥️ <b>System Status</b>"]
            parts.append(f"  Uptime:    <code>{sp.uptime_str}</code>")
            if sp.last_restart:
                parts.append(f"  Restarted: <code>{sp.last_restart}</code>")
            if sp.crash_count:
                parts.append(f"  Crashes:   <code>{sp.crash_count}</code>")
            if sp.active_blocks:
                parts.append(f"  Blocks:    <code>{sp.active_blocks}</code> players blocked")
            for name, status in (sp.provider_status or {}).items():
                icon = "✅" if status == "OK" else "⚠️" if status == "DEGRADED" else "❌"
                parts.append(f"  {icon} {name}: <code>{status}</code>")
            parts.append("")

        # ── Intelligence panel ────────────────────────────────────────────────
        ip = self.intelligence_panel
        if ip is not None and ip.total_graded > 0:
            parts += ["", "🧠 <b>Learning & Intelligence</b>"]
            if ip.hit_rate is not None:
                parts.append(
                    f"  Graded: <code>{ip.total_graded}</code>  "
                    f"Hit rate: <code>{ip.hit_rate*100:.0f}%</code>"
                )
            if ip.provider_breakdown:
                parts.append("  By source:")
                for source, counts in sorted(ip.provider_breakdown.items()):
                    parts.append(
                        f"    {source}: {counts['W']}W-{counts['L']}L-{counts['P']}P"
                    )
            if ip.total_misses > 0:
                parts.append(
                    f"  Miss types:  "
                    f"Model <code>{ip.model_errors}</code>  "
                    f"Market <code>{ip.market_errors}</code>  "
                    f"Settlement <code>{ip.settlement_errors}</code>  "
                    f"Variance <code>{ip.variance_errors}</code>"
                )
            if ip.model_error_rate is not None:
                parts.append(
                    f"  Model error rate: <code>{ip.model_error_rate*100:.0f}%</code>"
                    " <i>(only these update weights)</i>"
                )
            if ip.strongest_sport:
                parts.append(f"  Strongest sport:  <b>{ip.strongest_sport}</b>")
            if ip.weakest_sport:
                parts.append(f"  Weakest sport:    <b>{ip.weakest_sport}</b>")
            if ip.strongest_market:
                parts.append(f"  Strongest market: <b>{ip.strongest_market}</b>")

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

        # Keep report reads on one async path.  SQLite WAL permits readers
        # during writes, but launching eleven independent sessions at once
        # competes with the scanner's bulk snapshot transaction and can leave
        # every report task waiting until the Telegram command timeout.  The
        # individual helpers already isolate failures, so serialization only
        # changes scheduling; it does not change report semantics.
        await cls._gather_totals(db, report)
        try:
            tracking = await db.get_tracking_summary()
        except Exception as exc:
            tracking = exc
        provider_rows = []
        for provider in ("Underdog", "PrizePicks"):
            try:
                provider_rows.append(
                    await db.get_recent_tracked_props(
                        provider, since_hours=1, limit=100
                    )
                )
            except Exception as exc:
                logger.exception(
                    "dashboard recent tracked props failed for provider=%s",
                    provider,
                )
                provider_rows.append(exc)
        if not isinstance(tracking, dict):
            report.unavailable.add("tracking")
        try:
            if isinstance(tracking, dict):
                report.tracked_observations = int(
                    tracking.get(
                        "historical_observations",
                        tracking.get("tracked_observations", 0),
                    )
                    or 0
                )
                report.active_tracked_props = int(
                    tracking.get("active_tracked", tracking.get("tracked", 0)) or 0
                )
                report.directional_opportunities = int(
                    tracking.get("directional_total", tracking.get("total", 0)) or 0
                )
                report.delivered_opportunities = int(tracking.get("delivered", 0) or 0)
                report.pending_opportunities = int(tracking.get("pending", 0) or 0)
                report.resolved_opportunities = int(tracking.get("resolved", 0) or 0)
                result_counts = tracking.get("result_counts", {}) or {}
                report.outcome_hits = int(result_counts.get("HIT", 0) or 0)
                report.outcome_misses = int(result_counts.get("MISS", 0) or 0)
                report.outcome_pushes = int(result_counts.get("PUSH", 0) or 0)
                delivered_today = tracking.get("delivered_today_by_provider", {}) or {}
                report.today_ud_alerts = int(
                    delivered_today.get("Underdog", 0) or 0
                )
                report.today_pp_alerts = int(
                    delivered_today.get("PrizePicks", 0) or 0
                )
                active_by_provider = tracking.get("active_by_provider", {}) or {}
                observed_by_provider = (
                    tracking.get(
                        "historical_observations_by_provider",
                        tracking.get("tracked_observations_by_provider", {}),
                    )
                    or {}
                )
                directional_by_provider = tracking.get("by_provider", {}) or {}
                directional_totals = tracking.get("directional_by_provider", {}) or {}
                delivered_by_provider = tracking.get(
                    "delivered_by_provider",
                    tracking.get("provider_delivered", {}),
                ) or {}
                pending_by_provider = tracking.get("pending_by_provider", {}) or {}
                resolved_by_provider = tracking.get("resolved_by_provider", {}) or {}
                providers = (
                    set(active_by_provider)
                    | set(observed_by_provider)
                    | set(directional_by_provider)
                    | set(directional_totals)
                )
                for provider in providers:
                    values = directional_by_provider.get(provider, {})
                    report.tracking_by_provider[provider] = {
                        "active": int(active_by_provider.get(provider, 0) or 0),
                        "observed": int(observed_by_provider.get(provider, 0) or 0),
                        "directional": int(directional_totals.get(provider, 0) or 0),
                        "delivered": int(delivered_by_provider.get(provider, 0) or 0),
                        "pending": int(pending_by_provider.get(provider, 0) or 0),
                        "resolved": int(resolved_by_provider.get(provider, 0) or 0),
                        "hit": 0,
                        "miss": 0,
                        "push": 0,
                    }
                    provider_row = report.tracking_by_provider[provider]
                    for rec in ("OVER", "UNDER"):
                        provider_row["hit"] += int(values.get(rec, {}).get("HIT", 0) or 0)
                        provider_row["miss"] += int(values.get(rec, {}).get("MISS", 0) or 0)
                        provider_row["push"] += int(values.get(rec, {}).get("PUSH", 0) or 0)
                active_by_sport = tracking.get("active_by_sport", {}) or {}
                observed_by_sport = tracking.get("observations_by_sport", {}) or {}
                directional_by_sport = tracking.get("directional_by_sport", {}) or {}
                delivered_by_sport = tracking.get("delivered_by_sport", {}) or {}
                for sport in (
                    set(active_by_sport)
                    | set(observed_by_sport)
                    | set(directional_by_sport)
                ):
                    report.tracking_by_sport[sport] = {
                        "active": int(active_by_sport.get(sport, 0) or 0),
                        "observed": int(observed_by_sport.get(sport, 0) or 0),
                        "directional": int(directional_by_sport.get(sport, 0) or 0),
                        "delivered": int(delivered_by_sport.get(sport, 0) or 0),
                    }
        except Exception:
            report.unavailable.add("tracking")
        try:
            if isinstance(provider_rows, Exception):
                provider_rows = []
            report.recent_tracked_props = []
            for provider, rows in zip(("Underdog", "PrizePicks"), provider_rows):
                if isinstance(rows, Exception):
                    continue
                report.recent_tracked_props.extend(
                    {
                        "provider": provider,
                        "player_name": row.player_name,
                        "stat_type": row.stat_type,
                        "line_value": float(row.line_value),
                        "sport": row.sport,
                        "fetched_at": row.fetched_at,
                    }
                    for row in rows
                )
            report.recent_tracked_props.sort(
                key=lambda prop: prop.get("fetched_at") or datetime.min,
                reverse=True,
            )
        except Exception:
            report.recent_tracked_props = []
        # Run the remaining sections in order for the same reason as the
        # top-level reads: bounded database concurrency is more reliable than
        # parallel SQLite sessions while the monitoring writer is active.
        for gatherer in (
            cls._gather_ev_performance,
            cls._gather_clv_performance,
            cls._gather_ud_breakdown,
            cls._gather_by_sport,
            cls._gather_by_market,
            cls._gather_daily_trend,
            cls._gather_system_panel,
            cls._gather_intelligence_panel,
        ):
            await gatherer(db, report)
        cls._compute_best_worst(report)
        await cls._gather_provider_pipeline(db, report)

        return report

    # ── Query helpers ──────────────────────────────────────────────────────────

    @staticmethod
    async def _gather_totals(db: "Database", r: DashboardReport) -> None:
        try:
            async def _read_with_deadline(reader, *args):
                """Return one counter without letting SQLite stall the report."""
                task = asyncio.ensure_future(reader(*args))
                done, _pending = await asyncio.wait({task}, timeout=1.75)
                if task in done:
                    return task.result()
                task.cancel()
                task.add_done_callback(
                    lambda finished: (
                        None
                        if finished.cancelled()
                        else finished.exception()
                    )
                )
                raise asyncio.TimeoutError

            readers = (
                db.count_ev_records,
                db.count_steam_records,
                DashboardEngine._count_underdog_deliveries,
                db.count_pp_edge_records,
                db.count_clv_records,
                DashboardEngine._count_today_underdog_deliveries,
                db.count_today_pp_alerts,
            )
            values = await asyncio.gather(
                *(
                    _read_with_deadline(
                        reader,
                        db,
                    )
                    if reader in (
                        DashboardEngine._count_underdog_deliveries,
                        DashboardEngine._count_today_underdog_deliveries,
                    )
                    else _read_with_deadline(reader)
                    for reader in readers
                ),
                return_exceptions=True,
            )
            fields = (
                "total_ev_alerts",
                "total_steam_alerts",
                "total_ud_alerts",
                "total_pp_alerts",
                "total_clv_records",
                "today_ud_alerts",
                "today_pp_alerts",
            )
            for field_name, value in zip(fields, values):
                if not isinstance(value, Exception):
                    setattr(r, field_name, int(value or 0))
                else:
                    # Keep the rest of the dashboard useful when one optional
                    # count is blocked by a long-running SQLite writer.
                    logger.debug(
                        "dashboard counter unavailable field=%s error=%s",
                        field_name,
                        type(value).__name__,
                    )
        except Exception:
            r.unavailable.add("totals")

    @staticmethod
    async def _count_underdog_deliveries(db: "Database") -> int:
        """Count delivered UD opportunities without scanning raw snapshots."""
        from sqlalchemy import func, or_, select
        from database import PropOpportunityLog

        async with db.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(PropOpportunityLog)
                .where(
                    or_(
                        PropOpportunityLog.provider == "Underdog",
                        PropOpportunityLog.provider.is_(None),
                    ),
                    PropOpportunityLog.alert_sent == True,  # noqa: E712
                )
            )
            return int(result.scalar() or 0)

    @staticmethod
    async def _count_today_underdog_deliveries(db: "Database") -> int:
        from sqlalchemy import func, or_, select
        from database import PropOpportunityLog
        from reporting import current_reporting_day_bounds

        day_start, day_end = current_reporting_day_bounds()
        delivered_at = func.coalesce(
            PropOpportunityLog.alert_sent_at,
            PropOpportunityLog.detected_at,
        )
        async with db.session() as s:
            result = await s.execute(
                select(func.count())
                .select_from(PropOpportunityLog)
                .where(
                    or_(
                        PropOpportunityLog.provider == "Underdog",
                        PropOpportunityLog.provider.is_(None),
                    ),
                    PropOpportunityLog.alert_sent == True,  # noqa: E712
                    delivered_at >= day_start,
                    delivered_at < day_end,
                )
            )
            return int(result.scalar() or 0)

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
            logger.exception("dashboard EV performance query failed")
            r.unavailable.add("ev")

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
            r.unavailable.add("clv")

        # Pending seeds
        try:
            r.clv_seeds_pending = await db.count_pending_clv_seeds()
        except Exception:
            r.unavailable.add("clv")

    @staticmethod
    async def _gather_ud_breakdown(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import PropLineHistory
            r.ud_tracked_snapshots = int(
                (r.tracking_by_provider.get("Underdog") or {}).get("observed", 0)
                or 0
            )
            async with db.session() as s:
                score_res = await s.execute(
                    select(func.avg(PropLineHistory.score_total))
                    .where(
                        PropLineHistory.provider == "Underdog",
                        PropLineHistory.score_total.isnot(None),
                    )
                )
                r.ud_avg_score = score_res.scalar()
        except Exception:
            r.unavailable.add("ud")

    @staticmethod
    async def _gather_by_sport(db: "Database", r: DashboardReport) -> None:
        try:
            from sqlalchemy import select, func
            from database import EVRecord, PPEdgeRecord, PropOpportunityLog
            from sqlalchemy import or_

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
                    select(PropOpportunityLog.sport, func.count())
                    .where(
                        or_(
                            PropOpportunityLog.provider == "Underdog",
                            PropOpportunityLog.provider.is_(None),
                        ),
                        PropOpportunityLog.alert_sent == True,  # noqa: E712
                    )
                    .group_by(PropOpportunityLog.sport)
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
            r.unavailable.add("sport")

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
            logger.exception("dashboard market performance query failed")
            r.unavailable.add("market")

    @staticmethod
    async def _gather_daily_trend(db: "Database", r: DashboardReport) -> None:
        """Gather today's canonical reporting-day activity counts.

        Only queries the current reporting calendar day (not a rolling window).
        Historical data remains stored in DB for backtesting; this display
        focuses on the current day's actionable activity.
        """
        try:
            from sqlalchemy import select, func
            from database import EVRecord, PropOpportunityLog, SteamRecord, PPEdgeRecord
            from sqlalchemy import or_

            from reporting import current_reporting_day_bounds
            day_start, day_end = current_reporting_day_bounds()
            days: list[DailyTrend] = []

            async with db.session() as s:
                date_str = day_start.strftime("%m/%d")

                dt = DailyTrend(date_str=date_str)

                ev_res = await s.execute(
                    select(func.count())
                    .select_from(EVRecord)
                    .where(
                        EVRecord.alert_sent == True,  # noqa: E712
                        EVRecord.detected_at >= day_start,
                        EVRecord.detected_at < day_end,
                    )
                )
                dt.ev_count = ev_res.scalar() or 0

                delivered_at = func.coalesce(
                    PropOpportunityLog.alert_sent_at,
                    PropOpportunityLog.detected_at,
                )
                ud_res = await s.execute(
                    select(func.count())
                    .select_from(PropOpportunityLog)
                    .where(
                        or_(
                            PropOpportunityLog.provider == "Underdog",
                            PropOpportunityLog.provider.is_(None),
                        ),
                        PropOpportunityLog.alert_sent == True,  # noqa: E712
                        delivered_at >= day_start,
                        delivered_at < day_end,
                    )
                )
                dt.ud_count = ud_res.scalar() or 0

                st_res = await s.execute(
                    select(func.count())
                    .select_from(SteamRecord)
                    .where(
                        SteamRecord.alert_sent == True,  # noqa: E712
                        SteamRecord.detected_at >= day_start,
                        SteamRecord.detected_at < day_end,
                    )
                )
                dt.steam_count = st_res.scalar() or 0

                pp_res = await s.execute(
                    select(func.count())
                    .select_from(PPEdgeRecord)
                    .where(
                        PPEdgeRecord.alert_sent == True,  # noqa: E712
                        PPEdgeRecord.detected_at >= day_start,
                        PPEdgeRecord.detected_at < day_end,
                    )
                )
                dt.pp_count = pp_res.scalar() or 0

                days.append(dt)

            r.daily_trend = days
        except Exception:
            r.unavailable.add("daily")

    @staticmethod
    async def _gather_system_panel(db: "Database", r: DashboardReport) -> None:
        """Gather system health metrics for the SystemPanel."""
        try:
            sp = SystemPanel()

            # Active player blocks
            try:
                sp.active_blocks = await db.count_active_blocks()
            except Exception:
                pass

            # Uptime from HealthTracker sidecar if available
            try:
                from engine.health import get_health_tracker
                ht = get_health_tracker()
                if ht is not None:
                    # Use current_uptime_str() directly — it's always available
                    # and derived from the startup timestamp, so it matches what
                    # /health and /status report.  Avoid summary().get("uptime")
                    # which may return "unknown" when the key is absent.
                    uptime = ht.current_uptime_str()
                    sp.uptime_str = uptime if uptime else "unknown"
                    summary = ht.summary()
                    sp.crash_count = summary.get("crash_count", 0)
                    last_rs = summary.get("last_restart")
                    if last_rs:
                        sp.last_restart = str(last_rs)[:19]
                    prov_health = summary.get("providers", {})
                    for pname, ok in prov_health.items():
                        sp.provider_status[pname] = "OK" if ok else "DEGRADED"
            except Exception:
                pass

            r.system_panel = sp
        except Exception:
            pass

    @staticmethod
    async def _gather_provider_pipeline(db: "Database", r: DashboardReport) -> None:
        """Build the single provider-separated dashboard pipeline view."""
        # Dashboard is a current-status view.  Re-running the full
        # reconciliation ledger here duplicated a large report query and made
        # /dashboard contend with an active scan.  Tracking was already
        # gathered above and contains the same provider-separated populations
        # needed for this presentation.
        by_provider = r.tracking_by_provider

        known_providers = (
            {"Underdog", "PrizePicks"}
            | set(by_provider)
        )
        pipeline: dict[str, dict] = {}
        health_monitor = None
        health_tracker = None
        try:
            from providers import get_health_monitor
            health_monitor = get_health_monitor()
        except Exception:
            pass
        try:
            from engine.health import get_health_tracker
            health_tracker = get_health_tracker()
        except Exception:
            pass

        for provider in sorted(known_providers):
            base = r.tracking_by_provider.get(provider, {})
            if not isinstance(base, dict):
                base = {}
            normalized = by_provider.get(provider, {})
            if not isinstance(normalized, dict):
                normalized = {}
            values = {
                "observed": base.get("observed", normalized.get("observed", 0)),
                "active": base.get(
                    "active", base.get("tracked", normalized.get("tracked", 0))
                ),
                "evaluated": normalized.get("evaluated", 0),
                "directional": base.get(
                    "directional", normalized.get("directional", 0)
                ),
                "actionable_persisted": normalized.get(
                    "actionable_persisted", base.get("directional", 0)
                ),
                "delivered": base.get(
                    "delivered", normalized.get("delivered_alerts", 0)
                ),
                "pending": base.get("pending", normalized.get("pending", 0)),
                "resolved": base.get("resolved", normalized.get("resolved", 0)),
                "hit": base.get("hit", normalized.get("hit", 0)),
                "miss": base.get("miss", normalized.get("miss", 0)),
                "push": base.get("push", normalized.get("push", 0)),
            }

            if health_monitor is not None:
                try:
                    health = health_monitor.get_health(provider)
                    values["status"] = getattr(
                        getattr(health, "status", None), "value", None
                    ) or str(getattr(health, "status", "unavailable"))
                    values["last_success"] = health.format_last_success()
                except Exception:
                    pass
            if health_tracker is not None:
                try:
                    values["last_fetch"] = health_tracker.provider_last_fetch_str(provider)
                except Exception:
                    pass
            if "status" not in values and r.system_panel is not None:
                values["status"] = (
                    r.system_panel.provider_status or {}
                ).get(provider, "unavailable")
            pipeline[provider] = values

        r.provider_pipeline = pipeline

    @staticmethod
    async def _gather_intelligence_panel(db: "Database", r: DashboardReport) -> None:
        """Gather learning and intelligence metrics for the IntelligencePanel."""
        try:
            from sqlalchemy import select, func
            from database import PropOpportunityLog

            ip = IntelligencePanel()

            async with db.session() as s:
                # Graded totals
                count_res = await s.execute(
                    select(
                        PropOpportunityLog.result,
                        func.count(),
                    )
                    .where(
                        PropOpportunityLog.result.in_(["HIT", "MISS", "PUSH"]),
                        PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]),
                    )
                    .group_by(PropOpportunityLog.result)
                )
                for result_val, cnt in count_res.all():
                    if result_val == "HIT":
                        ip.total_hits = cnt
                    elif result_val == "MISS":
                        ip.total_misses = cnt

                ip.total_graded = ip.total_hits + ip.total_misses
                if ip.total_graded > 0:
                    ip.hit_rate = ip.total_hits / ip.total_graded

                # Error type breakdown
                err_res = await s.execute(
                    select(PropOpportunityLog.error_type, func.count())
                    .where(
                        PropOpportunityLog.error_type.isnot(None),
                        PropOpportunityLog.result == "MISS",
                        PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]),
                    )
                    .group_by(PropOpportunityLog.error_type)
                )
                for err_type, cnt in err_res.all():
                    if err_type == "Model":
                        ip.model_errors = cnt
                    elif err_type == "Market":
                        ip.market_errors = cnt
                    elif err_type == "Settlement":
                        ip.settlement_errors = cnt
                    elif err_type == "Variance":
                        ip.variance_errors = cnt

                if ip.total_misses > 0:
                    ip.model_error_rate = ip.model_errors / ip.total_misses

                # Strongest sport (hit rate by sport, min 5 resolved)
                sport_res = await s.execute(
                    select(
                        PropOpportunityLog.sport,
                        PropOpportunityLog.result,
                        func.count(),
                    )
                    .where(
                        PropOpportunityLog.result.in_(["HIT", "MISS"]),
                        PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]),
                    )
                    .group_by(PropOpportunityLog.sport, PropOpportunityLog.result)
                )
                sport_stats: dict = {}
                for sport_val, res_val, cnt in sport_res.all():
                    if sport_val not in sport_stats:
                        sport_stats[sport_val] = {"HIT": 0, "MISS": 0}
                    sport_stats[sport_val][res_val] = cnt

                sport_hr = {}
                for sport_val, counts in sport_stats.items():
                    total = counts["HIT"] + counts["MISS"]
                    if total >= 5:
                        sport_hr[sport_val] = counts["HIT"] / total

                if sport_hr:
                    ip.strongest_sport = max(sport_hr, key=sport_hr.__getitem__)
                    ip.weakest_sport   = min(sport_hr, key=sport_hr.__getitem__)
                    if ip.strongest_sport == ip.weakest_sport:
                        ip.weakest_sport = None

                # Keep provider performance separate so PrizePicks results
                # cannot appear to be Underdog results in the dashboard.
                provider_res = await s.execute(
                    select(
                        PropOpportunityLog.provider,
                        PropOpportunityLog.recommendation,
                        PropOpportunityLog.result,
                        func.count(),
                    )
                    .where(PropOpportunityLog.result.in_(["HIT", "MISS", "PUSH"]))
                    .where(PropOpportunityLog.recommendation.in_(["OVER", "UNDER"]))
                    .group_by(
                        PropOpportunityLog.provider,
                        PropOpportunityLog.recommendation,
                        PropOpportunityLog.result,
                    )
                )
                for provider_val, rec_val, result_val, cnt in provider_res.all():
                    source = provider_val or "Underdog"
                    source_counts = ip.provider_breakdown.setdefault(
                        source, {"W": 0, "L": 0, "P": 0}
                    )
                    if result_val == "PUSH":
                        source_counts["P"] += cnt
                    elif rec_val == "OVER":
                        source_counts["W" if result_val == "HIT" else "L"] += cnt
                    else:
                        source_counts["W" if result_val == "MISS" else "L"] += cnt

            r.intelligence_panel = ip
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
