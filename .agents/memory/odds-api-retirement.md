---
name: The Odds API supplemental boundary
description: User-authorized Free-plan Odds API use is supplemental only and shares a conservative persisted quota.
---

The user explicitly reauthorized The Odds API as a supplemental provider for
current team scores, event-market discovery, and bookmaker quote context for an
already-qualified pick. PrizePicks and Underdog remain equal primary platforms;
PropLine continues to feed PrizePicks and may supplement either platform.

Existing persisted odds, evidence, and CLV rows remain readable for historical
reports and compatibility. Current Odds API prices, event metadata, or team
scores must never create a pick, validate an offering, replace a source line,
or supply player history/results/HIT-MISS-PUSH settlement. Historical endpoints
remain prohibited.

Use only documented V4 endpoints: quota-free `/sports` and per-sport `/events`;
paid event `/markets` (1 credit), event `/odds` (requested markets × regions
reserved, actual unique returned market keys × regions charged), and `/scores`
(1 credit without `daysFrom`, 2 with it). `/scores` supplies team/event status
only. `/sports?all=true` is explicit, not the default.

Every paid request across all endpoints and sports shares one persistent
monthly UTC ledger: user-confirmed Free plan 500-credit hard limit, 450-credit
operating target, roughly 50-credit reserve, and reset on the first day of the
month at 00:00 UTC. Pace requests across the remaining time to reset, persist
usage/cooldown/rotation over restarts, and fail closed when cost is unknown.
Do not change PropLine's separate shared budget.

Quota-free event discovery keeps a separate persisted last-sport position from
the paid market/score rotation. Advance the discovery position before fetching
events, so an error or cooldown moves on and the failed sport is retried after
the remaining active sports get a turn.

**Why:** Sharing the paid cursor would couple independent rotations, while
advancing only after a successful fetch can let one failing sport monopolize
every discovery tick.

**How to apply:** Preserve separate rotation state for free event discovery and
paid credit-consuming work; keep discovery requests cached and quota-free.

**Verified capability boundary (2026-10-17):** Official V4 documentation says
the scores endpoint returns event completion and team scores, not player stats.
Historical odds endpoints return market quotes, not actual player-game results,
and are paid-plan only. The user confirmed the Free-plan monthly limit and UTC
reset; no live provider request was made during the implementation.

**Why:** The user requested Odds API restoration only for safe supplemental
context, while keeping platform ownership, original lines, and actual
player-result grading independent of bookmaker data.

**How to apply:** Route every paid endpoint through the same persistent budget
and paced all-sport scheduler. Keep market availability and current odds
supplemental, exact-event matched, and separate from qualification, offering
validation, and player settlement.