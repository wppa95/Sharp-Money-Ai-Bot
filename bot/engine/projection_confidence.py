"""
engine/projection_confidence.py — Projection Confidence Engine.

Measures how confident we are in the historical / projection signal
(L5–L30, sample strength, consistency) — independent of Bet Quality.

Bet Quality  = "How good is this bet at the current line?"
Projection Confidence = "How reliable is the historical edge signal?"

This module does NOT decide OVER/UNDER or grade market quality.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class ProjectionConfidenceTier(str, enum.Enum):
    ELITE     = "ELITE"       # 90–100
    HIGH      = "HIGH"        # 75–89
    MEDIUM    = "MEDIUM"      # 60–74
    LOW       = "LOW"         # 40–59
    THIN      = "THIN"        # < 40

    @classmethod
    def from_score(cls, score: int) -> "ProjectionConfidenceTier":
        if score >= 90:
            return cls.ELITE
        if score >= 75:
            return cls.HIGH
        if score >= 60:
            return cls.MEDIUM
        if score >= 40:
            return cls.LOW
        return cls.THIN


@dataclass(frozen=True)
class ProjectionConfidenceResult:
    score: int                          # 0–100
    tier: ProjectionConfidenceTier
    sample_strength: int = 0            # 0–100
    n_history: int = 0
    notes: str = ""

    @property
    def label(self) -> str:
        return f"{self.tier.value} ({self.score}/100)"


def score_projection_confidence(
    *,
    sample_strength: int = 0,
    n_history: int = 0,
    l5_hit_rate: Optional[float] = None,
    l10_hit_rate: Optional[float] = None,
    l20_hit_rate: Optional[float] = None,
) -> ProjectionConfidenceResult:
    """
    Score projection confidence from historical evidence only.

    Weights (simple v1):
      50% sample_strength
      30% sample size (n_history)
      20% short-window alignment (L5/L10 if present)
    """
    # Sample size component (0–100)
    if n_history >= 30:
        size_score = 100
    elif n_history >= 20:
        size_score = 80
    elif n_history >= 10:
        size_score = 60
    elif n_history >= 5:
        size_score = 40
    else:
        size_score = 20

    ss = max(0, min(100, int(sample_strength)))

    # Window alignment: average available hit rates (as 0–100)
    rates = [r for r in (l5_hit_rate, l10_hit_rate, l20_hit_rate) if r is not None]
    if rates:
        window_score = int(sum(rates) / len(rates) * 100)
    else:
        window_score = 50  # neutral when missing

    score = int(0.50 * ss + 0.30 * size_score + 0.20 * window_score)
    score = max(0, min(100, score))
    tier = ProjectionConfidenceTier.from_score(score)

    notes_parts = [f"n={n_history}", f"ss={ss}"]
    if rates:
        notes_parts.append(f"windows={len(rates)}")

    return ProjectionConfidenceResult(
        score=score,
        tier=tier,
        sample_strength=ss,
        n_history=n_history,
        notes=" · ".join(notes_parts),
    )