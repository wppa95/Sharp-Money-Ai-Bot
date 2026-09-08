---
name: Provider scope separation
description: ACTIVE_SPORTS is the Underdog scanner universe; PropLine is internal Underdog evidence, not a user-facing provider.
---

`ACTIVE_SPORTS` may contain Underdog-only identifiers such as CS, LOL, VAL, DOTA, PGA, FIFA, and TENNIS. Do not parse that list as the `models.Sport` enum or use it to drive broad OddsAPI polling.

**Why:** The two providers use different sport namespaces. Treating the Underdog list as an OddsAPI list produced false unknown-sport warnings and encouraged unnecessary broad sportsbook polling.

**How to apply:** Keep Underdog scanning all provider-returned active sports. Use OddsAPI only through its supported sport/key mapping for explicit confirmation, CLV, or a separately scoped sportsbook job.

For current pick/slip views, apply `ACTIVE_SPORTS` in the provider query before overfetch and sport balancing. Filtering only after a recent-row cap can let inactive high-volume sports crowd out the active slate and create a false empty result.

**Why:** A live slip check selected only recent inactive rows before filtering, so active rows were hidden without surfacing the available sports.

**How to apply:** Pass the configured active-sport allowlist into the Underdog current-view query, while keeping the scanner itself provider-wide.

PropLine is an internal, read-only Underdog feed in production rather than an independent PrizePicks opportunity or Telegram provider. Legacy provider and storage names may remain for compatibility, but they must not reappear in command menus, current pick/slip candidates, or scheduled alert paths.

**Why:** The product has one user-facing player-prop source (Underdog), while PropLine is retained only to supply additional observations, resolved results, and history without creating duplicate alerts or confusing labels.

**How to apply:** Instantiate the runtime PropLine feed with the configured Underdog bookmaker namespace, keep it behind the shared ingestion/evidence pipeline, and preserve source identity internally for grading and reconciliation.