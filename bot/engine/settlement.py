"""
engine/settlement.py — Settlement Awareness (Framework v3.0 Layer 12).

Detects settlement anomalies before they corrupt the learning loop.

Settlement flag types
──────────────────────
  VOID               — Result likely voided (player did not play, 0 recorded,
                       or result within floating-point epsilon of the line).
  PLATFORM_DIFF      — Known difference in how platforms settle this stat type.
  UNUSUAL_RESULT     — Actual value is a statistical outlier vs the line.
  PENDING            — Game has not yet completed.
  CLEAN              — No anomalies detected.

Design constraints
──────────────────
• All functions are pure — no IO, no async.
• Only MODEL errors should update weights (calibration.MissType.MODEL).
• Settlement and Variance misses must NOT update weights.
• classify_miss() in calibration.py is not replaced — settlement.py adds a
  pre-check that may override the MissType before it reaches classify_miss().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# ── Settlement flag ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SettlementFlag:
    """
    Result of a settlement anomaly check for one graded prop.

    code        : "VOID" | "PLATFORM_DIFF" | "UNUSUAL_RESULT" | "PENDING" | "CLEAN"
    severity    : "LOW" | "MEDIUM" | "HIGH" | "NONE"
    description : Human-readable explanation stored in grading log.
    is_learnable: When False, this result must NOT update model weights.
    """
    code:         str   # see module docstring
    severity:     str   # "NONE" | "LOW" | "MEDIUM" | "HIGH"
    description:  str
    is_learnable: bool  # False → do not update weights


# ── Singleton flags for common outcomes ───────────────────────────────────────

FLAG_CLEAN = SettlementFlag(
    code="CLEAN", severity="NONE",
    description="No settlement anomalies detected.",
    is_learnable=True,
)

FLAG_PENDING = SettlementFlag(
    code="PENDING", severity="NONE",
    description="Game has not yet completed — result is pending.",
    is_learnable=False,
)


# ── Platform settlement profiles ───────────────────────────────────────────────

#: Known platform-specific settlement rules.
#: Each entry: {stat_type_keyword: description}
_PLATFORM_DIFFS: dict[str, dict[str, str]] = {
    "PrizePicks": {
        "points":           "PrizePicks settles points on standard NBA scoring (excl. OT for some contests).",
        "rebounds":         "PrizePicks includes offensive and defensive rebounds.",
        "assists":          "PrizePicks uses official box score assists.",
        "strikeouts":       "PrizePicks uses pitcher K totals from official MLB box score.",
        "home_runs":        "PrizePicks settles home runs on official MLB scoring.",
        "kills":            "PrizePicks CS/DOTA kills from official match data.",
        "pitching_strikeouts": "PrizePicks settles pitcher Ks including the 9th inning.",
    },
    "Underdog": {
        "points":           "Underdog Fantasy settles on official NBA scoring.",
        "rebounds":         "Underdog counts total rebounds.",
        "minutes":          "Underdog settles on official minutes played (rounded to nearest 0.5).",
        "aces":             "Underdog Tennis settles on ace count from official ATP/WTA data.",
    },
}

# Stat types where platforms are known to differ in edge cases
_KNOWN_PLATFORM_DIFF_STATS = frozenset({
    "minutes",           # rounding differs between platforms
    "pitching_strikeouts",  # 9th inning treatment
    "aces",             # some platforms count unreturnable serves differently
    "turnovers",        # some platforms include team turnovers
})


# ── Detection functions ────────────────────────────────────────────────────────

_VOID_EPSILON = 0.01   # abs(actual - line) within this → likely data artefact


def detect_void(
    actual:   float,
    line:     float,
    provider: str = "",
) -> bool:
    """
    Return True when the actual value strongly suggests the result was voided.

    Void conditions:
    1. actual == 0.0 and line > 0.5 — player almost certainly did not play.
    2. abs(actual - line) < _VOID_EPSILON — result suspiciously equal to the line
       (platform may have settled at the line due to voiding).
    """
    if actual == 0.0 and line >= 0.5:
        return True
    # Only flag "actual equals line" as suspicious for meaningful lines (≥ 0.5).
    # A line of 0.0 is degenerate / unmeasured — not a void signal.
    if line >= 0.5 and abs(actual - line) < _VOID_EPSILON:
        return True
    return False


def detect_platform_difference(
    stat_type:   str,
    provider_a:  str,
    provider_b:  str,
) -> Optional[str]:
    """
    Return a description if *stat_type* is known to be settled differently
    between *provider_a* and *provider_b*, else None.
    """
    if stat_type in _KNOWN_PLATFORM_DIFF_STATS:
        desc_a = _PLATFORM_DIFFS.get(provider_a, {}).get(stat_type, "")
        desc_b = _PLATFORM_DIFFS.get(provider_b, {}).get(stat_type, "")
        if desc_a or desc_b:
            return (
                f"{stat_type} is settled differently across platforms. "
                + (f"{provider_a}: {desc_a} " if desc_a else "")
                + (f"{provider_b}: {desc_b}" if desc_b else "")
            ).strip()
    return None


def detect_unusual_result(
    actual: float,
    line:   float,
) -> Optional[str]:
    """
    Return a description if *actual* is a statistical outlier relative to *line*.

    Unusual conditions:
    1. actual > 3 * line  — blowout performance; valid but outlier.
    2. actual < 0.1 * line and line > 2.0 — drastically under-performed.
    3. Negative actual   — data error.
    """
    if actual < 0:
        return f"Negative actual value ({actual}) — likely a data error."
    if line > 0.5 and actual > 3 * line:
        return (
            f"Actual {actual:.1f} is >3× the prop line {line:.1f} — "
            "outlier performance; valid but statistical anomaly."
        )
    if line > 2.0 and actual < 0.1 * line:
        return (
            f"Actual {actual:.1f} is <10% of prop line {line:.1f} — "
            "player may not have participated or data may be incorrect."
        )
    return None


# ── Primary entry point ────────────────────────────────────────────────────────

def check_settlement(
    actual:    Optional[float],
    line:      Optional[float],
    provider:  str = "Underdog",
    stat_type: str = "",
    game_time: Optional[datetime] = None,
) -> SettlementFlag:
    """
    Run all settlement checks and return the most severe flag.

    Parameters
    ----------
    actual    : Graded actual stat value from game results.  None = pending.
    line      : The prop line at alert time.
    provider  : Platform that offered the prop (for platform-diff checks).
    stat_type : Prop stat type (for platform-diff checks).
    game_time : UTC game start time (for pending detection).

    Returns
    -------
    SettlementFlag — always returned; never raises.
    """
    # Pending: game time not yet passed
    if actual is None:
        return FLAG_PENDING

    if line is None:
        return SettlementFlag(
            code="VOID", severity="HIGH",
            description="Line value not recorded at alert time — cannot grade.",
            is_learnable=False,
        )

    # Void detection
    if detect_void(actual, line, provider):
        return SettlementFlag(
            code="VOID", severity="HIGH",
            description=(
                f"Actual value {actual:.2f} vs line {line:.2f} — "
                "result appears voided (player may not have participated)."
            ),
            is_learnable=False,
        )

    # Unusual result
    unusual = detect_unusual_result(actual, line)
    if unusual:
        return SettlementFlag(
            code="UNUSUAL_RESULT", severity="MEDIUM",
            description=unusual,
            is_learnable=False,  # outliers must not update weights
        )

    # Platform difference
    other_provider = "PrizePicks" if provider == "Underdog" else "Underdog"
    diff = detect_platform_difference(stat_type, provider, other_provider)
    if diff:
        return SettlementFlag(
            code="PLATFORM_DIFF", severity="LOW",
            description=diff,
            is_learnable=True,   # low-severity; still learnable
        )

    return FLAG_CLEAN


def override_miss_type_for_settlement(
    flag: SettlementFlag,
    current_miss_type: str,
) -> str:
    """
    Override a MissType classification based on a settlement flag.

    Called before classify_miss() result is stored in PropOpportunityLog.error_type.

    Rules
    -----
    • VOID flag          → always "Settlement" (never update weights)
    • UNUSUAL_RESULT     → "Settlement" (outlier, not a model failure)
    • PLATFORM_DIFF      → "Market" (structural, not a model failure)
    • CLEAN / PENDING    → return current_miss_type unchanged

    Parameters
    ----------
    flag              : SettlementFlag from check_settlement().
    current_miss_type : The MissType.value string from classify_miss().
    """
    if flag.code == "VOID":
        return "Settlement"
    if flag.code == "UNUSUAL_RESULT":
        return "Settlement"
    if flag.code == "PLATFORM_DIFF":
        # Only override if the current type is more severe
        if current_miss_type == "Model":
            return "Market"
    return current_miss_type


def settlement_flag_telegram(flag: SettlementFlag) -> str:
    """Format a SettlementFlag for Telegram HTML inline display."""
    import html
    icons = {"CLEAN": "✅", "PENDING": "⏳", "VOID": "❌", "UNUSUAL_RESULT": "⚠️", "PLATFORM_DIFF": "ℹ️"}
    icon = icons.get(flag.code, "❓")
    return f"{icon} Settlement: <code>{flag.code}</code> — <i>{html.escape(flag.description[:120])}</i>"
