"""
Shared pytest fixtures for the Sharp Money Bot test suite.
"""

import pytest


@pytest.fixture(autouse=True)
def reset_telegram_rate_limiter():
    """
    Reset the global Telegram rate-limiter singleton before every test.

    The limiter is a process-wide singleton whose sliding-window state
    persists across tests that call deliver_underdog().  Without this
    reset, tests running after delivery-heavy tests see a full window
    and get rate-limited, causing spurious failures.

    This fixture is autouse=True so it applies to every test without
    any decorator required.
    """
    try:
        import engine.telegram_rate_limiter as _rl_mod
        _rl_mod.reset_limiter()
    except Exception:
        pass
    yield
    # No teardown needed — next test will reset again on entry.
