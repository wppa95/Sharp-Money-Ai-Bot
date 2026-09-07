"""
Shared pytest fixtures for the Sharp Money Bot test suite.
"""

import pytest


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
        import market_engine as _me
        _me._prop_market_alerted.clear()
    except Exception:
        pass
    yield
    # No teardown needed — next test will reset again on entry.
