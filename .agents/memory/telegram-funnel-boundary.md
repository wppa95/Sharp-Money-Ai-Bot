---
name: Telegram funnel boundary
description: Durable tracking and opportunity checks required before user-facing player-prop delivery
---

Telegram player-prop delivery is the final edge of the tracking and grading funnel: the source observation must already be tracked and have a persisted OVER/UNDER opportunity. The durable delivery marker suppresses repeat sends after re-evaluation or restart.

**Why:** Feed scans intentionally observe and rescore many rows, while only a small subset should become user-facing alerts. Checking the durable ledgers before formatting or sending prevents untracked evaluations and repeated line refreshes from becoming Telegram spam.

**How to apply:** Keep `/picks` and monitoring populations independent. Enforce the tracking/opportunity/delivered check only in Telegram delivery, preserve the existing direction/evidence and timing gates, and report current-session scan sends separately from confirmed Telegram deliveries.