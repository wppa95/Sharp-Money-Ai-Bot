---
name: Provider scope separation
description: ACTIVE_SPORTS is the Underdog scanner universe; PropLine supplies internal evidence
---

`ACTIVE_SPORTS` may contain provider-specific identifiers such as CS, LOL, VAL,
DOTA, PGA, FIFA, and TENNIS. Keep those identifiers in the Underdog namespace
and do not parse them as a generic `models.Sport` enum.

Apply the configured active-sport allowlist in current pick/slip queries before
overfetch and sport balancing, while the scanner remains provider-wide.
PropLine is an internal read-only observation/evidence feed in production, not
an independent opportunity or Telegram provider. Preserve source identity for
grading and reconciliation without creating a duplicate delivery path.