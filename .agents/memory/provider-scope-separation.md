---
name: Provider scope separation
description: ACTIVE_SPORTS is the Underdog scanner universe; it is not a generic OddsAPI sport list.
---

`ACTIVE_SPORTS` may contain Underdog-only identifiers such as CS, LOL, VAL, DOTA, PGA, FIFA, and TENNIS. Do not parse that list as the `models.Sport` enum or use it to drive broad OddsAPI polling.

**Why:** The two providers use different sport namespaces. Treating the Underdog list as an OddsAPI list produced false unknown-sport warnings and encouraged unnecessary broad sportsbook polling.

**How to apply:** Keep Underdog scanning all provider-returned active sports. Use OddsAPI only through its supported sport/key mapping for explicit confirmation, CLV, or a separately scoped sportsbook job.

For current pick/slip views, apply `ACTIVE_SPORTS` in the provider query before
overfetch and sport balancing. Filtering only after a recent-row cap can let
inactive high-volume sports crowd out the active slate and create a false empty
result.

**Why:** The August 28, 2026 live slip check selected only recent inactive
rows before filtering, so NBA appeared despite not being active; post-filtering
then hid all rows without surfacing the active sports that were available.

**How to apply:** Pass the configured active-sport allowlist into the
Underdog current-view query, while keeping the scanner itself provider-wide.

PrizePicks current views use the active sport catalog returned by its authorized
PropLine provider, not the Underdog `ACTIVE_SPORTS` allowlist. If the official
catalog is empty, do not guess static sports; custom endpoints without catalog
support may use the configured compatibility fallback.

**Why:** PrizePicks and Underdog are equally primary but have independent
provider scopes; constraining PP to Underdog's list hides legitimate PP-only
sports and can misattribute provider data.

**How to apply:** Discover PP sports through PropLine, scan one sport per shared
cycle for pacing, and keep PP/UD recommendation and alternate-line lookups
provider-scoped in `/picks` and `/slip`.

The serialized full-pool rotation must merge fresh PrizePicks `PropLineHistory`
rows with the legacy Underdog snapshot pool; normalized provider rows alone are
not a replacement for the Underdog source of truth.

**Why:** Underdog live observations still originate in its provider-specific
snapshot table, while PrizePicks observations use the normalized shared table.
Replacing one with the other silently removes a primary provider from FPR.

**How to apply:** Keep provider and event identity in normalized FPR keys,
exclude normalized rows older than the active-feed freshness window, and use
the shared opportunity log for provider-aware grading/reporting.

Provider reconciliation must count PrizePicks observations from fresh,
non-removed `PropLineHistory` rows rather than the legacy `PrizePicksRecord`
table. The legacy table can remain empty while normalized PP persistence is
working correctly.

**Why:** The live PP companion cycle writes the provider-neutral history path;
using the legacy source made a successful normalized fetch appear as
`observations=0` in provider funnel telemetry.

**How to apply:** Keep raw Underdog observation counts on its snapshot source,
but use normalized history for PP observed/tracked counts and candidate logs for
the broad PP evaluation count.