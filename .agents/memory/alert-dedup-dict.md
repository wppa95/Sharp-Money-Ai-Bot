---
name: Provider-aware alert deduplication
description: The in-memory and restored delivery dedup contract for provider-owned player props.
---

The delivery dedup key must carry provider, source identity, event identity, and direction in addition to player, sport, and stat. Compact three-part keys remain supported only as legacy aliases for older callers and stored history.

**Why:** The same player/stat can legitimately exist on PrizePicks and Underdog, across different events, or on opposite sides. A shared compact key can suppress a valid independent alert or fail to restore the correct post-restart state.

**How to apply:** Extend the identity consistently at every claim, check, record, release, and database-recovery call site. Keep the legacy shape accepted for compatibility, but do not use it as the production identity when provider-owned metadata is available.