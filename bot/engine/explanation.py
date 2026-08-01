"""
Explanation Service — Sharp Money Bot Framework v3.0 Layer 4.

Centralised explanation generation from stored decision artifacts.

Contract
────────
• render() NEVER recalculates confidence scores.
• render() NEVER pulls live data.
• All explanations come from Candidate.decision_trace and Candidate.decision_reason.
• Output format is chosen by the caller: TELEGRAM / CONSOLE / DICT.
• The service is stateless — instantiate once or use the module singleton.

Usage::

    from engine.explanation import get_explanation_service, ExplanationFormat

    svc  = get_explanation_service()
    text = svc.render(candidate, ExplanationFormat.TELEGRAM)
    data = svc.render(candidate, ExplanationFormat.DICT)
"""

from __future__ import annotations

import enum
import html as _html
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    # Avoid circular import at runtime — Candidate is only needed for annotations
    from engine.candidate import Candidate


# ─────────────────────────────────────────────────────────────────────────────
# Output format enum
# ─────────────────────────────────────────────────────────────────────────────

class ExplanationFormat(str, enum.Enum):
    """Supported output formats for ExplanationService.render()."""
    TELEGRAM = "telegram"   # HTML string for Telegram parse_mode=HTML
    CONSOLE  = "console"    # Plain text for logging / CLI
    DICT     = "dict"       # Serialisable dict for API / dashboard


# ─────────────────────────────────────────────────────────────────────────────
# Explanation Service
# ─────────────────────────────────────────────────────────────────────────────

class ExplanationService:
    """
    Single entry point for all decision explanations.

    The service reads from Candidate.decision_trace and Candidate.decision_reason
    — stored artifacts from the time of the original decision — so explanations
    are always consistent with what was decided, even if market data changes later.
    """

    def render(
        self,
        candidate: "Candidate",
        fmt: ExplanationFormat = ExplanationFormat.TELEGRAM,
    ) -> Union[str, dict]:
        """
        Render the explanation for *candidate* in the requested *fmt*.

        Parameters
        ----------
        candidate : Candidate
            The decision container.  Must have decision_reason and
            decision_trace populated (from any factory adapter).
        fmt : ExplanationFormat
            Output format.

        Returns
        -------
        str  — for TELEGRAM and CONSOLE formats.
        dict — for DICT format.
        """
        if fmt == ExplanationFormat.DICT:
            return self._render_dict(candidate)
        if fmt == ExplanationFormat.CONSOLE:
            return self._render_console(candidate)
        return self._render_telegram(candidate)

    # ── Format implementations ────────────────────────────────────────────────

    def _render_telegram(self, candidate: "Candidate") -> str:
        """Produce an HTML string for Telegram parse_mode=HTML."""
        conf  = candidate.confidence
        lines: list[str] = [
            f"<b>{_esc(candidate.player_name)}</b>  ·  {_esc(candidate.sport)}",
            f"Market: {_esc(candidate.stat_type)}  ·  Line: <code>{candidate.line:.1f}</code>",
            "",
            (
                f"🎯 Decision: <b>{_esc(candidate.decision)}</b>"
                f"  ·  Tier: <b>{_esc(candidate.tier)}</b>"
                f"  ·  Risk: {_esc(candidate.risk_level)}"
            ),
        ]

        # 4-dimension confidence breakdown — from stored dims, NOT recalculated
        if conf is not None:
            lines += [
                "",
                "<b>📊 Confidence</b>",
                f"  Overall:         <code>{conf.overall}/100</code>",
                f"  Data quality:    <code>{conf.data_confidence}/100</code>",
                f"  Market signal:   <code>{conf.market_confidence}/100</code>",
                f"  Betting edge:    <code>{conf.betting_edge}/100</code>",
            ]

        # Decision reason — stored artifact, not recomputed
        if candidate.decision_reason:
            safe_reason = _esc(candidate.decision_reason[:300])
            lines += ["", f"<b>Reason:</b>  <i>{safe_reason}</i>"]

        # Evidence snippets from the decision trace
        trace   = candidate.decision_trace
        snippets = _format_trace_snippets_telegram(trace)
        if snippets:
            lines += ["", "<b>Evidence:</b>"] + [f"  {s}" for s in snippets]

        # Prop intelligence summary (from Layer 8)
        pi_lines = _format_prop_intelligence_telegram(trace.get("prop_intelligence"))
        if pi_lines:
            lines += ["", "<b>🧮 Intelligence:</b>"] + [f"  {l}" for l in pi_lines]

        # Analyst narrative (from Layer 9) — brief inline form
        analyst = trace.get("analyst")
        if isinstance(analyst, dict) and analyst.get("final_recommendation"):
            lines += [
                "",
                "🎯 <b>Analyst:</b>",
                f"  <i>{_esc(analyst['final_recommendation'][:200])}</i>",
            ]

        return "\n".join(lines)

    def _render_console(self, candidate: "Candidate") -> str:
        """Produce a compact plain-text explanation for logs / CLI output."""
        overall = candidate.confidence.overall if candidate.confidence else 0
        return (
            f"[{candidate.tier}] {candidate.player_name}"
            f" | {candidate.stat_type} {candidate.line:.1f}"
            f" | {candidate.decision}"
            f" | conf={overall}/100"
            f" | {candidate.decision_reason[:140]}"
        )

    def _render_dict(self, candidate: "Candidate") -> dict:
        """Produce a serialisable dict for API / dashboard consumption."""
        return {
            "player":     candidate.player_name,
            "player_key": candidate.player_key,
            "sport":      candidate.sport,
            "stat":       candidate.stat_type,
            "stat_key":   candidate.stat_key,
            "line":       candidate.line,
            "decision":   candidate.decision,
            "tier":       candidate.tier,
            "risk":       candidate.risk_level,
            "actionable": candidate.is_actionable,
            "confidence": candidate.confidence.to_dict() if candidate.confidence else {},
            "reason":     candidate.decision_reason,
            "trace":      candidate.decision_trace,
            "provider":   candidate.provider,
            "created_at": candidate.created_at.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """HTML-escape a string for Telegram HTML parse mode."""
    return _html.escape(str(text))


def _format_prop_intelligence_telegram(pi: Optional[dict]) -> list[str]:
    """Extract prop intelligence highlights from the prop_intelligence trace dict."""
    if not isinstance(pi, dict):
        return []
    parts: list[str] = []

    hist = pi.get("historical")
    if isinstance(hist, dict):
        ss  = hist.get("sample_strength")
        n   = hist.get("n")
        hr  = hist.get("hit_rate")
        if ss is not None and n is not None:
            parts.append(f"Sample: <code>{n}g</code>  strength <code>{ss}/100</code>")
        if hr is not None and hr >= 0:
            parts.append(f"OVER proxy hit rate: <code>{hr*100:.0f}%</code>")
        dd = hist.get("data_confidence_delta")
        if dd is not None:
            sign = "+" if dd >= 0 else ""
            parts.append(f"Data confidence adj: <code>{sign}{dd}</code>")

    role = pi.get("role")
    if isinstance(role, dict) and role.get("label") not in (None, "Unknown"):
        trend = role.get("trend", "")
        parts.append(f"Role: {_esc(role['label'])} / {_esc(role.get('stability',''))} "
                     f"({trend})")

    matchup = pi.get("matchup")
    if isinstance(matchup, dict) and matchup.get("label") not in (None, "Unknown"):
        sig = matchup.get("signal", 0)
        sign = "+" if sig >= 0 else ""
        parts.append(f"Matchup: {_esc(matchup['label'])} (signal {sign}{sig})")

    return parts


def _format_trace_snippets_telegram(trace: dict) -> list[str]:
    """Extract human-readable evidence snippets from a decision trace dict.

    Returns a list of already-escaped HTML strings suitable for indented
    display in a Telegram message.
    """
    parts: list[str] = []

    # EV / Steam evidence
    if "ev_pct" in trace:
        parts.append(f"EV: <code>{trace['ev_pct']:+.2f}%</code>")
    if "steam_score" in trace:
        parts.append(f"Steam: <code>{trace['steam_score']}/100</code>")
    if "ai_confidence" in trace:
        parts.append(f"AI conf: <code>{trace['ai_confidence']}/100</code>")

    # UD window evidence (from candidate_from_ud_decision trace)
    for window in ("l5", "l10", "l20", "l30", "season"):
        w = trace.get(window)
        if isinstance(w, dict) and "games" in w and "hit_rate" in w:
            pct = round(w["hit_rate"] * 100)
            parts.append(f"{window.upper()}: <code>{w['games']}g  {pct}% hit</code>")

    return parts


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_service: ExplanationService | None = None


def get_explanation_service() -> ExplanationService:
    """Return (or lazily create) the module-level ExplanationService singleton."""
    global _service
    if _service is None:
        _service = ExplanationService()
    return _service
