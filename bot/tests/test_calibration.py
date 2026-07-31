"""
Tests for engine/calibration.py — CalibrationEngine and CalibrationReport.

Tests focus on:
1. CalibrationReport.to_telegram() with various data states
2. _confidence_to_tier() helper
3. TierCalibration computed properties (hit_rate, resolved)
4. DetectionAccuracy confirmation_rate
5. RecommendationAccuracy.accuracy
6. CalibrationEngine gracefully handles empty database
7. CalibrationEngine gracefully handles partial failures
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.calibration import (
    CalibrationEngine,
    CalibrationReport,
    DetectionAccuracy,
    RecommendationAccuracy,
    TierCalibration,
    _confidence_to_tier,
)

# ── Shared event loop ─────────────────────────────────────────────────────────

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def _run(coro):
    return _loop.run_until_complete(coro)


# ── _confidence_to_tier ───────────────────────────────────────────────────────

class TestConfidenceToTier:
    def test_s_tier_at_95(self):
        assert _confidence_to_tier(95)  == "S"

    def test_s_tier_above_95(self):
        assert _confidence_to_tier(100) == "S"

    def test_a_tier_at_85(self):
        assert _confidence_to_tier(85)  == "A"

    def test_a_tier_at_94(self):
        assert _confidence_to_tier(94)  == "A"

    def test_b_tier_at_75(self):
        assert _confidence_to_tier(75)  == "B"

    def test_b_tier_at_84(self):
        assert _confidence_to_tier(84)  == "B"

    def test_pass_tier_at_74(self):
        assert _confidence_to_tier(74)  == "PASS"

    def test_pass_tier_at_0(self):
        assert _confidence_to_tier(0)   == "PASS"


# ── TierCalibration ───────────────────────────────────────────────────────────

class TestTierCalibration:
    def test_hit_rate_requires_min_5(self):
        tc = TierCalibration(tier="S", wins=3, losses=1)
        assert tc.hit_rate is None  # n < 5

    def test_hit_rate_computed_at_5_or_more(self):
        tc = TierCalibration(tier="A", wins=4, losses=1)
        assert abs(tc.hit_rate - 0.8) < 1e-6

    def test_resolved_excludes_total(self):
        tc = TierCalibration(tier="B", total=10, wins=4, losses=3, pushes=1)
        assert tc.resolved == 8  # wins + losses + pushes

    def test_resolved_zero_when_empty(self):
        tc = TierCalibration(tier="PASS")
        assert tc.resolved == 0

    def test_tier_emoji_s(self):
        assert TierCalibration(tier="S").tier_emoji == "🔥"

    def test_tier_emoji_a(self):
        assert TierCalibration(tier="A").tier_emoji == "🟢"

    def test_tier_emoji_b(self):
        assert TierCalibration(tier="B").tier_emoji == "🟡"

    def test_tier_emoji_pass(self):
        assert TierCalibration(tier="PASS").tier_emoji == "⚪"


# ── DetectionAccuracy ─────────────────────────────────────────────────────────

class TestDetectionAccuracy:
    def test_confirmation_rate_requires_min_5(self):
        da = DetectionAccuracy(source="UNDERDOG_LINE_CHANGE", total_detected=4,
                               confirmed=3, reversed=1)
        assert da.confirmation_rate is None  # n = confirmed + reversed = 4

    def test_confirmation_rate_at_5(self):
        da = DetectionAccuracy(source="UNDERDOG_LINE_CHANGE", total_detected=6,
                               confirmed=4, reversed=1)
        # 4 / (4+1) = 0.8
        assert abs(da.confirmation_rate - 0.8) < 1e-6

    def test_inconclusive_not_counted_in_rate(self):
        da = DetectionAccuracy(source="X", total_detected=10,
                               confirmed=5, reversed=0, inconclusive=5)
        # rate = 5 / (5+0) = 1.0 — inconclusive doesn't count
        assert abs(da.confirmation_rate - 1.0) < 1e-6


# ── RecommendationAccuracy ────────────────────────────────────────────────────

class TestRecommendationAccuracy:
    def test_accuracy_requires_min_5(self):
        ra = RecommendationAccuracy(total=4, correct=3, incorrect=1)
        assert ra.accuracy is None

    def test_accuracy_at_5(self):
        ra = RecommendationAccuracy(total=5, correct=4, incorrect=1)
        assert abs(ra.accuracy - 0.8) < 1e-6

    def test_resolved_is_correct_plus_incorrect(self):
        ra = RecommendationAccuracy(correct=3, incorrect=2, unresolved=5)
        assert ra.resolved == 5


# ── CalibrationReport.to_telegram() ──────────────────────────────────────────

class TestCalibrationReportToTelegram:
    def _empty_report(self) -> CalibrationReport:
        return CalibrationReport(generated_at=datetime(2026, 7, 31, 12, 0, 0))

    def test_contains_header(self):
        msg = self._empty_report().to_telegram()
        assert "Model Calibration Report" in msg

    def test_contains_date(self):
        msg = self._empty_report().to_telegram()
        assert "Jul 31 2026" in msg

    def test_no_resolved_shows_placeholder(self):
        msg = self._empty_report().to_telegram()
        assert "No resolved records yet" in msg

    def test_clv_zero_records_shows_placeholder(self):
        msg = self._empty_report().to_telegram()
        assert "No CLV records yet" in msg

    def test_tier_calibration_rendered(self):
        report = self._empty_report()
        report.tier_calibration = {
            "S": TierCalibration(tier="S", total=6, wins=5, losses=1),
        }
        msg = report.to_telegram()
        assert "Confidence Tier Accuracy" in msg
        assert "S" in msg

    def test_hit_rate_shown_when_n_ge_5(self):
        report = self._empty_report()
        report.tier_calibration = {
            "A": TierCalibration(tier="A", total=10, wins=8, losses=2),
        }
        msg = report.to_telegram()
        assert "80%" in msg

    def test_hit_rate_hidden_when_n_lt_5(self):
        report = self._empty_report()
        report.tier_calibration = {
            "B": TierCalibration(tier="B", total=4, wins=3, losses=1),
        }
        msg = report.to_telegram()
        # n<5 placeholder shown
        assert "n&lt;5" in msg

    def test_clv_stats_rendered(self):
        report = self._empty_report()
        report.total_clv_records = 10
        report.clv_records_used  = 10
        report.avg_clv_all       = 1.5
        report.clv_positive_rate = 0.7
        msg = report.to_telegram()
        assert "1.50%" in msg
        assert "70%" in msg

    def test_detection_accuracy_rendered(self):
        report = self._empty_report()
        report.detection["UNDERDOG_LINE_CHANGE"] = DetectionAccuracy(
            source="UNDERDOG_LINE_CHANGE",
            total_detected=10,
            confirmed=8,
            reversed=2,
        )
        msg = report.to_telegram()
        assert "Line-Movement Detection" in msg
        assert "80%" in msg

    def test_recommendation_accuracy_rendered(self):
        report = self._empty_report()
        report.recommendation = RecommendationAccuracy(
            total=10, correct=8, incorrect=2
        )
        msg = report.to_telegram()
        assert "Bet Recommendation Accuracy" in msg
        assert "80%" in msg

    def test_footer_record_counts(self):
        report = self._empty_report()
        report.ev_records_used  = 42
        report.ud_records_used  = 17
        report.clv_records_used = 5
        msg = report.to_telegram()
        assert "42" in msg
        assert "17" in msg
        assert "5" in msg

    def test_detection_separates_from_recommendation(self):
        """Key invariant: detection and recommendation are shown in separate sections."""
        report = self._empty_report()
        msg = report.to_telegram()
        assert "Line-Movement Detection" in msg
        assert "Bet Recommendation Accuracy" in msg
        # They must appear in separate paragraphs — check relative ordering
        detection_pos     = msg.index("Line-Movement Detection")
        recommendation_pos = msg.index("Bet Recommendation Accuracy")
        assert detection_pos < recommendation_pos


# ── CalibrationEngine ─────────────────────────────────────────────────────────

class TestCalibrationEngine:
    """Engine gracefully handles empty or failing database calls."""

    def _make_mock_db(self):
        db = MagicMock()
        db.get_ev_records_with_results    = AsyncMock(return_value=[])
        db.count_ev_records               = AsyncMock(return_value=0)
        db.get_recent_clv_records         = AsyncMock(return_value=[])
        db.count_clv_records              = AsyncMock(return_value=0)
        db.get_clv_seeds_by_tier_stats    = AsyncMock(return_value={})
        db.get_recent_underdog_snapshots  = AsyncMock(return_value=[])
        return db

    def test_empty_db_returns_report(self):
        db = self._make_mock_db()
        report = _run(CalibrationEngine().compute(db))
        assert isinstance(report, CalibrationReport)

    def test_empty_db_has_zero_ev_records_used(self):
        db = self._make_mock_db()
        report = _run(CalibrationEngine().compute(db))
        assert report.ev_records_used == 0

    def test_empty_db_to_telegram_does_not_raise(self):
        db = self._make_mock_db()
        report = _run(CalibrationEngine().compute(db))
        msg = report.to_telegram()
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_partial_db_failure_does_not_crash(self):
        db = self._make_mock_db()
        db.get_ev_records_with_results = AsyncMock(side_effect=RuntimeError("db error"))
        report = _run(CalibrationEngine().compute(db))
        # Should still return a report — tier section will be empty
        assert isinstance(report, CalibrationReport)

    def test_clv_stats_failure_does_not_crash(self):
        db = self._make_mock_db()
        db.get_recent_clv_records = AsyncMock(side_effect=RuntimeError("db error"))
        report = _run(CalibrationEngine().compute(db))
        assert isinstance(report, CalibrationReport)

    def test_ud_failure_does_not_crash(self):
        db = self._make_mock_db()
        db.get_recent_underdog_snapshots = AsyncMock(side_effect=AttributeError("no attr"))
        report = _run(CalibrationEngine().compute(db))
        assert isinstance(report, CalibrationReport)

    def test_tier_calibration_computed_correctly(self):
        """S-tier record with WIN should show 100% hit rate (n≥5)."""
        db = self._make_mock_db()

        @dataclass
        class FakeEVRecord:
            ai_confidence: int
            expected_value: float
            clv: Optional[float]
            result: str

        records = [
            FakeEVRecord(ai_confidence=97, expected_value=5.0, clv=None, result="WIN"),
            FakeEVRecord(ai_confidence=96, expected_value=4.0, clv=None, result="WIN"),
            FakeEVRecord(ai_confidence=98, expected_value=6.0, clv=None, result="WIN"),
            FakeEVRecord(ai_confidence=95, expected_value=5.5, clv=None, result="WIN"),
            FakeEVRecord(ai_confidence=99, expected_value=7.0, clv=None, result="WIN"),
        ]
        db.get_ev_records_with_results = AsyncMock(return_value=records)

        report = _run(CalibrationEngine().compute(db))
        s_tier = report.tier_calibration.get("S")
        assert s_tier is not None
        assert s_tier.wins == 5
        assert abs(s_tier.hit_rate - 1.0) < 1e-6

    def test_clv_positive_rate(self):
        db = self._make_mock_db()

        @dataclass
        class FakeCLV:
            clv_pct: float

        db.get_recent_clv_records = AsyncMock(return_value=[
            FakeCLV(clv_pct=2.0),
            FakeCLV(clv_pct=-1.0),
            FakeCLV(clv_pct=3.0),
            FakeCLV(clv_pct=0.5),
            FakeCLV(clv_pct=-0.5),
        ])

        report = _run(CalibrationEngine().compute(db))
        assert abs(report.avg_clv_all - 0.8) < 1e-6        # (2-1+3+0.5-0.5)/5 = 0.8
        assert abs(report.clv_positive_rate - 0.6) < 1e-6  # 3 positive out of 5

    def test_detection_accuracy_confirmed_by_removal(self):
        """A line-change alert followed by [REMOVED] should be counted as confirmed."""
        db = self._make_mock_db()

        @dataclass
        class FakeSnap:
            player_name: str
            stat_type: str
            line_moved: bool
            line_delta: Optional[float]
            removed: bool
            alert_outcome: Optional[str]
            bet_recommendation: Optional[str]
            fetched_at: datetime = field(default_factory=datetime.utcnow)

        snaps = [
            FakeSnap("Mike Trout", "Hits", line_moved=True,  line_delta=0.5,
                     removed=False, alert_outcome="sent", bet_recommendation="OVER"),
            FakeSnap("Mike Trout", "Hits", line_moved=False, line_delta=None,
                     removed=True,  alert_outcome=None,   bet_recommendation=None),
        ]
        db.get_recent_underdog_snapshots = AsyncMock(return_value=snaps)

        report = _run(CalibrationEngine().compute(db))
        da = report.detection.get("UNDERDOG_LINE_CHANGE")
        assert da is not None
        assert da.total_detected == 1
        assert da.confirmed      == 1
        assert da.reversed       == 0
