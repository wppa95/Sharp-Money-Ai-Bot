---
name: Alert source priority
description: Equal primary PrizePicks/Underdog and retired sportsbook boundary.
---

## Rule
PrizePicks and Underdog are equal primary platforms.

**Why:** The user explicitly corrected the unequal-priority audit interpretation.

## How to apply
- Keep both feeds, original source lines, and independent eligible alerts.
- Never prefer ingestion, qualification, evaluation, or delivery solely by platform.
- Apply the [primary platform scope](provider-scope-separation.md) correction.
- Retired sportsbook work is out of scope unless the user explicitly requests a
  new provider integration.
