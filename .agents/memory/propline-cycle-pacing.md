---
name: PropLine cycle pacing
description: How the primary PrizePicks feed is scheduled without a competing job.
---

The primary PrizePicks monitor uses the existing Underdog trigger for its first
companion cycle, then the existing 60-second heartbeat provides a cadence
handoff when a long Underdog scan would otherwise delay it. It fetches one
configured PrizePicks sport per cycle and must not call the provider-wide
all-sports fetch in production. The fetch handoff completes before the slower
shared evaluation, which is serialized separately so evaluation time cannot
stretch provider polling.

**Why:** Underdog scans can exceed two minutes on a large SQLite history, so a
companion-only trigger silently stretches PrizePicks polling. The authorized
PropLine client also enforces its own request pacing; serially fetching the
full discovered sport list delays persistence and evaluation for hours. The
heartbeat guard uses a small margin below the client's 120-second request
pacing so scheduler jitter cannot skip an entire heartbeat.

**How to apply:** Keep the configured-sport round robin, one active companion
fetch task, serialized evaluation, and the heartbeat cadence handoff. Keep the
provider's own pacing authoritative; do not add a separate PrizePicks scheduler
or bypass that pacing just to increase coverage.