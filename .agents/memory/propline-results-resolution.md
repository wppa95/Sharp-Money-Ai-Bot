---
name: PropLine results boundary
description: Official PropLine event-results contract and safe resolution handling
---

The documented event-results route is read-only provider evidence. Only explicit `won`, `lost`, `push`, or `void` values are settled; blank/null resolution is unresolved, and malformed payloads are incomplete. A terminal or last-observed projection is never a settlement. Separately, an explicitly final PropLine player-game stat may grade a directional pending opportunity only with exact event/player/sport/stat identity; it cannot establish a void or use a date-only match.

**Why:** Explicit resolution remains the strongest settlement evidence. Final box-score stats can resolve a saved numeric line only when the event identity is exact; status, last line, and unrelated history cannot prove a result.

**How to apply:** Keep result requests opt-in and on the official host, preserve source and event/player/stat identity, and reuse existing pacing/budgets. Use final player-game stats only for directional grading and never as live scoring or delivery input.