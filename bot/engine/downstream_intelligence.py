"""Downstream pick intelligence.

This module is deliberately downstream-only: it interprets signals already
produced by the scoring/decision engines and never changes qualification or
delivery decisions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MovementInterpretation:
    direction: str = "UNKNOWN"
    magnitude: Optional[float] = None
    velocity: Optional[float] = None
    persistence: Optional[float] = None
    reversal: bool = False
    sample_size: int = 0
    available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceCompleteness:
    available: int = 0
    expected: int = 0
    missing: tuple[str, ...] = ()

    @property
    def score(self) -> int:
        return round(self.available / self.expected * 100) if self.expected else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "expected": self.expected,
            "missing": list(self.missing),
            "score": self.score,
        }


@dataclass(frozen=True)
class SharpConfidence:
    score: int
    tier: str
    sample_size: int
    components: dict[str, float]
    calibrated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "tier": self.tier,
            "sample_size": self.sample_size,
            "components": self.components,
            "calibrated": self.calibrated,
        }


def interpret_movement(
    *,
    line_delta: Any = None,
    previous_delta: Any = None,
    change_count: int = 0,
    observations: Optional[list[Any]] = None,
) -> MovementInterpretation:
    """Interpret movement without inventing history that is not present."""
    current = _num(line_delta)
    prior = _num(previous_delta)
    vals = [_num(v) for v in (observations or [])]
    vals = [v for v in vals if v is not None]
    if current is None and not vals:
        return MovementInterpretation()
    if current is None:
        current = vals[-1]
    direction = "UP" if current > 0 else "DOWN" if current < 0 else "FLAT"
    reversal = bool(prior is not None and current and prior and (prior > 0) != (current > 0))
    persistence = None
    if vals:
        same = sum(1 for value in vals if value and (value > 0) == (current > 0))
        persistence = round(same / len(vals) * 100, 1)
    return MovementInterpretation(
        direction=direction,
        magnitude=abs(current),
        velocity=abs(current) / max(len(vals), 1),
        persistence=persistence,
        reversal=reversal,
        sample_size=max(len(vals), int(change_count or 0)),
        available=True,
    )


def compute_evidence_completeness(
    evidence: Mapping[str, Any] | None,
    *,
    expected: tuple[str, ...] = (
        "historical",
        "movement",
        "market",
        "value",
        "matchup",
    ),
) -> EvidenceCompleteness:
    evidence = evidence or {}
    missing = tuple(
        key for key in expected
        if evidence.get(key) is None or evidence.get(key) == {} or evidence.get(key) == []
    )
    return EvidenceCompleteness(len(expected) - len(missing), len(expected), missing)


def current_line_value(
    *,
    line: Any = None,
    projected_value: Any = None,
    average_value: Any = None,
    hit_rate: Any = None,
) -> dict[str, Any]:
    """Return value only when a projection or average is actually supplied."""
    line_n = _num(line)
    projection = _num(projected_value)
    if projection is None:
        projection = _num(average_value)
    if line_n is None or projection is None:
        return {"available": False, "reason": "projection_unavailable"}
    edge = projection - line_n
    return {
        "available": True,
        "line": line_n,
        "projection": projection,
        "edge": round(edge, 3),
        "edge_pct": round(edge / abs(line_n) * 100, 2) if line_n else None,
        "direction": "OVER" if edge > 0 else "UNDER" if edge < 0 else "FLAT",
        "hit_rate": _num(hit_rate),
    }


def compute_sharp_confidence(
    *,
    bet_quality: Any = None,
    bet_confidence: Any = None,
    market_quality: Any = None,
    projection_confidence: Any = None,
    evidence_score: Any = None,
    movement_confidence: Any = None,
    sample_size: int = 0,
) -> SharpConfidence:
    """Separate, transparent confidence metric; no delivery gate side effects."""
    values = {
        "bet_quality": _num(bet_quality),
        "bet_confidence": _num(bet_confidence),
        "market_quality": _num(market_quality),
        "projection_confidence": _num(projection_confidence),
        "evidence_completeness": _num(evidence_score),
        "movement_confidence": _num(movement_confidence),
    }
    weights = {
        "bet_quality": 0.25,
        "bet_confidence": 0.20,
        "market_quality": 0.15,
        "projection_confidence": 0.15,
        "evidence_completeness": 0.15,
        "movement_confidence": 0.10,
    }
    present = {k: max(0.0, min(100.0, v)) for k, v in values.items() if v is not None}
    if not present:
        score = 0
    else:
        weight_total = sum(weights[k] for k in present)
        score = round(sum(present[k] * weights[k] for k in present) / weight_total)
    sample = max(0, int(sample_size or 0))
    calibrated = sample >= 5
    tier = "HIGH" if score >= 80 else "MEDIUM" if score >= 60 else "LOW"
    return SharpConfidence(score, tier, sample, present, calibrated)


def build_downstream_payload(
    *,
    line_delta: Any = None,
    previous_delta: Any = None,
    change_count: int = 0,
    movement_history: Optional[list[Any]] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    line: Any = None,
    projected_value: Any = None,
    average_value: Any = None,
    hit_rate: Any = None,
    sample_size: int = 0,
    **signals: Any,
) -> dict[str, Any]:
    movement = interpret_movement(
        line_delta=line_delta,
        previous_delta=previous_delta,
        change_count=change_count,
        observations=movement_history,
    )
    value = current_line_value(
        line=line,
        projected_value=projected_value,
        average_value=average_value,
        hit_rate=hit_rate,
    )
    evidence_map = dict(evidence or {})
    evidence_map.setdefault("movement", movement.to_dict() if movement.available else None)
    evidence_map.setdefault("value", value if value["available"] else None)
    completeness = compute_evidence_completeness(evidence_map)
    sharp = compute_sharp_confidence(
        evidence_score=completeness.score,
        sample_size=sample_size,
        **signals,
    )
    return {
        "version": 1,
        "movement": movement.to_dict(),
        "value": value,
        "evidence": evidence_map,
        "evidence_completeness": completeness.to_dict(),
        "sharp_confidence": sharp.to_dict(),
    }


def serialize_payload(payload: Mapping[str, Any] | None) -> Optional[str]:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True) if payload else None