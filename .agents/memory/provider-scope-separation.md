---
name: Provider scope separation
description: PrizePicks primary, Underdog secondary, with shared PropLine evidence
---

`ACTIVE_SPORTS` may contain provider-specific identifiers such as CS, LOL, VAL,
DOTA, PGA, FIFA, and TENNIS. Keep those identifiers in the Underdog namespace
and do not parse them as a generic `models.Sport` enum.

PrizePicks is primary and Underdog is secondary. Keep the native Underdog
feed and the PrizePicks feed through PropLine. PropLine is also a shared
supplemental history/results provider for either platform, not a third pick
platform. Secondary does not mean disabled: retain independent evaluation and
eligible individual alerts for Underdog.

Keep platform-owned picks and original lines distinct in the same central
SQLite database. Supporting evidence keeps its provider provenance and may
support both picks only after identity and market validation. Never infer
settlements from recent-hit percentages.

The bot creates and sends individual picks only, not multi-pick slips or
parlays. Existing journal history may remain readable and gradeable.

**Why:** The user's comprehensive V4 audit/repair instructions explicitly
superseded the equal-primary policy with PrizePicks primary / Underdog secondary.

**How to apply:** Prefer valid existing history, then originating-platform
history, then matched PropLine evidence; otherwise mark insufficient. Retain
one shared paced PropLine budget across ingestion, history, and settlement.