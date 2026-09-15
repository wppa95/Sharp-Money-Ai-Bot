---
name: Underdog event timing
description: Durable safety boundary for joining Underdog projection rows to scheduled event metadata.
---

Underdog projection timing must be resolved through explicit provider event identifiers such as match, game, fixture, or event IDs. Do not infer an event from team names or player context. Preserve the provider event status alongside the scheduled UTC start; the delivery boundary remains fail-closed when both are unknown and blocks explicit live/final/completed/ended states.

**Why:** Underdog has exposed different event containers and separate join-only match IDs across sports, while a guessed join could allow a post-start pick through Telegram.

**How to apply:** When adding or changing Underdog envelope parsing, expand explicit ID/time/status aliases and fixtures, but do not weaken `engine.timing` or bypass `strict_pregame` for missing metadata.