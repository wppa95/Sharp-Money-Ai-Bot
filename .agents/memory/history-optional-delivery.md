---
name: History source coverage policy
description: Public-first player-history coverage requirements across configured sports.
---

Every configured prop sport should have player history through a public source where available, or an existing authorized provider connection otherwise. Every user-facing pick needs at least five usable completed results; "history optional" only skips stricter sport-specific quality checks after that proof. Missing usable history or OVER/UNDER direction blocks delivery; Watchlist and Near-Miss labels do not.

**Why:** The user explicitly chose public sources first and expects every configured sport to have a history route, rather than treating missing coverage as an acceptable default.

**How to apply:** When adding or changing configured sports, verify a public player-results adapter or mapped authorized provider route. If neither is available, ask for access through the secure secrets flow. Require five usable completed primary results or explicitly settled PropLine results before Telegram delivery. Do not block solely on Watchlist or Near-Miss state.