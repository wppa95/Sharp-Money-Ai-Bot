---
name: Async test event loop pattern for Python 3.11 + aiosqlite
description: Rule for writing async test files that use aiosqlite — required to avoid RuntimeError 'no current event loop' when run as part of the full pytest suite.
---

## The rule

Every test file that runs async code (via `asyncio.get_event_loop()`, `run_until_complete`, or fixtures that `init()` an aiosqlite Database) **must** declare a module-level shared event loop:

```python
import asyncio

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

def _run(coro):
    return _loop.run_until_complete(coro)
```

Use `_run(coro)` everywhere instead of `asyncio.get_event_loop().run_until_complete(coro)`.

Database fixtures must also use `_run()`:
```python
@pytest.fixture()
def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    _run(database.init())
    yield database
    _run(database.close())
```

**Why:** In Python 3.11, `asyncio.get_event_loop()` raises `RuntimeError: There is no current event loop in thread 'MainThread'` if any previous test file has created and then closed an event loop (e.g. via `asyncio.new_event_loop()` in a fixture that calls `loop.close()`). The module-level `_loop` is created once and reused across all tests in the file, avoiding this.

**How to apply:** Any new test file that `import asyncio` and calls async code must follow this pattern. This applies to test files that:
- Use a `Database` fixture
- Call `_run()` on any async function
- Use aiosqlite in any way

Files that only test synchronous code (dataclasses, pure functions) don't need it.
