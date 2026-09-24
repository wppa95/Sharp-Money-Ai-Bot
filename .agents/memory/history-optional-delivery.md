---
name: History source coverage policy
description: Public-first player-history coverage requirements across configured sports.
---

Every configured prop sport should have player history through a public source where available, or an existing authorized provider connection otherwise. Delivery may be blocked only by missing usable history or missing OVER/UNDER direction; Watchlist and Near-Miss labels are not blockers.

**Why:** The user explicitly chose public sources first and expects every configured sport to have a history route, rather than treating missing coverage as an acceptable default.

**How to apply:** When adding or changing configured sports, verify a public player-results adapter or mapped authorized provider route. If neither is available, ask for access through the secure secrets flow. Keep history and direction as the only candidate blockers; do not block solely on Watchlist or Near-Miss state.