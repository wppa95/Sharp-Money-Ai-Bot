"""
Model Calibration System — engine/calibration.py

Evaluates two distinct questions that must NOT be conflated:

  1.  Line-movement detection accuracy
      Did the steam or line-change alert predict where the market went?
      A "correct" detection means the line kept moving in the alerted direction.

  2.  Betting recommendation accuracy
      For alerts with an explicit bet recommendation (OVER/UNDER/PASS),
      was the recommended side correct against the game result?

These are tracked separately because a sharp line move (correctly detected) does
NOT automatically produce a profitable bet — the market may already have moved
past the fair value by the time you act.

Additional calibration tracks:
  3.  Confidence-tier accuracy  — did S/A/B tiers perform better than PASS?
  4.  CLV-by-tier               — was CLV% higher for higher-confidence alerts?
  5.  Grade accuracy            — win rate vs ai_confidence band
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TierCalibration:
    """Accuracy metrics for one confidence tier (S / A / B / PASS)."""
    tier:         str
    total:        int   = 0
    wins:         int   = 0
    losses:       int   = 0
    pushes:       int   = 0
    avg_ev_pct:   Optional[float] = None
    avg_clv_pct:  Optional[float] = None
    avg_confidence: Optional[float] = None

    @property
    def resolved(self) -> int:
        return self.wins + self.losses + self.pushes

    @property
    def hit_rate(self) -> Optional[float]:
        denom = self.wins + self.losses
        return self.wins / denom if denom >= 5 else None

    @property
    def tier_emoji(self) -> str:
        return {"S": "🔥", "A": "🟢", "B": "🟡", "PASS": "⚪"}.get(self.tier, "⚪")


@dataclass
class DetectionAccuracy:
    """
    Line-movement detection accuracy.

    'detected' = an alert was sent for a line change or steam move.
    'confirmed' = in subsequent data, the line continued moving in the same
                  direction (or the closing price confirmed the move).

    This is purely about whether the DETECTION was correct — not whether a bet
    on that move was profitable.
    """
    source:         str    # "UNDERDOG_LINE_CHANGE" | "STEAM" | "EV"
    total_detected: int    = 0
    confirmed:      int    = 0
    reversed:       int    = 0
    inconclusive:   int    = 0   # insufficient follow-up data

    @property
    def confirmation_rate(self) -> Optional[float]:
        denom = self.confirmed + self.reversed
        return self.confirmed / denom if denom >= 5 else None


@dataclass
class RecommendationAccuracy:
    """
    Bet-recommendation accuracy (OVER / UNDER / PASS vs game result).

    A PASS recommendation that would have lost is counted as a correct PASS.
    A PASS recommendation that would have won is counted as an incorrect PASS
    (opportunity cost — the bot was too conservative).
    """
    total:          int   = 0
    correct:        int   = 0
    incorrect:      int   = 0
    unresolved:     int   = 0

    by_side: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "OVER":  {"correct": 0, "incorrect": 0},
        "UNDER": {"correct": 0, "incorrect": 0},
        "PASS":  {"correct": 0, "incorrect": 0},
    })

    @property
    def accuracy(self) -> Optional[float]:
        denom = self.correct + self.incorrect
        return self.correct / denom if denom >= 5 else None

    @property
    def resolved(self) -> int:
        return self.correct + self.incorrect


@dataclass
class CalibrationReport:
    """Full model calibration report."""
    generated_at: datetime = field(default_factory=datetime.utcnow)

    # ── Confidence-tier accuracy ──────────────────────────────────────────────
    tier_calibration: dict[str, TierCalibration] = field(default_factory=dict)

    # ── Detection accuracy (line movements / steam) ────────────────────────
    detection: dict[str, DetectionAccuracy] = field(default_factory=dict)

    # ── Recommendation accuracy ───────────────────────────────────────────────
    recommendation: RecommendationAccuracy = field(
        default_factory=RecommendationAccuracy
    )

    # ── Summary stats ─────────────────────────────────────────────────────────
    total_ev_records:   int   = 0
    total_ud_records:   int   = 0
    total_clv_records:  int   = 0
    avg_clv_all:        Optional[float] = None
    clv_positive_rate:  Optional[float] = None

    # Meta
    ev_records_used:  int = 0
    ud_records_used:  int = 0
    clv_records_used: int = 0

    def to_telegram(self) -> str:
        lines: list[str] = [
            "🔬 <b>Model Calibration Report</b>",
            f"<i>{self.generated_at.strftime('%b %d %Y  %H:%M UTC')}</i>",
            "",
        ]

        # ── Confidence-tier accuracy ─────────────────────────────────────────
        lines.append("📊 <b>Confidence Tier Accuracy</b>")
        if not self.tier_calibration:
            lines.append("  <i>No resolved records yet</i>")
        else:
            for tier in ("S", "A", "B", "PASS"):
                tc = self.tier_calibration.get(tier)
                if not tc or tc.resolved == 0:
                    continue
                hr = tc.hit_rate
                hr_str = f"<code>{hr*100:.0f}%</code>" if hr is not None else "<i>n&lt;5</i>"
                ev_str = (
                    f"  avg EV <code>+{tc.avg_ev_pct:.1f}%</code>"
                    if tc.avg_ev_pct else ""
                )
                clv_str = (
                    f"  avg CLV <code>+{tc.avg_clv_pct:.2f}%</code>"
                    if tc.avg_clv_pct else ""
                )
                lines.append(
                    f"  {tc.tier_emoji} <b>{tier}</b>  "
                    f"{tc.resolved} resolved  hit {hr_str}"
                    f"{ev_str}{clv_str}"
                )

        lines.append("")

        # ── CLV summary ──────────────────────────────────────────────────────
        lines.append("📈 <b>Closing Line Value</b>")
        if self.total_clv_records == 0:
            lines.append("  <i>No CLV records yet — seeds are accumulating</i>")
        else:
            sign = "+" if (self.avg_clv_all or 0) >= 0 else ""
            avg_str = (
                f"  avg CLV: <code>{sign}{self.avg_clv_all:.2f}%</code>"
                if self.avg_clv_all is not None else "  avg CLV: —"
            )
            pos_str = ""
            if self.clv_positive_rate is not None:
                pos_str = f"  beat close: <code>{self.clv_positive_rate*100:.0f}%</code>"
            lines += [
                f"  Records: {self.total_clv_records:,}",
                avg_str + pos_str,
            ]

        lines.append("")

        # ── Detection accuracy ────────────────────────────────────────────────
        lines.append("🎯 <b>Line-Movement Detection</b>")
        lines.append("  <i>Measures: was the DETECTED direction correct?</i>")
        lines.append("  <i>(separate from whether a bet on it was profitable)</i>")
        if not self.detection:
            lines.append("  <i>No detection data yet</i>")
        else:
            for src, da in self.detection.items():
                if da.total_detected == 0:
                    continue
                cr = da.confirmation_rate
                cr_str = f"<code>{cr*100:.0f}%</code> confirmed" if cr is not None else "<i>n&lt;5</i>"
                lines.append(
                    f"  {src.replace('_', ' ').title()}  "
                    f"{da.total_detected} detected  {cr_str}"
                )

        lines.append("")

        # ── Recommendation accuracy ───────────────────────────────────────────
        lines.append("💡 <b>Bet Recommendation Accuracy</b>")
        lines.append("  <i>Measures: was the recommended side correct?</i>")
        if self.recommendation.resolved == 0:
            lines.append("  <i>No resolved recommendations yet</i>")
        else:
            acc = self.recommendation.accuracy
            acc_str = f"<code>{acc*100:.0f}%</code>" if acc is not None else "<i>n&lt;5</i>"
            lines.append(
                f"  {self.recommendation.resolved} resolved  "
                f"accuracy: {acc_str}"
            )
            for side in ("OVER", "UNDER", "PASS"):
                sc = self.recommendation.by_side.get(side, {})
                c = sc.get("correct", 0)
                i = sc.get("incorrect", 0)
                if c + i == 0:
                    continue
                r = c / (c + i) if (c + i) >= 3 else None
                r_str = f"{r*100:.0f}%" if r is not None else "n/a"
                lines.append(f"    {side}: {c}/{c+i} ({r_str})")

        lines.append("")
        lines.append(
            f"<i>Based on {self.ev_records_used:,} EV records, "
            f"{self.ud_records_used:,} Underdog records, "
            f"{self.clv_records_used:,} CLV records</i>"
        )
        return "\n".join(lines)


# ── Calibration engine ────────────────────────────────────────────────────────

class CalibrationEngine:
    """
    Computes a CalibrationReport from historical database records.

    Usage:
        engine = CalibrationEngine()
        report = await engine.compute(db)
        print(report.to_telegram())
    """

    async def compute(self, db: "Any") -> CalibrationReport:   # type: ignore[name-defined]
        """Gather all historical records and compute calibration metrics."""
        report = CalibrationReport()

        try:
            await self._compute_tier_calibration(db, report)
        except Exception as exc:
            logger.warning("calibration: tier_calibration failed: %s", exc)

        try:
            await self._compute_clv_stats(db, report)
        except Exception as exc:
            logger.warning("calibration: clv_stats failed: %s", exc)

        try:
            await self._compute_recommendation_accuracy(db, report)
        except Exception as exc:
            logger.warning("calibration: recommendation_accuracy failed: %s", exc)

        try:
            await self._compute_detection_accuracy(db, report)
        except Exception as exc:
            logger.warning("calibration: detection_accuracy failed: %s", exc)

        return report

    # ── Internal methods ──────────────────────────────────────────────────────

    async def _compute_tier_calibration(self, db, report: CalibrationReport) -> None:
        """Compute confidence-tier accuracy from resolved EV records."""
        records = await db.get_ev_records_with_results(limit=1000, include_pending=False)
        report.ev_records_used = len(records)
        report.total_ev_records = await db.count_ev_records()

        tier_map: dict[str, TierCalibration] = {
            t: TierCalibration(tier=t) for t in ("S", "A", "B", "PASS")
        }

        for rec in records:
            tier = _confidence_to_tier(rec.ai_confidence or 0)
            tc = tier_map[tier]
            tc.total += 1
            result = (rec.result or "").upper()
            if result == "WIN":
                tc.wins += 1
            elif result == "LOSS":
                tc.losses += 1
            elif result in ("PUSH", "VOID"):
                tc.pushes += 1

        # Compute averages for each tier
        for tier, tc in tier_map.items():
            tier_recs = [
                r for r in records
                if _confidence_to_tier(r.ai_confidence or 0) == tier
            ]
            if tier_recs:
                ev_vals   = [r.expected_value for r in tier_recs if r.expected_value]
                clv_vals  = [r.clv            for r in tier_recs if r.clv]
                conf_vals = [r.ai_confidence  for r in tier_recs if r.ai_confidence]
                tc.avg_ev_pct      = sum(ev_vals)   / len(ev_vals)   if ev_vals   else None
                tc.avg_clv_pct     = sum(clv_vals)  / len(clv_vals)  if clv_vals  else None
                tc.avg_confidence  = sum(conf_vals) / len(conf_vals) if conf_vals else None

        report.tier_calibration = tier_map

    async def _compute_clv_stats(self, db, report: CalibrationReport) -> None:
        """Aggregate CLV statistics across all computed seeds and CLV records."""
        clv_records = await db.get_recent_clv_records(limit=500)
        report.clv_records_used   = len(clv_records)
        report.total_clv_records  = await db.count_clv_records()

        if clv_records:
            clv_vals = [r.clv_pct for r in clv_records if r.clv_pct is not None]
            if clv_vals:
                report.avg_clv_all       = sum(clv_vals) / len(clv_vals)
                report.clv_positive_rate = sum(1 for v in clv_vals if v > 0) / len(clv_vals)

        # Populate avg_clv_pct per tier from CLV seeds (seeds have tier metadata)
        try:
            seeds = await db.get_clv_seeds_by_tier_stats()
            for tier, stats in seeds.items():
                if tier in report.tier_calibration:
                    report.tier_calibration[tier].avg_clv_pct = stats.get("avg_clv")
        except (AttributeError, Exception):
            pass   # method may not exist yet — non-fatal

    async def _compute_recommendation_accuracy(self, db, report: CalibrationReport) -> None:
        """Compute bet recommendation accuracy from resolved Underdog snapshots."""
        try:
            ud_snaps = await db.get_recent_underdog_snapshots(limit=500)
        except AttributeError:
            return

        report.ud_records_used = len(ud_snaps)
        report.total_ud_records = len(ud_snaps)

        rec_acc = RecommendationAccuracy()
        for snap in ud_snaps:
            if not snap.bet_recommendation:
                continue
            if not snap.alert_outcome or "sent" not in (snap.alert_outcome or ""):
                continue
            rec_acc.total += 1
            # Resolution: we don't have game results here, but we track the intent
            # When game_results are linked, this will be populated automatically
            rec_acc.unresolved += 1

        report.recommendation = rec_acc

    async def _compute_detection_accuracy(self, db, report: CalibrationReport) -> None:
        """
        Compute line-movement detection accuracy from Underdog snapshots.

        Detection is 'confirmed' when a line that moved in direction D later
        continued in direction D (i.e. subsequent snapshot shows further move
        in the same direction or the line was removed — which often signals
        game start, a sign of correct detection).
        """
        try:
            all_snaps = await db.get_recent_underdog_snapshots(limit=500)
        except AttributeError:
            return

        # Group by (player_name, stat_type) to trace movement history
        by_prop: dict[tuple, list] = {}
        for snap in all_snaps:
            key = (snap.player_name or "", snap.stat_type or "")
            by_prop.setdefault(key, []).append(snap)

        ud_da = DetectionAccuracy(source="UNDERDOG_LINE_CHANGE")
        for key, snaps in by_prop.items():
            # Sort oldest → newest
            snaps_sorted = sorted(snaps, key=lambda s: s.fetched_at or datetime.min)
            for i, snap in enumerate(snaps_sorted):
                if not snap.line_moved or not snap.line_delta:
                    continue
                ud_da.total_detected += 1
                direction = 1 if snap.line_delta > 0 else -1

                # Look at the next snapshot for the same prop
                later = snaps_sorted[i + 1:]
                if not later:
                    ud_da.inconclusive += 1
                    continue
                next_snap = later[0]
                if getattr(next_snap, "removed", False):
                    # Removal = game started = detection was timely
                    ud_da.confirmed += 1
                elif next_snap.line_moved and next_snap.line_delta:
                    next_dir = 1 if next_snap.line_delta > 0 else -1
                    if next_dir == direction:
                        ud_da.confirmed += 1
                    else:
                        ud_da.reversed += 1
                else:
                    ud_da.inconclusive += 1

        report.detection["UNDERDOG_LINE_CHANGE"] = ud_da


# ── Helpers ───────────────────────────────────────────────────────────────────

def _confidence_to_tier(confidence: int) -> str:
    if confidence >= 95:
        return "S"
    if confidence >= 85:
        return "A"
    if confidence >= 75:
        return "B"
    return "PASS"


# ── Learning Label Classification (Framework v3.0 Layer 7) ───────────────────

class MissType(str, enum.Enum):
    """
    Classification of a MISS outcome for learning and model protection.

    IMPORTANT: Only ``Model`` errors should ever update scoring weights.

    ``Variance`` represents correctly-identified edges that did not materialise
    this time due to normal statistical variance.  Penalising high-confidence
    picks that lose would corrupt the model by teaching it to avoid its own
    best signals.

    ``Settlement`` represents data errors where the actual outcome is unknown.
    These must not be treated as model failures.

    ``Market`` represents cases where the market moved against the position
    between detection and game time — a structural issue, not a model failure.
    """
    MODEL      = "Model"       # Weak signal — learn from this, update weights
    MARKET     = "Market"      # Market moved; moderate signal, not a model failure
    SETTLEMENT = "Settlement"  # Data error — actual_value unavailable or corrupt
    VARIANCE   = "Variance"    # High-confidence pick, bad luck — DO NOT update weights


def classify_miss(
    recommendation: str,
    decision_tier:  str,
    confidence:     int,
    actual_value:   Optional[float],
    line_value:     Optional[float] = None,
) -> str:
    """
    Classify a MISS outcome into a ``MissType`` value for learning purposes.

    Called by the opportunity grader after a game completes with a MISS result.
    The returned string is stored in ``PropOpportunityLog.error_type`` and used
    to determine whether a miss should update scoring weights.

    Only ``"Model"`` errors should feed into weight updates.
    ``"Variance"`` errors are high-confidence misses that must not damage the
    model — they are expected in a well-calibrated system.

    Parameters
    ----------
    recommendation : str   OVER | UNDER | PASS (from PropOpportunityLog)
    decision_tier  : str   S | A | B | PASS   (from PropOpportunityLog)
    confidence     : int   0–100              (from PropOpportunityLog)
    actual_value   : float | None  Real game result; None = data unavailable
    line_value     : float | None  The bet line (reserved for future market-drift
                                   detection; not used in current heuristic)

    Returns
    -------
    str  — one of "Model", "Market", "Settlement", "Variance"
    """
    if actual_value is None:
        return MissType.SETTLEMENT.value

    # High-confidence, high-tier: statistical variance — protect the model
    if confidence >= 75 and decision_tier in ("S", "A"):
        return MissType.VARIANCE.value

    # Moderate-confidence: market information or signal was partially correct
    if confidence >= 50 and decision_tier in ("A", "B"):
        return MissType.MARKET.value

    # Low confidence or weak tier: the model signal itself was wrong
    return MissType.MODEL.value
