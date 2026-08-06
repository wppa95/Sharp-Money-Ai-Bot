from engine.projection_confidence import (
    score_projection_confidence,
    ProjectionConfidenceTier,
)


def test_thin_sample_is_low_or_thin():
    r = score_projection_confidence(sample_strength=20, n_history=3)
    assert r.score < 60
    assert r.tier in (ProjectionConfidenceTier.LOW, ProjectionConfidenceTier.THIN)


def test_strong_history_is_high_or_elite():
    r = score_projection_confidence(
        sample_strength=90,
        n_history=30,
        l5_hit_rate=0.80,
        l10_hit_rate=0.75,
        l20_hit_rate=0.70,
    )
    assert r.score >= 75
    assert r.tier in (
        ProjectionConfidenceTier.HIGH,
        ProjectionConfidenceTier.ELITE,
    )


def test_score_clamped_0_100():
    r = score_projection_confidence(sample_strength=999, n_history=100)
    assert 0 <= r.score <= 100