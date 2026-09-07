---
name: Telegram command catalog
description: Durable command metadata and runtime ownership for Telegram commands
---

Telegram command names, descriptions, categories, aliases, ordering, and
enabled state belong in a durable database catalog. The Python handlers remain
the executable implementation; database rows drive the visible menu and help
surface, with startup seeding that preserves operator enablement changes.

**Why:** A static help string can drift from registered commands and cannot
reflect which commands are currently exposed or enabled.

**How to apply:** Seed metadata idempotently during startup, preserve existing
enabled values on reseed, publish only enabled rows to Telegram, and retain a
safe static compatibility fallback before database initialization.