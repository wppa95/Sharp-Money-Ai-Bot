---
name: History coverage boundaries
description: Durable lessons for full-book player history collection and DB-first settlement coverage.
---

The history collector must treat a provider-empty response as an explicit coverage gap, not as a successful evidence collection or a transport error. Reports should expose empty-result counts by sport alongside attempted, deadline, and persisted counts.

**Why:** A full active-book pass can persist hundreds of thousands of MLB rows while returning zero rows for other active sports. Calling that run successful without per-sport empty coverage hides settlement blind spots.

**How to apply:** Keep the database authoritative, persist every returned row in bounded batches, and leave unresolved opportunities pending when exact settlement evidence is unavailable. Composite settlement may use only complete, same-game component rows already stored in the database.

Generic Underdog `BASKETBALL` targets require a WNBA-first ESPN route with an NBA fallback, then the returned rows must retain the original `BASKETBALL` identity for database joins.

**Why:** The active-book key is not itself an ESPN league key; routing it directly as NBA can silently produce no history for the active WNBA slate.

**How to apply:** Normalize provider routing internally, but relabel returned evidence back to the source sport key before persistence.

Full-book collection should use bounded provider-task batches rather than one task per active prop in a single gather. A hard cadence deadline must cancel/report unfinished work explicitly and must never be represented as silently completed.

**Why:** Thousands of concurrent task objects can increase memory sharply and make cancellation unreliable while the bot is also persisting the live feed.

**How to apply:** Keep durable persistence separate from the provider deadline, report remaining and deadline-target counts, and retain unresolved opportunities for later DB-first grading.

Active-target discovery needs a separate, longer bounded timeout than individual provider requests.

**Why:** Full-feed SQLite writes can briefly delay the large active-target read beyond a short provider timeout, causing a false cadence warning and an empty history cycle even when the database is healthy.

**How to apply:** Bound the discovery read independently, keep provider request timeouts strict, and treat a controlled cadence stop as informational while retaining warnings for actual read, request, quota, and persistence failures.