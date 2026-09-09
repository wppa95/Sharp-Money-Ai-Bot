---
name: CLV bulk writes
description: CLV scheduler performance boundary for large SQLite histories
---

CLV seeding and harvesting must avoid per-row SQLite write transactions and full-history sorts before a bounded batch limit.

**Why:** A scheduled CLV pass can appear never to finish when each seed opens its own write transaction or when an unindexed historical sort runs before `LIMIT`.

**How to apply:** Keep CLV reads bounded, collect computed/expired seed changes in memory, and persist them through bounded bulk transactions.