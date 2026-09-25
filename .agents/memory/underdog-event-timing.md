---
name: Underdog event timing
description: Durable safety boundary for joining Underdog projection rows to scheduled event metadata.
---

Underdog projection timing must be resolved through explicit provider event identifiers such as match, game, fixture, or event IDs. Tennis schedule metadata is in the top-level `solo_games` collection: `solo_games[].id` matches `appearances[].match_id`, with `scheduled_at` and `status` on the event. Do not infer an event from team names or player context. Preserve provider status alongside the scheduled UTC start; delivery remains fail-closed when timing is unknown and blocks explicit live/final/completed/ended states.

**Why:** Underdog exposes different event containers and separate join-only match IDs across sports. Tennis lines had explicit numeric match IDs, but their schedule records were in `solo_games`, not the collections previously indexed; guessing from player or line IDs could allow a post-start pick through Telegram.

**How to apply:** When adding or changing Underdog envelope parsing, index confirmed event collections and join only on exact provider IDs; keep fixtures for each observed shape. Do not weaken `engine.timing` or bypass `strict_pregame` for missing metadata.