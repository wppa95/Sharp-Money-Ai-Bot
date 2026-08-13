"""
Shared pytest fixtures for the Sharp Money Bot test suite.
"""

import pytest


@pytest.fixture(autouse=True)
def reset_telegram_rate_limiter():
    """
    Reset global singletons before every test:

    1. Telegram rate-limiter — process-wide sliding window; stale state
       from delivery-heavy tests causes spurious rate-limit failures.

    2. _prop_market_alerted (delivery dedup dict) — module-level dict;
       entries from one test bleed into the next and cause
       _try_claim_delivery_slot() to return False, blocking delivery
       and causing lifecycle / alert-sent assertions to fail.

    This fixture is autouse=True so it applies to every test without
    any decorator required.
    """
    try:
        import engine.telegram_rate_limiter as _rl_mod
        _rl_mod.reset_limiter()
    except Exception:
        pass
    try:
        import market_engine as _me
        _me._prop_market_alerted.clear()
    except Exception:
        pass
    yield
    # No teardown needed — next test will reset again on entry.
