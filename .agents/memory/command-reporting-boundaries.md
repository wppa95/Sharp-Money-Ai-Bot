---
name: Command reporting boundaries
description: Rules for keeping Telegram reports honest and population-specific.
---

User-facing reports must distinguish observations, tracked rows, evaluated candidates, directional opportunities, deliveries, pending grades, and resolved outcomes. Current pick/slip views may only present future, fresh rows with persisted engine output; missing confidence or movement must remain unavailable rather than use a synthetic default.

**Why:** Mixing these populations or manufacturing display values makes a healthy monitor appear unreliable and can present stale rows as actionable recommendations.

**How to apply:** Reuse persisted observations for report views, apply presentation-only freshness/actionability gates to current picks, and keep historical reconciliation independent from current pick display.