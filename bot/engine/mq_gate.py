from __future__ import annotations

from typing import Optional


_DIRECTION_SUPPORT = {
    "OVER": {"support": 0.60, "contradict": 0.40},
    "UNDER": {"support": 0.40, "contradict": 0.60},
}

_WINDOW_FIELDS = (
    ("l5", "l5_games", "l5_hit_rate"),
    ("l10", "l10_games", "l10_hit_rate"),
    ("l20", "l20_games", "l20_hit_rate"),
    ("l30", "l30_games", "l30_hit_rate"),
    ("season", "season_games", "season_hit_rate"),
)

_MQ_DIRECTIONAL_REASONS = (
    ("HIGH-FLOOR STAT", {"OVER": "support", "UNDER": "contradict"}),
    ("HIGH-VARIANCE MARKET", {"OVER": "contradict", "UNDER": "support"}),
)


def _normalize_mq_label(market_quality: Optional[object]) -> Optional[str]:
    raw = getattr(market_quality, "label", market_quality)
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    label = str(value).strip().upper()
    if "." in label:
        label = label.rsplit(".", 1)[-1]
    return label or None


def _iter_windows(decision: object):
    for name, games_attr, hit_attr in _WINDOW_FIELDS:
        games = getattr(decision, games_attr, None)
        hit_rate = getattr(decision, hit_attr, None)
        if games is None or hit_rate is None or games < 5:
            continue
        yield name, games, hit_rate


def _mq_directional_stance(
    decision: object,
    market_quality: object,
) -> str:
    direction = (getattr(decision, "recommendation", "") or "").upper()
    if direction not in _DIRECTION_SUPPORT:
        return "neutral"

    reasons = getattr(market_quality, "reasons", ()) or ()
    normalized_reasons = [str(reason).strip().upper() for reason in reasons if reason is not None]

    supported = False
    contradicted = False
    for reason_text in normalized_reasons:
        for prefix, outcomes in _MQ_DIRECTIONAL_REASONS:
            if reason_text.startswith(prefix):
                stance = outcomes[direction]
                if stance == "support":
                    supported = True
                elif stance == "contradict":
                    contradicted = True

    if contradicted:
        return "contradict"
    if supported:
        return "support"
    return "neutral"


def mq_allows_action(
    decision: Optional[object],
    market_quality: Optional[object],
) -> tuple[bool, Optional[str]]:
    if market_quality is None or decision is None:
        return True, None

    direction = (getattr(decision, "recommendation", "") or "").upper()
    if direction not in _DIRECTION_SUPPORT:
        return True, None

    label = _normalize_mq_label(market_quality)
    if label is None:
        return True, None
    if label in {"ELITE", "HIGH", "STRONG"}:
        return True, None
    directional_stance = _mq_directional_stance(decision, market_quality)
    if label == "MEDIUM":
        if directional_stance == "support":
            return True, None
        if directional_stance == "contradict":
            return False, "MEDIUM_MQ_contradicted"
        return False, "MEDIUM_MQ_neutral_block"
    if label != "LOW":
        return True, None

    bet_quality = getattr(decision, "confidence", None)
    if not isinstance(bet_quality, (int, float)) or bet_quality <= 80:
        return False, "LOW_MQ_BQ_must_be_gt_80"
    if directional_stance == "contradict":
        return False, "LOW_MQ_contradicted"
    if directional_stance != "support":
        return False, "LOW_MQ_neutral_block"

    support_cfg = _DIRECTION_SUPPORT[direction]
    support_count = 0
    has_recent_support = False
    has_contradiction = False

    for name, _, hit_rate in _iter_windows(decision):
        if direction == "OVER":
            if hit_rate >= support_cfg["support"]:
                support_count += 1
                if name in {"l5", "l10"}:
                    has_recent_support = True
            if hit_rate <= support_cfg["contradict"]:
                has_contradiction = True
        else:
            if hit_rate <= support_cfg["support"]:
                support_count += 1
                if name in {"l5", "l10"}:
                    has_recent_support = True
            if hit_rate >= support_cfg["contradict"]:
                has_contradiction = True

    if has_contradiction:
        return False, "LOW_MQ_contradicted"
    if support_count < 2:
        return False, "LOW_MQ_needs_2_supporting_windows"
    if not has_recent_support:
        return False, "LOW_MQ_needs_recent_support"
    return True, None
