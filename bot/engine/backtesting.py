"""
engine/backtesting.py — Backtesting Engine.

Replays historical EVRecord data through the AI ranking model to
evaluate how well the ranking tiers predicted actual outcomes.

This module is intentionally read-only: it never writes to the database
and never fires alerts.  It is used by the /backtest Telegram command
and can be called offline for analysis.

Key metrics
-----------
    Accuracy      — Win rate among TAKE decisions
    CLV           — Average Closing Line Value generated
    ROI           — Estimated return per bet (from EV realised)
    Tier breakdown— Accuracy, CLV, ROI per S / A / B tier
    Market breakdown — Accuracy by market type
    Sport breakdown  — Accuracy by sport

The backtester maps stored ai_confidence scores → ranking tier directly
(no re-computation), so results represent the decisions the bot
would have made given the historical signals.

Tier thresholds mirror RankingTier.from_score():
    S   ≥ 95   A   ≥ 85   B   ≥ 75   Pass < 75
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # EVRecord is an ORM type; avoid importing SQLAlchemy at module load time.
    from database import EVRecord

from .ranking import RankingTier, RankingDecision


# ── Record types ──────────────────────────────────────────────────────────────

@dataclass
class BacktestRecord:
    """
    A single resolved bet replayed through the ranking model.

    ``result`` is one of WIN / LOSS / PUSH (records with result=PENDING
    or result=None are excluded from accuracy statistics).
    """
    event:       str
    selection:   str
    sport:       str
    market_type: str
    score:       int             # ai_confidence from the stored EVRecord
    tier:        RankingTier     # derived from score
    decision:    RankingDecision # TAKE when tier ≥ B, else PASS
    result:      Optional[str]   # WIN / LOSS / PUSH / None
    clv:         Optional[float] # Closing Line Value %; None if not tracked
    ev_pct:      float           # stored expected_value from the EVRecord


@dataclass
class DimensionStats:
    """
    Aggregated performance for one slice (tier / market / sport).
    """
    label:       str
    total:       int   = 0
    wins:        int   = 0
    losses:      int   = 0
    pushes:      int   = 0
    take_count:  int   = 0
    pass_count:  int   = 0
    clv_sum:     float = 0.0
    clv_count:   int   = 0
    ev_sum:      float = 0.0

    @property
    def resolved(self) -> int:
        """Bets with WIN / LOSS / PUSH result (pushes counted as half)."""
        return self.wins + self.losses + self.pushes

    @property
    def win_rate(self) -> Optional[float]:
        denom = self.wins + self.losses   # exclude pushes from win rate
        return self.wins / denom if denom >= 5 else None

    @property
    def avg_clv(self) -> Optional[float]:
        return round(self.clv_sum / self.clv_count, 2) if self.clv_count >= 3 else None

    @property
    def avg_ev(self) -> Optional[float]:
        return round(self.ev_sum / self.total, 2) if self.total >= 3 else None

    def absorb(self, rec: BacktestRecord) -> None:
        self.total += 1
        if rec.decision == RankingDecision.TAKE:
            self.take_count += 1
        else:
            self.pass_count += 1
        r = (rec.result or "").upper()
        if r == "WIN":
            self.wins += 1
        elif r == "LOSS":
            self.losses += 1
        elif r == "PUSH":
            self.pushes += 1
        if rec.clv is not None:
            self.clv_sum   += rec.clv
            self.clv_count += 1
        self.ev_sum += rec.ev_pct


@dataclass
class BacktestReport:
    """
    Full backtest report produced by BacktestEngine.run().

    All DimensionStats objects only contain TAKE decisions
    (PASS records are excluded from accuracy / CLV calculations).
    """
    records:    list[BacktestRecord]

    # ── Aggregate stats ───────────────────────────────────────────────────────
    overall:    DimensionStats

    # ── Breakdowns ────────────────────────────────────────────────────────────
    by_tier:    dict[str, DimensionStats]   # "S" / "A" / "B" / "Pass"
    by_market:  dict[str, DimensionStats]
    by_sport:   dict[str, DimensionStats]

    # ── Summary helpers ───────────────────────────────────────────────────────

    @property
    def total_evaluated(self) -> int:
        return len(self.records)

    @property
    def total_take(self) -> int:
        return sum(1 for r in self.records if r.decision == RankingDecision.TAKE)

    @property
    def total_pass(self) -> int:
        return sum(1 for r in self.records if r.decision == RankingDecision.PASS)

    @property
    def best_tier(self) -> Optional[str]:
        """Tier with the highest win rate (min 5 resolved bets)."""
        candidates = [
            (t, s) for t, s in self.by_tier.items()
            if s.win_rate is not None
        ]
        return max(candidates, key=lambda x: x[1].win_rate)[0] if candidates else None

    @property
    def best_market(self) -> Optional[str]:
        candidates = [
            (m, s) for m, s in self.by_market.items()
            if s.win_rate is not None
        ]
        return max(candidates, key=lambda x: x[1].win_rate)[0] if candidates else None

    def to_telegram(self) -> str:
        """Format the full backtest report as Telegram HTML."""
        lines = [
            "📊 <b>Backtest Report</b>",
            "",
            f"  Records analysed : <code>{self.total_evaluated}</code>",
            f"  TAKE decisions   : <code>{self.total_take}</code>",
            f"  PASS decisions   : <code>{self.total_pass}</code>",
            "",
        ]

        # ── Overall ──────────────────────────────────────────────────────
        ov = self.overall
        wr_str  = f"{ov.win_rate * 100:.1f}%" if ov.win_rate is not None else "n/a"
        clv_str = f"{ov.avg_clv:+.2f}%" if ov.avg_clv is not None else "n/a"
        ev_str  = f"{ov.avg_ev:+.2f}%" if ov.avg_ev is not None else "n/a"
        lines += [
            "<b>📈 Overall (TAKE bets only)</b>",
            f"  Win rate : <code>{wr_str}</code>",
            f"  Avg CLV  : <code>{clv_str}</code>",
            f"  Avg EV   : <code>{ev_str}</code>",
            f"  W/L/P    : <code>{ov.wins}/{ov.losses}/{ov.pushes}</code>",
            "",
        ]

        # ── By tier ───────────────────────────────────────────────────────
        lines.append("<b>📊 By Tier</b>")
        for tier_key in ("S", "A", "B", "Pass"):
            s = self.by_tier.get(tier_key)
            if not s or s.total == 0:
                continue
            wr  = f"{s.win_rate * 100:.1f}%" if s.win_rate is not None else "—"
            clv = f"{s.avg_clv:+.2f}%"       if s.avg_clv is not None else "—"
            lines.append(
                f"  {tier_key:<5} n={s.total:<3}  W/L={s.wins}/{s.losses}  "
                f"WR={wr}  CLV={clv}"
            )
        lines.append("")

        # ── By market ─────────────────────────────────────────────────────
        lines.append("<b>🏷 By Market Type</b>")
        for mkt, s in sorted(self.by_market.items()):
            if s.total == 0:
                continue
            wr = f"{s.win_rate * 100:.1f}%" if s.win_rate is not None else "—"
            lines.append(f"  {mkt:<14} n={s.total:<3}  WR={wr}")
        lines.append("")

        # ── By sport ──────────────────────────────────────────────────────
        lines.append("<b>🏆 By Sport</b>")
        for sport, s in sorted(self.by_sport.items()):
            if s.total == 0:
                continue
            wr = f"{s.win_rate * 100:.1f}%" if s.win_rate is not None else "—"
            lines.append(f"  {sport:<10} n={s.total:<3}  WR={wr}")

        if self.best_tier:
            lines += ["", f"🥇 <b>Best tier:</b>   {self.best_tier}"]
        if self.best_market:
            lines += [f"🥇 <b>Best market:</b>  {self.best_market}"]

        return "\n".join(lines)

    def to_console(self) -> str:
        ov = self.overall
        wr_str = f"{ov.win_rate * 100:.1f}%" if ov.win_rate else "n/a"
        return (
            f"[Backtest] n={self.total_evaluated}  take={self.total_take}  "
            f"win_rate={wr_str}  "
            f"avg_clv={ov.avg_clv:+.2f}%  best_tier={self.best_tier}"
        )


# ── Backtesting engine ────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Stateless backtesting engine.

    Accepts a list of EVRecord ORM objects (from the database) and
    replays each through the ranking tier logic to produce a BacktestReport.

    Scoring
    -------
    Uses the stored ``ai_confidence`` column as the ranking score directly —
    this is the score the bot computed at alert time.  This avoids the need
    to reconstruct all 9 confidence inputs from historical records.

    A TAKE decision is simulated for any record whose ai_confidence ≥ 75
    (tier ≥ B).  Records with ai_confidence < 75 are PASS.
    """

    TAKE_THRESHOLD = 75   # matches RankingTier.B lower bound

    def run(self, ev_records: "list[EVRecord]") -> BacktestReport:
        """
        Replay *ev_records* and return a full BacktestReport.

        Only records with result in {WIN, LOSS, PUSH} are counted in
        accuracy / CLV statistics.  Records with result=PENDING or
        result=None contribute to tier / decision counts but are not
        counted in win_rate calculations.
        """
        bt_records: list[BacktestRecord] = []

        for r in ev_records:
            score = r.ai_confidence
            tier  = RankingTier.from_score(score)

            # Simulate the decision the bot would have made
            decision = (
                RankingDecision.TAKE
                if score >= self.TAKE_THRESHOLD
                else RankingDecision.PASS
            )

            bt_records.append(BacktestRecord(
                event       = r.event,
                selection   = r.selection,
                sport       = r.sport,
                market_type = r.market_type,
                score       = score,
                tier        = tier,
                decision    = decision,
                result      = r.result,
                clv         = r.clv,
                ev_pct      = r.expected_value,
            ))

        return self._build_report(bt_records)

    def _build_report(self, records: list[BacktestRecord]) -> BacktestReport:
        overall    = DimensionStats(label="Overall")
        by_tier:   dict[str, DimensionStats] = {}
        by_market: dict[str, DimensionStats] = {}
        by_sport:  dict[str, DimensionStats] = {}

        for rec in records:
            # Only count TAKE decisions in breakdown stats
            if rec.decision != RankingDecision.TAKE:
                continue

            tier_key = rec.tier.value
            mkt_key  = rec.market_type
            spt_key  = rec.sport

            if tier_key  not in by_tier:    by_tier[tier_key]   = DimensionStats(label=tier_key)
            if mkt_key   not in by_market:  by_market[mkt_key]  = DimensionStats(label=mkt_key)
            if spt_key   not in by_sport:   by_sport[spt_key]   = DimensionStats(label=spt_key)

            for ds in [overall, by_tier[tier_key], by_market[mkt_key], by_sport[spt_key]]:
                ds.absorb(rec)

        return BacktestReport(
            records    = records,
            overall    = overall,
            by_tier    = by_tier,
            by_market  = by_market,
            by_sport   = by_sport,
        )


# ── Convenience wrapper ───────────────────────────────────────────────────────

def run_backtest(ev_records: "list[EVRecord]") -> BacktestReport:
    """
    One-call convenience wrapper around BacktestEngine.

    Equivalent to BacktestEngine().run(ev_records).
    """
    return BacktestEngine().run(ev_records)
