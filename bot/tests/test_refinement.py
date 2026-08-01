"""
test_refinement.py — Contract tests for engine/refinement.py.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from engine.refinement import (
    RefinementRule,
    RefinementTrigger,
    RefinementEngine,
    default_rules,
    get_refinement_engine,
    VALID_TRIGGERS,
    VALID_ACTIONS,
)


# ── RefinementRule ────────────────────────────────────────────────────────────

class TestRefinementRule:
    def test_valid_rule(self):
        r = RefinementRule(
            rule_id="test-rule", description="Test",
            trigger="MISS_RATE_HIGH", threshold=0.60, action="LOG",
        )
        assert r.rule_id == "test-rule"
        assert r.active is True

    def test_invalid_trigger_raises(self):
        with pytest.raises(ValueError, match="trigger"):
            RefinementRule(
                rule_id="x", description="x",
                trigger="INVALID_TRIGGER", threshold=0.5, action="LOG",
            )

    def test_invalid_action_raises(self):
        with pytest.raises(ValueError, match="action"):
            RefinementRule(
                rule_id="x", description="x",
                trigger="MISS_RATE_HIGH", threshold=0.5, action="INVALID_ACTION",
            )

    def test_all_valid_triggers_accepted(self):
        for trigger in VALID_TRIGGERS:
            r = RefinementRule(
                rule_id=f"t-{trigger}", description="x",
                trigger=trigger, threshold=0.5, action="LOG",
            )
            assert r.trigger == trigger

    def test_all_valid_actions_accepted(self):
        for action in VALID_ACTIONS:
            r = RefinementRule(
                rule_id=f"a-{action}", description="x",
                trigger="MISS_RATE_HIGH", threshold=0.5, action=action,
            )
            assert r.action == action

    def test_optional_sport_filter(self):
        r = RefinementRule(
            rule_id="x", description="x",
            trigger="MISS_RATE_HIGH", threshold=0.60, action="LOG",
            sport="NBA",
        )
        assert r.sport == "NBA"

    def test_fire_count_starts_zero(self):
        r = RefinementRule(
            rule_id="x", description="x",
            trigger="MISS_RATE_HIGH", threshold=0.60, action="LOG",
        )
        assert r.fire_count == 0


# ── RefinementTrigger ─────────────────────────────────────────────────────────

class TestRefinementTrigger:
    def _make_trigger(self):
        return RefinementTrigger(
            rule_id="test", rule_description="Test Rule",
            trigger="MISS_RATE_HIGH", action="ALERT",
            sport=None, threshold=0.60, metric_observed=0.70,
            action_advisory="Review calibration.",
        )

    def test_frozen(self):
        t = self._make_trigger()
        with pytest.raises((AttributeError, TypeError)):
            t.rule_id = "changed"

    def test_to_telegram_returns_string(self):
        t = self._make_trigger()
        s = t.to_telegram()
        assert isinstance(s, str)
        assert "Refinement" in s

    def test_to_telegram_shows_metric(self):
        t = self._make_trigger()
        s = t.to_telegram()
        assert "0.700" in s or "0.70" in s

    def test_to_telegram_shows_advisory(self):
        t = self._make_trigger()
        s = t.to_telegram()
        assert "Review calibration" in s

    def test_to_telegram_with_sport(self):
        t = RefinementTrigger(
            rule_id="x", rule_description="x", trigger="MISS_RATE_HIGH",
            action="ALERT", sport="NBA", threshold=0.65, metric_observed=0.70,
            action_advisory="advisory",
        )
        s = t.to_telegram()
        assert "NBA" in s


# ── RefinementEngine ──────────────────────────────────────────────────────────

class TestRefinementEngine:
    def test_default_rules_loaded(self):
        eng = RefinementEngine()
        assert len(eng.rules) > 0

    def test_custom_rules(self):
        custom = [
            RefinementRule(
                rule_id="x", description="x",
                trigger="MISS_RATE_HIGH", threshold=0.5, action="LOG",
            )
        ]
        eng = RefinementEngine(rules=custom)
        assert len(eng.rules) == 1

    def test_add_rule(self):
        eng = RefinementEngine(rules=[])
        r = RefinementRule(rule_id="new", description="x",
                           trigger="CLV_NEGATIVE", threshold=-1.0, action="LOG")
        eng.add_rule(r)
        assert any(x.rule_id == "new" for x in eng.rules)

    def test_add_rule_replaces_existing_same_id(self):
        eng = RefinementEngine(rules=[])
        r1 = RefinementRule(rule_id="dup", description="v1",
                            trigger="CLV_NEGATIVE", threshold=-1.0, action="LOG")
        r2 = RefinementRule(rule_id="dup", description="v2",
                            trigger="CLV_NEGATIVE", threshold=-2.0, action="ALERT")
        eng.add_rule(r1)
        eng.add_rule(r2)
        matching = [r for r in eng.rules if r.rule_id == "dup"]
        assert len(matching) == 1
        assert matching[0].description == "v2"

    def test_remove_rule(self):
        eng = RefinementEngine()
        rules_before = len(eng.rules)
        first_id = eng.rules[0].rule_id
        removed = eng.remove_rule(first_id)
        assert removed is True
        assert len(eng.rules) == rules_before - 1

    def test_remove_nonexistent_returns_false(self):
        eng = RefinementEngine()
        assert eng.remove_rule("nonexistent-xxx") is False

    def test_evaluate_empty_metrics_no_triggers(self):
        eng = RefinementEngine()
        triggers = eng.evaluate({})
        assert isinstance(triggers, list)

    def test_miss_rate_high_fires(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="t", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.60, action="ALERT")
        ])
        triggers = eng.evaluate({"miss_rate": 0.70})
        assert len(triggers) == 1
        assert triggers[0].rule_id == "t"
        assert triggers[0].metric_observed == 0.70

    def test_miss_rate_high_does_not_fire_below_threshold(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="t", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.60, action="ALERT")
        ])
        triggers = eng.evaluate({"miss_rate": 0.50})
        assert len(triggers) == 0

    def test_clv_negative_fires(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="c", description="d",
                           trigger="CLV_NEGATIVE", threshold=-1.0, action="LOG")
        ])
        triggers = eng.evaluate({"avg_clv": -2.5})
        assert len(triggers) == 1

    def test_clv_negative_does_not_fire_for_positive(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="c", description="d",
                           trigger="CLV_NEGATIVE", threshold=-1.0, action="LOG")
        ])
        triggers = eng.evaluate({"avg_clv": 1.5})
        assert len(triggers) == 0

    def test_tier_degraded_fires(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="t", description="d",
                           trigger="TIER_DEGRADED", threshold=0.40, action="ALERT")
        ])
        triggers = eng.evaluate({"s_tier_miss_rate": 0.55})
        assert len(triggers) == 1

    def test_alert_volume_drop_fires(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="v", description="d",
                           trigger="ALERT_VOLUME_DROP", threshold=0.40, action="ALERT")
        ])
        triggers = eng.evaluate({"today_alerts": 5, "yesterday_alerts": 30})
        assert len(triggers) == 1

    def test_alert_volume_drop_no_baseline_no_fire(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="v", description="d",
                           trigger="ALERT_VOLUME_DROP", threshold=0.40, action="ALERT")
        ])
        triggers = eng.evaluate({"today_alerts": 0, "yesterday_alerts": 1})
        assert len(triggers) == 0

    def test_sport_filtered_rule_fires_for_matching_sport(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="mlb", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.65, action="ALERT",
                           sport="MLB")
        ])
        triggers = eng.evaluate({"miss_rate_by_sport": {"MLB": 0.70}})
        assert len(triggers) == 1

    def test_sport_filtered_rule_does_not_fire_for_other_sport(self):
        eng = RefinementEngine(rules=[
            RefinementRule(rule_id="mlb", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.65, action="ALERT",
                           sport="MLB")
        ])
        triggers = eng.evaluate({"miss_rate_by_sport": {"NBA": 0.70}})
        assert len(triggers) == 0

    def test_inactive_rule_does_not_fire(self):
        r = RefinementRule(rule_id="x", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.60, action="LOG",
                           active=False)
        eng = RefinementEngine(rules=[r])
        triggers = eng.evaluate({"miss_rate": 0.90})
        assert len(triggers) == 0

    def test_fire_count_increments(self):
        r = RefinementRule(rule_id="x", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.60, action="LOG")
        eng = RefinementEngine(rules=[r])
        eng.evaluate({"miss_rate": 0.70})
        eng.evaluate({"miss_rate": 0.75})
        assert eng.rules[0].fire_count == 2

    def test_last_triggered_at_set(self):
        r = RefinementRule(rule_id="x", description="d",
                           trigger="MISS_RATE_HIGH", threshold=0.60, action="LOG")
        eng = RefinementEngine(rules=[r])
        eng.evaluate({"miss_rate": 0.70})
        assert eng.rules[0].last_triggered_at is not None

    def test_rules_summary_returns_string(self):
        eng = RefinementEngine()
        s = eng.rules_summary()
        assert isinstance(s, str)


# ── default_rules ─────────────────────────────────────────────────────────────

class TestDefaultRules:
    def test_returns_list(self):
        rules = default_rules()
        assert isinstance(rules, list)
        assert len(rules) > 0

    def test_all_rules_valid(self):
        for rule in default_rules():
            assert rule.trigger in VALID_TRIGGERS
            assert rule.action in VALID_ACTIONS

    def test_no_duplicate_ids(self):
        rules = default_rules()
        ids = [r.rule_id for r in rules]
        assert len(ids) == len(set(ids))


# ── get_refinement_engine singleton ───────────────────────────────────────────

class TestGetRefinementEngine:
    def test_returns_refinement_engine(self):
        eng = get_refinement_engine()
        assert isinstance(eng, RefinementEngine)

    def test_returns_same_instance(self):
        eng1 = get_refinement_engine()
        eng2 = get_refinement_engine()
        assert eng1 is eng2
