"""
engine/refinement.py — Continuous Refinement Engine (Framework v3.0 Layer 13).

Implements the problem → rule → update → test → track feedback loop.

Architecture
─────────────
  RefinementRule    — A named condition that triggers when a threshold is breached.
  RefinementTrigger — A fired rule: what triggered, when, and what action to take.
  RefinementEngine  — Pure evaluator: given a metrics dict, returns fired triggers.

Rules are evaluated by evaluate(metrics) → list[RefinementTrigger].
Rules are never applied automatically — the action is advisory.

Supported triggers (rule.trigger values)
──────────────────────────────────────────
  MISS_RATE_HIGH        — win rate for a tier/sport drops below threshold
  CLV_NEGATIVE          — average CLV drops below threshold
  TIER_DEGRADED         — S/A-tier accuracy drops below B-tier accuracy
  SAMPLE_TOO_THIN       — active sport has fewer samples than minimum
  BLOCK_RATE_HIGH       — too many props blocked for a sport (data quality issue)
  ALERT_VOLUME_DROP     — daily alert count drops >50% vs prior day
  VARIANCE_SPIKE        — rolling variance for a stat exceeds threshold
  CUSTOM                — user-defined trigger condition

Supported actions (rule.action values)
──────────────────────────────────────
  LOG              — write to logger only; no user action required
  ALERT            — send a Telegram notification to the operator
  BLOCK_SPORT      — flag the sport for manual review (does NOT auto-block)
  ADJUST_THRESHOLD — flag that a min-confidence threshold may need adjustment
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Valid trigger and action codes ────────────────────────────────────────────

VALID_TRIGGERS = frozenset({
    "MISS_RATE_HIGH",
    "CLV_NEGATIVE",
    "TIER_DEGRADED",
    "SAMPLE_TOO_THIN",
    "BLOCK_RATE_HIGH",
    "ALERT_VOLUME_DROP",
    "VARIANCE_SPIKE",
    "CUSTOM",
})

VALID_ACTIONS = frozenset({
    "LOG",
    "ALERT",
    "BLOCK_SPORT",
    "ADJUST_THRESHOLD",
})


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class RefinementRule:
    """
    A named monitoring rule that fires when a threshold is breached.

    Attributes
    ----------
    rule_id     : Unique identifier (slug style: "nba-miss-rate-high").
    description : Human-readable description of what this rule monitors.
    trigger     : One of VALID_TRIGGERS.
    threshold   : Numeric boundary that, when crossed, fires the rule.
                  Interpretation depends on trigger type — see module docstring.
    action      : Advisory action when the rule fires. One of VALID_ACTIONS.
    active      : When False, rule is not evaluated.
    sport       : Optional sport filter ("NBA", "MLB", …). None = all sports.
    stat_type   : Optional stat filter. None = all stat types.
    created_at  : UTC datetime of rule creation.
    last_triggered_at : UTC datetime the rule last fired. None = never.
    fire_count  : How many times this rule has fired since creation.
    """
    rule_id:             str
    description:         str
    trigger:             str
    threshold:           float
    action:              str
    active:              bool              = True
    sport:               Optional[str]    = None
    stat_type:           Optional[str]    = None
    created_at:          datetime         = field(default_factory=datetime.utcnow)
    last_triggered_at:   Optional[datetime] = None
    fire_count:          int              = 0

    def __post_init__(self) -> None:
        if self.trigger not in VALID_TRIGGERS:
            raise ValueError(
                f"trigger {self.trigger!r} is not valid. "
                f"Use one of: {sorted(VALID_TRIGGERS)}"
            )
        if self.action not in VALID_ACTIONS:
            raise ValueError(
                f"action {self.action!r} is not valid. "
                f"Use one of: {sorted(VALID_ACTIONS)}"
            )


@dataclass(frozen=True)
class RefinementTrigger:
    """
    A fired rule — produced when evaluate() detects a threshold breach.

    action_advisory is the human-readable instruction for the operator.
    metric_observed is the value that crossed the threshold.
    """
    rule_id:           str
    rule_description:  str
    trigger:           str
    action:            str
    sport:             Optional[str]
    threshold:         float
    metric_observed:   float
    action_advisory:   str
    fired_at:          datetime = field(default_factory=datetime.utcnow)

    def to_telegram(self) -> str:
        import html
        icon = {"LOG": "📝", "ALERT": "🔔", "BLOCK_SPORT": "🚫", "ADJUST_THRESHOLD": "⚙️"}.get(
            self.action, "⚠️"
        )
        sport_str = f" [{html.escape(self.sport)}]" if self.sport else ""
        return (
            f"{icon} <b>Refinement Rule Fired</b>{sport_str}\n"
            f"  Rule: <i>{html.escape(self.rule_description)}</i>\n"
            f"  Observed: <code>{self.metric_observed:.3f}</code>  "
            f"Threshold: <code>{self.threshold:.3f}</code>\n"
            f"  Advisory: {html.escape(self.action_advisory)}"
        )


# ── Built-in default rules ─────────────────────────────────────────────────────

def default_rules() -> list[RefinementRule]:
    """
    Return the default set of refinement rules.

    These rules are added to every new RefinementEngine instance.
    """
    return [
        RefinementRule(
            rule_id     = "global-miss-rate-high",
            description = "Overall MISS rate exceeds 60% across all resolved props",
            trigger     = "MISS_RATE_HIGH",
            threshold   = 0.60,
            action      = "ALERT",
        ),
        RefinementRule(
            rule_id     = "global-clv-negative",
            description = "Average CLV has gone negative — alerts are beating the market less than expected",
            trigger     = "CLV_NEGATIVE",
            threshold   = -1.0,    # CLV below -1.0% triggers
            action      = "ALERT",
        ),
        RefinementRule(
            rule_id     = "s-tier-miss-high",
            description = "S-tier MISS rate exceeds 40% — highest-confidence picks are underperforming",
            trigger     = "TIER_DEGRADED",
            threshold   = 0.40,
            action      = "ALERT",
        ),
        RefinementRule(
            rule_id     = "mlb-miss-rate-high",
            description = "MLB MISS rate exceeds 65% — high-variance sport may need threshold adjustment",
            trigger     = "MISS_RATE_HIGH",
            threshold   = 0.65,
            action      = "ADJUST_THRESHOLD",
            sport       = "MLB",
        ),
        RefinementRule(
            rule_id     = "alert-volume-drop",
            description = "Daily alert count dropped >60% vs prior day — provider may be down",
            trigger     = "ALERT_VOLUME_DROP",
            threshold   = 0.40,    # today_count / yesterday_count < 0.40
            action      = "ALERT",
        ),
    ]


# ── Evaluation engine ─────────────────────────────────────────────────────────

class RefinementEngine:
    """
    Pure evaluator: given a performance metrics dict, return fired triggers.

    Usage::

        engine = RefinementEngine()
        metrics = {
            "miss_rate":          0.55,
            "miss_rate_by_sport": {"MLB": 0.70},
            "avg_clv":            -2.0,
            "s_tier_miss_rate":   0.45,
            "today_alerts":       5,
            "yesterday_alerts":   30,
        }
        triggers = engine.evaluate(metrics)
        for t in triggers:
            print(t.to_telegram())

    The engine is stateless between evaluate() calls — it does not store
    performance data itself.  Callers are responsible for supplying the metrics.
    """

    def __init__(self, rules: Optional[list[RefinementRule]] = None) -> None:
        self._rules: list[RefinementRule] = list(rules or default_rules())

    @property
    def rules(self) -> list[RefinementRule]:
        return list(self._rules)

    def add_rule(self, rule: RefinementRule) -> None:
        """Add a custom rule to the engine."""
        # Replace if rule_id already exists
        self._rules = [r for r in self._rules if r.rule_id != rule.rule_id]
        self._rules.append(rule)

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a rule by rule_id. Returns True if removed."""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.rule_id != rule_id]
        return len(self._rules) < before

    def evaluate(self, metrics: dict) -> list[RefinementTrigger]:
        """
        Evaluate all active rules against the provided metrics dict.

        Parameters
        ----------
        metrics : dict of performance metrics.  Keys consumed:

          miss_rate            : float 0-1  — overall MISS rate
          miss_rate_by_sport   : dict[str, float] — per-sport MISS rate
          avg_clv              : float  — average CLV% (negative = losing CLV)
          s_tier_miss_rate     : float 0-1 — S-tier MISS rate
          a_tier_miss_rate     : float 0-1 — A-tier MISS rate
          b_tier_miss_rate     : float 0-1 — B-tier MISS rate
          today_alerts         : int   — alert count today
          yesterday_alerts     : int   — alert count yesterday
          sample_by_sport      : dict[str, int] — sample sizes by sport
          block_rate_by_sport  : dict[str, float] — block rate by sport

        Returns
        -------
        list[RefinementTrigger] — fired triggers; empty when all rules pass.
        """
        triggers: list[RefinementTrigger] = []

        for rule in self._rules:
            if not rule.active:
                continue
            trigger = self._evaluate_rule(rule, metrics)
            if trigger is not None:
                rule.last_triggered_at = datetime.utcnow()
                rule.fire_count += 1
                triggers.append(trigger)

        if triggers:
            logger.info(
                "refinement: %d rule(s) fired: %s",
                len(triggers),
                [t.rule_id for t in triggers],
            )

        return triggers

    # ── Internal evaluators ───────────────────────────────────────────────────

    def _evaluate_rule(
        self,
        rule:    RefinementRule,
        metrics: dict,
    ) -> Optional[RefinementTrigger]:
        """Evaluate one rule; return a trigger if it fires, else None."""
        try:
            if rule.trigger == "MISS_RATE_HIGH":
                return self._eval_miss_rate(rule, metrics)
            if rule.trigger == "CLV_NEGATIVE":
                return self._eval_clv_negative(rule, metrics)
            if rule.trigger == "TIER_DEGRADED":
                return self._eval_tier_degraded(rule, metrics)
            if rule.trigger == "ALERT_VOLUME_DROP":
                return self._eval_alert_volume_drop(rule, metrics)
            if rule.trigger == "SAMPLE_TOO_THIN":
                return self._eval_sample_thin(rule, metrics)
        except Exception as exc:
            logger.debug("refinement: rule %s eval error: %s", rule.rule_id, exc)
        return None

    def _eval_miss_rate(self, rule: RefinementRule, m: dict) -> Optional[RefinementTrigger]:
        if rule.sport:
            rate = (m.get("miss_rate_by_sport") or {}).get(rule.sport)
        else:
            rate = m.get("miss_rate")
        if rate is None or rate < rule.threshold:
            return None
        return RefinementTrigger(
            rule_id          = rule.rule_id,
            rule_description = rule.description,
            trigger          = rule.trigger,
            action           = rule.action,
            sport            = rule.sport,
            threshold        = rule.threshold,
            metric_observed  = rate,
            action_advisory  = (
                f"MISS rate of {rate*100:.1f}% exceeds threshold {rule.threshold*100:.0f}%. "
                + (f"Review {rule.sport} scoring thresholds." if rule.sport
                   else "Review overall confidence calibration.")
            ),
        )

    def _eval_clv_negative(self, rule: RefinementRule, m: dict) -> Optional[RefinementTrigger]:
        clv = m.get("avg_clv")
        if clv is None or clv >= rule.threshold:
            return None
        return RefinementTrigger(
            rule_id          = rule.rule_id,
            rule_description = rule.description,
            trigger          = rule.trigger,
            action           = rule.action,
            sport            = rule.sport,
            threshold        = rule.threshold,
            metric_observed  = clv,
            action_advisory  = (
                f"Average CLV {clv:+.2f}% is below threshold {rule.threshold:+.2f}%. "
                "Review alert selection criteria and EV calculation."
            ),
        )

    def _eval_tier_degraded(self, rule: RefinementRule, m: dict) -> Optional[RefinementTrigger]:
        s_rate = m.get("s_tier_miss_rate")
        if s_rate is None or s_rate < rule.threshold:
            return None
        return RefinementTrigger(
            rule_id          = rule.rule_id,
            rule_description = rule.description,
            trigger          = rule.trigger,
            action           = rule.action,
            sport            = rule.sport,
            threshold        = rule.threshold,
            metric_observed  = s_rate,
            action_advisory  = (
                f"S-tier MISS rate of {s_rate*100:.1f}% exceeds threshold "
                f"{rule.threshold*100:.0f}%. "
                "High-confidence picks are underperforming — review S-tier calibration."
            ),
        )

    def _eval_alert_volume_drop(self, rule: RefinementRule, m: dict) -> Optional[RefinementTrigger]:
        today     = m.get("today_alerts", 0)
        yesterday = m.get("yesterday_alerts", 0)
        if yesterday <= 2:
            return None   # Not enough baseline
        ratio = today / yesterday
        if ratio >= rule.threshold:
            return None
        return RefinementTrigger(
            rule_id          = rule.rule_id,
            rule_description = rule.description,
            trigger          = rule.trigger,
            action           = rule.action,
            sport            = rule.sport,
            threshold        = rule.threshold,
            metric_observed  = ratio,
            action_advisory  = (
                f"Today: {today} alerts vs yesterday: {yesterday} "
                f"(ratio {ratio:.2f} < threshold {rule.threshold:.2f}). "
                "Check provider connectivity and Underdog API status."
            ),
        )

    def _eval_sample_thin(self, rule: RefinementRule, m: dict) -> Optional[RefinementTrigger]:
        if rule.sport:
            n = (m.get("sample_by_sport") or {}).get(rule.sport, 0)
        else:
            n = m.get("total_samples", 0)
        if n >= rule.threshold:
            return None
        return RefinementTrigger(
            rule_id          = rule.rule_id,
            rule_description = rule.description,
            trigger          = rule.trigger,
            action           = rule.action,
            sport            = rule.sport,
            threshold        = rule.threshold,
            metric_observed  = float(n),
            action_advisory  = (
                f"Sample size {n} is below minimum {rule.threshold:.0f}. "
                "Insufficient data to calibrate this sport/stat reliably."
            ),
        )

    def rules_summary(self, include_inactive: bool = False) -> str:
        """Return a plain-text summary of all rules for Telegram display."""
        lines = [f"⚙️ <b>Refinement Rules ({len(self._rules)} total)</b>", ""]
        for rule in self._rules:
            if not include_inactive and not rule.active:
                continue
            status = "✅" if rule.active else "⏸"
            sport_str = f" [{rule.sport}]" if rule.sport else ""
            import html
            lines.append(
                f"  {status} <code>{html.escape(rule.rule_id)}</code>"
                f"{sport_str}\n"
                f"     {html.escape(rule.description)}\n"
                f"     Threshold: <code>{rule.threshold}</code>  "
                f"Action: <code>{rule.action}</code>  "
                f"Fired: {rule.fire_count}×"
            )
        return "\n".join(lines) if len(lines) > 2 else "No rules configured."


# ── Module-level singleton ─────────────────────────────────────────────────────

_engine: Optional[RefinementEngine] = None


def get_refinement_engine() -> RefinementEngine:
    """Return (or lazily create) the module-level RefinementEngine singleton."""
    global _engine
    if _engine is None:
        _engine = RefinementEngine()
    return _engine
