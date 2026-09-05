---
name: SQLite pool backpressure
description: Cross-pool connection ownership for the bot's shared SQLite database.
---

When multiple async SQLAlchemy `Database` objects point to the same SQLite file, pool capacity must be bounded per engine and shared across the process. Heavy monitoring and history jobs should also share one scheduler ownership group.

**Why:** Separate implicit pools can exhaust independently while SQLite still has one writer slot, causing a monitoring snapshot persistence operation to wait for a pool connection and miss the cycle.

**How to apply:** Keep the production file-backed pool configuration explicit, use a per-event-loop/per-URL session budget as backpressure, and measure waits at both the scheduler ownership and database gate boundaries. Never solve this by introducing another database file.