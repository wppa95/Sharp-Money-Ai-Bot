"""
engine/analyst.py — AI Analyst Layer (Framework v3.0 Layer 9).

Generates stored decision narratives from Candidate artifacts only.
No new scoring engine. No new confidence engine. No live data recalculation.

The analyst reads from:
  Candidate.tier, .decision, .risk_level, .confidence
  Candidate.decision_trace["prop_intelligence"]   (prop intelligence layer)
  Candidate.decision_trace (general evidence)
  Candidate.decision_reason

And produces four narrative components stored in Candidate.decision_trace["analyst"]:
  recommended_because  — positive case for the pick
  risk_because         — honest risk statement
  would_avoid_because  — conditions that would flip the call
  final_recommendation — one-sentence bottom line

Contract
────────
• build_analyst_narrative() is a pure function. No IO, no async.
• Narratives are stored via Candidate.with_analyst_narrative(narrative) and
  never recomputed by ExplanationService.
• All evidence is drawn from decision_trace — the frozen snapshot from
  decision time.

Usage::

    from engine.analyst import build_analyst_narrative
    from engine.candidate import Candidate

    narrative = build_analyst_narrative(candidate)
    candidate = candidate.with_analyst_narrative(narrative)

    # ExplanationService reads the narrative from decision_trace["analyst"]
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from engine.candidate import Candidate


# ── Analyst narrative dataclass ────────────────────────────────────────────────

@dataclass(frozen=True)
class AnalystNarrative:
    """
    Four-component decision narrative produced by the AI Analyst.

    All fields are plain strings — pre-formatted for both Telegram HTML (when
    escaped by ExplanationService) and console output.

    Stored verbatim in Candidate.decision_trace["analyst"] so they can be
    rendered later without recalculating any signals.
    """
    recommended_because:  str   # Why we like this pick
    risk_because:         str   # What could go wrong
    would_avoid_because:  str   # Conditions that flip the call
    final_recommendation: str   # One-sentence bottom line

    def to_dict(self) -> dict:
        return {
            "recommended_because":  self.recommended_because,
            "risk_because":         self.risk_because,
            "would_avoid_because":  self.would_avoid_because,
            "final_recommendation": self.final_recommendation,
        }


# ── Internal helpers ───────────────────────────────────────────────────────────

def _g(obj: Any, *keys, default: Any = None) -> Any:
    """Traverse nested dict by keys; return default if any key is missing."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is default:
            return default
    return cur


def _tier_label(tier: str) -> str:
    return {
        "S": "highest-confidence (S-tier)",
        "A": "high-confidence (A-tier)",
        "B": "moderate-confidence (B-tier)",
        "PASS": "low-confidence (PASS)",
    }.get(tier, tier)


def _confidence_label(conf: int) -> str:
    """
    Return a calibrated signal label based on actual confidence score.

    Use this instead of ``_tier_label`` when you want the language to
    reflect the real confidence number rather than the tier name.

    Thresholds
    ----------
    95+  →  "Elite confidence signal"
    80-94 → "High-confidence signal"
    65-79 → "Strong but monitored signal"
    55-64 → "Moderate signal"
    <55  →  "Low-confidence signal"
    """
    if conf >= 95:
        return "Elite confidence signal"
    if conf >= 80:
        return "High-confidence signal"
    if conf >= 65:
        return "Strong but monitored signal"
    if conf >= 55:
        return "Moderate signal"
    return "Low-confidence signal"


def _risk_label(risk: str) -> str:
    return {
        "LOW":    "low-risk",
        "MEDIUM": "moderate-risk",
        "HIGH":   "high-risk",
    }.get(risk.upper() if risk else "", risk or "unknown risk")


# ── Recommended-because narrative ─────────────────────────────────────────────

def _build_recommended_because(candidate: "Candidate") -> str:
    """Build the positive case for this pick from stored trace artifacts."""
    parts: list[str] = []
    conf  = candidate.confidence
    trace = candidate.decision_trace

    # Lead with confidence tier
    parts.append(f"This is a {_tier_label(candidate.tier)} {candidate.decision} on "
                 f"{candidate.stat_type} ({candidate.line:.1f}) for {candidate.player_name}.")

    # Confidence dimension narrative
    if conf is not None:
        if conf.overall >= 75:
            parts.append(f"Overall confidence is strong at {conf.overall}/100.")
        if conf.betting_edge >= 70:
            parts.append(f"Betting edge score of {conf.betting_edge}/100 indicates "
                         "favourable historical patterns.")
        if conf.market_confidence >= 70:
            parts.append(f"Market confidence of {conf.market_confidence}/100 "
                         "suggests market mispricing.")
        if conf.data_confidence >= 70:
            parts.append(f"Data quality score of {conf.data_confidence}/100 "
                         "means the sample is reliable.")

    # Prop intelligence narrative
    pi = _g(trace, "prop_intelligence")
    if pi:
        hist = _g(pi, "historical")
        if hist:
            ss  = _g(hist, "sample_strength", default=0)
            n   = _g(hist, "n", default=0)
            hr  = _g(hist, "hit_rate", default=-1)
            avg = _g(hist, "avg_vs_line", default=0)

            if n >= 10:
                parts.append(f"Line history: {n} snapshots with sample strength {ss}/100.")
            if hr >= 0.6:
                parts.append(f"OVER hit rate proxy of {hr*100:.0f}% supports the {candidate.decision} call.")
            if avg > 0.5 and candidate.decision == "OVER":
                parts.append(f"Historical average line is {avg:.1f} above current line — "
                              "value may exist on the OVER.")


        matchup = _g(pi, "matchup")
        if matchup and _g(matchup, "label") in ("Favorable",):
            parts.append(f"Matchup context: {_g(matchup, 'reasoning', default='')}".rstrip(".") + ".")

    # Window evidence from trace
    for window in ("l5", "l10", "l20"):
        w = trace.get(window)
        if isinstance(w, dict) and w.get("games", 0) >= 3:
            pct = round(w.get("hit_rate", 0) * 100)
            if pct >= 60:
                parts.append(f"{window.upper()} window: {w['games']} games, {pct}% hit rate.")

    # EV signal
    if "ev_pct" in trace and (trace["ev_pct"] or 0) > 0:
        parts.append(f"EV signal: +{trace['ev_pct']:.2f}% expected value.")

    return " ".join(parts) if parts else (
        f"{candidate.decision} on {candidate.stat_type} ({candidate.line:.1f}) "
        f"for {candidate.player_name}."
    )


# ── Risk-because narrative ────────────────────────────────────────────────────

def _build_risk_because(candidate: "Candidate", risk: Optional[Any] = None) -> str:
    """Build the honest risk statement from stored trace artifacts."""
    parts: list[str] = []
    conf  = candidate.confidence
    trace = candidate.decision_trace

    parts.append(f"Risk level: {_risk_label(candidate.risk_level)}.")

    # Low data quality warning
    if conf is not None and conf.data_confidence < 50:
        parts.append(f"Data quality is limited (score {conf.data_confidence}/100) — "
                     "sample may be insufficient to trust the pattern.")

    # Low market confidence
    if conf is not None and conf.market_confidence < 50:
        parts.append(f"Market signal is weak (score {conf.market_confidence}/100) — "
                     "the market may not be confirming this move.")

    # Prop intelligence risks
    pi = _g(trace, "prop_intelligence")
    if pi:
        hist = _g(pi, "historical")
        if hist:
            ss = _g(hist, "sample_strength", default=0)
            if ss < 30:
                parts.append(f"Thin sample: strength score {ss}/100 — "
                              "confidence in the historical pattern is limited.")
            var = _g(hist, "variance", default=0)
            if var > 3.0:
                parts.append(f"High line variance ({var:.1f}) — "
                              "this prop fluctuates significantly and may be unreliable.")

            # Role / playtime language removed — not reliable across all sports.

        matchup = _g(pi, "matchup")
        if matchup and _g(matchup, "label") == "Tough":
            parts.append(f"Matchup signal: {_g(matchup, 'reasoning', default='')}".rstrip(".") + ".")

    # External risk assessment (risk_manager.RiskAssessment)
    if risk is not None:
        factors = getattr(risk, "factors", [])
        for f in factors:
            sev = getattr(f, "severity", "")
            if sev in ("MEDIUM", "HIGH"):
                parts.append(f"Portfolio risk: {getattr(f, 'description', '')}.")
        adj = getattr(risk, "recommendation_adjustment", "NONE")
        if adj == "REDUCE":
            parts.append("Recommendation: reduce stake due to portfolio correlation.")
        elif adj == "AVOID":
            parts.append("Recommendation: avoid this pick due to high portfolio risk.")

    return " ".join(parts) if parts else "Standard market risk — no additional risk flags."


# ── Would-avoid-because narrative ─────────────────────────────────────────────

def _build_would_avoid_because(candidate: "Candidate") -> str:
    """Build the conditions that would flip the call from stored artifacts."""
    parts: list[str] = []
    conf  = candidate.confidence
    trace = candidate.decision_trace

    parts.append("Would avoid this pick if:")

    conditions: list[str] = []

    # Confidence below threshold
    if conf is not None:
        if conf.overall < 60:
            conditions.append(f"overall confidence falls below 60 (currently {conf.overall})")
        if conf.data_confidence < 40:
            conditions.append(
                f"data quality deteriorates further (currently {conf.data_confidence}/100)"
            )

    # Prop intelligence conditions
    pi = _g(trace, "prop_intelligence")
    if pi:
        hist = _g(pi, "historical")
        if hist:
            n = _g(hist, "n", default=0)
            if n < 10:
                conditions.append(
                    f"sample size drops below 5 (currently {n} snapshots)"
                )

        role = _g(pi, "role")
        if role:
            if _g(role, "label") == "Bench":
                conditions.append("player's role as bench player is confirmed by official lineup")
            if _g(role, "trend") not in (None, "Unknown"):
                pass  # trend already covered in risk narrative

        matchup = _g(pi, "matchup")
        if matchup and _g(matchup, "label") == "Favorable":
            conditions.append("market reverses the recent line move upward")

    # Tier-specific conditions
    if candidate.tier == "B":
        conditions.append("a substitute game-time alert contradicts this prop")

    conditions.append("player is listed as questionable or out before game time")
    conditions.append("line moves more than 1.0 against the recommended side")

    if not conditions:
        conditions.append("game-time availability is in question")

    for i, c in enumerate(conditions, 1):
        parts.append(f"  {i}. {c.capitalize()}.")

    return "\n".join(parts)


# ── Final-recommendation narrative ────────────────────────────────────────────

def _build_final_recommendation(candidate: "Candidate") -> str:
    """Build the one-sentence bottom line from stored artifacts."""
    conf  = candidate.confidence
    pi    = candidate.decision_trace.get("prop_intelligence", {})

    decision_str   = candidate.decision
    tier_str       = candidate.tier
    overall        = conf.overall if conf else 0
    sport          = candidate.sport
    stat           = candidate.stat_type
    line           = candidate.line
    player         = candidate.player_name

    if candidate.tier == "BLOCK":
        return (
            f"Do not act on {player} {stat} ({line:.1f}) — player is blocked "
            "due to a reliability concern."
        )

    if decision_str == "PASS":
        return (
            f"Pass on {player} {stat} ({line:.1f}) in {sport} — "
            f"the edge is not strong enough at {overall}/100 confidence."
        )

    hist = _g(pi, "historical")
    ss   = _g(hist, "sample_strength", default=0) if hist else 0

    qualifier = ""
    if tier_str == "S" and ss >= 60:
        qualifier = "Strong signal with reliable history — "
    elif tier_str == "A":
        qualifier = "Solid edge — "
    elif tier_str == "B":
        qualifier = "Moderate edge — "

    return (
        f"{qualifier}recommended {decision_str} on {player} {stat} ({line:.1f}) "
        f"in {sport} at {overall}/100 confidence ({tier_str}-tier, "
        f"{candidate.risk_level.capitalize()} risk)."
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def build_analyst_narrative(
    candidate: "Candidate",
    risk: Optional[Any] = None,
) -> AnalystNarrative:
    """
    Build a four-component analyst narrative from a Candidate's stored artifacts.

    This is a pure function — no IO, no async, no live data.  All evidence is
    read from Candidate.decision_trace and Candidate.confidence, which are frozen
    at decision time.

    Parameters
    ----------
    candidate : Candidate
        The fully-evaluated decision container.  Must have at minimum a non-empty
        decision_trace and decision_reason.
    risk : RiskAssessment | None
        Optional risk assessment from engine.risk_manager.  If provided, the
        risk_because component will include portfolio-level risk factors.

    Returns
    -------
    AnalystNarrative — apply to the candidate via
        ``candidate.with_analyst_narrative(narrative)``
    """
    return AnalystNarrative(
        recommended_because  = _build_recommended_because(candidate),
        risk_because         = _build_risk_because(candidate, risk),
        would_avoid_because  = _build_would_avoid_because(candidate),
        final_recommendation = _build_final_recommendation(candidate),
    )


def format_analyst_telegram(narrative: AnalystNarrative) -> str:
    """
    Format an AnalystNarrative for Telegram HTML output.

    Safe to call with any AnalystNarrative; all text is HTML-escaped.
    """
    def esc(s: str) -> str:
        return html.escape(str(s))

    lines = [
        "🧠 <b>Analyst Assessment</b>",
        "",
        f"✅ <b>Recommended because</b>",
        f"  <i>{esc(narrative.recommended_because)}</i>",
        "",
        f"⚠️ <b>Risk</b>",
        f"  <i>{esc(narrative.risk_because)}</i>",
        "",
        f"🚫 <b>Would avoid if</b>",
        f"  <i>{esc(narrative.would_avoid_because)}</i>",
        "",
        f"🎯 <b>Bottom line</b>",
        f"  <b>{esc(narrative.final_recommendation)}</b>",
    ]
    return "\n".join(lines)


def build_analyst_from_alert_parts(
    player_name: str,
    stat_type: str,
    sport: str,
    line: float,
    decision_rec: str,
    decision_tier: str,
    confidence: int,
    risk_level: str = "MEDIUM",
    intelligence_trace: Optional[dict] = None,
) -> AnalystNarrative:
    """
    Build an AnalystNarrative directly from raw alert-time data.

    This is an alternative entry point to build_analyst_narrative() for use in
    the Telegram alert pipeline, where a full Candidate object is not always
    available.  Produces the same four-component narrative structure from
    individual score/decision/intelligence fields.

    Parameters
    ----------
    player_name    : Player display name.
    stat_type      : Prop stat type (e.g. "Points").
    sport          : Sport code (e.g. "NBA").
    line           : Current prop line value.
    decision_rec   : "OVER" | "UNDER" | "PASS"
    decision_tier  : "S" | "A" | "B" | "PASS"
    confidence     : Overall confidence score 0-100.
    risk_level     : "LOW" | "MEDIUM" | "HIGH"
    intelligence_trace : prop_intelligence trace dict (optional).
    """
    trace = intelligence_trace or {}
    pi    = trace  # for this path the trace IS the prop_intelligence payload

    # ── Recommended because ─────────────────────────────────────────────────
    why_parts: list[str] = []
    # Use actual confidence to calibrate language — not the tier name.
    # This prevents "highest-confidence" language when confidence is 79/100.
    conf_label = _confidence_label(confidence)
    why_parts.append(
        f"{conf_label} — {decision_rec} on {stat_type} "
        f"({line:.1f}) for {player_name} ({decision_tier}-tier)."
    )
    if confidence >= 80:
        why_parts.append(f"Overall confidence is strong at {confidence}/100.")
    elif confidence >= 65:
        why_parts.append(f"Confidence at {confidence}/100 — monitoring risk factors.")
    elif confidence >= 55:
        why_parts.append(f"Moderate confidence at {confidence}/100 — playable with caution.")

    hist = _g(pi, "historical") or {}
    if hist:
        ss = _g(hist, "sample_strength", default=0)
        n  = _g(hist, "n", default=0)
        hr = _g(hist, "hit_rate", default=-1)
        if n >= 5 and hr >= 0.60:
                side = (decision_rec or "OVER").upper()
                why_parts.append(
                    f"{side} hit rate proxy of {hr * 100:.0f}%"
                    f" across {n} samples supports the {side} call."
                )
        if ss >= 50:
            why_parts.append(f"Sample strength score of {ss}/100 — history is reliable.")
        # Window evidence
        for w_key in ("l5", "l10", "l20"):
            w = _g(hist, "windows", w_key)
            if w and _g(w, "n", default=0) >= 3:
                pct = round((_g(w, "hit_rate") or 0) * 100)
                if pct >= 60:
                    why_parts.append(f"{w_key.upper()}: {pct}% hit rate.")
                    break  # one window example is enough here


    matchup = _g(pi, "matchup") or {}
    if _g(matchup, "label") == "Favorable":
        rsns = _g(matchup, "reasoning") or []
        if rsns:
            why_parts.append(f"Matchup context: {rsns[0]}.")

    recommended_because = " ".join(why_parts) if why_parts else (
        f"{decision_rec} on {stat_type} ({line:.1f}) for {player_name} in {sport}."
    )

    # ── Risk because ────────────────────────────────────────────────────────
    risk_parts: list[str] = []

    if hist:
        ss  = _g(hist, "sample_strength", default=0)
        var = _g(hist, "variance", default=0.0)
        if ss < 30:
            risk_parts.append(
                f"Thin sample (strength score {ss}/100) — history may not be reliable."
            )
        if (var or 0) > 3.0:
            risk_parts.append(
                f"High line variance ({var:.1f}) — this prop fluctuates significantly."
            )

        # Role / playtime language removed — not reliable across all sports.

    matchup = _g(pi, "matchup") or {}
    if _g(matchup, "label") == "Tough":
        rsns = _g(matchup, "reasoning") or []
        risk_parts.append(
            "Tough matchup" + (f": {rsns[0]}" if rsns else "") + "."
        )

    risk_because = " ".join(risk_parts)

    # ── Would avoid because ─────────────────────────────────────────────────
    conditions = [
        "player is listed as questionable or out before game time",
        "line moves more than 1.0 against the recommended side",
    ]
    if decision_tier == "B":
        conditions.append("a contradicting game-time alert appears for this prop")
    if (confidence or 0) < 65:
        conditions.append(
            f"overall confidence falls further from current {confidence}/100"
        )
    would_avoid = "Would avoid if: " + "; ".join(conditions[:3]) + "."

    # ── Final recommendation ────────────────────────────────────────────────
    qualifier = {"S": "Strong signal — ", "A": "Solid edge — ", "B": "Moderate edge — "}.get(
        decision_tier, ""
    )
    final_recommendation = (
        f"{qualifier}recommended {decision_rec} on {player_name} {stat_type} ({line:.1f}) "
        f"in {sport} at {confidence}/100 confidence ({decision_tier}-tier)."
    )

    return AnalystNarrative(
        recommended_because  = recommended_because,
        risk_because         = risk_because,
        would_avoid_because  = would_avoid,
        final_recommendation = final_recommendation,
    )


def format_analyst_alert_block(
    player_name: str,
    stat_type: str,
    sport: str,
    line: float,
    decision_rec: str,
    decision_tier: str,
    confidence: int,
    risk_level: str = "MEDIUM",
    intelligence_trace: Optional[dict] = None,
) -> str:
    """
    Return a compact Telegram HTML analyst block for appending to prop alerts.

    Returns an empty string when the recommendation is PASS or missing — the
    analyst block is only shown for actionable directional picks (OVER/UNDER).
    Catches all exceptions silently so a narrative failure never blocks an alert.
    """
    if not decision_rec or decision_rec == "PASS":
        return ""
    try:
        narrative = build_analyst_from_alert_parts(
            player_name   = player_name,
            stat_type     = stat_type,
            sport         = sport,
            line          = line,
            decision_rec  = decision_rec,
            decision_tier = decision_tier,
            confidence    = confidence,
            risk_level    = risk_level,
            intelligence_trace = intelligence_trace,
        )
        def esc(s: str) -> str:
            return html.escape(str(s))

        return (
            "\n\n🤖 <b>Analyst</b>\n"
            f"  ✅ <i>{esc(narrative.recommended_because)}</i>\n"
            f"  ⚠️ <i>{esc(narrative.risk_because)}</i>\n"
            f"  🎯 <b>{esc(narrative.final_recommendation)}</b>"
        )
    except Exception:
        return ""


def format_analyst_console(narrative: AnalystNarrative) -> str:
    """Compact plain-text analyst summary for logs / CLI."""
    return (
        f"[ANALYST] {narrative.final_recommendation} | "
        f"Risk: {narrative.risk_because[:100]}"
    )
