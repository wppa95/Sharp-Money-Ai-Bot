---
name: PropLine cycle pacing
description: How the primary PrizePicks feed is scheduled without a competing job.
---

The primary PrizePicks monitor uses the existing Underdog trigger for its first
companion cycle, then the existing 60-second heartbeat provides a cadence
handoff when a long Underdog scan would otherwise delay it. It fetches one
configured PrizePicks sport per cycle and must not call the provider-wide
all-sports fetch in production.

**Why:** Underdog scans can exceed two minutes on a large SQLite history, so a
companion-only trigger silently stretches PrizePicks polling. The authorized
PropLine client also enforces its own request pacing; serially fetching the
full discovered sport list delays persistence and evaluation for hours.

**How to apply:** Keep the configured-sport round robin, one active companion
task, and the heartbeat cadence handoff. Do not add a separate PrizePicks
scheduler or bypass the client's pacing just to increase coverage.