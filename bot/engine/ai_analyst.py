"""Typed, evidence-bound advisory analyst for player-prop decisions.

This module deliberately has no provider, network, scraper, or scoring imports.
The default implementation only interprets values already produced by the
quantitative pipeline. A future LLM adapter can implement ``AnalystProvider``
without changing the prop pipeline or the persistence contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Protocol


ANALYST_SCHEMA_VERSION = "1"
ANALYST_MODEL = "evidence-bound"
ANALYST_MODEL_VERSION = "1"
AnalystPrediction = Literal["OVER", "UNDER", "PASS", "UNAVAILABLE"]
EvidenceStatus = Literal["AVAILABLE", "PARTIAL", "UNAVAILABLE"]


@dataclass(frozen=True)
class AnalystInput:
    """Immutable snapshot of quantitative inputs available at analyst time."""

    player_name: str
    sport: str
    stat_type: str
    line: Optional[float]
    provider: str
    event_id: str
    quantitative: Mapping[str, Any]
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_name": self.player_name,
            "sport": self.sport,
            "stat_type": self.stat_type,
            "line": self.line,
            "provider": self.provider,
            "event_id": self.event_id,
            "quantitative": dict(self.quantitative),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class AnalystDecision:
    """Auditable advisory output. It never changes pipeline qualification."""

    prediction: AnalystPrediction
    evidence_status: EvidenceStatus
    rationale: str
    evidence_used: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    confidence: Optional[int]
    confidence_source: Optional[str]
    risk_level: Optional[str]
    model: str = ANALYST_MODEL
    model_version: str = ANALYST_MODEL_VERSION
    schema_version: str = ANALYST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction": self.prediction,
            "evidence_status": self.evidence_status,
            "rationale": self.rationale,
            "evidence_used": list(self.evidence_used),
            "missing_inputs": list(self.missing_inputs),
            "confidence": self.confidence,
            "confidence_source": self.confidence_source,
            "risk_level": self.risk_level,
            "model": self.model,
            "model_version": self.model_version,
            "schema_version": self.schema_version,
        }


class AnalystProvider(Protocol):
    """Replaceable synchronous adapter for a future model-backed analyst."""

    model: str
    model_version: str

    def analyze(self, snapshot: AnalystInput) -> AnalystDecision:
        ...


def _present(value: Any) -> bool:
    return value is not None and value not in ("", {}, [], ())


class EvidenceBoundAnalyst:
    """Safe default analyst that cannot invent values or external context."""

    model = ANALYST_MODEL
    model_version = ANALYST_MODEL_VERSION

    def analyze(self, snapshot: AnalystInput) -> AnalystDecision:
        quantitative = snapshot.quantitative
        direction = str(quantitative.get("direction") or "").upper()
        evidence_used = tuple(
            str(key)
            for key, value in {
                **dict(quantitative),
                **dict(snapshot.evidence),
            }.items()
            if _present(value)
        )
        missing = tuple(
            key for key in (
                "direction",
                "decision_confidence",
                "supporting_history",
                "market_evidence",
            )
            if not _present(quantitative.get(key))
            and not _present(snapshot.evidence.get(key))
        )

        if direction not in {"OVER", "UNDER"}:
            return AnalystDecision(
                prediction="UNAVAILABLE",
                evidence_status="UNAVAILABLE",
                rationale=(
                    "Directional quantitative output is unavailable; "
                    "the analyst did not generate a prediction."
                ),
                evidence_used=evidence_used,
                missing_inputs=missing or ("direction",),
                confidence=None,
                confidence_source=None,
                risk_level=None,
            )

        confidence = quantitative.get("decision_confidence")
        if isinstance(confidence, bool):
            confidence = None
        try:
            confidence = int(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and not 0 <= confidence <= 100:
            confidence = None

        status: EvidenceStatus = "AVAILABLE" if not missing else "PARTIAL"
        details = ", ".join(evidence_used) if evidence_used else "none"
        missing_text = (
            f" Missing inputs remain unavailable: {', '.join(missing)}."
            if missing else ""
        )
        return AnalystDecision(
            prediction=direction,  # type: ignore[arg-type]
            evidence_status=status,
            rationale=(
                f"Advisory interpretation of the stored quantitative direction "
                f"{direction}; evidence fields used: {details}.{missing_text}"
            ),
            evidence_used=evidence_used,
            missing_inputs=missing,
            confidence=confidence,
            confidence_source=(
                "stored_decision_confidence" if confidence is not None else None
            ),
            risk_level=(
                str(quantitative["risk_level"])
                if _present(quantitative.get("risk_level"))
                else None
            ),
        )


def _as_mapping(value: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception:
            return None
        return converted if isinstance(converted, Mapping) else None
    return None


def build_advisory_from_pipeline(
    *,
    player_name: str,
    sport: str,
    stat_type: str,
    line: Optional[float],
    provider: str,
    event_id: str = "",
    decision: Any,
    score: Any = None,
    validation: Any = None,
    hit_rates: Any = None,
    intelligence_trace: Optional[Mapping[str, Any]] = None,
    analyst: Optional[AnalystProvider] = None,
) -> AnalystDecision:
    """Build an advisory result exclusively from existing pipeline objects."""

    quantitative: dict[str, Any] = {
        "direction": getattr(decision, "recommendation", None),
        "decision_confidence": getattr(decision, "confidence", None),
    }
    for key in ("total", "market_quality", "n_history", "stars"):
        value = getattr(score, key, None) if score is not None else None
        if value is not None:
            quantitative[f"score_{key}"] = value
    evidence: dict[str, Any] = {}
    for name, source in (
        ("supporting_history", validation),
        ("hit_rates", hit_rates),
        ("market_evidence", getattr(decision, "evidence", None)),
        ("intelligence", intelligence_trace),
    ):
        mapped = _as_mapping(source)
        if mapped:
            evidence[name] = dict(mapped)
    quantitative["risk_level"] = getattr(decision, "risk_level", None)
    snapshot = AnalystInput(
        player_name=str(player_name or ""),
        sport=str(sport or "").upper(),
        stat_type=str(stat_type or ""),
        line=float(line) if line is not None else None,
        provider=str(provider or ""),
        event_id=str(event_id or ""),
        quantitative=quantitative,
        evidence=evidence,
    )
    return (analyst or EvidenceBoundAnalyst()).analyze(snapshot)


def analyst_grade(
    prediction: Optional[str],
    result: Optional[str],
    recommendation: Optional[str],
) -> Optional[str]:
    """Translate the stored pick result to the analyst's predicted direction."""

    prediction = str(prediction or "").upper()
    result = str(result or "").upper()
    recommendation = str(recommendation or "").upper()
    if (
        prediction not in {"OVER", "UNDER"}
        or recommendation not in {"OVER", "UNDER"}
        or result not in {"HIT", "MISS", "PUSH"}
    ):
        return None
    if result == "PUSH":
        return "PUSH"
    if prediction == recommendation:
        return result
    return "MISS" if result == "HIT" else "HIT"