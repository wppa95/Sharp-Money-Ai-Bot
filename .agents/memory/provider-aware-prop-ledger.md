---
name: Provider-aware prop ledger
description: Durable boundary for combining pick'em and sportsbook player-prop feeds without conflating evidence or delivery.
---

All supported player-prop feeds should enter one canonical observation/history/tracking lifecycle while retaining provider identity in every source and report population. Sportsbook observations can support evidence and settlement tracking, but must not silently become Underdog alert candidates.

**Why:** A blended row loses the ability to explain which provider supplied a line, which source settled it, and whether a report describes monitoring or actionable picks. It also risks turning confirmation data into an unverified delivery source.

**How to apply:** Normalize provider rows once, persist immutable observations plus current history and neutral tracking, preserve provider-aware identities through grading and rollups, and keep provider delivery gates separate from the shared ledger.

PropLine is the configured observation feed for PrizePicks, DraftKings, FanDuel,
and supplemental Underdog rows. The native Underdog connector remains active
and primary; the legacy Odds API player-prop polling job must not create new
DK/FD rows.

**Why:** Routing DK/FD through the same PropLine feed preserves one source
contract and prevents pacing limits on the legacy Odds API path from creating
provider gaps.

**How to apply:** Keep the four bookmaker keys configured at the PropLine
request boundary, map each to its provider label before persistence, and never
allow supplemental PropLine rows to enter a separate Telegram delivery lane.