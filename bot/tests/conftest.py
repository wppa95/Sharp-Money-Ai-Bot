"""
Shared pytest fixtures for the Sharp Money Bot test suite.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_propline_quota(monkeypatch, tmp_path):
    """Fixture HTTP requests must never spend the running bot's live ledger."""
    import providers.propline as propline

    ledger = str(tmp_path / "propline_quota.sqlite3")
    budget = propline.PropLineRequestBudget(
        propline.PROPLINE_DAILY_REQUEST_TARGET, ledger_path=ledger,
    )
    export_budget = propline.PropLineRequestBudget(
        50, ledger_path=ledger, day_factory=lambda: "fixture-export",
    )
    clock = [100000.0]

    async def advance(seconds):
        clock[0] += seconds

    limiter = propline.PropLineRequestLimiter(
        ledger_path=ledger, ledger_clock=lambda: clock[0],
        clock=lambda: clock[0], sleep=advance,
    )
    for name, value in {
        "_DEFAULT_PROPLINE_BUDGET_LEDGER_PATH": ledger,
        "_DEFAULT_PROPLINE_LEDGER_PATH": ledger,
        "_DEFAULT_PROPLINE_REQUEST_BUDGET": budget,
        "_DEFAULT_PROPLINE_ENDPOINT_REQUEST_BUDGET": budget,
        "_DEFAULT_PROPLINE_EXPORT_REQUEST_BUDGET": export_budget,
        "_DEFAULT_PROPLINE_REQUEST_LIMITER": limiter,
        "_DEFAULT_PROPLINE_ENDPOINT_REQUEST_LIMITER": limiter,
    }.items():
        monkeypatch.setattr(propline, name, value)


@pytest.fixture(autouse=True)
def reset_delivery_claims():
    """
    Reset delivery state before every test:

    1. _prop_market_alerted (delivery dedup dict) — module-level dict;
       entries from one test bleed into the next and cause
       _try_claim_delivery_slot() to return False, blocking delivery
       and causing lifecycle / alert-sent assertions to fail.
    """
    try:
        from engine.offering_coverage import coverage
        coverage.__init__()
        import market_engine as _me
        _me._prop_market_alerted.clear()
    except Exception:
        pass
    yield
    # No teardown needed — next test will reset again on entry.
