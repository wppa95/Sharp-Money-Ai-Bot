---
name: Universal prop persistence policy
description: Eligible observations are retained for settlement tracking independently of scoring or Telegram delivery; futures remain disabled.
---

Every eligible player-prop observation is retained in the delivery-independent tracking ledger before scoring or Telegram gates. PASS, no-direction, removed, and undelivered observations remain gradeable when a supported settlement exists; futures and season-long markets are intentionally excluded because they are disabled.

**Why:** Settlement coverage must not depend on whether a prop qualified for an alert, while disabled season-long markets do not have the supported single-game settlement contract.

**How to apply:** Write eligible observations to universal tracking before scoring and await that write as a required barrier; keep directional opportunity/candidate records and Telegram delivery separately gated. Never re-enable futures tracking without a settlement design.