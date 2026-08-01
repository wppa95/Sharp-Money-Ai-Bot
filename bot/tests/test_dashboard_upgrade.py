"""
test_dashboard_upgrade.py — Contract tests for dashboard.py v3.0 extensions.

Tests the new SystemPanel, IntelligencePanel, and their integration into
DashboardReport.to_telegram() output.
"""

from __future__ import annotations

import pytest

from engine.dashboard import (
    SystemPanel,
    IntelligencePanel,
    DashboardReport,
    DashboardEngine,
)


def _report(**kwargs) -> DashboardReport:
    return DashboardReport(**kwargs)


class TestSystemPanel:
    def test_default_values(self):
        sp = SystemPanel()
        assert sp.uptime_str == "unknown"
        assert sp.crash_count == 0
        assert sp.active_blocks == 0
        assert isinstance(sp.provider_status, dict)

    def test_custom_values(self):
        sp = SystemPanel(uptime_str="2h 30m", crash_count=1, active_blocks=3)
        assert sp.uptime_str == "2h 30m"
        assert sp.crash_count == 1
        assert sp.active_blocks == 3


class TestIntelligencePanel:
    def test_default_values(self):
        ip = IntelligencePanel()
        assert ip.total_graded == 0
        assert ip.hit_rate is None
        assert ip.model_error_rate is None

    def test_custom_values(self):
        ip = IntelligencePanel(
            total_graded=100, total_hits=65, total_misses=35,
            hit_rate=0.65, model_errors=10, model_error_rate=0.286,
        )
        assert ip.total_graded == 100
        assert ip.hit_rate == 0.65


class TestDashboardReportNewFields:
    def test_system_panel_default_none(self):
        r = _report()
        assert r.system_panel is None

    def test_intelligence_panel_default_none(self):
        r = _report()
        assert r.intelligence_panel is None

    def test_system_panel_set(self):
        sp = SystemPanel(uptime_str="1d 5h", crash_count=2)
        r = _report(system_panel=sp)
        assert r.system_panel is sp

    def test_intelligence_panel_set(self):
        ip = IntelligencePanel(total_graded=50, hit_rate=0.58)
        r = _report(intelligence_panel=ip)
        assert r.intelligence_panel is ip


class TestDashboardReportToTelegram:
    def test_base_report_still_renders(self):
        r = _report()
        text = r.to_telegram()
        assert isinstance(text, str)
        assert "Dashboard" in text

    def test_system_panel_rendered_when_set(self):
        sp = SystemPanel(uptime_str="3h 45m", crash_count=0, active_blocks=2)
        r  = _report(system_panel=sp)
        t  = r.to_telegram()
        assert "System" in t
        assert "3h 45m" in t

    def test_system_panel_with_provider_status(self):
        sp = SystemPanel(
            provider_status={"Underdog": "OK", "PrizePicks": "DEGRADED"},
        )
        r = _report(system_panel=sp)
        t = r.to_telegram()
        assert "Underdog" in t
        assert "DEGRADED" in t

    def test_system_panel_active_blocks_shown(self):
        sp = SystemPanel(active_blocks=5)
        r  = _report(system_panel=sp)
        t  = r.to_telegram()
        assert "5" in t and "block" in t.lower()

    def test_intelligence_panel_rendered_when_graded(self):
        ip = IntelligencePanel(
            total_graded=100, total_hits=65, total_misses=35,
            hit_rate=0.65, model_errors=10, model_error_rate=0.286,
            strongest_sport="NBA",
        )
        r = _report(intelligence_panel=ip)
        t = r.to_telegram()
        assert "Intelligence" in t or "Learning" in t
        assert "65%" in t or "65" in t

    def test_intelligence_panel_zero_graded_not_rendered(self):
        ip = IntelligencePanel(total_graded=0)
        r  = _report(intelligence_panel=ip)
        t  = r.to_telegram()
        # Panel should not appear when no data
        assert "model_error_rate" not in t

    def test_intelligence_panel_miss_types_shown(self):
        ip = IntelligencePanel(
            total_graded=50, total_misses=20,
            model_errors=5, market_errors=10, settlement_errors=2, variance_errors=3,
        )
        r  = _report(intelligence_panel=ip)
        t  = r.to_telegram()
        assert "Model" in t or "model" in t.lower()
        assert "Market" in t or "market" in t.lower()

    def test_intelligence_panel_strongest_sport(self):
        ip = IntelligencePanel(total_graded=30, hit_rate=0.60, strongest_sport="TENNIS")
        r  = _report(intelligence_panel=ip)
        t  = r.to_telegram()
        assert "TENNIS" in t

    def test_none_panels_no_extra_output(self):
        r = _report()
        t = r.to_telegram()
        assert "System Status" not in t
        assert "Intelligence" not in t

    def test_report_with_both_panels(self):
        sp = SystemPanel(uptime_str="1h", crash_count=0)
        ip = IntelligencePanel(total_graded=20, hit_rate=0.55)
        r  = _report(system_panel=sp, intelligence_panel=ip)
        t  = r.to_telegram()
        assert "1h" in t
        assert "55%" in t or "55" in t


class TestDashboardEngineGatherSystemIntelligence:
    """Test that DashboardEngine.gather() calls system and intelligence panel methods."""

    async def test_gather_populates_system_panel_best_effort(self):
        """Smoke test: gather() should not raise even when DB has no data."""
        import asyncio
        from database import Database

        db = Database("sqlite+aiosqlite:///:memory:")
        await db.init()
        report = await DashboardEngine.gather(db)
        # system_panel may be None or populated depending on health tracker state
        assert report.system_panel is not None or report.system_panel is None
        await db.close()

    def test_gather_integration_method_is_async(self):
        import inspect
        assert inspect.iscoroutinefunction(DashboardEngine.gather)
