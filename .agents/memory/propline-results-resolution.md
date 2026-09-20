---
name: PropLine results boundary
description: Official PropLine event-results contract and safe resolution handling
---

The documented event-results route is read-only provider evidence. Only explicit `won`, `lost`, `push`, or `void` values are settled; blank/null resolution is unresolved, and malformed payloads are incomplete. A terminal or last-observed projection is never a settlement. Event-scoped rows may grade the matching pending opportunity; player/stat history alone may not.

**Why:** PropLine's results response carries the strongest available over/under settlement evidence, while event status, last line, or unrelated player history does not prove a prop outcome.

**How to apply:** Keep result requests opt-in and on the official host, preserve event/player/stat identity, reuse existing request pacing/budgets, and use explicit event results for the matching provider row before falling back to resolved player history. Do not use them as live scoring or delivery input.