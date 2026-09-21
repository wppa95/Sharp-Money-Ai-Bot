---
name: Completed-history delivery boundary
description: The evidence provenance rule shared by candidate selection, delivery gates, and alert rendering.
---

Telegram delivery may use either completed primary-provider player-game results or explicit settled PropLine results, provided the history has at least five usable samples and a direction exists. Historical line averages and proxy snapshots are not completed history.

**Why:** The decision object can contain legacy or serialized fields that look like settled results even when the attached validation is proxy-only. Treating that decision metadata as authoritative allowed new-prop alerts to bypass the real-history requirement.

**How to apply:** When a validation object is present, it must independently prove completed history. Only fall back to decision/intelligence evidence when validation is absent. Render a history block from the decision or validated sample summary; do not send a normal alert whose history section is only N/A.