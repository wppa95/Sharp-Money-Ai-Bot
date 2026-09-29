---
name: Opportunity event identity
description: Rules for preserving Underdog event IDs through opportunity persistence and reconciliation.
---

Underdog opportunity writes must derive event identity from the snapshot's supported event/game fields, and a later blank observation must never erase a stored nonblank identity.

**Why:** Older rows showed blank game IDs even though retained Underdog snapshots had the event identity. This weakened event-specific settlement reconciliation and made reports imply that evidence was missing.

**How to apply:** Keep event identity separate from team/provider labels. Prefer exact saved event IDs and never let a blank observation erase one.

For Underdog/PropLine cross-ID grading, accept only a trusted completed player-game result with exact player/sport/stat, scheduled game date (or the existing prior-day adjustment), a two-team Underdog event, and matching player-team/opponent context. Require one unique PropLine event, preserve both original IDs and result source, and leave incomplete or ambiguous matches pending; never match on player/date alone.

**Why:** Underdog and PropLine use different event-ID namespaces, but date/player/stat alone can select the wrong game when players have multiple events or schedules shift.

**How to apply:** Resolve cross-ID matches in bulk from stored event teams and final player-game results. Attach a per-opportunity validation marker only after every identity check succeeds; the grader must reject unmarked cross-ID rows.

Directional opportunity rows used by Telegram must be persisted before send under the same provider source ID passed to the durable delivery-state check and alert-sent update. Keep `:line=<threshold>` identities confined to the tracking ledger unless lookup and send-bookkeeping paths consistently use that identity.

**Why:** The durable delivery guard fails closed when the main scan sends before its batched opportunity write; FPR and regular monitoring have separate delivery lifecycles, so both need an explicit pre-send barrier.

**How to apply:** For every delivery path, verify source-ID equality and persistence order across tracking, opportunity logging, delivery-state lookup, and alert-sent bookkeeping. Keep delivery fail-closed when the required opportunity write fails.

Large append-only snapshot repairs must remain explicit maintenance operations rather than startup migrations.

**Why:** A correlated repair over the live snapshot ledger delayed bot startup and blocked health readiness.

**How to apply:** Fix identity on the live write path, and run historical repair only through a controlled maintenance invocation.