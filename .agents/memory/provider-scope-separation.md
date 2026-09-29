---
name: Provider scope separation
description: ACTIVE_SPORTS is the Underdog scanner universe; PropLine supplies internal evidence
---

`ACTIVE_SPORTS` may contain provider-specific identifiers such as CS, LOL, VAL,
DOTA, PGA, FIFA, and TENNIS. Keep those identifiers in the Underdog namespace
and do not parse them as a generic `models.Sport` enum.

Apply the configured active-sport allowlist in current pick/slip queries before
overfetch and sport balancing, while the scanner remains provider-wide.
PropLine is an internal observation/evidence feed for Underdog picks in
production, not an independent opportunity or Telegram provider. Persist its
normalized observations, history, and event-result evidence for Underdog while
preserving source and event identity for grading and reconciliation; do not
create a duplicate delivery path.

**Why:** PropLine fills evidence and settlement gaps for Underdog without
becoming a second user-facing book or changing the Underdog pick identity.

**How to apply:** Store PropLine data under its original provenance, and only
grade a saved opportunity when player, sport, stat, and exact event identity
match; keep no-result and incomplete feeds pending.