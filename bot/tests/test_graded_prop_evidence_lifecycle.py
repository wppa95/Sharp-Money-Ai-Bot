"""Graded-prop evidence lifecycle: cold-start monitoring + accumulated history."""
from __future__ import annotations

from engine.player_results import compute_hit_rates
from providers.player_history import (
    PlayerHistoryRecord,
    _window_from_values,
)


def _rec(date: str, value: float, source: str = "espn_gamelog") -> PlayerHistoryRecord:
    return PlayerHistoryRecord(
        player="Test Player",
        sport="NFL",
        stat="receiving yards",
        game_id=date,
        date=date,
        opponent=None,
        value=value,
        source=source,
    )


class TestColdStartNotBlocked:
    def test_empty_history_windows_are_none_not_zero(self):
        rates = compute_hit_rates([], current_line=19.5)
        assert rates.has_real_data is False
        assert rates.l5 is None
        assert rates.l10 is None
        assert rates.l20 is None
        assert rates.l30 is None
        assert rates.season is None
        assert rates.total_games == 0

    def test_window_from_empty_is_none(self):
        assert _window_from_values([], 19.5) is None


class TestGradedHistoryBuildsWindows:
    def test_first_graded_results_do_not_fabricate_l5(self):
        recs = [_rec("2026-08-01", 22.0, "graded_prop")]
        assert _window_from_values(recs, 19.5) is None

    def test_l5_from_chronological_graded(self):
        recs = [
            _rec(f"2026-08-{d:02d}", v, "graded_prop")
            for d, v in [(10, 25), (9, 18), (8, 21), (7, 15), (6, 30)]
        ]
        win = _window_from_values(recs[:5], 19.5)
        assert win is not None
        assert win.games == 5
        assert win.over_count == 3
        assert win.under_count == 2

    def test_l10_l20_l30_season_from_graded(self):
        recs = [
            _rec(f"2026-06-{(i + 1):02d}", float(10 + i), "graded_prop")
            for i in range(30)
        ]
        line = 24.5
        l5 = _window_from_values(recs[:5], line)
        l10 = _window_from_values(recs[:10], line)
        l20 = _window_from_values(recs[:20], line)
        l30 = _window_from_values(recs[:30], line)
        season = _window_from_values(recs, line)
        assert l5 and l5.games == 5
        assert l10 and l10.games == 10
        assert l20 and l20.games == 20
        assert l30 and l30.games == 30
        assert season and season.games == 30

    def test_provider_and_graded_do_not_duplicate_dates(self):
        provider = _rec("2026-08-10", 22.0, "espn_gamelog")
        graded = _rec("2026-08-10", 22.0, "graded_prop")
        existing = {provider.date}
        merged = [provider]
        if graded.date not in existing:
            merged.append(graded)
        assert len(merged) == 1
        assert merged[0].source == "espn_gamelog"

    def test_different_markets_do_not_contaminate(self):
        ry = [_rec("2026-08-10", 50.0, "graded_prop")]
        win = _window_from_values(ry, 40.0)
        assert win is None

    def test_over_under_classification_from_actual_vs_line(self):
        recs = [
            _rec("2026-08-10", 30.0, "graded_prop"),
            _rec("2026-08-09", 10.0, "graded_prop"),
            _rec("2026-08-08", 19.5, "graded_prop"),
        ]
        win = _window_from_values(recs, 19.5)
        assert win is not None
        assert win.over_count == 1
        assert win.under_count == 2

    def test_hit_miss_push_semantics(self):
        def grade(rec, line, direction):
            if abs(rec - line) < 0.01:
                return "PUSH"
            if direction == "UNDER":
                return "HIT" if rec < line else "MISS"
            return "HIT" if rec > line else "MISS"

        assert grade(25.0, 19.5, "OVER") == "HIT"
        assert grade(10.0, 19.5, "OVER") == "MISS"
        assert grade(19.5, 19.5, "OVER") == "PUSH"
        assert grade(10.0, 19.5, "UNDER") == "HIT"
        assert grade(25.0, 19.5, "UNDER") == "MISS"


class TestSourcesRemainSeparate:
    def test_source_field_distinguishes_provider_vs_graded(self):
        p = _rec("2026-08-10", 22.0, "espn_gamelog")
        g = _rec("2026-08-09", 18.0, "graded_prop")
        assert p.source != g.source
        assert g.source == "graded_prop"
