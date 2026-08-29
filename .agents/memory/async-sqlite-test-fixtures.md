---
name: Async SQLite test fixtures
description: Test-isolation guidance for async SQLAlchemy in-memory databases under pytest.
---

Use function-scoped fixtures for async SQLAlchemy databases backed by SQLite `:memory:` when tests may run on separate pytest event loops. A module-scoped engine can lose its in-memory schema when its pooled connection is collected or recreated.

**Why:** The failure appears only under broader test ordering and presents as `no such table` after earlier tests have passed, making it look like application schema corruption rather than fixture lifecycle leakage.

**How to apply:** Keep each test's in-memory database and async engine within the same fixture/event-loop lifetime. Do not weaken assertions or hide the failure with skips.