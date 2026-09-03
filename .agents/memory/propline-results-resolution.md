---
name: PropLine results boundary
description: Official PropLine event-results contract and safe resolution handling
---

The documented event-results route is read-only supplemental evidence for PrizePicks. Only explicit `won`, `lost`, `push`, or `void` values are settled; blank/null resolution is unresolved, and malformed payloads are incomplete. A terminal or last-observed projection is never a settlement.

**Why:** PropLine's results response is a separate paid-tier capability and the event status/last line does not prove a player prop outcome.

**How to apply:** Keep result requests opt-in and on the official host, filter to the PrizePicks bookmaker, preserve source timestamps and identity, reuse existing request pacing/budgets, and do not connect results to scoring, grading, alerts, Telegram, or Underdog data.