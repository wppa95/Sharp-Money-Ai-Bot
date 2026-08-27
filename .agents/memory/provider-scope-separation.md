---
name: Provider scope separation
description: ACTIVE_SPORTS is the Underdog scanner universe; it is not a generic OddsAPI sport list.
---

`ACTIVE_SPORTS` may contain Underdog-only identifiers such as CS, LOL, VAL, DOTA, PGA, FIFA, and TENNIS. Do not parse that list as the `models.Sport` enum or use it to drive broad OddsAPI polling.

**Why:** The two providers use different sport namespaces. Treating the Underdog list as an OddsAPI list produced false unknown-sport warnings and encouraged unnecessary broad sportsbook polling.

**How to apply:** Keep Underdog scanning all provider-returned active sports. Use OddsAPI only through its supported sport/key mapping for explicit confirmation, CLV, or a separately scoped sportsbook job.