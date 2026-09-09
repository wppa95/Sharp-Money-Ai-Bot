"""Audited, provider-aware adaptive learning helpers.

This module is deliberately independent from Telegram delivery and the live
Underdog qualification gates.  It turns persisted grading labels into a small,
bounded ranking adjustment only when the evidence is large enough to support it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


LEARNABLE_ERROR_TYPE = "Model"
DEFAULT_MIN_SAMPLE_SIZE = 30
DEFAULT_MIN_MODEL_ERRORS = 5
DEFAULT_MAX_DELTA = 0.10
MIN_MULTIPLIER = 1.0 - DEFAULT_MAX_DELTA
MAX_MULTIPLIER = 1.0 + DEFAULT_MAX_DELTA


@dataclass(frozen=True)
class AdaptiveSample:
    """Evidence eligible for one provider/sport/market weight."""

    provider: str
    sport: str
    market_type: str
    hits: int
    model_errors: int
    pushes: int = 0
    excluded_misses: int = 0

    @property
    def eligible_samples(self) -> int:
        return self.hits + self.model_errors

    @property
    def total_resolved(self) -> int:
        return self.hits + self.model_errors + self.pushes + self.excluded_misses

    @property
    def hit_rate(self) -> Optional[float]:
        if self.eligible_samples <= 0:
            return None
        return self.hits / self.eligible_samples


@dataclass(frozen=True)
class AdaptiveWeight:
    """A bounded multiplier and its audit metadata."""

    version: str
    provider: str
    sport: str
    market_type: str
    sample_size: int
    eligible_samples: int
    model_errors: int
    hit_rate: Optional[float]
    multiplier: float
    active: bool = True
    status: str = "INSUFFICIENT_SAMPLE"

    @property
    def eligible(self) -> bool:
        return self.status == "ACTIVE"


def build_adaptive_weight(
    sample: AdaptiveSample,
    *,
    version: str,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    min_model_errors: int = DEFAULT_MIN_MODEL_ERRORS,
    max_delta: float = DEFAULT_MAX_DELTA,
    active: bool = True,
) -> AdaptiveWeight:
    """Build a bounded weight from one grouped grading population.

    The eligible population is ``HIT`` plus ``MISS/Model``.  Pushes and misses
    classified as Market, Variance, or Settlement remain visible in audit
    counts but cannot move the weight.
    """
    max_delta = max(0.0, min(float(max_delta), 0.25))
    eligible_samples = sample.eligible_samples
    enough_data = (
        eligible_samples >= max(1, int(min_sample_size))
        and sample.model_errors >= max(1, int(min_model_errors))
    )
    hit_rate = sample.hit_rate
    if not enough_data or hit_rate is None:
        multiplier = 1.0
        status = "INSUFFICIENT_SAMPLE"
    else:
        # Shrink the adjustment around 1.0.  Even a very strong/weak group can
        # only move the ranking by the configured bounded amount.
        multiplier = 1.0 + ((hit_rate - 0.5) * 2.0 * max_delta)
        multiplier = max(1.0 - max_delta, min(1.0 + max_delta, multiplier))
        status = "ACTIVE"

    return AdaptiveWeight(
        version=version,
        provider=sample.provider,
        sport=sample.sport,
        market_type=sample.market_type,
        sample_size=sample.total_resolved,
        eligible_samples=eligible_samples,
        model_errors=sample.model_errors,
        hit_rate=hit_rate,
        multiplier=round(multiplier, 6),
        active=bool(active),
        status=status,
    )


def adaptive_score_adjustment(
    base_score: int | float,
    multiplier: Optional[float],
    *,
    max_points: int = 2,
) -> int:
    """Translate an eligible multiplier into a tiny ranking-only adjustment."""
    if multiplier is None or not isinstance(base_score, (int, float)):
        return 0
    if not isinstance(multiplier, (int, float)):
        return 0
    # The point cap is separate from the multiplier cap so the ranking budget
    # remains small if a future database row contains an invalid multiplier.
    raw = (float(multiplier) - 1.0) * float(max_points) / DEFAULT_MAX_DELTA
    return max(-abs(int(max_points)), min(abs(int(max_points)), int(round(raw))))


def sample_from_outcomes(
    *,
    provider: str,
    sport: str,
    market_type: str,
    outcomes: list[tuple[str, Optional[str]]],
) -> AdaptiveSample:
    """Aggregate ``(result, error_type)`` pairs using the learning policy."""
    hits = model_errors = pushes = excluded_misses = 0
    for result, error_type in outcomes:
        result = str(result or "").upper()
        if result == "HIT":
            hits += 1
        elif result == "PUSH":
            pushes += 1
        elif result == "MISS" and str(error_type or "") == LEARNABLE_ERROR_TYPE:
            model_errors += 1
        elif result == "MISS":
            excluded_misses += 1
    return AdaptiveSample(
        provider=str(provider or "Underdog"),
        sport=str(sport or "UNKNOWN").upper(),
        market_type=str(market_type or "UNKNOWN"),
        hits=hits,
        model_errors=model_errors,
        pushes=pushes,
        excluded_misses=excluded_misses,
    )